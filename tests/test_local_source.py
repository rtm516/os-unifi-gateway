# -*- coding: utf-8 -*-
"""The on-box source: configd output reshaped like the REST API."""
import json
from unittest.mock import patch

import pytest

from collectors.local_source import LocalSource, format_uptime
from collectors.opnsense import OPNsenseCollector
from collectors.source import SourceError

CONFIG_XML = """<?xml version="1.0"?>
<opnsense>
  <system><hostname>router</hostname><domain>example.net</domain></system>
  <interfaces>
    <wan><if>pppoe0</if><descr>WAN</descr><enable>1</enable></wan>
    <lan><if>bridge0</if><descr>LAN</descr><enable>1</enable></lan>
    <opt1><if>em0</if><descr>IoT</descr></opt1>
  </interfaces>
  <ppps>
    <ppp><ptpid>0</ptpid><type>pppoe</type><if>pppoe0</if><ports>igb0</ports>
      <username>user</username><password>secret</password></ppp>
  </ppps>
  <OPNsense>
    <unboundplus>
      <forwarding><enabled>0</enabled></forwarding>
      <dots>
        <dot uuid="a1"><enabled>1</enabled><domain/><server>9.9.9.9</server><port>853</port></dot>
        <dot uuid="b2"><enabled>1</enabled><domain>corp.example</domain><server>10.0.0.53</server></dot>
      </dots>
    </unboundplus>
  </OPNsense>
</opnsense>
"""

IFCONFIG = {
    'pppoe0': {'flags': ['up', 'pointopoint', 'running'], 'mtu': '1492',
               'ipv4': [{'ipaddr': '81.2.3.4', 'subnetbits': 32}]},
    'bridge0': {'flags': ['up', 'broadcast', 'running'], 'mtu': '1500',
                'macaddr': '00:00:5e:00:53:01', 'status': 'active',
                'members': {'igb1': {'flags': ['learning']}, 'em1': {'flags': ['learning']}},
                'ipv4': [{'ipaddr': '192.168.48.1', 'subnetbits': 22}]},
    'em0': {'flags': ['broadcast'], 'status': 'no carrier', 'macaddr': 'aa:bb:cc:dd:ee:ff',
            'is_physical': True, 'media': 'Ethernet autoselect'},
    # the PPPoE port, plus a second unassigned NIC so it can't be guessed
    'igb0': {'flags': ['up', 'running'], 'status': 'active', 'is_physical': True,
             'media': '2500Base-T <full-duplex>'},
    'igb2': {'flags': ['up', 'running'], 'status': 'active', 'is_physical': True,
             'media': '1000baseT <full-duplex>'},
    'igb1': {'flags': ['up', 'running'], 'status': 'active', 'is_physical': True,
             'media': '10Gbase-SR <full-duplex>'},
    'em1': {'flags': ['up', 'running'], 'status': 'active', 'is_physical': True,
            'media': '100baseTX <half-duplex>'},
    'lo0': {'flags': ['up', 'loopback']},
}

STATS = {
    'pppoe0': {'bytes received': '1000', 'bytes transmitted': '2000'},
    'bridge0': {'bytes received': '3000', 'bytes transmitted': '4000'},
}

ROUTES = [
    {'proto': 'ipv4', 'destination': 'default', 'gateway': '81.2.3.1', 'netif': 'pppoe0'},
    {'proto': 'ipv6', 'destination': 'default', 'gateway': 'fe80::1%pppoe0', 'netif': 'pppoe0'},
    {'proto': 'ipv4', 'destination': '192.168.48.0/22', 'gateway': 'link#5', 'netif': 'bridge0'},
]

ACTIONS = {
    'interface list ifconfig': IFCONFIG,
    'interface list stats': STATS,
    'interface routes list -n json': ROUTES,
    'interface list arp json': [
        {'mac': '00:00:5e:00:53:02', 'ip': '192.168.48.2', 'intf': 'bridge0',
         'expired': False, 'hostname': ''},
    ],
    'interface gateways status': {
        'WAN_PPPOE': {'name': 'WAN_PPPOE', 'address': '81.2.3.1', 'delay': '7.5 ms'},
    },
    'kea list leases4': {'records': [
        {'address': '192.168.48.2', 'hwaddr': '00:00:5e:00:53:02', 'hostname': 'pi'},
    ]},
    'system sysctl values kern.boottime': {
        'kern.boottime': '{ sec = 1000000, usec = 0 } Mon Jan 12 13:46:40 1970'},
    'system sysctl values hw.physmem,vm.stats.vm.v_page_count,vm.stats.vm.v_inactive_count,'
    'vm.stats.vm.v_cache_count,vm.stats.vm.v_laundry_count,vm.stats.vm.v_free_count': {
        'hw.physmem': '8000', 'vm.stats.vm.v_page_count': '100',
        'vm.stats.vm.v_inactive_count': '10', 'vm.stats.vm.v_cache_count': '0',
        'vm.stats.vm.v_laundry_count': '5', 'vm.stats.vm.v_free_count': '60'},
    'unbound dumpinfra': [{'ip': '1.1.1.1', 'host': '.'}],
}


class FakeRunner:

    def __init__(self, actions=None):
        self.actions = dict(ACTIONS if actions is None else actions)
        self.cp_times = ['100 0 50 0 850', '110 0 60 0 930']
        self.calls = []

    def __call__(self, args):
        self.calls.append(args)
        if args[0].endswith('configctl'):
            key = ' '.join(args[1:])
            if key.startswith('netflow aggregate top'):
                return json.dumps([{'src_addr': '192.168.48.2', 'total': 512}])
            if key not in self.actions:
                return 'Action not found\n'
            return json.dumps(self.actions[key])
        if args[0].endswith('opnsense-version'):
            return 'OPNsense 25.7.5\n'
        if args[0].endswith('sysctl'):
            return self.cp_times.pop(0) + '\n'
        raise AssertionError(args)


@pytest.fixture
def local(tmp_path):
    config = tmp_path / 'config.xml'
    config.write_text(CONFIG_XML)
    (tmp_path / 'pppoe0_nameserver').write_text('81.2.3.53\n81.2.3.54\n')
    (tmp_path / 'pppoe0_router').write_text('81.2.3.1\n')
    runner = FakeRunner()
    source = LocalSource(config_xml=str(config), ifctl_dir=str(tmp_path), run=runner)
    source.runner = runner
    return source


def test_format_uptime():
    assert format_uptime(59) == '00:00:59'
    assert format_uptime(86400 + 3661) == '1 day, 01:01:01'
    assert format_uptime(3 * 86400) == '3 days, 00:00:00'


def test_interfaces_match_api_shape(local):
    all_rows = local.interfaces()
    rows = {r['identifier']: r for r in all_rows if r['identifier']}
    assert set(rows) == {'wan', 'lan', 'opt1'}

    wan = rows['wan']
    assert (wan['description'], wan['device'], wan['status']) == ('WAN', 'pppoe0', 'up')
    assert wan['ipv4'] == [{'ipaddr': '81.2.3.4/32'}]
    assert wan['gateways'] == ['81.2.3.1']
    assert wan['ifctl.nameserver'] == ['81.2.3.53', '81.2.3.54']
    assert wan['statistics'] == STATS['pppoe0']

    lan = rows['lan']
    assert lan['macaddr'] == '00:00:5e:00:53:01'
    assert lan['gateways'] == [] and 'ifctl.nameserver' not in lan
    assert rows['opt1']['status'] == 'no carrier'
    assert lan['members'] == IFCONFIG['bridge0']['members']


def test_unassigned_devices_are_listed(local):
    unassigned = {r['device']: r for r in local.interfaces() if not r['identifier']}
    assert set(unassigned) == {'igb0', 'igb1', 'igb2', 'em1', 'lo0'}
    igb0 = unassigned['igb0']
    assert (igb0['description'], igb0['status']) == ('Unassigned Interface', 'up')
    assert igb0['media'] == '2500Base-T <full-duplex>' and igb0['is_physical'] is True


def test_ppps_from_config(local):
    assert local.ppps() == [{'if': 'pppoe0', 'type': 'pppoe', 'ports': ['igb0']}]


def test_no_ppps(local, tmp_path):
    (tmp_path / 'config.xml').write_text(CONFIG_XML.replace('<ppps>', '<x>').replace('</ppps>', '</x>'))
    local._xml = (None, None)
    assert local.ppps() == []


def test_link_speed_from_physical_ports(local, gateway_config):
    with patch('collectors.local_source.time.sleep'):
        dc = OPNsenseCollector(gateway_config, local, wan_port='eth1', lan_port='eth0')
    wan, lan = dc.data['ifstat']['eth1'], dc.data['ifstat']['eth0']
    # PPPoE port from config.xml, not a guess (igb2 makes guessing ambiguous)
    assert (wan['speed'], wan['full_duplex'], wan['up']) == (2500, True, True)
    # fastest member of the bridge
    assert (lan['speed'], lan['full_duplex'], lan['up']) == (10000, True, True)


def test_configd_error_text_raises(local):
    local.runner.actions.pop('interface list ifconfig')
    with pytest.raises(SourceError, match='Action not found'):
        local.interfaces()


def test_system(local):
    assert local.system_information() == {'name': 'router.example.net',
                                          'versions': ['OPNsense 25.7.5']}
    with patch('collectors.local_source.time.time', return_value=1000000 + 90061):
        assert local.system_time() == {'uptime': '1 day, 01:01:01'}
    assert local.system_resources() == {'memory': {'total': 8000.0, 'used': 2000}}


def test_cpu_from_cp_time_deltas(local):
    with patch('collectors.local_source.time.sleep'):
        # 100 ticks elapsed, 80 of them idle
        assert local.cpu_usage() == {'total': 20.0}


def test_unbound_settings_from_config(local):
    settings = local.unbound_settings()['unbound']
    assert settings['forwarding'] == {'enabled': '0'}
    assert settings['dots']['dot']['a1']['server'] == '9.9.9.9'
    assert settings['dots']['dot']['b2']['domain'] == 'corp.example'


def test_unsupported_lease_server(local):
    with pytest.raises(SourceError):
        local.dhcp_leases('isc')
    with pytest.raises(SourceError):
        local.dhcp_leases('dnsmasq')  # not in the fake: configd says "Action not found"
    assert local.dhcp_leases('kea')[0]['hostname'] == 'pi'


def test_flow_totals_passes_filter(local):
    assert local.flow_totals(2700, 3000, 'bridge0', 'in') == [{'src_addr': '192.168.48.2', 'total': 512}]
    assert local.runner.calls[-1][1:] == [
        'netflow', 'aggregate', 'top', 'FlowSourceAddrTotals', '2700', '3000',
        'src_addr', 'octets', 'if=bridge0,direction=in', '1000']


def test_reverse_dns_skips_unresolved(local):
    def gethostbyaddr(address):
        if address == '192.168.48.2':
            return ('pi.example.net', [], [address])
        raise OSError('host not found')
    with patch('collectors.local_source.socket.gethostbyaddr', gethostbyaddr):
        assert local.reverse_dns(['192.168.48.2', '192.168.48.9']) == {'192.168.48.2': 'pi.example.net'}


def test_collector_runs_on_local_source(local, gateway_config):
    with patch('collectors.local_source.time.sleep'), \
            patch('collectors.local_source.socket.gethostbyaddr', side_effect=OSError):
        dc = OPNsenseCollector(gateway_config, local, wan_port='eth1', lan_port='eth0')
    assert dc.lan_ip == '192.168.48.1' and dc.lan_mac == '00:00:5e:00:53:01'
    assert dc.data['ip']['eth1']['gateway'] == '81.2.3.1'
    assert dc.data['nameservers'] == ['9.9.9.9']
    assert dc.data['latency'] == 7.5
    assert dc.data['hostname'] == 'router'
    assert dc.data['system_stats'] == {'cpu': '20', 'mem': '25'}
    hosts = {h['mac']: h for h in dc.data['host_table']}
    assert hosts['00:00:5e:00:53:02']['hostname'] == 'pi'
