# -*- coding: utf-8 -*-
"""Router state read directly on the OPNsense box (the plugin's source).

Uses the same configd actions the REST API controllers wrap, plus
/conf/config.xml for interface assignments and Unbound settings, so no API
key is needed. Output is reshaped to match the API responses (see source.py).
"""
import json
import os
import re
import socket
import subprocess
import threading
import time
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor, wait

from .source import PingJob, Source, SourceError

CONFIGCTL = '/usr/local/sbin/configctl'
PING = '/sbin/ping'
CONFIG_XML = '/conf/config.xml'
SYSCTL = '/sbin/sysctl'
VERSION_CMD = '/usr/local/sbin/opnsense-version'
IFCTL_DIR = '/tmp'

# FreeBSD ping's reply and summary lines (hlim for IPv6)
PING_REPLY = re.compile(r'icmp_seq=(\d+) (?:ttl|hlim)=(\d+) time=([\d.]+) ms')
PING_SUMMARY = re.compile(r'(\d+) packets transmitted')
# A packet still unanswered after this long counts as lost until it's answered
PING_WAIT = 1.0

LEASE_ACTIONS = {
    'dnsmasq': ('dnsmasq', 'list', 'leases'),
    'kea': ('kea', 'list', 'leases4'),
}


def format_uptime(seconds):
    days, rest = divmod(int(seconds), 86400)
    hours, rest = divmod(rest, 3600)
    minutes, secs = divmod(rest, 60)
    clock = '%02d:%02d:%02d' % (hours, minutes, secs)
    if days:
        return '%d %s, %s' % (days, 'days' if days > 1 else 'day', clock)
    return clock


class LocalSource(Source):

    def __init__(self, configctl=CONFIGCTL, config_xml=CONFIG_XML, ifctl_dir=IFCTL_DIR,
                 run=None):
        self.configctl = configctl
        self.config_xml = config_xml
        self.ifctl_dir = ifctl_dir
        self._run = run or self._subprocess
        self._xml = (None, None)
        self._cp_time = None

    # ── plumbing ─────────────────────────────────────────────────────────

    @staticmethod
    def _subprocess(args):
        try:
            result = subprocess.run(args, capture_output=True, timeout=60)
        except (OSError, subprocess.SubprocessError) as e:
            raise SourceError(' '.join(args), None, str(e)) from None
        if result.returncode != 0:
            raise SourceError(' '.join(args), None,
                              result.stderr.decode('utf-8', 'replace').strip())
        return result.stdout.decode('utf-8', 'replace')

    def _action(self, *args):
        """Run a configd action and parse its JSON output."""
        output = self._run([self.configctl] + list(args)).strip()
        try:
            return json.loads(output)
        except ValueError:
            # configd answers "Execute error" / "Action not found" in plain text
            raise SourceError('configctl ' + ' '.join(args), None,
                              output[:200] or 'no output') from None

    def _config(self):
        """config.xml, re-parsed only when it changes."""
        try:
            mtime = os.path.getmtime(self.config_xml)
        except OSError as e:
            raise SourceError(self.config_xml, None, str(e)) from None
        if self._xml[0] != mtime:
            try:
                self._xml = (mtime, ET.parse(self.config_xml).getroot())
            except (OSError, ET.ParseError) as e:
                raise SourceError(self.config_xml, None, str(e)) from None
        return self._xml[1]

    def _ifctl(self, device, kind):
        """Values OPNsense's ifctl stored for a dynamic link (DHCP, PPPoE)."""
        for suffix in ('', ':slaac'):
            path = os.path.join(self.ifctl_dir, '%s%s_%s' % (device, suffix, kind))
            try:
                with open(path) as f:
                    return f.read().split()
            except OSError:
                continue
        return []

    def _sysctl(self, *names):
        return self._action('system', 'sysctl', 'values', ','.join(names))

    # ── Source ───────────────────────────────────────────────────────────

    def interfaces(self):
        ifconfig = self._action('interface', 'list', 'ifconfig')
        try:
            stats = self._action('interface', 'list', 'stats')
        except SourceError:
            stats = {}
        try:
            routes = self._action('interface', 'routes', 'list', '-n', 'json')
        except SourceError:
            routes = []

        default_gw = {}
        for route in routes if isinstance(routes, list) else []:
            if route.get('destination') == 'default' and route.get('proto') != 'ipv6':
                gateway = route.get('gateway', '')
                if re.match(r'^\d+\.\d+\.\d+\.\d+$', gateway):
                    default_gw[route.get('netif')] = gateway

        def status(details):
            flags = details.get('flags') or []
            if details.get('status') and details['status'] not in ('active', 'running'):
                return details['status']
            return 'up' if 'up' in flags else 'down'

        rows = []
        assigned = self._config().find('interfaces')
        for node in list(assigned) if assigned is not None else []:
            device = node.findtext('if', '')
            details = ifconfig.get(device)
            if not device or details is None:
                continue
            row = dict(details)
            row.update({
                'identifier': node.tag,
                'description': node.findtext('descr') or node.tag.upper(),
                'device': device,
                'enabled': node.find('enable') is not None,
                'status': status(details),
                'ipv4': [{'ipaddr': '%s/%s' % (ip['ipaddr'], ip['subnetbits'])}
                         for ip in details.get('ipv4') or [] if ip.get('ipaddr')],
                'gateways': [default_gw[device]] if device in default_gw else [],
                'statistics': stats.get(device, {}),
            })
            nameservers = self._ifctl(device, 'nameserver')
            if nameservers:
                row['ifctl.nameserver'] = nameservers
            router = self._ifctl(device, 'router')
            if router:
                row['ifctl.router'] = router
            rows.append(row)

        # the API lists unassigned devices too, e.g. a bridge member or the
        # NIC a PPPoE link dials over, which is where their link speed is
        assigned_devices = {row['device'] for row in rows}
        for device, details in sorted(ifconfig.items()):
            if device not in assigned_devices:
                row = dict(details)
                row.update({'identifier': '', 'description': 'Unassigned Interface',
                            'device': device, 'status': status(details)})
                rows.append(row)
        return rows

    def ppps(self):
        rows = []
        for node in self._config().findall('ppps/ppp'):
            device = node.findtext('if', '')
            if device:
                rows.append({
                    'if': device,
                    'type': node.findtext('type', ''),
                    'ports': [p for p in node.findtext('ports', '').split(',') if p],
                })
        return rows

    def system_information(self):
        system = self._config().find('system')
        hostname = system.findtext('hostname', 'OPNsense') if system is not None else 'OPNsense'
        domain = system.findtext('domain', '') if system is not None else ''
        try:
            version = self._run([VERSION_CMD]).strip()
        except SourceError:
            version = ''
        return {
            'name': '%s.%s' % (hostname, domain) if domain else hostname,
            'versions': [version] if version else [],
        }

    def system_time(self):
        values = self._sysctl('kern.boottime', 'vm.loadavg')
        boottime = values.get('kern.boottime', '')
        match = re.search(r'sec = (\d+)', boottime)
        if not match:
            raise SourceError('kern.boottime', None, 'unexpected value %r' % boottime)
        result = {'uptime': format_uptime(time.time() - int(match.group(1)))}
        # "{ 0.10 0.20 0.30 }", as the API reformats it
        loadavg = str(values.get('vm.loadavg', '')).strip('{} ').split()
        if len(loadavg) == 3:
            result['loadavg'] = ', '.join(loadavg)
        return result

    def system_resources(self):
        mem = self._sysctl(
            'hw.physmem', 'vm.stats.vm.v_page_count', 'vm.stats.vm.v_inactive_count',
            'vm.stats.vm.v_cache_count', 'vm.stats.vm.v_laundry_count',
            'vm.stats.vm.v_free_count')
        try:
            pages = float(mem['vm.stats.vm.v_page_count'])
            spare = sum(float(mem.get(k) or 0) for k in (
                'vm.stats.vm.v_inactive_count', 'vm.stats.vm.v_cache_count',
                'vm.stats.vm.v_laundry_count', 'vm.stats.vm.v_free_count'))
            total = float(mem['hw.physmem'])
        except (KeyError, ValueError) as e:
            raise SourceError('memory sysctls', None, str(e)) from None
        return {'memory': {'total': total, 'used': round((pages - spare) / pages * total)}}

    def cpu_usage(self):
        """Busy percentage since the previous call (kern.cp_time ticks)."""
        def sample():
            ticks = self._run([SYSCTL, '-n', 'kern.cp_time']).split()
            return [int(t) for t in ticks[:5]]

        previous = self._cp_time
        if previous is None:
            previous = sample()
            time.sleep(0.5)
        current = self._cp_time = sample()
        deltas = [c - p for c, p in zip(current, previous)]
        total = sum(deltas)
        if total <= 0:
            return {'total': 0.0}
        return {'total': 100.0 * (total - deltas[4]) / total}

    def arp(self):
        return self._action('interface', 'list', 'arp', 'json')

    def dhcp_leases(self, server):
        if server not in LEASE_ACTIONS:
            raise SourceError('dhcp leases', None, '%s is not supported on-box' % server)
        return self._action(*LEASE_ACTIONS[server]).get('records', [])

    def dhcp_reservations(self, server):
        if server != 'kea':
            return []
        return [{'hw_address': node.findtext('hw_address', ''), 'hostname': node.findtext('hostname', '')}
                for node in self._config().findall('OPNsense/Kea/dhcp4/reservations/reservation')]

    def dhcp_subnets(self, server):
        root = self._config()
        if server == 'kea':
            return [{'subnet': node.findtext('subnet', ''), 'pools': node.findtext('pools', '')}
                    for node in root.findall('OPNsense/Kea/dhcp4/subnets/subnet4')]
        if server == 'dnsmasq':
            ranges = [(node.findtext('start_addr', ''), node.findtext('end_addr', ''))
                      for node in root.findall('dnsmasq/dhcp_ranges')]
        else:
            ranges = [(node.findtext('range/from', ''), node.findtext('range/to', ''))
                      for node in root.findall('dhcpd/*') if node.find('enable') is not None]
        return [{'subnet': '', 'pools': '%s-%s' % r} for r in ranges if all(r)]

    def reverse_dns(self, addresses):
        def lookup(address):
            try:
                return socket.gethostbyaddr(address)[0]
            except (OSError, UnicodeError):
                return None

        # gethostbyaddr has no timeout, so a slow resolver only costs the
        # names that miss the deadline, not the inform
        pool = ThreadPoolExecutor(max_workers=8)
        try:
            futures = {pool.submit(lookup, a): a for a in set(addresses)}
            done, _ = wait(futures, timeout=10)
            return {futures[f]: f.result() for f in done if f.result()}
        finally:
            pool.shutdown(wait=False, cancel_futures=True)

    def gateway_status(self):
        status = self._action('interface', 'gateways', 'status')
        return list(status.values()) if isinstance(status, dict) else []

    def unbound_settings(self):
        node = self._config().find('OPNsense/unboundplus')
        if node is None:
            raise SourceError('unbound settings', None, 'Unbound is not configured')
        dots = {}
        for i, dot in enumerate(node.findall('dots/dot')):
            dots[dot.get('uuid', str(i))] = {
                'enabled': dot.findtext('enabled', '0'),
                'domain': dot.findtext('domain', ''),
                'server': dot.findtext('server', ''),
                'port': dot.findtext('port', ''),
            }
        return {'unbound': {
            'forwarding': {'enabled': node.findtext('forwarding/enabled', '0')},
            'dots': {'dot': dots},
        }}

    def unbound_infra(self):
        return self._action('unbound', 'dumpinfra')

    def flow_totals(self, since, until, device, direction):
        return self._action(
            'netflow', 'aggregate', 'top', 'FlowSourceAddrTotals', str(int(since)),
            str(int(until)), 'src_addr', 'octets',
            'if=%s,direction=%s' % (device, direction), '1000')

    def ping(self, destination, count, interval, packet_size):
        return LocalPing(destination, count, interval, packet_size)


class LocalPing(PingJob):
    """/sbin/ping in the background, its output read as it comes."""

    def __init__(self, destination, count, interval, packet_size):
        self.count = count
        self.interval = interval
        args = [PING, '-n', '-c', str(count), '-i', str(interval), '-s', str(packet_size),
                destination]
        try:
            self.proc = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                         stdin=subprocess.DEVNULL, text=True)
        except OSError as e:
            raise SourceError(PING, None, str(e)) from None
        self.started = time.monotonic()
        self._lines = []
        self._lock = threading.Lock()
        self._reader = threading.Thread(target=self._read, daemon=True)
        self._reader.start()
        self.transmitted = None   # from ping's summary
        self.last_line = ''

    def _read(self):
        for line in self.proc.stdout:
            with self._lock:
                self._lines.append(line.strip())

    def poll(self):
        done = not self._reader.is_alive()
        with self._lock:
            lines, self._lines = self._lines, []
        replies = []
        for line in lines:
            if line:
                self.last_line = line
            match = PING_REPLY.search(line)
            if match:
                replies.append({'id': int(match.group(1)), 'ttl': int(match.group(2)),
                                'time': float(match.group(3))})
            match = PING_SUMMARY.match(line)
            if match:
                self.transmitted = int(match.group(1))
        if done and self.transmitted is None:
            # it never got going: unknown host, no route, bad arguments
            raise SourceError(PING, self.proc.wait(), self.last_line or 'no output')
        if self.transmitted is not None:
            return self.transmitted, replies, done
        waited = time.monotonic() - self.started - PING_WAIT
        sent = int(waited // self.interval) + 1 if waited >= 0 else 0
        return min(sent, self.count), replies, done

    def stop(self):
        if self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.proc.kill()
