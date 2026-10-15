# -*- coding: utf-8 -*-
import ipaddress
import json
import logging
import os
import socket
import time
import urllib.parse
import zlib
from binascii import a2b_hex
from struct import pack, unpack

try:
    import snappy
    HAS_SNAPPY = True
except ImportError:
    HAS_SNAPPY = False

import aes
from tlv import UnifiTLV
from tools import (
    mac_string_2_array, ip_string_2_array,
    get_if_table, get_network_table,
)

logger = logging.getLogger('unifi-gateway')

MASTER_KEY = "ba86f2bbe107c7c57eb5f2690775c712"
BLOCK_SIZE = 16

# usg2_caps bit 16 asks the controller for live updates: while someone has its
# UI open, its noop replies carry `live_update` and we inform this often
USG2_LIVE_UPDATE = 16
LIVE_INFORM_INTERVAL = 2


def encode_inform(config, data, encryption='CBC'):
    iv = os.urandom(16)
    key = MASTER_KEY

    if config.getboolean('gateway', 'is_adopted'):
        key = config.get('gateway', 'key')
        if config.getboolean('gateway', 'use_aes_gcm'):
            encryption = 'GCM'
        else:
            encryption = 'CBC'

    mac = config.get('gateway', 'lan_mac')

    # Flags: bit 0 = encrypted, bit 1 = zlib, bit 2 = snappy, bit 3 = GCM
    flags = 0x01
    if encryption == 'GCM':
        flags |= 0x08

    if HAS_SNAPPY:
        payload = snappy.compress(data.encode('utf-8'))
        flags |= 0x04
    else:
        payload = zlib.compress(data.encode('utf-8'))
        flags |= 0x02

    encoded_data = b'TNBU'
    encoded_data += pack('>I', 1)
    encoded_data += pack('BBBBBB', *mac_string_2_array(mac))
    encoded_data += pack('>H', flags)
    encoded_data += iv
    encoded_data += pack('>I', 1)

    if encryption == 'GCM':
        encoded_data += pack('>I', len(payload) + aes.TAG_SIZE)
        payload = aes.gcm_encrypt(a2b_hex(key), iv, encoded_data, payload)
    elif encryption == 'CBC':
        pad_len = BLOCK_SIZE - (len(payload) % BLOCK_SIZE)
        payload += bytes([pad_len]) * pad_len
        payload = aes.cbc_encrypt(a2b_hex(key), iv, payload)
        encoded_data += pack('>I', len(payload))

    encoded_data += payload
    return encoded_data


def decode_inform(config, encoded_data):
    magic = encoded_data[0:4]
    if magic != b'TNBU':
        raise Exception("Missing magic in response: '{}' instead of 'TNBU'".format(magic))

    header = encoded_data[:40]
    flags = unpack('>H', encoded_data[14:16])[0]
    iv = encoded_data[16:32]
    payload_len = unpack('>I', encoded_data[36:40])[0]
    payload = encoded_data[40:(40 + payload_len)]

    flag = {
        'encrypted': bool(flags & 0x01),
        'zlibCompressed': bool(flags & 0x02),
        'SnappyCompression': bool(flags & 0x04),
        'encryptedGCM': bool(flags & 0x08),
    }

    key = MASTER_KEY
    if config.getboolean('gateway', 'is_adopted'):
        key = config.get('gateway', 'key')

    if flag['encrypted']:
        if flag['encryptedGCM']:
            payload = aes.gcm_decrypt(a2b_hex(key), iv, header, payload)
        else:
            payload = aes.cbc_decrypt(a2b_hex(key), iv, payload)
            pad_size = payload[-1]
            if pad_size > BLOCK_SIZE:
                raise Exception('Response not padded or padding is corrupt')
            payload = payload[:(len(payload) - pad_size)]

    if flag['SnappyCompression']:
        if HAS_SNAPPY:
            payload = snappy.decompress(payload)
        else:
            raise Exception(
                'Response is snappy-compressed but python-snappy is not installed'
            )
    elif flag['zlibCompressed']:
        payload = zlib.decompress(payload)

    return json.loads(payload)


_inform_ip_cache = {}


def _inform_ip(url):
    """The controller's address; it rejects informs that only carry a name."""
    host = urllib.parse.urlparse(url or '').hostname or ''
    cached = _inform_ip_cache.get(host)
    if cached and time.time() - cached[1] < 300:
        return cached[0]
    try:
        address = socket.gethostbyname(host) if host else ''
    except OSError:
        address = host
    _inform_ip_cache[host] = (address, time.time())
    return address


def _resolve_lan_identity(config, dc, lan_if):
    """Get LAN MAC and IP, preferring live data but falling back to config."""
    mac = dc.data.get('macs', {}).get(lan_if)
    if not mac:
        mac = config.get('gateway', 'lan_mac')
    ip_info = dc.data.get('ip', {}).get(lan_if, {})
    ip_addr = ip_info.get('address')
    if not ip_addr:
        ip_addr = config.get('gateway', 'lan_ip')
    return mac, ip_addr, ip_info


def _create_partial_inform(config, dc, device):
    """What a factory-default device sends: everything it would report once
    adopted, flagged as unadopted and keyed with the default key."""
    payload = _complete_payload(config, dc, device)
    payload.update({
        'state': 1,
        'default': True,
        'adopted': False,
        'cfgversion': '?',
        'x_authkey': MASTER_KEY,
        '_default_key': True,
    })
    return json.dumps(payload)


def _port_idx(port):
    return int(port['ifname'].replace('eth', '')) + 1


def _uptime_stats(dc, uptime):
    """WAN health as a UniFi OS gateway reports it. The controller takes a
    UXG's WAN latency and availability only from here (the first of
    `alerting_monitors`), not from if_table, and shows the ISP as offline
    without them. `uptime` is the WAN's current online streak, left out
    while it is down; `downtime` is how long the current outage has lasted,
    0 while up. The controller records outages from `downtime`, says the
    WAN failed once it passes 6 s, and that it is back when `uptime`
    returns."""
    monitor = dc.data.get('wan_monitor')
    if not monitor:
        return None
    availability = 0.0 if monitor['down'] else round(max(0.0, 100.0 - monitor['loss']), 1)
    latency = int(round(monitor['latency']))
    check = {'availability': availability, 'latency_average': latency,
             'target': monitor['target'], 'type': 'icmp'}
    wan = {
        'availability': availability,
        'latency_average': latency,
        'downtime': monitor.get('downtime', 0),
        'monitors': [check],
        'alerting_monitors': [check],
    }
    if not monitor['down']:
        if monitor.get('up_since'):
            wan['uptime'] = max(0, int(time.time() - monitor['up_since']))
        else:
            wan['uptime'] = uptime
    return {'WAN': wan}


def _uxg_fields(dc, ports, uptime=0):
    """Fields a UXG reports on top of the USG-style tables."""
    port_table, assignments = [], []
    for port in ports:
        stats = dc.data.get('ifstat', {}).get(port['ifname'], {})
        idx = _port_idx(port)
        group = port['type'].upper()
        port_table.append({
            'port_idx': idx,
            'name': port['ifname'],
            'ifname': port['ifname'],
            'networkgroup': group,
            'network_name': group,
            'type': 'wire',
            'mac': dc.data.get('macs', {}).get(port['ifname'], ''),
            'up': stats.get('up', True),
            'enable': True,
            'speed': stats.get('speed', 1000),
            'full_duplex': stats.get('full_duplex', True),
            'rx_bytes': stats.get('rx_bytes', 0),
            'tx_bytes': stats.get('tx_bytes', 0),
            'rx_packets': stats.get('rx_packets', 0),
            'tx_packets': stats.get('tx_packets', 0),
            'rx_rate': stats.get('rx_bps', 0) * 8,
            'tx_rate': stats.get('tx_bps', 0) * 8,
        })
        if port['type'].lower() == 'lan':
            # UXG clients are what the LAN port learned: the controller
            # ignores network_table[].host_table for UniFi OS gateways and
            # reads the mac_table of `up` ports in the network's group.
            # Entries without `vlan` count as the untagged LAN (VLAN 1).
            port_table[-1]['mac_table'] = dc.data.get('host_table', [])
        assignments.append({
            'ifname': port['ifname'],
            'port_idx': idx,
            'networkgroup': group,
            'role': port['type'].lower(),
            'enable': True,
        })
    fields = {
        'type': 'uxg',
        'adopted': True,
        'num_port': len(ports),
        'port_table': port_table,
        'ethernet_overrides': assignments,
        'config_port_table': assignments,
        'udapi_version': {'version': 48},
        'udapi_caps': 0,
        'usg2_caps': USG2_LIVE_UPDATE,
        'inform_min_interval': LIVE_INFORM_INTERVAL,
        'gw_caps': {},
        'time_ms': int(time.time() * 1000),
    }
    uptime_stats = _uptime_stats(dc, uptime)
    if uptime_stats:
        fields['uptime_stats'] = uptime_stats
    if dc.data.get('isp'):
        # the controller shows the ISP from here, per WAN group
        fields['geo_info'] = {'WAN': dict(dc.data['isp'])}
    if dc.data.get('sys_stats'):
        fields['sys_stats'] = dict(dc.data['sys_stats'])
    return fields


def _complete_payload(config, dc, device):
    ports = dc.ports
    lan_if = [d['ifname'] for d in ports if d['type'].lower() == 'lan'][0]
    wan_if = [d['ifname'] for d in ports if d['type'].lower() == 'wan'][0]
    mac, ip_addr, ip_info = _resolve_lan_identity(config, dc, lan_if)
    netmask = ip_info.get('netmask', '255.255.255.0')

    hostname = config.get('gateway', 'hostname', fallback='') or \
        dc.data.get('hostname') or 'OPNsense'
    uptime = dc.uptime()

    system_stats = dc.data.get('system_stats', {'cpu': '0', 'mem': '0'})

    active_leases = []
    for lease in dc.data.get('dhcp_leases', []):
        entry = {'mac': lease['mac'], 'ip': lease['ip']}
        if 'hostname' in lease:
            entry['hostname'] = lease['hostname']
        if 'expire' in lease:
            # epoch seconds; the controller shows it as the lease expiry
            entry['leaseExpiry'] = lease['expire']
        active_leases.append(entry)

    payload = {
        'bootrom_version': 'unknown',
        'cfgversion': config.get('provisioned', 'cfgversion') if config.has_option('provisioned', 'cfgversion') else '',
        # Right after adoption the controller makes its networks match these
        # when they differ from a factory-default gateway. The WAN stays
        # "dhcp": anything else makes it rebuild its WAN network from this,
        # e.g. as PPPoE without the password, which the API can't read.
        'config_network_wan': {'type': 'dhcp'},
        'config_port_table': ports,
        'connect_request_ip': ip_addr,
        'connect_request_port': '36424',
        'default': False,
        'state': 2,
        'discovery_response': False,
        'fw_caps': 3,
        # 2: reports ipv4_active_leases (the IPV4_ACTIVE_LEASE_REPORTING site feature).
        # 1 would claim WireGuard tools
        'fw2_caps': 2,
        'guest_token': '4C1D46707239C6EB5A2366F505A44A91',
        # The controller folds has_* flags into usg_caps. UniFi OS firmware
        # sends these two but not the USG's has_dpi, has_porta and
        # radius_caps, which would claim DPI, portal and RADIUS support.
        'has_default_route_distance': True,
        'has_eth1': True,
        'has_ssh_disable': True,
        'hostname': hostname,
        'inform_url': config.get('gateway', 'url'),
        'inform_ip': _inform_ip(config.get('gateway', 'url')),
        'ip': ip_addr,
        'ipv4_active_leases': active_leases,
        'isolated': False,
        'locating': config.getboolean('gateway', 'locating') if config.has_option('gateway', 'locating') else False,
        'mac': mac,
        'model': device.model,
        'model_display': device.display,
        'netmask': netmask,
        'required_version': '4.0.0',
        'selfrun_beacon': True,
        'serial': mac.replace(':', '').upper(),
        'version': device.firmware(),
        'time': int(time.time()),
        'uplink': wan_if,
        'uptime': uptime,
        'pfor-stats': [],
        'ddns-status': {'dyndns': []},
        'system-stats': {
            'cpu': system_stats.get('cpu', '0'),
            'mem': system_stats.get('mem', '0'),
            'uptime': str(uptime),
        },
        'routes': _build_routes(dc, wan_if, lan_if),
        'network_table': get_network_table(dc.data, ports),
        'if_table': get_if_table(dc.data, ports, uptime),
    }
    if dc.data.get('config_network_lan'):
        payload['config_network_lan'] = dict(dc.data['config_network_lan'])
    payload.update(_uxg_fields(dc, ports, uptime))
    speedtest = dc.data.get('speedtest') or {}
    if speedtest.get('rundate'):
        # Only once a test has run: the controller files every finished
        # status as a result, so an idle one becomes a 0 Mbps test from 1970.
        payload['speedtest-status'] = dict(speedtest, interface_name=wan_if)
    for network in payload['network_table']:
        port = [p for p in ports if p['ifname'] == network['name']][0]
        network.update({
            'ifname': port['ifname'],
            'networkgroup': port['type'].upper(),
            'port_idx': _port_idx(port),
        })

    return payload


def _create_complete_inform(config, dc, device):
    return json.dumps(_complete_payload(config, dc, device))


def _build_routes(dc, wan_if, lan_if):
    routes = []
    wan_gw = dc.data.get('ip', {}).get(wan_if, {}).get('gateway')
    if wan_gw:
        routes.append({
            'nh': [{'intf': wan_if, 'metric': '1/0', 't': 'S>*', 'via': wan_gw}],
            'pfx': '0.0.0.0/0',
        })

    lan_ip = dc.data.get('ip', {}).get(lan_if, {})
    if lan_ip.get('address') and lan_ip.get('netmask'):
        routes.append({
            'nh': [{'intf': lan_if, 't': 'C>*'}],
            'pfx': str(ipaddress.ip_network(
                '%s/%s' % (lan_ip['address'], lan_ip['netmask']), strict=False)),
        })

    return routes


def create_inform(config, dc, device):
    if not config.getboolean('gateway', 'is_adopted'):
        return _create_partial_inform(config, dc, device)
    return _create_complete_inform(config, dc, device)


def create_broadcast_message(config, index, device, uptime=0, version=2, command=6):
    lan_mac = config.get('gateway', 'lan_mac')
    lan_ip = config.get('gateway', 'lan_ip')
    firmware = device.firmware()
    model = device.model

    platform_name = 'UNIFI-GW'

    tlv = UnifiTLV()
    tlv.add(1, bytearray(mac_string_2_array(lan_mac)))
    tlv.add(2, bytearray(mac_string_2_array(lan_mac) + ip_string_2_array(lan_ip)))
    tlv.add(3, '{}.v{}'.format(model, firmware).encode('ascii'))
    tlv.add(10, pack('!I', uptime))
    tlv.add(11, platform_name.encode('ascii'))
    tlv.add(12, model.encode('ascii'))
    tlv.add(19, bytearray(mac_string_2_array(lan_mac)))
    tlv.add(18, pack('!I', index))
    tlv.add(21, model.encode('ascii'))
    tlv.add(27, firmware.encode('ascii'))
    tlv.add(22, firmware.encode('ascii'))
    return tlv.get(version=version, command=command)
