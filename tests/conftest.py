# -*- coding: utf-8 -*-
import configparser
import copy
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from collectors.opnsense_api import OPNsenseAPIError  # noqa: E402


# ── Canned OPNsense API responses (shapes follow the OPNsense source) ────

# WAN and LAN are logical, as on a typical box: PPPoE over igb0 (unassigned)
# and a bridge of igb1 and em0 (no cable). Only NICs report media.
INTERFACES_INFO = {
    'total': 5, 'rowCount': 5, 'current': 1,
    'rows': [
        {
            'device': 'pppoe0', 'identifier': 'wan', 'description': 'WAN',
            'is_physical': False, 'macaddr': '00:00:00:00:00:00',
            'status': 'up', 'mtu': '1492', 'enabled': True,
            'link_type': 'pppoe', 'addr4': '81.2.3.4/32',
            'ipv4': [{'ipaddr': '81.2.3.4/32'}],
            'gateways': ['81.2.3.1', 'fe80::1'],
            'ifctl.nameserver': ['81.2.3.53', '81.2.3.54', '2001:db8::53'],
            'statistics': {
                'bytes received': '1000000', 'bytes transmitted': '200000',
                'packets received': '1000', 'packets transmitted': '500',
                'input errors': '1', 'output errors': '2',
                'input queue drops': '3', 'send queue drops': '4',
                'multicasts received': '5', 'multicasts transmitted': '6',
            },
        },
        {
            'device': 'bridge0', 'identifier': 'lan', 'description': 'LAN',
            'macaddr': '00:00:5E:00:53:01', 'status': 'up', 'mtu': '1500',
            'is_physical': False,
            'members': {'igb1': {'flags': ['learning', 'discover']},
                        'em0': {'flags': ['learning', 'discover']}},
            'addr4': '192.168.48.254/23',
            'ipv4': [{'ipaddr': '192.168.48.254/23'}],
            'gateways': [],
            'statistics': {
                'bytes received': '5000', 'bytes transmitted': '6000',
                'packets received': '50', 'packets transmitted': '60',
            },
        },
        {'device': 'em0', 'identifier': 'opt2', 'description': 'OPT2', 'status': 'no carrier',
         'is_physical': True, 'media': 'Ethernet autoselect'},
        {'device': 'igb0', 'identifier': '', 'description': 'Unassigned Interface',
         'status': 'up', 'is_physical': True, 'macaddr': '00:00:5e:00:53:10',
         'media': '10Gbase-SR <full-duplex,rxpause,txpause>'},
        {'device': 'igb1', 'identifier': '', 'description': 'Unassigned Interface',
         'status': 'up', 'is_physical': True, 'macaddr': '00:00:5e:00:53:11',
         'media': '2500Base-T <full-duplex>'},
    ],
}

ARP = {
    'total': 3, 'rows': [
        {'mac': '00:00:5e:00:53:02', 'ip': '192.168.48.2', 'intf': 'bridge0',
         'expired': False, 'hostname': '', 'intf_description': 'LAN'},
        {'mac': '00:00:5e:00:53:03', 'ip': '10.8.1.1', 'intf': 'bridge0',
         'expired': False, 'hostname': 'phone.lan', 'intf_description': 'LAN'},
        {'mac': '00:11:22:33:44:55', 'ip': '81.2.3.1', 'intf': 'pppoe0',
         'expired': False, 'hostname': '', 'intf_description': 'WAN'},
    ],
}

DNSMASQ_LEASES = {
    'total': 2, 'rows': [
        {'address': '192.168.48.2', 'hwaddr': '00:00:5E:00:53:02', 'hostname': 'pi', 'expire': 1},
        {'address': '192.168.48.9', 'hwaddr': 'aa:aa:aa:aa:aa:09', 'hostname': '*', 'expire': 1},
    ],
}

SYSTEM_INFORMATION = {
    'name': 'router.example.net',
    'versions': ['OPNsense 25.7.5-amd64', 'FreeBSD 14.3-RELEASE-p4', 'OpenSSL 3.0'],
}

SYSTEM_TIME = {'uptime': '2 days, 03:04:05', 'loadavg': '0.1, 0.2, 0.3'}

SYSTEM_RESOURCES = {'memory': {'total': '8000', 'used': '2000'}}

GATEWAY_STATUS = {
    'status': 'ok', 'items': [
        {'name': 'WAN_PPPOE', 'address': '81.2.3.1', 'status': 'none',
         'delay': '8.4 ms', 'stddev': '0.3 ms', 'loss': '0.0 %'},
    ],
}

RESPONSES = {
    '/api/interfaces/overview/interfaces_info/1': INTERFACES_INFO,
    '/api/diagnostics/interface/search_arp': ARP,
    '/api/dnsmasq/leases/search': DNSMASQ_LEASES,
    '/api/diagnostics/system/system_information': SYSTEM_INFORMATION,
    '/api/diagnostics/system/system_time': SYSTEM_TIME,
    '/api/diagnostics/system/system_resources': SYSTEM_RESOURCES,
    '/api/routes/gateway/status': GATEWAY_STATUS,
}


class FakeAPI:
    """Stands in for OPNsenseAPI. Paths missing from `responses` 404,
    paths listed in `forbidden` 403."""

    def __init__(self, responses=None, forbidden=(), cpu=None):
        self.responses = copy.deepcopy(RESPONSES if responses is None else responses)
        self.forbidden = set(forbidden)
        self.cpu = {'total': 12.5} if cpu is None else cpu
        self.calls = []

    def _answer(self, path):
        self.calls.append(path)
        base = path.split('?')[0]
        if base in self.forbidden or any(base.startswith(f) for f in self.forbidden):
            raise OPNsenseAPIError(path, 403, 'Forbidden')
        for prefix, response in self.responses.items():
            if base == prefix or (prefix.endswith('/') and base.startswith(prefix)):
                return copy.deepcopy(response(path) if callable(response) else response)
        raise OPNsenseAPIError(path, 404, 'Not Found')

    def get(self, path):
        return self._answer(path)

    def post(self, path, body=None):
        return self._answer(path)

    def first_event(self, path, timeout=5):
        self.calls.append(path)
        if path in self.forbidden:
            raise OPNsenseAPIError(path, 403, 'Forbidden')
        return dict(self.cpu)


@pytest.fixture
def fake_api():
    return FakeAPI()


@pytest.fixture
def gateway_config():
    cfg = configparser.RawConfigParser()
    cfg.read_dict({
        'global': {'pid_file': 'test.pid'},
        'opnsense': {'host': '192.168.48.254', 'api_key': 'k', 'api_secret': 's'},
        'gateway': {
            'is_adopted': 'True',
            'use_aes_gcm': 'False',
            'url': 'http://192.0.2.10:8080/inform',
        },
        'provisioned': {},
    })
    return cfg
