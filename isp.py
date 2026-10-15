# -*- coding: utf-8 -*-
"""The WAN's internet provider, which the controller shows as the ISP.

A UniFi OS gateway reports it in `geo_info`, keyed by WAN group:
`{"WAN": {"isp_name": ..., "isp_organization": ...}}`. OPNsense doesn't know
its provider, so it's looked up by the WAN address OPNsense reports (not by
whatever address a web service sees, which policy routing through a VPN can
change), through Team Cymru's IP-to-ASN DNS zones (with dnspython, which OPNsense
ships), asking the system's resolver: no web API, account or rate limit. Lookups run in the background, so informs never wait for them.
"""
import ipaddress
import logging
import threading
import time

import dns.exception
import dns.resolver

logger = logging.getLogger('unifi-gateway')

TIMEOUT = 5
REFRESH = 6 * 3600
RETRY = 600

# answers myip.opendns.com with the address it sees: our public address
# when the WAN address is private (CGNAT, behind another router)
OPENDNS = '208.67.222.222'


def query(name, rtype, server=None):
    """Answers of one record type for `name`, from the system's resolver
    (or `server`): TXT records as strings, A records as addresses."""
    # a new resolver each time picks up changes to the system's DNS settings
    resolver = dns.resolver.Resolver(configure=not server)
    if server:
        resolver.nameservers = [server]
    try:
        answer = resolver.resolve(name, rtype, lifetime=TIMEOUT)
    except (dns.resolver.NXDOMAIN, dns.resolver.NoAnswer):
        return []
    if rtype == 'TXT':
        return [b''.join(record.strings).decode('utf-8', 'replace') for record in answer]
    return [record.to_text() for record in answer]


def _fields(record):
    return [field.strip() for field in record.split('|')]


def _origin_name(address):
    if address.version == 4:
        return '.'.join(reversed(str(address).split('.'))) + '.origin.asn.cymru.com'
    return '.'.join(reversed(address.exploded.replace(':', ''))) + '.origin6.asn.cymru.com'


def _provider_name(description, country):
    """'BT-UK-AS - British Telecommunications Limited, GB' ->
    'British Telecommunications Limited'"""
    _, sep, name = description.partition(' - ')
    name = name.strip() if sep and name.strip() else description
    if country and name.endswith(', ' + country):
        name = name[:-len(country) - 2]
    return name


def _public_address(wan_ip):
    try:
        address = ipaddress.ip_address(wan_ip or '')
        if address.is_global:
            return address
    except ValueError:
        pass
    # Behind CGNAT or another router the WAN address says nothing
    found = query('myip.opendns.com', 'A', server=OPENDNS)
    if not found:
        raise ValueError('could not find our public address')
    return ipaddress.ip_address(found[0])


def lookup(wan_ip=None):
    """geo_info entry for the WAN address, or for our own public address
    when the WAN address isn't public (CGNAT, double NAT)."""
    address = _public_address(wan_ip)

    # the most specific announced prefix wins
    asn, country, bits = None, '', -1
    for record in query(_origin_name(address), 'TXT'):
        fields = _fields(record)
        if len(fields) < 3 or '/' not in fields[1]:
            continue
        prefix_bits = int(fields[1].split('/')[1])
        first = fields[0].split()[0] if fields[0] else ''
        if prefix_bits > bits and first.isdigit() and int(first):
            asn, country, bits = int(first), fields[2], prefix_bits
    if not asn:
        raise ValueError('no provider for %s' % address)

    name = None
    for record in query('AS%d.asn.cymru.com' % asn, 'TXT'):
        fields = _fields(record)
        if len(fields) >= 5 and fields[4]:
            name = _provider_name(fields[4], fields[1])
            break
    if not name:
        raise ValueError('no name for AS%d' % asn)

    info = {'isp_name': name, 'isp_organization': name, 'asn': asn, 'address': str(address)}
    if country:
        info['country_code'] = country
    return info


class IspInfo:
    """The last provider looked up, refreshed in the background when the WAN
    address changes or the answer gets old."""

    def __init__(self, fetch=None):
        self._fetch = fetch or lookup
        self._lock = threading.Lock()
        self._info = None
        self._ip = None
        self._next = 0
        self._busy = False
        self._thread = None

    def current(self, wan_ip):
        with self._lock:
            due = wan_ip != self._ip or time.time() >= self._next
            if due and not self._busy:
                self._busy = True
                self._thread = threading.Thread(target=self._refresh, args=(wan_ip,),
                                                name='isp-lookup', daemon=True)
                self._thread.start()
            return self._info

    def _refresh(self, wan_ip):
        try:
            info = self._fetch(wan_ip)
        except (dns.exception.DNSException, OSError, ValueError) as e:
            logger.warning('Could not look up the ISP for %s: %s', wan_ip or 'this address', e)
            info = None
        with self._lock:
            # on failure keep the previous answer: a new address usually
            # comes from the same provider
            if info:
                if info != self._info:
                    logger.info('ISP: %s (AS%s)', info['isp_name'], info['asn'])
                self._info = info
            self._ip = wan_ip
            self._next = time.time() + (REFRESH if info else RETRY)
            self._busy = False
