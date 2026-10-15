# -*- coding: utf-8 -*-
"""The WAN's internet provider, which the controller shows as the ISP.

A UniFi OS gateway reports it in `geo_info`, keyed by WAN group:
`{"WAN": {"isp_name": ..., "isp_organization": ...}}`. OPNsense doesn't know
its provider, so it's looked up by the WAN address OPNsense reports (not by
whatever address a web service sees, which policy routing through a VPN can
change). Lookups run in the background, so informs never wait for them.
"""
import ipaddress
import json
import logging
import re
import threading
import time
import urllib.error
import urllib.request

logger = logging.getLogger('unifi-gateway')

LOOKUP_URL = 'https://ipinfo.io/%sjson'
HEADERS = {'User-Agent': 'unifi-gateway', 'Accept': 'application/json'}
TIMEOUT = 10
REFRESH = 6 * 3600
RETRY = 600

_ORG = re.compile(r'^AS(\d+)\s+(.+)$')


def _is_public(ip):
    try:
        return ipaddress.ip_address(ip).is_global
    except ValueError:
        return False


def lookup(wan_ip=None, timeout=TIMEOUT):
    """geo_info entry for the WAN address, or for our own public address
    when the WAN address isn't public (CGNAT, double NAT)."""
    url = LOOKUP_URL % (wan_ip + '/' if wan_ip and _is_public(wan_ip) else '')
    request = urllib.request.Request(url, headers=HEADERS)
    with urllib.request.urlopen(request, timeout=timeout) as response:
        data = json.loads(response.read().decode('utf-8'))
    match = _ORG.match(data.get('org') or '')
    if not match:
        raise ValueError('no provider for %s' % (data.get('ip') or wan_ip))
    info = {
        'isp_name': match.group(2),
        'isp_organization': match.group(2),
        'asn': int(match.group(1)),
        'address': data.get('ip') or wan_ip or '',
    }
    for key, field in (('city', 'city'), ('country_code', 'country'), ('timezone', 'timezone')):
        if data.get(field):
            info[key] = data[field]
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
        except (urllib.error.URLError, OSError, ValueError) as e:
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
