# -*- coding: utf-8 -*-
"""Router state from the OPNsense REST API.

Each call needs a privilege on the API key's user; when one is missing the
SourceError carries the privilege name as its hint.
"""
import json
import urllib.parse

from .opnsense_api import OPNsenseAPI, OPNsenseAPIError, read_api_key_file
from .source import PingJob, Source, SourceError

DASHBOARD = 'Lobby: Dashboard'
PING = 'Diagnostics: Ping'
LIST_ALL = {'current': 1, 'rowCount': -1}

LEASE_ENDPOINTS = {
    'dnsmasq': ('/api/dnsmasq/leases/search', 'Services: Dnsmasq DNS/DHCP: Settings'),
    'kea': ('/api/kea/leases4/search', 'Services: DHCP: Kea(v4)'),
    'isc': ('/api/dhcpv4/leases/search_lease', 'Status: DHCP leases'),
}


def _rows(response):
    if isinstance(response, dict):
        return response.get('rows', response.get('items', []))
    return response if isinstance(response, list) else []


def create_api_source(config):
    """Build an API source from [opnsense] config, after env overrides."""
    host = config.get('opnsense', 'host', fallback='').strip()
    if not host:
        raise ValueError('No router address set: put host = <router IP> under '
                         '[opnsense] or set OPNSENSE_HOST')

    key = config.get('opnsense', 'api_key', fallback='').strip()
    secret = config.get('opnsense', 'api_secret', fallback='').strip()
    key_file = config.get('opnsense', 'api_key_file', fallback='').strip()
    if (not key or not secret) and key_file:
        key, secret = read_api_key_file(key_file)
    if not key or not secret:
        raise ValueError('No OPNsense API credentials: set api_key/api_secret '
                         'or api_key_file under [opnsense]')

    return APISource(OPNsenseAPI(
        host, key, secret,
        verify_ssl=config.getboolean('opnsense', 'verify_ssl', fallback=True),
        timeout=config.getint('opnsense', 'timeout', fallback=15),
    ))


class APISource(Source):

    def __init__(self, api):
        self.api = api

    def _get(self, path, privilege, body=None):
        try:
            if body is not None:
                return self.api.post(path, body)
            return self.api.get(path)
        except OPNsenseAPIError as e:
            e.hint = 'grant the "%s" privilege' % privilege
            raise

    def interfaces(self):
        return _rows(self._get('/api/interfaces/overview/interfaces_info/1',
                               'Status: Interfaces'))

    def ppps(self):
        # The REST API has no PPP device endpoint; the collector infers the
        # port a PPP link runs over from the unassigned interfaces instead
        return []

    def system_information(self):
        return self._get('/api/diagnostics/system/system_information', DASHBOARD)

    def system_time(self):
        return self._get('/api/diagnostics/system/system_time', DASHBOARD)

    def system_resources(self):
        return self._get('/api/diagnostics/system/system_resources', DASHBOARD)

    def cpu_usage(self):
        path = '/api/diagnostics/cpu_usage/stream'
        try:
            return self.api.first_event(path)
        except OPNsenseAPIError as e:
            e.hint = 'grant the "%s" privilege' % DASHBOARD
            raise

    def arp(self):
        return _rows(self._get('/api/diagnostics/interface/search_arp',
                               'Diagnostics: ARP Table', LIST_ALL))

    def dhcp_leases(self, server):
        path, privilege = LEASE_ENDPOINTS[server]
        return _rows(self._get(path, privilege, LIST_ALL))

    def dhcp_reservations(self, server):
        if server != 'kea':
            return []
        return _rows(self._get('/api/kea/dhcpv4/search_reservation',
                               LEASE_ENDPOINTS['kea'][1], LIST_ALL))

    def dhcp_subnets(self, server):
        if server == 'kea':
            return _rows(self._get('/api/kea/dhcpv4/search_subnet',
                                   LEASE_ENDPOINTS['kea'][1], LIST_ALL))
        if server == 'dnsmasq':
            rows = _rows(self._get('/api/dnsmasq/settings/search_range',
                                   LEASE_ENDPOINTS['dnsmasq'][1], LIST_ALL))
            return [{'subnet': '', 'pools': '%s-%s' % (row['start_addr'], row['end_addr'])}
                    for row in rows if row.get('start_addr') and row.get('end_addr')]
        # ISC's ranges have no API endpoint
        return []

    def reverse_dns(self, addresses):
        names = {}
        addresses = list(addresses)
        for i in range(0, len(addresses), 50):
            query = '&'.join('address[]=%s' % urllib.parse.quote(a) for a in addresses[i:i + 50])
            response = self._get('/api/diagnostics/dns/reverse_lookup?' + query,
                                 'Diagnostics: Network Insight')
            if isinstance(response, dict):
                names.update(response)
        return names

    def gateway_status(self):
        return _rows(self._get('/api/routes/gateway/status', 'System: Gateways'))

    def unbound_settings(self):
        return self._get('/api/unbound/settings/get', 'Services: Unbound')

    def unbound_infra(self):
        return _rows(self._get('/api/unbound/diagnostics/dumpinfra', 'Services: Unbound'))

    def flow_totals(self, since, until, device, direction):
        path = ('/api/diagnostics/networkinsight/top/FlowSourceAddrTotals/'
                '%d/%d/src_addr/octets/1000?filter_field=if,direction'
                '&filter_value=%s,%s' % (since, until, device, direction))
        return _rows(self._get(path, 'Diagnostics: Network Insight'))

    def ping(self, destination, count, interval, packet_size):
        return APIPing(self, destination, count, interval, packet_size)


class APIPing(PingJob):
    """A Diagnostics > Ping job. The API only reports running totals (sent,
    received, average), so each new reply's time is worked out from how the
    average moved, and its sequence number from the packets lost so far."""

    def __init__(self, source, destination, count, interval, packet_size):
        self.source = source
        self.count = count
        settings = {
            'hostname': destination,
            'fam': 'ip6' if ':' in destination else 'ip',
            'packetsize': str(packet_size),
            'interval': str(interval),
            'description': 'UniFi ping test',
        }
        result = source._get('/api/diagnostics/ping/set', PING, {'ping': {'settings': settings}})
        self.job = result.get('uuid') if isinstance(result, dict) else None
        if not self.job:
            raise SourceError('/api/diagnostics/ping/set', None,
                              json.dumps(result.get('validations', result)
                                         if isinstance(result, dict) else result))
        started = source._get('/api/diagnostics/ping/start/' + self.job, PING, {})
        if not isinstance(started, dict) or started.get('status') != 'ok':
            try:
                self.stop()
            except SourceError:
                pass
            raise SourceError('/api/diagnostics/ping/start', None,
                              (started or {}).get('status_msg', 'not started'))
        self.received = 0
        self.total = 0.0     # sum of reply times so far
        self.next_id = 0
        self.lost = 0
        self.assigned = set()
        self.late_polls = 0

    def _reply_id(self, sent):
        if self.next_id < sent:
            reply_id = self.next_id
            self.next_id += 1
        else:
            # a packet taken for lost was answered after all
            reply_id = max(i for i in range(sent) if i not in self.assigned)
        self.assigned.add(reply_id)
        return reply_id

    def poll(self):
        rows = _rows(self.source._get('/api/diagnostics/ping/search_jobs', PING, LIST_ALL))
        row = next((r for r in rows if r.get('id') == self.job), None)
        if row is None:
            raise SourceError('/api/diagnostics/ping/search_jobs', None, 'ping job gone')
        # the job pings until stopped, so anything past `count` is ignored
        sent = min(int(row.get('send') or 0), self.count)
        received = min(int(row.get('received') or 0), sent)
        # packets unanswered besides the one in flight take the next
        # sequence numbers (a late reply fills one of them in again)
        lost = max(sent - received - 1, 0)
        self.next_id += max(lost - self.lost, 0)
        self.lost = max(lost, self.lost)
        replies = []
        if received > self.received:
            # ping prints the average to the microsecond
            total = float(row.get('avg') or 0) * received
            each = max(total - self.total, 0.0) / (received - self.received)
            for _ in range(received - self.received):
                replies.append({'id': self._reply_id(sent), 'time': round(each, 3), 'ttl': 64})
            self.received, self.total = received, total
        if sent >= self.count:
            # give the last reply a moment; the totals lag a poll behind
            self.late_polls += 1
        done = (sent >= self.count and (received >= sent or self.late_polls > 2)) or \
            (row.get('status') != 'running' and sent > 0)
        return sent, replies, done

    def stop(self):
        for action in ('stop', 'remove'):
            self.source._get('/api/diagnostics/ping/%s/%s' % (action, self.job), PING, {})
