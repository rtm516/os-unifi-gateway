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
import time
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor, wait

from .source import Source, SourceError

CONFIGCTL = '/usr/local/sbin/configctl'
CONFIG_XML = '/conf/config.xml'
SYSCTL = '/sbin/sysctl'
VERSION_CMD = '/usr/local/sbin/opnsense-version'
IFCTL_DIR = '/tmp'

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
        boottime = self._sysctl('kern.boottime').get('kern.boottime', '')
        match = re.search(r'sec = (\d+)', boottime)
        if not match:
            raise SourceError('kern.boottime', None, 'unexpected value %r' % boottime)
        return {'uptime': format_uptime(time.time() - int(match.group(1)))}

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
