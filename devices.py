# -*- coding: utf-8 -*-
"""The UniFi gateway we impersonate, and its latest firmware.

A UniFi Gateway Pro (UXGPRO) on UniFi OS firmware: supported by current
UniFi Network releases, unlike the USG line, which can only be adopted up to
UniFi Network 10.6. Not the Gateway Lite (UXG): the controller refuses to run
speed tests on that model (and on the USG and UniFi Express). The controller
ignores model codes it doesn't know, so it has to be a real one.

The Pro's ports, as the controller's device database lays them out: eth0 WAN
and eth1 LAN (1 GbE), eth2 WAN2 and eth3 LAN2 (10G SFP+). We report the first
two.
"""
import json
import logging
import time
import urllib.error
import urllib.request

logger = logging.getLogger('unifi-gateway')


FIRMWARE_URL = (
    'https://fw-update.ubnt.com/api/firmware-latest'
    '?filter=eq~~platform~~{platform}'
    '&filter=eq~~channel~~release'
    '&filter=eq~~product~~unifi-firmware'
)

# How often to look for a newer release
FIRMWARE_CHECK_INTERVAL = 24 * 3600
FIRMWARE_RETRY_INTERVAL = 3600


def parse_firmware_version(response):
    """'v5.1.26+33914' from fw-update -> '5.1.26.33914' as devices report it."""
    firmware = (response.get('_embedded') or {}).get('firmware') or []
    if not firmware:
        return None
    version = firmware[0].get('version', '')
    version = version.lstrip('v').replace('+', '.')
    return version or None


def fetch_latest_firmware(platform, timeout=10):
    url = FIRMWARE_URL.format(platform=platform)
    request = urllib.request.Request(url, headers={'Accept': 'application/json'})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return parse_firmware_version(json.loads(response.read().decode('utf-8')))


class Device:
    """The gateway model we present to the controller.

    Reports the newest released firmware for the model, unless the config
    pins `firmware` to a fixed version.
    """

    model = 'UXGPRO'
    display = 'UniFi Gateway Pro'
    # UXGPRO is the legacy 1.x firmware line; the controller treats a UXGPRO
    # reporting 1.13.6 or later as the UniFi OS model, UXGPROV2
    fw_platform = 'UXGPROV2'
    fallback_firmware = '5.1.26.33914'
    wan_port = 'eth0'
    lan_port = 'eth1'

    def __init__(self, config):
        self.config = config
        self._latest = None
        self._next_check = 0

    @property
    def pinned_firmware(self):
        value = self.config.get('gateway', 'firmware', fallback='latest').strip()
        return None if value.lower() in ('', 'latest') else value

    def refresh_firmware(self):
        """Look up the latest release again on the next firmware() call."""
        self._next_check = 0

    def firmware(self):
        if self.pinned_firmware:
            return self.pinned_firmware
        if time.time() >= self._next_check:
            try:
                latest = fetch_latest_firmware(self.fw_platform)
                self._next_check = time.time() + FIRMWARE_CHECK_INTERVAL
            except (urllib.error.URLError, OSError, ValueError) as e:
                logger.warning('Could not look up latest %s firmware: %s', self.model, e)
                latest = None
                self._next_check = time.time() + FIRMWARE_RETRY_INTERVAL
            if latest and latest != self._latest:
                logger.info('Reporting %s firmware %s', self.model, latest)
                self._latest = latest
        return self._latest or self.fallback_firmware
