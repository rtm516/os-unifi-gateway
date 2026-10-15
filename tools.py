# -*- coding: utf-8 -*-


def mac_string_2_array(mac):
    return [int(i, 16) for i in mac.split(':')]


def ip_string_2_array(ip):
    return [int(i) for i in ip.split('.')]


def netmask_to_cidr(netmask):
    return sum(bin(int(x)).count('1') for x in netmask.split('.'))


def _port_names(ports):
    if_list = [d['ifname'] for d in ports]
    lan_if = [d['ifname'] for d in ports if d['type'].lower() == 'lan'][0]
    wan_if = [d['ifname'] for d in ports if d['type'].lower() == 'wan'][0]
    return if_list, lan_if, wan_if


def get_if_table(data, ports, uptime=0):
    if_list, _, wan_if = _port_names(ports)
    if_table = []
    if_data = data.get('ifstat', {})

    for iface in if_list:
        info = if_data.get(iface)
        if info is None or iface not in data.get('ip', {}):
            continue
        if_entry = {
            'drops': int(info['rx_dropped']) + int(info['tx_dropped']),
            'enable': True,
            'full_duplex': info.get('full_duplex', True),
            'ip': data['ip'][iface]['address'],
            'mac': data['macs'][iface],
            'name': iface,
            'netmask': data['ip'][iface]['netmask'],
            'num_port': 1,
            'rx_bytes': info['rx_bytes'],
            'rx_bps': info['rx_bps'],
            'rx_dropped': info['rx_dropped'],
            'rx_errors': info['rx_errors'],
            'rx_multicast': info['rx_multicast'],
            'rx_packets': info['rx_packets'],
            'speed': info.get('speed', 1000),
            'tx_bytes': info['tx_bytes'],
            'tx_bps': info['tx_bps'],
            'tx_dropped': info['tx_dropped'],
            'tx_errors': info['tx_errors'],
            'tx_packets': info['tx_packets'],
            'up': info.get('up', True),
            'uptime': uptime,
        }
        if iface == wan_if:
            if 'gateway' in data['ip'].get(iface, {}):
                if_entry['gateways'] = [data['ip'][iface]['gateway']]
            if_entry['nameservers'] = data.get('nameservers', [])
            if_entry['latency'] = data.get('latency', 0)
            speedtest = data.get('speedtest') or {}
            if_entry['speedtest_lastrun'] = speedtest.get('rundate', 0)
            if_entry['speedtest_ping'] = speedtest.get('latency', 0)
            if_entry['speedtest_status'] = 'Running' if speedtest.get('status_summary') == 1 else 'Idle'
            if_entry['xput_down'] = speedtest.get('xput_download', 0)
            if_entry['xput_up'] = speedtest.get('xput_upload', 0)
        if_table.append(if_entry)
    return if_table


def get_network_table(data, ports):
    if_list, lan_if, wan_if = _port_names(ports)
    network_table = []

    for iface in if_list:
        if iface not in data.get('ip', {}):
            continue
        info = data.get('ifstat', {}).get(iface, {})
        address = '%s/%s' % (data['ip'][iface]['address'], netmask_to_cidr(data['ip'][iface]['netmask']))
        up = 'true' if info.get('up', True) else 'false'
        net_entry = {
            'address': address,
            'addresses': [address],
            'autoneg': 'true',
            'duplex': 'full' if info.get('full_duplex', True) else 'half',
            'l1up': 'true' if info.get('l1up', info.get('up', True)) else 'false',
            'mac': data['macs'][iface],
            'mtu': str(info.get('mtu', 1500)),
            'name': iface,
            'speed': str(info.get('speed', 1000)),
            'stats': get_net_stats(data, iface),
            'up': up,
        }
        if iface == lan_if:
            net_entry['host_table'] = data.get('host_table', [])
        elif iface == wan_if:
            if 'gateway' in data['ip'].get(iface, {}):
                net_entry['gateways'] = [data['ip'][iface]['gateway']]
            net_entry['nameservers'] = data.get('nameservers', [])
        network_table.append(net_entry)
    return network_table


def get_net_stats(data, iface):
    if_stat = data.get('ifstat', {}).get(iface, {})
    return {
        'multicast': str(if_stat.get('rx_multicast', 0)),
        'rx_bps': if_stat.get('rx_bps', 0),
        'rx_bytes': str(if_stat.get('rx_bytes', 0)),
        'rx_dropped': str(if_stat.get('rx_dropped', 0)),
        'rx_errors': str(if_stat.get('rx_errors', 0)),
        'rx_multicast': str(if_stat.get('rx_multicast', 0)),
        'rx_packets': str(if_stat.get('rx_packets', 0)),
        'tx_bps': if_stat.get('tx_bps', 0),
        'tx_bytes': str(if_stat.get('tx_bytes', 0)),
        'tx_dropped': str(if_stat.get('tx_dropped', 0)),
        'tx_errors': str(if_stat.get('tx_errors', 0)),
        'tx_packets': str(if_stat.get('tx_packets', 0)),
    }
