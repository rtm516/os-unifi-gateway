# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

An OPNsense plugin (`os-unifi-gateway`) whose Python daemon makes OPNsense appear to a UniFi Network controller as a UniFi gateway, speaking the UniFi "inform" protocol. OPNsense is the only supported router. Docker runs the same daemon off-box against the OPNsense REST API, for testing.

Forked from [amd989/unifi-gateway](https://github.com/amd989/unifi-gateway), itself based on [stephanlascar/unifi-gateway](https://github.com/stephanlascar/unifi-gateway) and [qvr/unifi-gateway](https://github.com/qvr/unifi-gateway).

## Running

```bash
pip install -r requirements.txt pytest jinja2
python -m pytest                                  # pycryptodome AES backend
UNIFI_GW_AES_BACKEND=openssl python -m pytest     # ctypes/libcrypto backend (what OPNsense uses)

# Off-box against a router's API (config is created on first start)
OPNSENSE_HOST=192.168.1.1 OPNSENSE_API_KEY_FILE=apikey.txt \
UNIFI_ADOPT_URL=http://controller:8080/inform python unifi_gateway.py run
python unifi_gateway.py dump      # print the inform payload once
python unifi_gateway.py status    # adoption state as JSON
docker compose up -d
```

## Architecture

- `unifi_gateway.py`: entry point. Adoption (keeps informing until the controller adopts), inform loop, controller responses (`noop`, `setparam`, `cmd`, `upgrade`, `setdefault`), config persistence. Config values from defaults, env overrides (`ENV_OVERRIDES`) and the router-discovered identity are "runtime" values that `_save_config` keeps out of the file unless the daemon changes them.
- `unifi_protocol.py`: TNBU packets (AES-CBC before adoption / GCM after, zlib or snappy) and JSON payloads. The pre-adoption inform is the full payload flagged `state: 1, default: true, adopted: false` with `x_authkey`; the controller needs `inform_ip` as an IP address, not a hostname. UXG payloads add `type`, `port_table`, `ethernet_overrides`, `udapi_version` and similar fields.
- `devices.py`: the emulated model, a UniFi Gateway Lite (`UXG`, eth0=LAN/eth1=WAN), and its latest-firmware lookup from fw-update.ubnt.com, unless `firmware` is pinned. The controller ignores model codes it doesn't know, and the USG (UGW3) was dropped because Ubiquiti ends its support after Network 10.6.
- `aes.py`: AES via pycryptodome, falling back to OpenSSL libcrypto through ctypes (OPNsense ships no Python AES library).
- `collectors/opnsense.py`: `OPNsenseCollector` turns router state into payload data. Interfaces are required; everything else is optional and degrades gracefully (forbidden calls back off for 10 minutes). WAN/LAN are matched by interface description, then identifier, then device. Their speed, duplex and link come from the NICs underneath (`physical_ports`): bridge members, lagg ports, a VLAN's parent, a PPP link's ports (`Source.ppps`, or guessed from the one unused unassigned NIC when the API can't say), fastest up port wins.
- `collectors/source.py`: `Source` interface. Each method returns data shaped like the matching OPNsense API response. `SourceError.hint` says how to fix access problems.
  - `api_source.py`: `APISource`, the REST API with a key (Docker/testing). `opnsense_api.py` is its stdlib HTTP client.
  - `local_source.py`: `LocalSource`, on the router: `configctl` actions, `/conf/config.xml`, `/tmp/<if>_nameserver` (used by the plugin; `UNIFI_GW_SOURCE=local`).
- `tools.py`: builds `if_table` / `network_table`. `tlv.py`: discovery broadcast TLVs. `daemon.py`: double-fork daemon.

**Plugin (`plugin/`):** laid out like opnsense/plugins. MVC model/controllers/view under `src/opnsense/mvc`, configd actions, an rc.conf.d template that passes settings to the daemon as env vars, the `unifi_gateway` rc.d script (daemon(8) with syslog tag `unifi-gateway`) and a syslog filter. `build-pkg.sh` bundles the Python sources into `/usr/local/opnsense/scripts/unifigateway/`. `build-repo.sh` makes the pkg repository. Both need FreeBSD's `pkg`; `Dockerfile.pkg` builds it for Linux. The daemon keeps its state in `/var/db/unifi-gateway/unifi-gateway.conf`.

**Release:** a `v*` tag builds the plugin pkg, attaches it to the GitHub release, publishes the repository to the `pkg-repo` branch (served from raw.githubusercontent.com) and pushes the Docker image to GHCR.

**Unhandled command log:** `unhandled_commands.json` (next to the config) records controller operations we don't act on. Treat it as a living TODO.
