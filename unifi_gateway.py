# -*- coding: utf-8 -*-
import configparser
import argparse
import copy
import json
import logging
import logging.handlers
import os
import signal
import socket
import sys
import time
import urllib.request
import urllib.error

from collectors import SourceError, create_collector
from daemon import Daemon
from devices import Device
from isp import IspInfo
from speedtest import SpeedTest
from unifi_protocol import (
    create_broadcast_message, create_inform, encode_inform, decode_inform,
)

logger = logging.getLogger('unifi-gateway')

CONFIG_FILE = os.environ.get('UNIFI_GW_CONFIG', 'conf/unifi-gateway.conf')


def setup_logging():
    log_level = os.environ.get('UNIFI_GW_LOG_LEVEL', 'DEBUG').upper()
    log_file = os.environ.get('UNIFI_GW_LOG_FILE', None)
    formatter = logging.Formatter(
        '%(asctime)s [unifi-gateway] : %(levelname)s : %(message)s'
    )

    if log_file:
        handler = logging.handlers.RotatingFileHandler(
            log_file, maxBytes=10 * 1024 * 1024, backupCount=3
        )
    else:
        handler = logging.StreamHandler()

    handler.setFormatter(formatter)
    logger.setLevel(getattr(logging, log_level, logging.DEBUG))
    logger.addHandler(handler)


setup_logging()


# Written when no config file exists. Only the router address and API
# credentials need filling in; everything else is discovered from the API.
CONFIG_TEMPLATE = """\
[opnsense]
host = {host}
api_key =
api_secret =
verify_ssl = False
"""

DEFAULTS = {
    'global': {
        'pid_file': 'unifi-gateway.pid',
        'disable_broadcast': 'True',
    },
    'opnsense': {},
    'gateway': {
        'is_adopted': 'False',
        'use_aes_gcm': 'False',
    },
    'provisioned': {},
}

# Environment variables that override settings for this process only; they
# are never written back to the config file.
ENV_OVERRIDES = {
    'OPNSENSE_HOST': ('opnsense', 'host'),
    'OPNSENSE_API_KEY': ('opnsense', 'api_key'),
    'OPNSENSE_API_SECRET': ('opnsense', 'api_secret'),
    'OPNSENSE_API_KEY_FILE': ('opnsense', 'api_key_file'),
    'OPNSENSE_VERIFY_SSL': ('opnsense', 'verify_ssl'),
    'OPNSENSE_WAN_INTERFACE': ('opnsense', 'wan_interface'),
    'OPNSENSE_LAN_INTERFACE': ('opnsense', 'lan_interface'),
    'UNIFI_GW_SOURCE': ('opnsense', 'source'),
    'UNIFI_GW_FIRMWARE': ('gateway', 'firmware'),
    'UNIFI_GW_HOSTNAME': ('gateway', 'hostname'),
}


# setparam keys kept out of the config file. system_cfg is the full UDAPI
# configuration a UXG would apply (including the controller's site token and
# RADIUS secrets); nothing here uses it.
# Inform this often while a speed test runs, so the controller can show its
# progress live instead of one update per normal interval.
SPEEDTEST_INFORM_INTERVAL = 2

NOT_PERSISTED_SETPARAM_KEYS = {'_type', 'server_time_in_utc', 'blocked_sta', 'system_cfg'}


def _summarise(value, limit=500):
    """Shorten a controller value for the unhandled-commands log."""
    text = value if isinstance(value, str) else json.dumps(value)
    if len(text) <= limit:
        return value
    try:
        parsed = json.loads(text)
    except ValueError:
        parsed = None
    keys = sorted(parsed) if isinstance(parsed, dict) else []
    return {'_truncated': len(text), 'keys': keys}


def _describe_source_error(e):
    message = 'Cannot read interfaces from OPNsense: %s' % e
    return '%s -- %s' % (message, e.hint) if e.hint else message


def ensure_config(path):
    """Create a minimal config file if there is none yet."""
    if os.path.exists(path):
        return
    directory = os.path.dirname(path)
    if directory:
        os.makedirs(directory, exist_ok=True)
    with open(path, 'w') as f:
        f.write(CONFIG_TEMPLATE.format(host=os.environ.get('OPNSENSE_HOST', '')))
    logger.info('Created config file %s', path)


UNHANDLED_LOG = os.environ.get(
    'UNIFI_GW_UNHANDLED_LOG',
    os.path.join(os.path.dirname(CONFIG_FILE), 'unhandled_commands.json'),
)


class UnifiGateway(Daemon):

    def __init__(self, **kwargs):
        self.interval = 10
        self.running = True
        self.config = configparser.RawConfigParser()
        self.config.read(CONFIG_FILE)

        # Values that only live in memory: defaults, env overrides and the
        # identity discovered from the router. Maps (section, option) to
        # (value in the file or None, value in memory) so saving can leave
        # them out unless the daemon has since changed them.
        self._runtime = {}
        for section, options in DEFAULTS.items():
            if not self.config.has_section(section):
                self.config.add_section(section)
            for option, value in options.items():
                if not self.config.has_option(section, option):
                    self._set_runtime(section, option, value)
        for env, (section, option) in ENV_OVERRIDES.items():
            if os.environ.get(env):
                self._set_runtime(section, option, os.environ[env])

        self.device = Device(self.config)
        self.speedtest = SpeedTest(self.config.get(
            'gateway', 'speedtest_file',
            fallback=os.path.join(os.path.dirname(CONFIG_FILE), 'speedtest.json')))
        self.isp = IspInfo()
        self.datacollector = None
        self._unhandled = {}
        self._adopt_logged = False
        Daemon.__init__(
            self, pidfile=self.config.get('global', 'pid_file'), **kwargs
        )

    def _set_runtime(self, section, option, value):
        key = (section, option)
        file_value = self._runtime.get(key, (
            self.config.get(section, option, fallback=None), None))[0]
        self._runtime[key] = (file_value, value)
        self.config.set(section, option, value)

    def _init_collector(self):
        """Initialize data collector and unhandled log. Called at run() time
        so that stop/restart commands don't need the router to be reachable."""
        self.datacollector = create_collector(self.config, self.device)
        self._set_runtime('gateway', 'lan_mac', self.datacollector.lan_mac)
        self._set_runtime('gateway', 'lan_ip', self.datacollector.lan_ip)
        logger.info(
            'Connected to OPNsense %s: LAN %s (%s), WAN %s',
            self.datacollector.data.get('hostname', ''),
            self.datacollector.lan_ip, self.datacollector.lan_mac,
            self.datacollector.data['ip'].get(self.device.wan_port, {}).get('address', 'no IPv4'),
        )
        self._unhandled = self._load_unhandled()

    def _wait_for_collector(self):
        """Retry until the router API answers; config errors are fatal."""
        while self.running:
            try:
                self._init_collector()
                return True
            except ValueError as e:
                logger.critical('%s (config: %s)', e, CONFIG_FILE)
                return False
            except SourceError as e:
                logger.error('%s', _describe_source_error(e))
                logger.info('Retrying in 30s')
                time.sleep(30)
        return False

    def _load_unhandled(self):
        try:
            with open(UNHANDLED_LOG, 'r') as f:
                return json.load(f)
        except (IOError, OSError, json.JSONDecodeError):
            return {}

    def _save_unhandled(self):
        try:
            with open(UNHANDLED_LOG, 'w') as f:
                json.dump(self._unhandled, f, indent=2, sort_keys=True)
        except (IOError, OSError) as e:
            logger.warning('Failed to write unhandled log: %s', e)

    def _record_unhandled(self, category, key, response):
        entry_key = '%s/%s' % (category, key)
        is_new = entry_key not in self._unhandled
        self._unhandled[entry_key] = {
            'category': category,
            'key': key,
            'last_seen': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()),
            'count': self._unhandled.get(entry_key, {}).get('count', 0) + 1,
            'payload': {k: v for k, v in response.items()
                        if k not in ('server_time_in_utc',)},
        }
        self._save_unhandled()
        if is_new:
            logger.info(
                'New unhandled %s recorded: %s (see %s)',
                category, key, UNHANDLED_LOG,
            )

    def _adopt_url(self):
        return os.environ.get('UNIFI_ADOPT_URL') or \
            self.config.get('gateway', 'url', fallback='')

    def _try_auto_adopt(self):
        """Adopt to UNIFI_ADOPT_URL (or a URL from an earlier set-adopt)."""
        if self.config.getboolean('gateway', 'is_adopted'):
            return True

        url = self._adopt_url()
        if not url:
            return False

        if not self.config.has_option('gateway', 'url') or \
                self.config.get('gateway', 'url', fallback='') != url:
            self.config.set('gateway', 'url', url)
            self._save_config()

        key = os.environ.get('UNIFI_ADOPT_KEY')
        if not key and self.config.has_option('provisioned', 'key'):
            key = self.config.get('provisioned', 'key')

        if not self._adopt_logged:
            logger.info('Adopting to controller at %s', url)
        self.set_adopt(url, key)
        return self.config.getboolean('gateway', 'is_adopted')

    def run(self):
        signal.signal(signal.SIGTERM, self._handle_signal)
        signal.signal(signal.SIGINT, self._handle_signal)
        if not self._wait_for_collector():
            return

        # Until adopted, keep informing the controller (so clicking Adopt in
        # its UI completes without a restart) or broadcast for discovery
        broadcast_index = 1
        while self.running and not self.config.getboolean('gateway', 'is_adopted'):
            if self._adopt_url():
                if self._try_auto_adopt():
                    logger.info('Adoption complete')
                    break
            elif self.config.getboolean('global', 'disable_broadcast'):
                logger.critical(
                    'Not adopted and TLV broadcasting disabled: set '
                    'UNIFI_ADOPT_URL or run set-adopt first'
                )
                return
            else:
                self._send_broadcast(broadcast_index)
                broadcast_index += 1
            time.sleep(self.interval)
            try:
                self.datacollector.update()
            except SourceError as e:
                logger.warning('Failed to refresh data from OPNsense: %s', e)

        logger.info(
            'Device adopted, starting inform loop (interval=%ds)', self.interval
        )
        self._check_adopted_model()

        while self.running:
            try:
                self.datacollector.update()
                response = self._send_inform(self._create_inform())
                logger.debug(
                    'Received %s from controller', response.get('_type', 'unknown')
                )
                self._handle_response(response)
            except Exception as e:
                logger.error('Error in inform loop: %s', e, exc_info=True)
                self.interval = min(self.interval * 2, 60)
            time.sleep(self._inform_delay())

        logger.info('Shutting down gracefully')

    def status(self):
        """Adoption state from the config file; never contacts anything."""
        return {
            'adopted': self.config.getboolean('gateway', 'is_adopted'),
            'controller': self.config.get('gateway', 'url', fallback='') or self._adopt_url(),
            'model': self.device.model,
            'model_display': self.device.display,
            'firmware': self.device.pinned_firmware or 'latest',
            'cipher': 'AES-GCM' if self.config.getboolean('gateway', 'use_aes_gcm') else 'AES-CBC',
        }

    def dump(self):
        """Print the full inform payload, as an adopted gateway would send it."""
        self.running = False  # fail fast instead of retrying
        try:
            self._init_collector()
        except ValueError as e:
            logger.critical('%s (config: %s)', e, CONFIG_FILE)
            sys.exit(1)
        except SourceError as e:
            logger.critical('%s', _describe_source_error(e))
            sys.exit(1)
        self._set_runtime('gateway', 'is_adopted', 'True')
        self._set_runtime('gateway', 'url', self.config.get('gateway', 'url', fallback=''))
        payload = self._create_inform()
        print(json.dumps(json.loads(payload), indent=2))

    def _check_adopted_model(self):
        """The controller keeps the model a device was adopted as, so a
        gateway adopted by an older version (which emulated the UXG Lite)
        stays a Lite, with the Lite's port layout, until it's re-adopted."""
        adopted = self.config.get('gateway', 'adopted_model', fallback='UXG')
        if adopted != self.device.model:
            logger.warning(
                'This gateway was adopted as a %s but now reports itself as a %s: '
                'forget it in the UniFi controller and adopt it again, or ports, '
                'clients and the speed test will be wrong',
                adopted, self.device.display,
            )

    def _inform_delay(self):
        if self.speedtest.active:
            return min(self.interval, SPEEDTEST_INFORM_INTERVAL)
        return self.interval

    def _create_inform(self):
        data = self.datacollector.data
        data['speedtest'] = self.speedtest.status()
        data['isp'] = self.isp.current(
            data.get('ip', {}).get(self.device.wan_port, {}).get('address'))
        return create_inform(self.config, self.datacollector, self.device)

    def _handle_signal(self, signum, frame):
        logger.info('Received signal %d, shutting down', signum)
        self.running = False

    def _handle_response(self, response):
        resp_type = response.get('_type', '')

        if resp_type == 'noop':
            self.interval = response.get('interval', self.interval)

        elif resp_type == 'setparam':
            HANDLED_SETPARAM_KEYS = {
                '_type', 'server_time_in_utc', 'blocked_sta', 'mgmt_cfg',
            }
            for key, value in response.items():
                if key == 'mgmt_cfg':
                    self._parse_mgmt_cfg(value)
                if key not in NOT_PERSISTED_SETPARAM_KEYS:
                    self.config.set('provisioned', key, str(value))
                if key not in HANDLED_SETPARAM_KEYS:
                    self._record_unhandled(
                        'setparam', key,
                        {'_type': 'setparam', 'key': key, 'value': _summarise(value)},
                    )
            self._save_config()

        elif resp_type == 'reboot':
            logger.info('Received reboot request from controller (ignored)')

        elif resp_type == 'cmd':
            self._handle_cmd(response)

        elif resp_type == 'upgrade':
            version = response.get('version', '')
            logger.info('Received upgrade request to version %s', version)
            pinned = self.device.pinned_firmware
            if pinned and version and version != pinned:
                self.config.set('gateway', 'previous_firmware', pinned)
                self.config.set('gateway', 'firmware', version)
                self._save_config()
                logger.info('Pinned firmware version updated in config')
            elif not pinned:
                # we report the latest release, so a newer one is out
                self.device.refresh_firmware()

        elif resp_type == 'setdefault':
            logger.critical('Controller requested device reset')
            self.config.set('gateway', 'is_adopted', 'False')
            self.config.remove_option('gateway', 'key')
            self.config.remove_option('gateway', 'adopted_model')
            self.config.set('gateway', 'use_aes_gcm', 'False')
            self._save_config()
            self.running = False

        elif resp_type == 'httperror':
            logger.warning(
                'HTTP error from controller: %s %s',
                response.get('code'), response.get('msg'),
            )

        elif resp_type == 'urlerror':
            logger.error(
                'Connection error to controller, retry in 60s: %s',
                response.get('msg'),
            )
            self.interval = 60

        else:
            logger.warning('Unhandled response type: %s', resp_type)
            self._record_unhandled('response', resp_type, response)

    def _handle_cmd(self, response):
        cmd = response.get('cmd', '')
        logger.info('Received command: %s', cmd)

        if cmd == 'speed-test':
            self.speedtest.start(down_duration=response.get('down_duration'),
                                 up_duration=response.get('up_duration'))
        elif cmd == 'set-locate':
            self.config.set('gateway', 'locating', 'True')
            logger.info('Locate mode enabled')
        elif cmd == 'unset-locate':
            self.config.set('gateway', 'locating', 'False')
            logger.info('Locate mode disabled')
        else:
            logger.warning('Unknown command: %s', cmd)
            self._record_unhandled('command', cmd, response)

    def _send_broadcast(self, broadcast_index):
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 20)
            sock.sendto(
                create_broadcast_message(
                    self.config, broadcast_index, self.device,
                    self.datacollector.uptime(),
                ),
                ('233.89.188.1', 10001),
            )
            sock.close()
            logger.debug(
                'Sent broadcast #%d from %s',
                broadcast_index, self.config.get('gateway', 'lan_ip'),
            )
        except Exception as e:
            logger.error('Failed to send broadcast: %s', e)

    def quit(self):
        self.running = False

    def set_adopt(self, url, key):
        if self.datacollector is None and not self._wait_for_collector():
            return
        self.config.set('gateway', 'url', url)
        if key:
            self.config.set('gateway', 'key', key)
        self._save_config()

        response = self._send_inform(self._create_inform())
        logger.debug('Received %s from controller', response)

        if response['_type'] == 'httperror':
            if response['code'] == '404':
                if not self._adopt_logged:
                    logger.info(
                        'Controller has received the initial inform: click '
                        'Adopt on the %s in the UniFi UI', self.device.display,
                    )
                self._adopt_logged = True
                return
            if response['code'] == '400':
                logger.error(
                    'Authentication failed -- wrong authkey or '
                    'device removed from controller?'
                )
                return
            logger.error(
                'HTTP error from controller: %s %s',
                response['code'], response['msg'],
            )
            return

        if response['_type'] == 'urlerror':
            logger.error(
                'Connection error to controller: %s', response['msg']
            )
            return

        if response['_type'] == 'setparam':
            if not self.config.getboolean('gateway', 'is_adopted'):
                logger.info('setparam received, device now adopted')
                self.config.set('gateway', 'is_adopted', 'True')
                self.config.set('gateway', 'adopted_model', self.device.model)

            for k, value in response.items():
                if k == 'mgmt_cfg':
                    self._parse_mgmt_cfg(value)
                if k not in NOT_PERSISTED_SETPARAM_KEYS:
                    self.config.set('provisioned', k, str(value))
            self._save_config()

    def _parse_mgmt_cfg(self, data):
        HANDLED_MGMT_KEYS = {
            'cfgversion', 'mgmt_url', 'authkey', 'use_aes_gcm',
            'inform_url', 'stun_url', 'report_crash',
            'capability', 'selfrun_guest_mode', 'led_enabled',
        }
        for row in data.split('\n'):
            if '=' not in row:
                continue
            key, value = row.split('=', 1)
            if key == 'cfgversion':
                self.config.set('provisioned', 'cfgversion', value)
            elif key == 'mgmt_url':
                self.config.set('provisioned', 'mgmt_url', value)
            elif key == 'authkey':
                logger.debug('Updating device authkey from mgmt_cfg')
                self.config.set('provisioned', 'key', value)
                self.config.set('gateway', 'key', value)
            elif key == 'use_aes_gcm':
                if not self.config.getboolean('gateway', 'use_aes_gcm'):
                    self.config.set('gateway', 'use_aes_gcm', 'True')
                    logger.debug('Switching encryption to AES-GCM')
            if key not in HANDLED_MGMT_KEYS:
                self._record_unhandled(
                    'mgmt_cfg', key,
                    {'_type': 'mgmt_cfg', 'key': key, 'value': value},
                )

    def _send_inform(self, data, encryption='CBC'):
        headers = {
            'Accept': '*/*',
            'Content-Type': 'application/x-binary',
            'User-Agent': 'AirControl Agent v1.0',
            'Expect': '100-continue',
        }
        url = self.config.get('gateway', 'url')
        request = urllib.request.Request(
            url, encode_inform(self.config, data, encryption=encryption), headers
        )
        logger.debug('Sending inform to %s', url)
        try:
            response = urllib.request.urlopen(request, timeout=30)
        except urllib.error.HTTPError as e:
            return {'_type': 'httperror', 'code': str(e.code), 'msg': str(e.reason)}
        except urllib.error.URLError as e:
            return {'_type': 'urlerror', 'msg': str(e.reason)}
        return decode_inform(self.config, response.read())

    def _save_config(self):
        config = copy.deepcopy(self.config)
        for (section, option), (file_value, value) in self._runtime.items():
            if config.get(section, option, fallback=None) != value:
                continue  # changed since, so it belongs in the file now
            if file_value is None:
                config.remove_option(section, option)
            else:
                config.set(section, option, file_value)
        for section in config.sections():
            if not config.items(section):
                config.remove_section(section)
        with open(CONFIG_FILE, 'w') as config_file:
            config.write(config_file)


def main():
    parser = argparse.ArgumentParser(description='UniFi Gateway Emulator')
    subparsers = parser.add_subparsers(dest='command')

    subparsers.add_parser('start', help='Start as background daemon')
    subparsers.add_parser('stop', help='Stop the daemon')
    subparsers.add_parser('restart', help='Restart the daemon')
    subparsers.add_parser('run', help='Run in foreground')
    subparsers.add_parser(
        'dump', help='Collect from OPNsense once and print the inform payload'
    )
    subparsers.add_parser('status', help='Print adoption state as JSON')

    parser_adopt = subparsers.add_parser(
        'set-adopt', help='Send adoption request to controller'
    )
    parser_adopt.add_argument('-s', type=str, help='Controller inform URL')
    parser_adopt.add_argument('-k', type=str, help='Auth key')

    args = parser.parse_args()

    if not args.command:
        parser.print_help()
        sys.exit(1)

    if args.command in ('start', 'run', 'set-adopt', 'dump'):
        ensure_config(CONFIG_FILE)

    gw = UnifiGateway()

    if args.command == 'start':
        gw.start()
    elif args.command == 'stop':
        gw.stop()
    elif args.command == 'restart':
        gw.restart()
    elif args.command == 'run':
        gw.run()
    elif args.command == 'dump':
        gw.dump()
    elif args.command == 'status':
        print(json.dumps(gw.status()))
    elif args.command == 'set-adopt':
        url = args.s
        if not url and gw.config.has_option('gateway', 'url'):
            url = gw.config.get('gateway', 'url')
        if not url:
            logger.error('No controller URL specified. Use -s <url>')
            sys.exit(1)
        key = args.k
        if not key and gw.config.has_option('provisioned', 'key'):
            key = gw.config.get('provisioned', 'key')
        gw.set_adopt(url, key)


if __name__ == '__main__':
    main()
