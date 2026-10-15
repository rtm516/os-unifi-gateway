# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

An OPNsense plugin (`os-unifi-gateway`) whose Python daemon makes OPNsense appear to a UniFi Network controller as a UniFi gateway, speaking the UniFi "inform" protocol. OPNsense is the only supported router. Docker runs the same daemon off-box against the OPNsense REST API, for testing.

Forked from [amd989/unifi-gateway](https://github.com/amd989/unifi-gateway), itself based on [stephanlascar/unifi-gateway](https://github.com/stephanlascar/unifi-gateway) and [qvr/unifi-gateway](https://github.com/qvr/unifi-gateway).

## Running

```bash
pip install -r requirements.txt pytest jinja2
python -m pytest

# Off-box against a router's API (config is created on first start)
OPNSENSE_HOST=192.168.1.1 OPNSENSE_API_KEY_FILE=apikey.txt \
UNIFI_ADOPT_URL=http://controller:8080/inform python unifi_gateway.py run
python unifi_gateway.py dump      # print the inform payload once
python unifi_gateway.py status    # adoption state as JSON
docker compose up -d
```

## Architecture

- `unifi_gateway.py`: entry point. Adoption (keeps informing until the controller adopts), inform loop, controller responses (`noop`, `setparam`, `cmd`, `upgrade`, `setdefault`), config persistence. The payload declares `usg2_caps` bit 16 and `inform_min_interval`, so while someone has the controller's UI open its `noop` replies carry `live_update: true` and the daemon informs every 2 s. Config values from defaults, env overrides (`ENV_OVERRIDES`) and the router-discovered identity are "runtime" values that `_save_config` keeps out of the file unless the daemon changes them.
- `unifi_protocol.py`: TNBU packets (AES-CBC before adoption / GCM after, zlib or snappy) and JSON payloads. The pre-adoption inform is the full payload flagged `state: 1, default: true, adopted: false` with `x_authkey`; the controller needs `inform_ip` as an IP address, not a hostname. On the informs right after adoption (`initial_adoption`) it makes its own networks match `config_network_lan` (`cidr` router address/prefix, `vlan`, `dhcp_enabled`, `dhcp_range_start`/`stop` as address/prefix, from `Source.dhcp_subnets`) and `config_network_wan` when they differ from a factory-default gateway; the WAN stays `type: dhcp`, since anything else makes it rebuild its WAN network from what we send. UXG payloads add `type`, `port_table`, `ethernet_overrides`, `udapi_version` and similar fields. A UXG's WAN latency and availability (and so whether the controller shows the ISP online) come only from `uptime_stats.WAN.alerting_monitors[0]` (`latency_average`, `availability`), built from dpinger's gateway status; `if_table` latency is ignored for it. There `uptime` is the WAN's online streak, left out while it is down, and `downtime` how long the current outage has lasted (0 while up): the controller records each outage from it (started `downtime` s ago, closed by 0), raises its WAN-failed notification once it passes 6 s, and the back-online one when `uptime` returns. `EVT_GW_WANTransition` events are for multi-WAN failover, and an `active` one turns that detection off for the WAN, so we don't send them. The gateway panel shows load average from `sys_stats`; Network 10.0 shows no temperature or storage for a gateway, so we don't send them.
- `devices.py`: the emulated model, a UniFi Gateway Pro (`UXGPRO`, eth0=WAN/eth1=LAN; firmware platform `UXGPROV2`, since `UXGPRO` is the legacy 1.x line), and its latest-firmware lookup from fw-update.ubnt.com, unless `firmware` is pinned. Not the Gateway Lite (`UXG`): the controller refuses speed tests on UXG, UGW3 and UX. The controller keeps the model a device was adopted as, so a model change needs forget + re-adopt; the daemon records `adopted_model` and warns at start when it differs. The controller ignores model codes it doesn't know, and the USG (UGW3) was dropped because Ubiquiti ends its support after Network 10.6.
- `aes.py`: AES-CBC/GCM via `cryptography`.
- `collectors/opnsense.py`: `OPNsenseCollector` turns router state into payload data. Interfaces are required; everything else is optional and degrades gracefully (forbidden calls back off for 10 minutes). Client names aim for what a UniFi gateway reports, the Host Name a client sends in DHCP (its dnsmasq keeps it as sent; switches and APs report the same, and the controller keeps a UniFi OS gateway's name over theirs, `hostname_source: ubios`): a Kea reservation's hostname as entered, else the lease name without the router's domain (Kea lower-cases and qualifies it), else DNS, else ARP, kept while the client stays listed. Kea's made-up `<prefix>-<address with dashes>` names for clients that send none are dropped. With `dhcp = auto` the first lease server that has leases wins (on the router Dnsmasq's action answers with none next to Kea). WAN/LAN are matched by interface description, then identifier, then device. Their speed, duplex and link come from the NICs underneath (`physical_ports`): bridge members, lagg ports, a VLAN's parent, a PPP link's ports (`Source.ppps`, or guessed from the one unused unassigned NIC when the API can't say), fastest up port wins.
- `collectors/source.py`: `Source` interface. Each method returns data shaped like the matching OPNsense API response. `SourceError.hint` says how to fix access problems.
  - `api_source.py`: `APISource`, the REST API with a key (Docker/testing). `opnsense_api.py` is its requests client.
  - `local_source.py`: `LocalSource`, on the router: `configctl` actions, `/conf/config.xml`, `/tmp/<if>_nameserver` (used by the plugin; `UNIFI_GW_SOURCE=local`).
- `speedtest.py`: the controller's `speed-test` command, against Ookla speedtest.net servers (UniFi OS gateways' `ui-speed` uses Ubiquiti's own servers, from `sp-dir.uwn.com`): the server list from `www.speedtest.net/api/js/servers` (needs a User-Agent), the lowest-latency of the nearest 5, then HTTPS on `server-<id>.prod.hosts.ooklaserver.net:8080` (`/latency.txt`, `/download?size=`, `POST /upload`; the servers' own hostnames redirect uploads there). Measured over raw `http.client` connections: 8 streams, at least 10 s per direction, result from half-second samples without the slowest 30% and fastest 10%. Progress samples go out as `download-progress`/`upload-progress` and the daemon informs every 2 s during a test, which is what the dashboard draws live. Runs in a thread; `speedtest-status` reports each phase as 0 idle / 1 running / 2 done, and the controller only stores a result after an inform has shown `status_summary: 1`. The last result is kept in `speedtest.json` next to the config.
- `isp.py`: the ISP the controller shows (name and logo), sent as `geo_info: {"WAN": {isp_name, isp_organization, asn, ...}}`. Looked up through Team Cymru's IP-to-ASN DNS zones (TXT records via dnspython, which OPNsense core depends on, using the system's resolver) by the WAN address OPNsense reports, not by the address web services see (policy routing through a VPN changes that); a private WAN address (CGNAT) is swapped for the public one from OpenDNS's `myip.opendns.com`. Runs in a background thread: on start, on WAN address change and every 6 h.
- `tools.py`: builds `if_table` / `network_table`. `tlv.py`: discovery broadcast TLVs.

**Python libraries:** the plugin package declares no dependencies (one noarch package serves both `py311-` and `py313-` OPNsense, and pkg can't express "either"), so the daemon may only use the standard library plus what every OPNsense install has: core's CORE_DEPENDS (`Mk/version.mk`: requests, dnspython, Jinja2, ujson…) and their dependencies. `cryptography` qualifies only as a dependency of dnspython's default DNSSEC option. Anything else must be added to `requirements.txt` for Docker too.

**Plugin (`plugin/`):** laid out like opnsense/plugins. MVC model/controllers/view under `src/opnsense/mvc`, configd actions, an rc.conf.d template that passes settings to the daemon as env vars, the `unifi_gateway` rc.d script (daemon(8) with syslog tag `unifi-gateway`) and a syslog filter. `build-pkg.sh` bundles the Python sources into `/usr/local/opnsense/scripts/unifigateway/`. `build-repo.sh` makes the pkg repository. Both need FreeBSD's `pkg`; `Dockerfile.pkg` builds it for Linux. The daemon keeps its state in `/var/db/unifi-gateway/unifi-gateway.conf`.

**Release:** a `v*` tag builds the plugin pkg, attaches it to the GitHub release, publishes the repository to the `pkg-repo` branch (served from raw.githubusercontent.com) and pushes the Docker image to GHCR.

**Unhandled command log:** `unhandled_commands.json` (next to the config) records controller operations we don't act on. Treat it as a living TODO.
