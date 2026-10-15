# -*- coding: utf-8 -*-
"""Inform payloads built from OPNsense data."""
import json
from unittest.mock import patch

import pytest

from collectors.api_source import APISource
from collectors.opnsense import OPNsenseCollector
from devices import Device
from tests.conftest import FakeAPI


@pytest.fixture
def device(gateway_config):
    with patch('devices.fetch_latest_firmware', return_value='9.9.9.99999'):
        dev = Device(gateway_config)
        dev.firmware()
    return dev


@pytest.fixture
def collector(gateway_config, device):
    dc = OPNsenseCollector(gateway_config, APISource(FakeAPI()), device.wan_port, device.lan_port)
    gateway_config.set('gateway', 'lan_mac', dc.lan_mac)
    gateway_config.set('gateway', 'lan_ip', dc.lan_ip)
    return dc


def test_complete_inform(gateway_config, collector, device):
    from unifi_protocol import create_inform
    payload = json.loads(create_inform(gateway_config, collector, device))

    assert payload['model'] == device.model
    assert payload['version'] == '9.9.9.99999'
    wan, lan = device.wan_port, device.lan_port

    assert payload['mac'] == '00:00:5e:00:53:01'
    assert payload['ip'] == '192.168.48.254'
    assert payload['netmask'] == '255.255.254.0'
    assert payload['hostname'] == 'router'
    assert payload['uptime'] > 0
    assert payload['system-stats']['cpu'] == '12'

    if_table = {i['name']: i for i in payload['if_table']}
    assert if_table[wan]['gateways'] == ['81.2.3.1']
    assert if_table[wan]['latency'] == 8.4
    assert if_table[lan]['speed'] == 2500

    lan_net = [n for n in payload['network_table'] if n['name'] == lan][0]
    assert len(lan_net['host_table']) == 3
    assert {'pfx': '0.0.0.0/0', 'nh': [{'intf': wan, 'metric': '1/0', 't': 'S>*', 'via': '81.2.3.1'}]} \
        in payload['routes']
    assert {'pfx': '192.168.48.0/23', 'nh': [{'intf': lan, 't': 'C>*'}]} in payload['routes']
    assert payload['uplink'] == wan


def test_uxg_fields(gateway_config, collector, device):
    from unifi_protocol import create_inform
    payload = json.loads(create_inform(gateway_config, collector, device))
    assert payload['type'] == 'uxg'
    assert payload['model_display'] == 'UniFi Gateway Lite'
    assert payload['num_port'] == 2
    roles = {p['ifname']: p['role'] for p in payload['ethernet_overrides']}
    assert roles == {'eth0': 'lan', 'eth1': 'wan'}
    assert [p['port_idx'] for p in payload['port_table']] == [1, 2]


def test_uxg_clients_in_lan_port_mac_table(gateway_config, collector, device):
    """The controller lists a UXG's clients from the mac_table of its up LAN
    ports (matched through ethernet_overrides), not from host_table."""
    from unifi_protocol import create_inform
    payload = json.loads(create_inform(gateway_config, collector, device))
    lan_ports = {o['ifname'] for o in payload['ethernet_overrides'] if o['networkgroup'] == 'LAN'}
    ports = {p['name']: p for p in payload['port_table']}
    assert lan_ports == {'eth0'}
    lan = ports['eth0']
    assert lan['up'] is True
    assert sorted(h['mac'] for h in lan['mac_table']) == \
        sorted(h['mac'] for h in collector.data['host_table'])
    assert len(lan['mac_table']) == 3
    # no vlan means the untagged LAN; anything else would hide the clients
    assert all(h.get('vlan', 1) == 1 for h in lan['mac_table'])
    assert 'mac_table' not in ports['eth1']


def test_hostname_override(gateway_config, collector, device):
    from unifi_protocol import create_inform
    gateway_config.set('gateway', 'hostname', 'edge')
    assert json.loads(create_inform(gateway_config, collector, device))['hostname'] == 'edge'


def test_partial_inform_before_adoption(gateway_config, collector, device):
    from unifi_protocol import create_inform
    gateway_config.set('gateway', 'is_adopted', 'False')
    payload = json.loads(create_inform(gateway_config, collector, device))
    assert payload['mac'] == '00:00:5e:00:53:01'
    assert (payload['state'], payload['default'], payload['adopted']) == (1, True, False)
    assert payload['x_authkey'] == 'ba86f2bbe107c7c57eb5f2690775c712'
    # a factory-default device still reports everything it knows
    assert payload['if_table'] and payload['network_table']


def test_inform_ip_is_resolved(gateway_config, collector, device):
    """The controller rejects adoption when inform_ip is a hostname."""
    from unifi_protocol import create_inform
    gateway_config.set('gateway', 'url', 'http://localhost:8080/inform')
    payload = json.loads(create_inform(gateway_config, collector, device))
    assert payload['inform_ip'] == '127.0.0.1'


def test_encode_decode_round_trip(gateway_config, collector, device):
    from unifi_protocol import create_inform, encode_inform, decode_inform
    gateway_config.set('gateway', 'is_adopted', 'False')
    data = create_inform(gateway_config, collector, device)
    assert decode_inform(gateway_config, encode_inform(gateway_config, data)) == json.loads(data)


def test_broadcast_message(gateway_config, collector, device):
    from unifi_protocol import create_broadcast_message
    message = create_broadcast_message(gateway_config, 1, device, collector.uptime())
    assert bytes([0x00, 0x00, 0x5e, 0x00, 0x53, 0x01]) in message
