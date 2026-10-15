# -*- coding: utf-8 -*-
"""Turns OPNsense router state into what the UniFi inform payload needs.

Where the state comes from is a Source (see source.py): the REST API when
running off-box, configd when running on the router as a plugin. Only the
interfaces are required; everything else degrades gracefully when the
source cannot provide it.
"""
import logging
import re
import time

from .source import LEASE_SERVERS, SourceError

logger = logging.getLogger('unifi-gateway')

ZERO_MAC = '00:00:00:00:00:00'

# How long to stop asking for something after access was refused
FORBIDDEN_BACKOFF = 600


def parse_uptime(text):
    """Parse OPNsense's uptime string: '3 days, 04:05:06' or '04:05:06'."""
    if not text:
        return 0
    days = 0
    match = re.match(r'\s*(\d+)\s+days?,\s*(.*)', text)
    if match:
        days = int(match.group(1))
        text = match.group(2)
    try:
        h, m, s = (int(x) for x in text.strip().split(':'))
    except ValueError:
        return 0
    return days * 86400 + h * 3600 + m * 60 + s


def parse_media_speed(media):
    """Link speed in Mbps from ifconfig media, e.g. '... (2500Base-T <full-duplex>)'."""
    match = re.search(r'(\d+)(G?)base', media or '', re.I)
    if not match:
        return None
    speed = int(match.group(1))
    return speed * 1000 if match.group(2) else speed


def parse_media_duplex(media):
    """True for full duplex, False for half, None when the media doesn't say."""
    media = (media or '').lower()
    if 'full-duplex' in media:
        return True
    if 'half-duplex' in media:
        return False
    return None


def _leading_number(text):
    """4.1 from '4.1 ms', 0.0 from '0.0 %'; None for '~' or nothing."""
    match = re.match(r'\s*(\d+(?:\.\d+)?)', str(text or ''))
    return float(match.group(1)) if match else None


def is_ppp(row):
    return bool(re.match(r'(pppoe|pptp|l2tp|ppp)\d+$', row.get('device') or ''))


def _children(row):
    """Devices a logical interface runs over: bridge members, lagg ports, a
    VLAN's parent."""
    devices = list(row.get('members') or {}) + list(row.get('laggport') or {})
    parent = (row.get('vlan') or {}).get('parent')
    if parent:
        devices.append(parent)
    return devices


def guess_ppp_ports(rows):
    """The NIC a PPP link probably runs over, when the source can't say (the
    REST API doesn't expose PPP devices): the one unassigned NIC or VLAN that
    no bridge, lagg or VLAN uses. Up NICs are preferred; anything ambiguous
    gives nothing."""
    used = set()
    for row in rows:
        used.update(_children(row))
    candidates = [row for row in rows
                  if row.get('device') and not row.get('identifier')
                  and row['device'] not in used
                  and (row.get('is_physical') or row.get('vlan'))]
    up = [row for row in candidates if row.get('status') == 'up']
    for group in (up, candidates):
        if group:
            return [row['device'] for row in group] if len(group) == 1 else []
    return []


def physical_ports(rows, row, ppps=None):
    """The NIC rows underneath an interface row: the row itself for a NIC,
    the members of a bridge or lagg, the parent of a VLAN, the ports a PPP
    link dials over (`ppps`, see Source.ppps), followed recursively."""
    by_device = {r['device']: r for r in rows if r.get('device')}
    ppp_ports = {p.get('if'): p.get('ports') or [] for p in ppps or []}
    found, seen = [], set()

    def walk(device):
        current = by_device.get(device)
        if current is None or device in seen:
            return
        seen.add(device)
        children = _children(current)
        if is_ppp(current):
            children += ppp_ports.get(device) or guess_ppp_ports(rows)
        if children:
            for child in children:
                walk(child)
        elif current.get('is_physical') or current.get('media'):
            found.append(current)

    walk(row.get('device'))
    return found


def link_state(row, ports):
    """(speed in Mbps, full duplex, link up) for an interface from the NICs
    underneath it: the fastest port that is up. Speed is None when no port
    reports one."""
    up = [p for p in ports if p.get('status') == 'up']
    best = None
    for group in (up, ports, [row]):
        rated = [(parse_media_speed(p.get('media')), p) for p in group]
        rated = [(speed, p) for speed, p in rated if speed]
        if rated:
            best = max(rated, key=lambda item: item[0])
            break
    speed, port = best if best else (None, row)
    duplex = parse_media_duplex(port.get('media'))
    link = bool(up) if ports else row.get('status') == 'up'
    return speed, True if duplex is None else duplex, link


def cidr_to_netmask(bits):
    bits = int(bits)
    mask = (0xffffffff << (32 - bits)) & 0xffffffff if bits else 0
    return '.'.join(str((mask >> s) & 0xff) for s in (24, 16, 8, 0))


def find_interface(rows, wanted):
    """The interface row named `wanted`: matched on its description first,
    then its OPNsense identifier (wan, lan, opt1...), then its device."""
    wanted = wanted.strip().lower()
    for field in ('description', 'identifier', 'device'):
        for row in rows:
            if str(row.get(field) or '').lower() == wanted and row.get('identifier'):
                return row
    return None


def _stat(stats, *names):
    for name in names:
        if name in stats:
            try:
                return int(float(str(stats[name]).split()[0]))
            except (ValueError, IndexError):
                continue
    return 0


class OPNsenseCollector:

    def __init__(self, config, source, wan_port='eth0', lan_port='eth1'):
        self.config = config
        self.source = source
        self.wan_port = wan_port
        self.lan_port = lan_port
        self.data = {}
        self.updated = {}
        self._forbidden = {}
        self._failed = set()
        self._prev_io = {}
        self._lease_server = None
        self._flow_until = None
        self._flow_totals = {}
        self._wan_dns = []
        self._upstream_dns = []
        self._links = {}
        self._started = time.time()

        self.wan_interface = config.get('opnsense', 'wan_interface', fallback='WAN')
        self.lan_interface = config.get('opnsense', 'lan_interface', fallback='LAN')

        self.update()
        self.ports = self.data['ports']

    # ── Public identity, used by the inform/broadcast code ───────────────

    @property
    def lan_mac(self):
        return self.data['macs'].get(self.lan_port, ZERO_MAC)

    @property
    def lan_ip(self):
        return self.data['ip'].get(self.lan_port, {}).get('address', '0.0.0.0')

    @property
    def wan_device(self):
        return self._devices['wan']

    @property
    def lan_device(self):
        return self._devices['lan']

    # ── Update loop ──────────────────────────────────────────────────────

    def update(self):
        self._update_interfaces()

        if self._due('system', 30):
            self._update_system()
        if self._due('dhcp_leases', 60):
            self.data['dhcp_leases'] = self._get_dhcp_leases()
        if self._due('latency', 60):
            self._update_wan_monitor()
        if self._due('nameservers', 300):
            self._upstream_dns = self._get_upstream_dns()
        self.data['nameservers'] = self._upstream_dns or self._wan_dns
        if self._due('host_table', 60):
            self._update_client_traffic()
            self.data['host_table'] = self._build_host_table()

        self.updated['general'] = time.time()

    def _due(self, key, interval):
        now = time.time()
        if now - self.updated.get(key, 0) < interval:
            return False
        self.updated[key] = now
        return True

    def _call(self, name, *args):
        """Ask the source for optional data; None when it is unavailable."""
        until = self._forbidden.get(name)
        if until and time.time() < until:
            return None
        try:
            result = getattr(self.source, name)(*args)
            self._failed.discard(name)
            return result
        except SourceError as e:
            if e.forbidden:
                if name not in self._forbidden:
                    logger.warning('Not allowed to read %s from OPNsense -- %s',
                                   name.replace('_', ' '), e.hint or str(e))
                self._forbidden[name] = time.time() + FORBIDDEN_BACKOFF
            elif name not in self._failed:
                self._failed.add(name)
                logger.warning('Could not read %s from OPNsense: %s', name.replace('_', ' '), e)
            return None

    # ── Interfaces ───────────────────────────────────────────────────────

    def _update_interfaces(self):
        """Interfaces are required: without them there is no identity to
        report, so failures here propagate to the caller."""
        rows = self.source.interfaces()
        wan = find_interface(rows, self.wan_interface)
        lan = find_interface(rows, self.lan_interface)

        missing = [name for name, row in ((self.wan_interface, wan),
                                          (self.lan_interface, lan)) if row is None]
        if missing:
            available = sorted(
                '%s (%s)' % (r.get('description'), r['identifier'])
                for r in rows if r.get('identifier'))
            raise SourceError(
                'interfaces', None,
                'no interface named %s; available: %s' % (
                    ', '.join(missing), ', '.join(available)),
                hint='set wan_interface / lan_interface to an interface description',
            )

        self._devices = {'wan': wan.get('device', ''), 'lan': lan.get('device', '')}
        self.data['ports'] = sorted([
            {'ifname': self.wan_port, 'name': wan.get('description', 'WAN'),
             'type': 'wan', 'realif': self.wan_device},
            {'ifname': self.lan_port, 'name': lan.get('description', 'LAN'),
             'type': 'lan', 'realif': self.lan_device},
        ], key=lambda port: port['ifname'])

        ppps = (self._call('ppps') or []) if is_ppp(wan) or is_ppp(lan) else []

        now = time.time()
        macs, ips, ifstat = {}, {}, {}
        for ifname, row in ((self.wan_port, wan), (self.lan_port, lan)):
            macs[ifname] = (row.get('macaddr') or ZERO_MAC).lower()

            address = self._ipv4(row)
            if address:
                if ifname == self.wan_port:
                    gateway = self._ipv4_gateway(row)
                    if gateway:
                        address['gateway'] = gateway
                ips[ifname] = address

            ifstat[ifname] = self._ifstat(ifname, row, now)
            ifstat[ifname].update(self._link(ifname, row, physical_ports(rows, row, ppps)))

            if ifname == self.wan_port:
                self._wan_dns = self._nameservers(row)

        self.data['macs'] = macs
        self.data['ip'] = ips
        self.data['ifstat'] = ifstat

    @staticmethod
    def _ipv4(row):
        candidates = [ip.get('ipaddr', '') for ip in row.get('ipv4') or []]
        candidates.append(row.get('addr4', ''))
        for cidr in candidates:
            if cidr and '/' in cidr:
                address, bits = cidr.split('/', 1)
                return {'address': address, 'netmask': cidr_to_netmask(bits)}
        return None

    @staticmethod
    def _ipv4_gateway(row):
        for gw in row.get('gateways') or []:
            if gw and ':' not in gw:
                return gw
        router = row.get('ifctl.router')
        if isinstance(router, list):
            router = router[0] if router else None
        return router or None

    @staticmethod
    def _nameservers(row):
        servers = row.get('ifctl.nameserver') or []
        if isinstance(servers, str):
            servers = servers.split()
        return [s for s in servers if ':' not in s]

    def _ifstat(self, ifname, row, now):
        stats = row.get('statistics') or {}
        info = {
            'rx_bytes': _stat(stats, 'bytes received'),
            'rx_packets': _stat(stats, 'packets received'),
            'rx_errors': _stat(stats, 'input errors'),
            'rx_dropped': _stat(stats, 'input queue drops'),
            'rx_multicast': _stat(stats, 'multicasts received'),
            'tx_bytes': _stat(stats, 'bytes transmitted'),
            'tx_packets': _stat(stats, 'packets transmitted'),
            'tx_errors': _stat(stats, 'output errors'),
            'tx_dropped': _stat(stats, 'send queue drops', 'output queue drops'),
            'tx_multicast': _stat(stats, 'multicasts transmitted'),
            'up': row.get('status') == 'up',
            'mtu': _stat(row, 'mtu') or 1500,
            'rx_bps': 0,
            'tx_bps': 0,
        }

        prev = self._prev_io.get(ifname)
        if prev:
            elapsed = now - prev['time']
            rx = info['rx_bytes'] - prev['rx_bytes']
            tx = info['tx_bytes'] - prev['tx_bytes']
            # A counter reset (reboot, interface reconfigure) gives negative deltas
            if elapsed > 0 and rx >= 0 and tx >= 0:
                info['rx_bps'] = int(rx / elapsed)
                info['tx_bps'] = int(tx / elapsed)
        self._prev_io[ifname] = {
            'time': now, 'rx_bytes': info['rx_bytes'], 'tx_bytes': info['tx_bytes'],
        }
        return info

    def _link(self, ifname, row, ports):
        """Speed, duplex and link state from the physical NICs, since the
        WAN and LAN are often logical (PPPoE, bridge, VLAN) and report no
        media of their own."""
        speed, full_duplex, l1up = link_state(row, ports)
        devices = [p['device'] for p in ports]
        if self._links.get(ifname) != devices:
            self._links[ifname] = devices
            logger.info('%s (%s) runs over %s', ifname, row.get('device'),
                        ', '.join(devices) or 'no known NIC; reporting 1000 Mbps')
        return {
            'speed': speed or 1000,
            'full_duplex': full_duplex,
            'l1up': l1up,
            'up': row.get('status') == 'up' and l1up,
        }

    # ── DNS ──────────────────────────────────────────────────────────────

    def _get_upstream_dns(self):
        """The servers Unbound resolves through, when it forwards.

        DoT / query forwarding entries for all domains come first. With
        "use system nameservers" forwarding on, the servers Unbound has
        actually queried are read from its infrastructure cache. A
        recursive Unbound has no upstream (empty result), so the servers
        the WAN link provided are reported instead.
        """
        settings = self._call('unbound_settings')
        if not settings:
            return []
        unbound = settings.get('unbound', {})

        servers = []
        dots = (unbound.get('dots') or {}).get('dot') or {}
        for entry in (dots.values() if isinstance(dots, dict) else dots):
            if str(entry.get('enabled')) != '1':
                continue
            if (entry.get('domain') or '.') != '.':
                continue  # forwarding for a single zone only
            server = entry.get('server', '')
            if server and ':' not in server and server not in servers:
                servers.append(server)
        if servers:
            return servers

        if str((unbound.get('forwarding') or {}).get('enabled')) == '1':
            for record in self._call('unbound_infra') or []:
                ip = record.get('ip', '')
                if ip and ':' not in ip and ip not in servers:
                    servers.append(ip)
        return servers

    # ── System ───────────────────────────────────────────────────────────

    def _update_system(self):
        info = self._call('system_information')
        if info:
            self.data['hostname'] = info.get('name', '').split('.')[0]
            versions = info.get('versions') or []
            if versions:
                self.data['os_version'] = versions[0]

        sys_time = self._call('system_time')
        if sys_time:
            self.data['uptime'] = parse_uptime(sys_time.get('uptime'))
            self.data['uptime_at'] = time.time()

        stats = dict(self.data.get('system_stats', {'cpu': '0', 'mem': '0'}))
        memory = (self._call('system_resources') or {}).get('memory', {})
        try:
            stats['mem'] = str(int(100 * float(memory['used']) / float(memory['total'])))
        except (KeyError, TypeError, ValueError, ZeroDivisionError):
            pass

        sample = self._call('cpu_usage') or {}
        if 'total' in sample:
            stats['cpu'] = str(int(float(sample['total'])))
        elif 'idle' in sample:
            stats['cpu'] = str(int(100 - float(sample['idle'])))
        self.data['system_stats'] = stats

    def uptime(self):
        """Router uptime, advanced locally between refreshes. Without it (no
        "Lobby: Dashboard" privilege), the time since we started reporting:
        the controller's device list hides connected devices with uptime 0."""
        if 'uptime' not in self.data:
            return max(1, int(time.time() - self._started))
        return int(self.data['uptime'] + time.time() - self.data['uptime_at'])

    # ── Latency ──────────────────────────────────────────────────────────

    def _update_wan_monitor(self):
        """Latency and loss gateway monitoring (dpinger) measures for the WAN
        gateway: `latency` in ms, and `wan_monitor` with the details."""
        items = self._call('gateway_status') or []
        wan_gw = self.data['ip'].get(self.wan_port, {}).get('gateway')
        wan_dev = self.wan_device

        def matches(item):
            return item.get('address') == wan_gw or item.get('interface') == wan_dev

        self.data.setdefault('latency', 0)
        for item in sorted(items, key=lambda i: not matches(i)):
            delay = _leading_number(item.get('delay'))
            if delay is None:
                continue
            target = item.get('monitor', '')
            self.data['latency'] = delay
            self.data['wan_monitor'] = {
                'latency': delay,
                'loss': _leading_number(item.get('loss')) or 0.0,
                'target': target if re.match(r'^[\d.:a-fA-F]+$', target) else item.get('address', ''),
                'down': item.get('status') == 'down',
            }
            return

    # ── DHCP leases ──────────────────────────────────────────────────────

    def _get_dhcp_leases(self):
        configured = self.config.get('opnsense', 'dhcp', fallback='auto').strip().lower()
        if configured == 'none':
            return []
        if configured in LEASE_SERVERS:
            order = [configured]
        else:
            order = [self._lease_server] if self._lease_server else list(LEASE_SERVERS)

        hints = []
        for server in order:
            try:
                rows = self.source.dhcp_leases(server)
            except SourceError as e:
                logger.debug('DHCP leases from %s unavailable: %s', server, e)
                if e.hint:
                    hints.append(e.hint)
                continue
            if self._lease_server != server:
                logger.info('Reading DHCP leases from %s', server)
            self._lease_server = server
            return self._parse_leases(rows)

        if 'dhcp_hint' not in self.updated:
            self.updated['dhcp_hint'] = time.time()
            logger.warning('No DHCP leases available%s',
                           ': ' + '; or '.join(hints) if hints else '')
        self._lease_server = None
        return self.data.get('dhcp_leases', [])

    @staticmethod
    def _parse_leases(rows):
        leases = []
        for row in rows:
            mac = row.get('hwaddr') or row.get('mac') or ''
            ip = row.get('address') or row.get('ip') or ''
            if not mac or not ip or ':' in ip:
                continue
            if str(row.get('state', 'active')).lower() not in ('active', '0', ''):
                continue
            lease = {'mac': mac.lower(), 'ip': ip}
            hostname = row.get('hostname') or row.get('client_hostname')
            if hostname and hostname != '*':
                lease['hostname'] = hostname
            leases.append(lease)
        return leases

    # ── Clients ──────────────────────────────────────────────────────────

    def _update_client_traffic(self):
        """Accumulate per-client byte counters from Network Insight.

        The FlowSourceAddrTotals aggregate counts each flow twice: once under
        its source on the ingress interface (direction 'in') and once under
        its destination on the egress interface (direction 'out'). On the LAN
        side those are a client's upload and download. Only complete 5-minute
        buckets are queried, so nothing is counted twice.
        """
        resolution = 300
        until = int(time.time()) // resolution * resolution
        since = self._flow_until or until - resolution
        if until <= since:
            return

        totals = {}
        for direction, counter in (('in', 'tx_bytes'), ('out', 'rx_bytes')):
            rows = self._call('flow_totals', since, until, self.lan_device, direction)
            if rows is None:
                return
            for row in rows:
                addr = row.get('src_addr')
                if addr:
                    totals.setdefault(addr, {})[counter] = int(float(row.get('total') or 0))

        for addr, counts in totals.items():
            entry = self._flow_totals.setdefault(addr, {'rx_bytes': 0, 'tx_bytes': 0})
            entry['rx_bytes'] += counts.get('rx_bytes', 0)
            entry['tx_bytes'] += counts.get('tx_bytes', 0)
        self._flow_until = until

    def _build_host_table(self):
        """LAN clients from the ARP table, merged with DHCP leases."""
        rows = self._call('arp')
        if rows is None:
            # keep what we had rather than dropping every client from the UI
            return self.data.get('host_table', [])

        uptime = self.uptime()
        hosts = {}

        def add(mac, ip, hostname, client_uptime):
            entry = {
                'mac': mac, 'ip': ip, 'authorized': True, 'age': 0,
                'uptime': client_uptime, 'rx_bytes': 0, 'tx_bytes': 0,
                'rx_packets': 0, 'tx_packets': 0, 'bc_bytes': 0, 'mc_bytes': 0,
            }
            entry.update(self._flow_totals.get(ip, {}))
            if hostname:
                entry['hostname'] = hostname
            hosts[mac] = entry

        for row in rows:
            if row.get('intf') != self.lan_device or row.get('expired') or not row.get('mac'):
                continue
            add(row['mac'].lower(), row.get('ip', ''), row.get('hostname'), uptime)

        for lease in self.data.get('dhcp_leases', []):
            if lease['mac'] not in hosts:
                add(lease['mac'], lease['ip'], lease.get('hostname'), 0)
            elif 'hostname' in lease and 'hostname' not in hosts[lease['mac']]:
                hosts[lease['mac']]['hostname'] = lease['hostname']

        return list(hosts.values())
