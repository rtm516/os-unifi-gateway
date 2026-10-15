# -*- coding: utf-8 -*-
"""Tests for the API-driven OPNsense collector."""
import json
import time
from unittest.mock import patch

import pytest

from collectors.api_source import APISource, create_api_source
from collectors.opnsense import (
    OPNsenseCollector, cidr_to_netmask, parse_media_duplex, parse_media_speed, parse_uptime,
)
from collectors.source import SourceError
from tests.conftest import FakeAPI


def make(config, api):
    return OPNsenseCollector(config, APISource(api))


class TestHelpers:

    @pytest.mark.parametrize('text,expected', [
        ('2 days, 03:04:05', 2 * 86400 + 3 * 3600 + 4 * 60 + 5),
        ('1 day, 00:00:01', 86401),
        ('00:10:00', 600),
        ('', 0),
        ('garbage', 0),
    ])
    def test_parse_uptime(self, text, expected):
        assert parse_uptime(text) == expected

    @pytest.mark.parametrize('media,expected', [
        ('Ethernet autoselect (1000baseT <full-duplex>)', 1000),
        ('Ethernet autoselect (2500Base-T <full-duplex>)', 2500),
        ('Ethernet 10Gbase-SR <full-duplex>', 10000),
        ('', None),
        (None, None),
    ])
    def test_parse_media_speed(self, media, expected):
        assert parse_media_speed(media) == expected

    @pytest.mark.parametrize('bits,mask', [
        (24, '255.255.255.0'), (23, '255.255.254.0'), (32, '255.255.255.255'), (0, '0.0.0.0'),
    ])
    def test_cidr_to_netmask(self, bits, mask):
        assert cidr_to_netmask(bits) == mask


class TestCreateApi:

    def test_requires_host(self, gateway_config):
        gateway_config.set('opnsense', 'host', '')
        with pytest.raises(ValueError, match='router address'):
            create_api_source(gateway_config)

    def test_requires_credentials(self, gateway_config):
        gateway_config.set('opnsense', 'api_secret', '')
        with pytest.raises(ValueError, match='credentials'):
            create_api_source(gateway_config)

    def test_reads_key_file(self, gateway_config, tmp_path):
        key_file = tmp_path / 'apikey.txt'
        key_file.write_text('key=abc\nsecret=def\n')
        gateway_config.set('opnsense', 'api_key', '')
        gateway_config.set('opnsense', 'api_secret', '')
        gateway_config.set('opnsense', 'api_key_file', str(key_file))
        source = create_api_source(gateway_config)
        assert source.api.base_url == 'https://192.168.48.254'

    def test_keeps_explicit_scheme_and_port(self, gateway_config):
        gateway_config.set('opnsense', 'host', 'http://router:8443/')
        assert create_api_source(gateway_config).api.base_url == 'http://router:8443'


class TestInterfaces:

    def test_ports_follow_wan_and_lan_assignments(self, gateway_config, fake_api):
        dc = make(gateway_config, fake_api)
        assert dc.ports == [
            {'ifname': 'eth0', 'name': 'WAN', 'type': 'wan', 'realif': 'pppoe0'},
            {'ifname': 'eth1', 'name': 'LAN', 'type': 'lan', 'realif': 'bridge0'},
        ]

    def test_identity_comes_from_lan(self, gateway_config, fake_api):
        dc = make(gateway_config, fake_api)
        assert dc.lan_mac == '00:00:5e:00:53:01'
        assert dc.lan_ip == '192.168.48.254'

    def test_addresses_gateway_and_dns(self, gateway_config, fake_api):
        dc = make(gateway_config, fake_api)
        assert dc.data['ip']['eth0'] == {
            'address': '81.2.3.4', 'netmask': '255.255.255.255', 'gateway': '81.2.3.1',
        }
        assert dc.data['ip']['eth1'] == {'address': '192.168.48.254', 'netmask': '255.255.254.0'}
        assert dc.data['nameservers'] == ['81.2.3.53', '81.2.3.54']

    def test_dns_from_unbound_forwarders(self, gateway_config, fake_api):
        fake_api.responses['/api/unbound/settings/get'] = {'unbound': {
            'forwarding': {'enabled': '0'},
            'dots': {'dot': {
                'a': {'enabled': '1', 'domain': '', 'server': '9.9.9.9', 'port': '853'},
                'b': {'enabled': '0', 'domain': '', 'server': '8.8.8.8', 'port': ''},
                'c': {'enabled': '1', 'domain': 'corp.example', 'server': '10.0.0.53'},
                'd': {'enabled': '1', 'domain': '', 'server': '2620:fe::fe'},
                'e': {'enabled': '1', 'domain': '', 'server': '149.112.112.112'},
            }},
        }}
        dc = make(gateway_config, fake_api)
        assert dc.data['nameservers'] == ['9.9.9.9', '149.112.112.112']

    def test_dns_from_unbound_infra_when_forwarding_system_servers(self, gateway_config, fake_api):
        fake_api.responses['/api/unbound/settings/get'] = {'unbound': {
            'forwarding': {'enabled': '1'}, 'dots': {'dot': {}},
        }}
        fake_api.responses['/api/unbound/diagnostics/dumpinfra'] = [
            {'ip': '1.1.1.1', 'host': '.', 'ttl': '500', 'rtt': '24'},
            {'ip': '2606:4700:4700::1111', 'host': '.', 'ttl': '500'},
            {'ip': '1.0.0.1', 'host': '.', 'ttl': '500'},
        ]
        dc = make(gateway_config, fake_api)
        assert dc.data['nameservers'] == ['1.1.1.1', '1.0.0.1']

    def test_recursive_unbound_falls_back_to_wan_dns(self, gateway_config, fake_api):
        fake_api.responses['/api/unbound/settings/get'] = {'unbound': {
            'forwarding': {'enabled': '0'}, 'dots': {'dot': {}},
        }}
        dc = make(gateway_config, fake_api)
        assert dc.data['nameservers'] == ['81.2.3.53', '81.2.3.54']
        assert '/api/unbound/diagnostics/dumpinfra' not in fake_api.calls

    def test_pppoe_shows_the_mac_of_its_nic(self, gateway_config, fake_api):
        dc = make(gateway_config, fake_api)
        assert dc.data['macs']['eth0'] == '00:00:5e:00:53:10'  # igb0, which it dials over

    def test_mac_placeholder_without_a_nic(self, gateway_config, fake_api):
        rows = fake_api.responses['/api/interfaces/overview/interfaces_info/1']['rows']
        for row in rows:
            if row['device'] == 'igb0':
                del row['macaddr']
        dc = make(gateway_config, fake_api)
        assert dc.data['macs']['eth0'] == '00:00:00:00:00:00'

    def test_counters_and_link(self, gateway_config, fake_api):
        dc = make(gateway_config, fake_api)
        wan, lan = dc.data['ifstat']['eth0'], dc.data['ifstat']['eth1']
        assert wan['rx_bytes'] == 1000000 and wan['tx_bytes'] == 200000
        assert wan['rx_errors'] == 1 and wan['tx_dropped'] == 4
        assert wan['mtu'] == 1492 and wan['up'] is True
        assert lan['up'] is True

    def test_rates_from_counter_deltas(self, gateway_config, fake_api):
        with patch('collectors.opnsense.time.time', return_value=1000.0):
            dc = make(gateway_config, fake_api)
        stats = fake_api.responses['/api/interfaces/overview/interfaces_info/1']['rows'][0]['statistics']
        stats['bytes received'] = str(1000000 + 10 * 5000)
        with patch('collectors.opnsense.time.time', return_value=1010.0):
            dc.update()
        assert dc.data['ifstat']['eth0']['rx_bps'] == 5000
        assert dc.data['ifstat']['eth0']['tx_bps'] == 0

    def test_counter_reset_reports_zero_rate(self, gateway_config, fake_api):
        with patch('collectors.opnsense.time.time', return_value=1000.0):
            dc = make(gateway_config, fake_api)
        stats = fake_api.responses['/api/interfaces/overview/interfaces_info/1']['rows'][0]['statistics']
        stats['bytes received'] = '10'
        with patch('collectors.opnsense.time.time', return_value=1010.0):
            dc.update()
        assert dc.data['ifstat']['eth0']['rx_bps'] == 0

    def test_custom_interface_assignment(self, gateway_config, fake_api):
        gateway_config.set('opnsense', 'lan_interface', 'opt2')
        rows = fake_api.responses['/api/interfaces/overview/interfaces_info/1']['rows']
        rows[2].update({'macaddr': 'aa:bb:cc:00:00:02', 'ipv4': [{'ipaddr': '192.168.5.1/24'}]})
        dc = make(gateway_config, fake_api)
        assert dc.ports[1]['realif'] == 'em0'
        assert dc.lan_ip == '192.168.5.1'

    def test_matches_description_case_insensitively(self, gateway_config, fake_api):
        gateway_config.set('opnsense', 'lan_interface', 'opt2')
        rows = fake_api.responses['/api/interfaces/overview/interfaces_info/1']['rows']
        rows[2].update({'description': 'IoT', 'identifier': 'opt5',
                        'ipv4': [{'ipaddr': '192.168.7.1/24'}]})
        gateway_config.set('opnsense', 'lan_interface', 'iot')
        dc = make(gateway_config, fake_api)
        assert dc.ports[1]['realif'] == 'em0'
        assert dc.ports[1]['name'] == 'IoT'

    def test_description_wins_over_identifier(self, gateway_config, fake_api):
        rows = fake_api.responses['/api/interfaces/overview/interfaces_info/1']['rows']
        # the interface assigned as "lan" was renamed; another one is now called LAN
        rows[1]['description'] = 'Servers'
        rows[2].update({'description': 'LAN', 'macaddr': 'aa:bb:cc:00:00:02',
                        'ipv4': [{'ipaddr': '192.168.5.1/24'}]})
        dc = make(gateway_config, fake_api)
        assert dc.lan_device == 'em0'

    def test_unknown_interface_raises(self, gateway_config, fake_api):
        gateway_config.set('opnsense', 'lan_interface', 'Guests')
        with pytest.raises(SourceError, match='Guests') as exc:
            make(gateway_config, fake_api)
        assert 'LAN (lan)' in str(exc.value)

    def test_interfaces_are_required(self, gateway_config):
        api = FakeAPI(forbidden={'/api/interfaces/overview/interfaces_info/1'})
        with pytest.raises(SourceError) as exc:
            make(gateway_config, api)
        assert exc.value.forbidden
        assert 'Status: Interfaces' in exc.value.hint


def rows_of(api):
    return {r['device']: r for r in api.responses['/api/interfaces/overview/interfaces_info/1']['rows']}


def add_row(api, **row):
    api.responses['/api/interfaces/overview/interfaces_info/1']['rows'].append(row)


class TestLinkSpeed:
    """WAN and LAN are usually logical (PPPoE, bridge, VLAN) with no media of
    their own: speed, duplex and link come from the NICs underneath."""

    @pytest.mark.parametrize('media, expected', [
        ('1000baseT <full-duplex,rxpause,txpause>', True),
        ('100baseTX <half-duplex>', False),
        ('Ethernet autoselect', None),
        (None, None),
    ])
    def test_parse_media_duplex(self, media, expected):
        assert parse_media_duplex(media) is expected

    def test_pppoe_wan_and_bridge_lan_use_their_nics(self, gateway_config, fake_api):
        dc = make(gateway_config, fake_api)
        wan, lan = dc.data['ifstat']['eth0'], dc.data['ifstat']['eth1']
        assert (wan['speed'], wan['full_duplex'], wan['l1up']) == (10000, True, True)
        assert (lan['speed'], lan['full_duplex'], lan['l1up']) == (2500, True, True)

    def test_bridge_uses_fastest_up_member(self, gateway_config, fake_api):
        rows = rows_of(fake_api)
        rows['em0'].update(status='up', media='1000baseT <full-duplex>')
        rows['igb1'].update(status='no carrier', media='Ethernet autoselect')
        dc = make(gateway_config, fake_api)
        assert dc.data['ifstat']['eth1']['speed'] == 1000

        rows['igb1'].update(status='up', media='10Gbase-T <full-duplex>')
        dc = make(gateway_config, fake_api)
        assert dc.data['ifstat']['eth1']['speed'] == 10000

    def test_bridge_without_link_is_down(self, gateway_config, fake_api):
        rows_of(fake_api)['igb1'].update(status='no carrier', media='Ethernet autoselect')
        lan = make(gateway_config, fake_api).data['ifstat']['eth1']
        assert (lan['up'], lan['l1up'], lan['speed']) == (False, False, 1000)

    def test_vlan_uses_parent(self, gateway_config, fake_api):
        rows_of(fake_api)['bridge0'].update(device='vlan01', members=None,
                                            vlan={'tag': '10', 'parent': 'igb1'})
        assert make(gateway_config, fake_api).data['ifstat']['eth1']['speed'] == 2500

    def test_pppoe_over_vlan(self, gateway_config, fake_api):
        add_row(fake_api, device='vlan02', identifier='', is_physical=False, status='up',
                vlan={'tag': '911', 'parent': 'igb0'})
        dc = make(gateway_config, fake_api)
        assert dc.data['ifstat']['eth0']['speed'] == 10000

    def test_lagg_uses_fastest_port(self, gateway_config, fake_api):
        rows = rows_of(fake_api)
        rows['bridge0'].update(device='lagg0', members=None,
                               laggport={'igb1': {}, 'em0': {}})
        rows['em0'].update(status='up', media='1000baseT <half-duplex>')
        lan = make(gateway_config, fake_api).data['ifstat']['eth1']
        assert (lan['speed'], lan['full_duplex']) == (2500, True)

    def test_assigned_nic_uses_own_media(self, gateway_config, fake_api):
        gateway_config.set('opnsense', 'lan_interface', 'OPT2')
        rows_of(fake_api)['em0'].update(status='up', media='100baseTX <half-duplex>')
        lan = make(gateway_config, fake_api).data['ifstat']['eth1']
        assert (lan['speed'], lan['full_duplex'], lan['up']) == (100, False, True)

    def test_ambiguous_pppoe_port_falls_back(self, gateway_config, fake_api):
        add_row(fake_api, device='igb2', identifier='', is_physical=True, status='up',
                media='1000baseT <full-duplex>')
        assert make(gateway_config, fake_api).data['ifstat']['eth0']['speed'] == 1000

    def test_pppoe_prefers_up_unassigned_nic(self, gateway_config, fake_api):
        add_row(fake_api, device='igb2', identifier='', is_physical=True,
                status='no carrier', media='Ethernet autoselect')
        assert make(gateway_config, fake_api).data['ifstat']['eth0']['speed'] == 10000

    def test_pppoe_port_from_source(self, gateway_config, fake_api):
        add_row(fake_api, device='igb2', identifier='', is_physical=True, status='up',
                media='1000baseT <full-duplex>')
        source = APISource(fake_api)
        source.ppps = lambda: [{'if': 'pppoe0', 'type': 'pppoe', 'ports': ['igb2']}]
        dc = OPNsenseCollector(gateway_config, source)
        assert dc.data['ifstat']['eth0']['speed'] == 1000

    def test_wan_down_when_pppoe_nic_has_no_link(self, gateway_config, fake_api):
        rows_of(fake_api)['igb0'].update(status='no carrier', media='Ethernet autoselect')
        wan = make(gateway_config, fake_api).data['ifstat']['eth0']
        assert (wan['up'], wan['l1up']) == (False, False)

    def test_speed_reaches_payload(self, gateway_config, fake_api):
        from devices import Device
        from unifi_protocol import create_inform
        rows_of(fake_api)['igb1']['media'] = '100baseTX <half-duplex>'
        with patch('devices.fetch_latest_firmware', return_value='9.9.9.99999'):
            device = Device(gateway_config)
            device.firmware()
        dc = OPNsenseCollector(gateway_config, APISource(fake_api),
                               device.wan_port, device.lan_port)
        payload = json.loads(create_inform(gateway_config, dc, device))
        wan, lan = device.wan_port, device.lan_port

        if_table = {i['name']: i for i in payload['if_table']}
        assert (if_table[wan]['speed'], if_table[wan]['full_duplex']) == (10000, True)
        assert (if_table[lan]['speed'], if_table[lan]['full_duplex']) == (100, False)
        networks = {n['name']: n for n in payload['network_table']}
        assert (networks[wan]['speed'], networks[wan]['duplex']) == ('10000', 'full')
        assert (networks[lan]['speed'], networks[lan]['duplex']) == ('100', 'half')
        ports = {p['ifname']: p for p in payload['port_table']}
        assert (ports[wan]['speed'], ports[lan]['speed']) == (10000, 100)
        assert ports[lan]['full_duplex'] is False


class TestSystem:

    def test_system_data(self, gateway_config, fake_api):
        dc = make(gateway_config, fake_api)
        assert dc.data['hostname'] == 'router'
        assert dc.data['os_version'] == 'OPNsense 25.7.5-amd64'
        assert dc.data['system_stats'] == {'cpu': '12', 'mem': '25'}
        assert 183845 <= dc.uptime() <= 183846 + 5

    def test_cpu_from_idle(self, gateway_config):
        dc = make(gateway_config, FakeAPI(cpu={'idle': 70}))
        assert dc.data['system_stats']['cpu'] == '30'

    def test_sys_stats(self, gateway_config, fake_api):
        dc = make(gateway_config, fake_api)
        assert dc.data['sys_stats'] == {
            'loadavg_1': '0.1', 'loadavg_5': '0.2', 'loadavg_15': '0.3',
            'mem_total': 8000, 'mem_used': 2000, 'mem_buffer': 0}

    def test_latency_from_wan_gateway(self, gateway_config, fake_api):
        dc = make(gateway_config, fake_api)
        assert dc.data['latency'] == 8.4
        monitor = dict(dc.data['wan_monitor'])
        # up since the router started, as far as we know
        assert abs(monitor.pop('up_since') - (time.time() - dc.uptime())) < 2
        assert monitor == {'latency': 8.4, 'loss': 0.0, 'target': '81.2.3.1',
                           'down': False, 'downtime': 0}

    def test_wan_downtime_counts_up(self, gateway_config, fake_api):
        items = fake_api.responses['/api/routes/gateway/status']['items']
        with patch('collectors.opnsense.time.time', return_value=10000.0):
            dc = make(gateway_config, fake_api)

        def check(at, status, delay='8.4 ms'):
            items[0].update(status=status, delay=delay)
            dc.updated['latency'] = 0
            with patch('collectors.opnsense.time.time', return_value=at):
                dc.update()
            return dc.data['wan_monitor']

        # a dead link may have no delay at all
        down = check(10060.0, 'down', '~')
        assert (down['down'], down['up_since'], down['latency'], down['loss'],
                down['downtime']) == (True, None, 0.0, 100.0, 0)
        assert check(10180.0, 'down')['downtime'] == 120
        back = check(10240.0, 'none')
        assert (back['down'], back['downtime'], back['up_since']) == (False, 180, 10240.0)
        assert check(10300.0, 'none')['downtime'] == 180

    def test_wan_monitor_target_and_loss(self, gateway_config):
        api = FakeAPI()
        api.responses['/api/routes/gateway/status']['items'] = [
            {'name': 'VPN', 'address': '~', 'status': 'none', 'delay': '~', 'loss': '~', 'monitor': '~'},
            {'name': 'WAN_PPPOE', 'address': '81.2.3.1', 'status': 'down',
             'delay': '12.5 ms', 'loss': '35.0 %', 'monitor': '1.1.1.1'},
        ]
        dc = make(gateway_config, api)
        assert dc.data['latency'] == 12.5
        assert dc.data['wan_monitor'] == {
            'latency': 12.5, 'loss': 35.0, 'target': '1.1.1.1', 'down': True,
            'up_since': None, 'downtime': 0}

    def test_optional_endpoints_degrade(self, gateway_config):
        api = FakeAPI(forbidden={
            '/api/diagnostics/system/', '/api/diagnostics/cpu_usage/stream',
            '/api/routes/gateway/status', '/api/dnsmasq/', '/api/kea/', '/api/dhcpv4/',
            '/api/diagnostics/interface/search_arp', '/api/diagnostics/networkinsight/',
        })
        dc = make(gateway_config, api)
        assert 'uptime' not in dc.data
        assert dc.data['system_stats'] == {'cpu': '0', 'mem': '0'}
        assert dc.data['latency'] == 0
        assert 'wan_monitor' not in dc.data
        assert dc.data['dhcp_leases'] == []
        assert dc.data['host_table'] == []

    def test_unknown_uptime_counts_from_start(self, gateway_config, monkeypatch):
        # The controller's device list hides connected devices with uptime 0
        dc = make(gateway_config, FakeAPI(forbidden={'/api/diagnostics/system/'}))
        assert dc.uptime() == 1
        monkeypatch.setattr(time, 'time', lambda: dc._started + 120)
        assert dc.uptime() == 120

    def test_forbidden_endpoint_is_not_retried_immediately(self, gateway_config):
        api = FakeAPI(forbidden={'/api/routes/gateway/status'})
        dc = make(gateway_config, api)
        dc.updated['latency'] = 0
        dc.update()
        assert api.calls.count('/api/routes/gateway/status') == 1


class TestClients:

    def test_lease_source_autodetected(self, gateway_config, fake_api):
        dc = make(gateway_config, fake_api)
        assert dc._lease_server == 'dnsmasq'
        assert dc.data['dhcp_leases'] == [
            {'mac': '00:00:5e:00:53:02', 'ip': '192.168.48.2', 'expire': 1, 'hostname': 'pi'},
            {'mac': 'aa:aa:aa:aa:aa:09', 'ip': '192.168.48.9', 'expire': 1},
        ]

    def test_lease_expiry(self, gateway_config, fake_api):
        rows = fake_api.responses['/api/dnsmasq/leases/search']['rows']
        rows[0]['expire'] = 1700000000
        rows[1]['expire'] = 0  # a lease that never ends
        dc = make(gateway_config, fake_api)
        assert [lease.get('expire') for lease in dc.data['dhcp_leases']] == [1700000000, None]

    def test_kea_leases(self, gateway_config, fake_api):
        leases = fake_api.responses.pop('/api/dnsmasq/leases/search')
        fake_api.responses['/api/kea/leases4/search'] = leases
        dc = make(gateway_config, fake_api)
        assert dc._lease_server == 'kea'
        assert len(dc.data['dhcp_leases']) == 2

    def test_lease_source_with_leases_wins(self, gateway_config, fake_api):
        # on the router Dnsmasq's lease action answers next to Kea, with none
        leases = fake_api.responses['/api/dnsmasq/leases/search']
        fake_api.responses['/api/kea/leases4/search'] = leases
        fake_api.responses['/api/dnsmasq/leases/search'] = {'total': 0, 'rows': []}
        dc = make(gateway_config, fake_api)
        assert dc._lease_server == 'kea'
        assert len(dc.data['dhcp_leases']) == 2

        # Kea stays first, and is left when it has no leases but another has
        fake_api.responses['/api/kea/leases4/search'] = {'total': 0, 'rows': []}
        fake_api.responses['/api/dnsmasq/leases/search'] = leases
        dc.updated['dhcp_leases'] = 0
        dc.update()
        assert dc._lease_server == 'dnsmasq'
        assert len(dc.data['dhcp_leases']) == 2

        # no server with leases: none reported
        fake_api.responses['/api/dnsmasq/leases/search'] = {'total': 0, 'rows': []}
        dc.updated['dhcp_leases'] = 0
        dc.update()
        assert dc.data['dhcp_leases'] == []

    def test_dhcp_none(self, gateway_config, fake_api):
        gateway_config.set('opnsense', 'dhcp', 'none')
        dc = make(gateway_config, fake_api)
        assert dc.data['dhcp_leases'] == []
        assert '/api/dnsmasq/leases/search' not in fake_api.calls

    def test_host_table_merges_arp_and_leases(self, gateway_config, fake_api):
        dc = make(gateway_config, fake_api)
        hosts = {h['mac']: h for h in dc.data['host_table']}
        # WAN neighbours are not LAN clients
        assert set(hosts) == {'00:00:5e:00:53:02', '00:00:5e:00:53:03', 'aa:aa:aa:aa:aa:09'}
        assert hosts['00:00:5e:00:53:02']['hostname'] == 'pi'
        assert hosts['00:00:5e:00:53:03']['hostname'] == 'phone.lan'
        assert 'hostname' not in hosts['aa:aa:aa:aa:aa:09']

    def test_host_table_skips_router_itself(self, gateway_config, fake_api):
        # the LAN address's own permanent ARP entry made the controller
        # register the gateway as its own uplink
        fake_api.responses['/api/diagnostics/interface/search_arp']['rows'].append(
            {'mac': '00:00:5e:00:53:01', 'ip': '192.168.48.254', 'intf': 'bridge0',
             'expired': False, 'permanent': True, 'hostname': '', 'intf_description': 'LAN'})
        dc = make(gateway_config, fake_api)
        assert '00:00:5e:00:53:01' not in {h['mac'] for h in dc.data['host_table']}

    @staticmethod
    def names(dc):
        return {h['mac']: h.get('hostname') for h in dc.data['host_table']}

    @staticmethod
    def reverse_lookup(names):
        def answer(path):
            addresses = [p.split('=', 1)[1] for p in path.split('?', 1)[1].split('&')]
            return {a: names.get(a, a) for a in addresses}
        return answer

    def rebuild(self, dc):
        dc.updated['dhcp_leases'] = dc.updated['host_table'] = 0
        dc.update()
        return self.names(dc)

    def test_reservation_names_come_first(self, gateway_config, fake_api):
        # UniFi switches and APs report the Host Name a client sends, and the
        # controller shows whichever device reported last. Kea lower-cases
        # and qualifies it in the lease; a reservation keeps it as entered.
        leases = fake_api.responses.pop('/api/dnsmasq/leases/search')
        leases['rows'][0]['hostname'] = 'pi-kitchen.example.net.'
        fake_api.responses['/api/kea/leases4/search'] = leases
        reservations = '/api/kea/dhcpv4/search_reservation'
        fake_api.responses[reservations] = {'rows': [
            {'hw_address': '00:00:5E:00:53:02', 'hostname': 'Pi-Kitchen'},
            {'hw_address': '00:00:5e:00:53:99', 'hostname': ''},
        ]}
        fake_api.responses['/api/diagnostics/dns/reverse_lookup'] = \
            self.reverse_lookup({'192.168.48.2': 'media.example.net'})
        dc = make(gateway_config, fake_api)
        assert self.names(dc)['00:00:5e:00:53:02'] == 'Pi-Kitchen'

        # reservations that cannot be read are kept
        fake_api.forbidden.add(reservations)
        assert self.rebuild(dc)['00:00:5e:00:53:02'] == 'Pi-Kitchen'

    def test_name_without_a_reservation(self, gateway_config, fake_api):
        # the lease name without the router's domain, over DNS (which may be
        # an alias) and ARP
        arp = fake_api.responses['/api/diagnostics/interface/search_arp']['rows']
        arp[0]['hostname'] = 'PRINTER-3D'
        leases = fake_api.responses['/api/dnsmasq/leases/search']['rows']
        leases[0]['hostname'] = 'printer-3d.example.net.'
        lookup = '/api/diagnostics/dns/reverse_lookup'
        fake_api.responses[lookup] = self.reverse_lookup({'192.168.48.2': 'media.example.net'})
        dc = make(gateway_config, fake_api)
        assert self.names(dc)['00:00:5e:00:53:02'] == 'printer-3d'

        # a client without a lease is named by DNS, then ARP
        fake_api.responses[lookup] = self.reverse_lookup({'10.8.1.1': 'phone.example.net'})
        dc = make(gateway_config, fake_api)
        assert self.names(dc)['00:00:5e:00:53:03'] == 'phone'
        fake_api.responses[lookup] = self.reverse_lookup({})
        dc = make(gateway_config, fake_api)
        assert self.names(dc)['00:00:5e:00:53:03'] == 'phone.lan'

    def test_names_survive_refreshes_that_lack_them(self, gateway_config, fake_api):
        lookup = '/api/diagnostics/dns/reverse_lookup'
        fake_api.responses[lookup] = self.reverse_lookup({'10.8.1.1': 'phone.example.net'})
        dc = make(gateway_config, fake_api)
        first = self.names(dc)
        assert (first['00:00:5e:00:53:02'], first['00:00:5e:00:53:03']) == ('pi', 'phone')

        # the resolver answers with the bare address, then fails outright
        fake_api.responses[lookup] = self.reverse_lookup({})
        assert self.rebuild(dc) == first
        fake_api.forbidden.add(lookup)
        assert self.rebuild(dc) == first

        # a lease refresh that returns no hostnames, or no leases at all
        for row in fake_api.responses['/api/dnsmasq/leases/search']['rows']:
            row.pop('hostname', None)
        assert self.rebuild(dc) == first
        fake_api.responses['/api/dnsmasq/leases/search']['rows'] = []
        assert self.rebuild(dc)['00:00:5e:00:53:02'] == 'pi'

        # clients that are gone are forgotten
        assert set(dc._names) == {'00:00:5e:00:53:02', '00:00:5e:00:53:03'}
        assert set(dc._rdns) == {'10.8.1.1'}

    def test_rotating_ptr_records_keep_one_name(self, gateway_config, fake_api):
        # a host override with an alias gives the address two PTR records,
        # which Unbound hands out in turn
        fake_api.responses['/api/diagnostics/interface/search_arp']['rows'][1]['hostname'] = ''
        lookup = '/api/diagnostics/dns/reverse_lookup'
        fake_api.responses[lookup] = self.reverse_lookup({'10.8.1.1': 'nas.example.net'})
        with patch('collectors.opnsense.time.time', return_value=1000.0):
            dc = make(gateway_config, fake_api)
        for t, answer in ((1060, 'media.example.net'), (1120, 'nas.example.net'),
                          (1180, 'media.example.net'), (1900, 'media.example.net')):
            fake_api.responses[lookup] = self.reverse_lookup({'10.8.1.1': answer})
            with patch('collectors.opnsense.time.time', return_value=float(t)):
                assert self.rebuild(dc)['00:00:5e:00:53:03'] == 'nas'

        # a rename shows once the old name has stayed away long enough
        with patch('collectors.opnsense.time.time', return_value=1120.0 + 901):
            assert self.rebuild(dc)['00:00:5e:00:53:03'] == 'media'

    def test_reverse_dns_is_batched_and_optional(self, gateway_config, fake_api):
        dc = make(gateway_config, fake_api)  # no endpoint: lease and ARP names
        assert self.names(dc)['00:00:5e:00:53:02'] == 'pi'
        calls = [c for c in fake_api.calls if 'reverse_lookup' in c]
        assert len(calls) == 1
        assert sorted(calls[0].split('?')[1].split('&')) == [
            'address[]=10.8.1.1', 'address[]=192.168.48.2', 'address[]=192.168.48.9']

    def test_client_traffic_accumulates(self, gateway_config, fake_api):
        def top(path):
            if path.endswith('bridge0,in'):
                return [{'src_addr': '192.168.48.2', 'total': 100.0},
                        {'src_addr': '', 'total': 100.0}]
            return [{'src_addr': '192.168.48.2', 'total': 1000}]
        fake_api.responses['/api/diagnostics/networkinsight/top/'] = top

        with patch('collectors.opnsense.time.time', return_value=3000.0):
            dc = make(gateway_config, fake_api)
        host = [h for h in dc.data['host_table'] if h['ip'] == '192.168.48.2'][0]
        assert (host['tx_bytes'], host['rx_bytes']) == (100, 1000)
        flow_calls = [c for c in fake_api.calls if 'networkinsight' in c]
        assert '/FlowSourceAddrTotals/2700/3000/' in flow_calls[0]

        # same 5-minute bucket: nothing new is queried or added
        dc.updated['host_table'] = 0
        with patch('collectors.opnsense.time.time', return_value=3200.0):
            dc.update()
        assert len([c for c in fake_api.calls if 'networkinsight' in c]) == 2

        # next bucket adds on top
        dc.updated['host_table'] = 0
        with patch('collectors.opnsense.time.time', return_value=3301.0):
            dc.update()
        host = [h for h in dc.data['host_table'] if h['ip'] == '192.168.48.2'][0]
        assert (host['tx_bytes'], host['rx_bytes']) == (200, 2000)
        flow_calls = [c for c in fake_api.calls if 'networkinsight' in c]
        assert '/FlowSourceAddrTotals/3000/3300/' in flow_calls[-1]

