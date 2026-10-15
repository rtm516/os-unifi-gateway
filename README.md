# UniFi Gateway for OPNsense

An OPNsense plugin that makes your firewall appear in a UniFi Network controller as a UniFi gateway. The controller then shows the router's WAN and LAN status, traffic, clients and topology alongside your UniFi access points and switches, just as it would for a Ubiquiti gateway.

It appears as a **UniFi Gateway Pro (UXG-Pro)** running Ubiquiti's latest firmware, so the controller never offers an upgrade. The WAN is reported on the Pro's first port and the LAN on its second.

This project started as a fork of [amd989/unifi-gateway](https://github.com/amd989/unifi-gateway), which did the original work on the inform protocol, adoption flow and payloads that this plugin builds on.

```
┌───────────────────────────┐   inform (HTTP POST, AES)   ┌───────────────────┐
│ OPNsense                  │ ──────────────────────────► │ UniFi Network     │
│  └─ os-unifi-gateway      │ ◄────────────────────────── │ controller        │
│     reads interfaces,     │   adoption, settings        │                   │
│     clients, DHCP, DNS    │                             │ sees a UniFi      │
│     straight from configd │   UniFi APs / switches ───► │ gateway           │
└───────────────────────────┘   inform normally           └───────────────────┘
```

## Installation

The plugin is published as a pkg repository on this repository's [`pkg-repo`](https://github.com/rtm516/unifi-gateway/tree/pkg-repo) branch. On the OPNsense shell (**System > Console** or SSH):

```sh
fetch -o /usr/local/etc/pkg/repos/unifi-gateway.conf \
  https://raw.githubusercontent.com/rtm516/unifi-gateway/pkg-repo/unifi-gateway.conf
pkg update
pkg install os-unifi-gateway
```

After that the plugin shows up under **System > Firmware > Plugins** like any other, and updates arrive with normal OPNsense updates. Each release's package is also attached to its [GitHub release](https://github.com/rtm516/unifi-gateway/releases) (`pkg add os-unifi-gateway-<version>.pkg`).

To remove it: `pkg delete os-unifi-gateway`, then delete `/usr/local/etc/pkg/repos/unifi-gateway.conf`.

## Setup

1. Open **Services > UniFi Gateway**.
2. Tick **Enable** and enter the controller's **inform URL**, usually `http://<controller>:8080/inform`.
3. Pick the **WAN** and **LAN** interfaces to report. The LAN interface's address and MAC identify the gateway to the controller.
4. Click **Apply**.
5. In the UniFi controller, the gateway shows up as pending adoption. Click **Adopt**.

The page shows the adoption state, and **Services > UniFi Gateway > Log File** shows what the daemon is doing.

No API key is needed.

### Upgrading from a version that emulated the UXG Lite

Earlier versions appeared as a UniFi Gateway Lite, on which the controller won't run speed tests. The controller keeps the model a gateway was adopted as, so after upgrading, **forget the gateway in the UniFi controller and adopt it again**. Until then it stays a Gateway Lite with its WAN and LAN ports swapped, and the log says so at every start. Forgetting resets the plugin's adoption; the gateway then shows up as a pending **UniFi Gateway Pro**, ready to adopt.

## What gets reported

| Controller shows | Comes from |
|---|---|
| WAN/LAN addresses, MACs, link state, MTU, speed | interface status |
| Traffic counters and rates | interface statistics |
| WAN gateway and latency | routing table and gateway monitoring (dpinger) |
| ISP name and logo | the WAN address's provider, looked up at [ipinfo.io](https://ipinfo.io) |
| WAN DNS servers | Unbound's upstream servers, otherwise the WAN link's DNS servers |
| Clients, hostnames, per-client traffic | ARP table, DHCP leases (Dnsmasq or Kea), Network Insight (NetFlow) |
| Hostname, uptime, CPU, memory | system information |
| WAN speed test | measured by the daemon against the nearest fast [speedtest.net](https://www.speedtest.net) server, as UniFi gateways do |

Per-client traffic needs **Reporting > NetFlow** capturing the LAN interface.

The speed test (**ISP Speed Test** on the dashboard, or the controller's schedule) needs nothing extra installed, and shows its progress live. It runs where the daemon runs: on the plugin it measures the firewall's WAN link, but in Docker it measures the Docker host's connection, not the router's. It picks the lowest-latency of the nearest speedtest.net servers, then measures each direction for at least 10 seconds over 8 connections, so results should match speedtest.net. The last result is kept in `speedtest.json` next to the config file.

## Testing with Docker

For development and testing, the gateway can also run in Docker, reading from the OPNsense REST API.

1. On OPNsense, create a user under **System > Access > Users**, generate an API key for it, and grant it these privileges:

   | Privilege | Needed for |
   |---|---|
   | Status: Interfaces | **required**: interfaces, addresses, counters |
   | Lobby: Dashboard | hostname, version, uptime, CPU, memory |
   | Diagnostics: ARP Table | client list |
   | Diagnostics: Network Insight | per-client traffic |
   | Services: Dnsmasq DNS/DHCP: Settings *or* Services: DHCP: Kea(v4) | client hostnames from DHCP leases |
   | Services: Unbound | upstream DNS servers |
   | System: Gateways | WAN latency |

   Some of these privileges also allow changes on that page. Only **Status: Interfaces** is required; the log names any missing privilege.

2. Put the downloaded `apikey.txt` in `conf/`, set the router address and controller URL in `docker-compose.yml`, and start it:

   ```sh
   docker compose up -d
   docker compose logs -f
   ```

   `conf/unifi-gateway.conf` is created on first start.

To see what would be sent without talking to a controller:

```sh
docker compose run --rm unifi-gateway dump
```

### Running from source

```sh
pip install -r requirements.txt
OPNSENSE_HOST=192.168.1.1 OPNSENSE_API_KEY_FILE=apikey.txt \
UNIFI_ADOPT_URL=http://controller:8080/inform python unifi_gateway.py run
```

Commands: `run` (foreground), `start` / `stop` / `restart` (background daemon), `set-adopt -s <url>`, `dump`, `status`.

## Configuration reference

Only needed for Docker or source installs.

### Environment variables

Environment variables override the config file.

| Variable | Setting |
|---|---|
| `OPNSENSE_HOST` | Router address (IP or hostname, optionally with scheme and port) |
| `OPNSENSE_API_KEY`, `OPNSENSE_API_SECRET` | API credentials |
| `OPNSENSE_API_KEY_FILE` | Path to the `apikey.txt` OPNsense lets you download |
| `OPNSENSE_VERIFY_SSL` | `true` to verify the router's certificate |
| `OPNSENSE_WAN_INTERFACE`, `OPNSENSE_LAN_INTERFACE` | Interfaces to report (default `WAN` / `LAN`) |
| `UNIFI_ADOPT_URL` | Controller inform URL |
| `UNIFI_GW_FIRMWARE` | Pin the reported firmware instead of tracking the latest |
| `UNIFI_GW_HOSTNAME` | Hostname reported to the controller (default: the router's hostname) |
| `UNIFI_GW_SOURCE` | `api` (default) or `local` (on the router itself) |
| `UNIFI_GW_CONFIG` | Config file path (default `conf/unifi-gateway.conf`) |
| `UNIFI_GW_LOG_LEVEL` | `DEBUG`, `INFO`, `WARNING`, `ERROR` |
| `UNIFI_GW_LOG_FILE` | Log to a file instead of stderr |

### Config file

```ini
[opnsense]
host = 192.168.1.1
api_key =
api_secret =
# api_key_file = /path/to/apikey.txt
verify_ssl = False
# Interfaces are matched by description, then identifier (wan, lan, opt1), then device
# wan_interface = WAN
# lan_interface = LAN
# DHCP lease source: auto, dnsmasq, kea, isc or none
# dhcp = auto

[gateway]
# firmware = latest
# hostname = my-gateway
# speedtest_file = /path/to/speedtest.json
```

Adoption state is added to the file automatically; don't edit it.

## Development

```sh
pip install -r requirements.txt pytest jinja2
python -m pytest
UNIFI_GW_AES_BACKEND=openssl python -m pytest   # the AES path the plugin uses on OPNsense
```

| Path | Purpose |
|---|---|
| `unifi_gateway.py` | Entry point: adoption, inform loop, controller responses, config handling |
| `unifi_protocol.py` | Inform packets (TNBU header, AES-CBC/GCM, zlib/snappy) and payloads |
| `devices.py` | Emulated model and latest-firmware lookup |
| `speedtest.py` | The controller's WAN speed test, against speedtest.net servers |
| `isp.py` | Looks up the WAN's ISP for the controller |
| `aes.py` | AES via pycryptodome, or OpenSSL `libcrypto` through ctypes when that isn't installed (as on OPNsense) |
| `collectors/opnsense.py` | Turns router state into what the payload needs |
| `collectors/source.py` | The interface between the collector and the router |
| `collectors/local_source.py` | On the router: configd and `config.xml` (used by the plugin) |
| `collectors/api_source.py` | Off the router: the REST API (used for Docker and testing) |
| `plugin/` | The OPNsense plugin, laid out like [opnsense/plugins](https://github.com/opnsense/plugins) |

`plugin/build-pkg.sh` builds the package and `plugin/build-repo.sh` the repository. Both need FreeBSD's `pkg`; `plugin/Dockerfile.pkg` builds it for Linux, so packages can be made on any machine with Docker:

```sh
docker build -t unifi-gateway-pkg -f plugin/Dockerfile.pkg plugin
docker run --rm -v "$PWD:/src" -w /src unifi-gateway-pkg sh plugin/build-pkg.sh 0.1.0 dist
```

Pushing a `v*` tag builds the plugin package, attaches it to a GitHub release, publishes it to the `pkg-repo` branch and pushes the Docker image to GHCR.

## License

MIT; see [LICENSE](LICENSE).

## Acknowledgments

- [amd989/unifi-gateway](https://github.com/amd989/unifi-gateway): the project this was forked from
- [stephanlascar/unifi-gateway](https://github.com/stephanlascar/unifi-gateway): the original UniFi gateway emulator
- [qvr/unifi-gateway](https://github.com/qvr/unifi-gateway): Python 3 port, FreeBSD support, AES-GCM

Protocol documentation:

- [jk-5/unifi-inform-protocol](https://github.com/jk-5/unifi-inform-protocol)
- [fxkr/unifi-protocol-reverse-engineering](https://github.com/fxkr/unifi-protocol-reverse-engineering)
