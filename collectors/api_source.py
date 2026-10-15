# -*- coding: utf-8 -*-
"""Router state from the OPNsense REST API.

Each call needs a privilege on the API key's user; when one is missing the
SourceError carries the privilege name as its hint.
"""
import urllib.parse

from .opnsense_api import OPNsenseAPI, OPNsenseAPIError, read_api_key_file
from .source import Source

DASHBOARD = 'Lobby: Dashboard'
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
