# -*- coding: utf-8 -*-
"""The interface between the collector and wherever router state comes from.

Two implementations exist:

- APISource (api_source.py) talks to the OPNsense REST API with an API key,
  so the daemon can run anywhere that reaches the router (Docker, testing).
- LocalSource (local_source.py) runs on the router itself and asks configd
  directly, which is what the OPNsense plugin uses.

Each method returns data shaped like the OPNsense API response it mirrors, so
the REST implementation is a pass-through and the on-device one reproduces
the same shapes from the configd actions the API controllers wrap.
"""

LEASE_SERVERS = ('dnsmasq', 'kea', 'isc')


class SourceError(Exception):
    """Data is unavailable. `hint` says how to fix an access problem, e.g.
    which API privilege to grant."""

    def __init__(self, what, status=None, message='', hint=None):
        self.path = what
        self.status = status
        self.hint = hint
        super().__init__('%s: %s %s' % (what, status or 'error', message))

    @property
    def forbidden(self):
        return self.status in (401, 403)


class Source:

    def interfaces(self):
        """Rows of /api/interfaces/overview/interfaces_info/1: identifier,
        description, device, macaddr, status, mtu, media, ipv4 [{ipaddr:
        'a.b.c.d/nn'}], gateways, ifctl.nameserver, statistics {...}.

        Unassigned devices are rows too, with an empty identifier. Rows
        carry ifconfig's view of the device: is_physical, media ('1000baseT
        <full-duplex>'), members {dev: ...} for a bridge, laggport {dev:
        ...} for a lagg, vlan {parent} for a VLAN."""
        raise NotImplementedError

    def ppps(self):
        """PPP links from config.xml <ppps><ppp>: [{if: 'pppoe0', type:
        'pppoe', ports: ['igb0']}]. No REST API endpoint exposes them."""
        raise NotImplementedError

    def system_information(self):
        """{'name': 'host.domain', 'versions': ['OPNsense 25.7...', ...]}"""
        raise NotImplementedError

    def system_time(self):
        """{'uptime': '2 days, 03:04:05', 'loadavg': '0.10, 0.20, 0.30'}"""
        raise NotImplementedError

    def system_resources(self):
        """{'memory': {'total': bytes, 'used': bytes}}"""
        raise NotImplementedError

    def cpu_usage(self):
        """One sample: {'total': percent} or {'idle': percent}."""
        raise NotImplementedError

    def arp(self):
        """Rows of {mac, ip, intf, expired, hostname}."""
        raise NotImplementedError

    def dhcp_leases(self, server):
        """Lease rows from `server` (one of LEASE_SERVERS): {address,
        hwaddr, hostname, state?}."""
        raise NotImplementedError

    def dhcp_reservations(self, server):
        """Reservations of `server` (one of LEASE_SERVERS) as rows of
        {hw_address, hostname}, the hostname as entered. Kea lower-cases it
        in its leases."""
        raise NotImplementedError

    def dhcp_subnets(self, server):
        """IPv4 address pools of `server` (one of LEASE_SERVERS) as rows
        shaped like Kea's subnets: {subnet: 'a.b.c.d/nn' or '', pools:
        'start-end' (maybe with spaces around the dash) or 'a.b.c.d/nn',
        one per line}."""
        raise NotImplementedError

    def reverse_dns(self, addresses):
        """{address: name} as the router's resolver answers PTR lookups,
        like /api/diagnostics/dns/reverse_lookup. Addresses without a name
        may be missing or map to themselves."""
        raise NotImplementedError

    def gateway_status(self):
        """Gateway rows: {name, address, interface?, delay: '8.4 ms'}."""
        raise NotImplementedError

    def unbound_settings(self):
        """{'unbound': {'forwarding': {'enabled'}, 'dots': {'dot': {...}}}}"""
        raise NotImplementedError

    def unbound_infra(self):
        """Unbound infrastructure cache rows: {ip, host, ...}."""
        raise NotImplementedError

    def flow_totals(self, since, until, device, direction):
        """Network Insight FlowSourceAddrTotals rows {src_addr, total} for
        complete buckets in [since, until) on `device` in `direction`."""
        raise NotImplementedError
