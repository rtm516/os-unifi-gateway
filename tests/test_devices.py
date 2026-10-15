# -*- coding: utf-8 -*-
"""Emulated device profiles and firmware lookup."""
import json
from unittest.mock import patch

import requests

from devices import Device, fetch_latest_firmware, parse_firmware_version

FW_RESPONSE = {'_embedded': {'firmware': [{
    'version': 'v5.1.26+33914', 'platform': 'UXGPROV2', 'channel': 'release',
}]}}


def test_parse_firmware_version():
    assert parse_firmware_version(FW_RESPONSE) == '5.1.26.33914'
    assert parse_firmware_version({'_embedded': {'firmware': []}}) is None
    assert parse_firmware_version({}) is None


def test_fetch_queries_platform():
    seen = {}

    def get(url, headers=None, timeout=None):
        seen['url'] = url
        response = requests.Response()
        response.status_code, response._content = 200, json.dumps(FW_RESPONSE).encode()
        return response
    with patch('devices.requests.get', get):
        assert fetch_latest_firmware('UXGPROV2') == '5.1.26.33914'
    assert 'filter=eq~~platform~~UXGPROV2' in seen['url']
    assert 'filter=eq~~channel~~release' in seen['url']


def test_emulates_uxg_pro(gateway_config):
    """Not the UXG Lite: the controller refuses speed tests on it."""
    device = Device(gateway_config)
    assert (device.model, device.display) == ('UXGPRO', 'UniFi Gateway Pro')
    assert (device.wan_port, device.lan_port) == ('eth0', 'eth1')


def test_reports_latest_and_caches(gateway_config):
    device = Device(gateway_config)
    with patch('devices.fetch_latest_firmware', return_value='5.1.26.33914') as fetch:
        assert device.firmware() == '5.1.26.33914'
        assert device.firmware() == '5.1.26.33914'
    fetch.assert_called_once_with('UXGPROV2')


def test_refresh_looks_up_again(gateway_config):
    device = Device(gateway_config)
    with patch('devices.fetch_latest_firmware', side_effect=['5.1.26.1', '5.2.0.2']):
        assert device.firmware() == '5.1.26.1'
        device.refresh_firmware()
        assert device.firmware() == '5.2.0.2'


def test_lookup_failure_keeps_last_known(gateway_config):
    device = Device(gateway_config)
    with patch('devices.fetch_latest_firmware', side_effect=requests.ConnectionError('offline')):
        assert device.firmware() == '5.1.26.33914'  # built-in fallback
    with patch('devices.fetch_latest_firmware', return_value='5.3.0.1'):
        device.refresh_firmware()
        assert device.firmware() == '5.3.0.1'
    with patch('devices.fetch_latest_firmware', side_effect=requests.ConnectionError('offline')):
        device.refresh_firmware()
        assert device.firmware() == '5.3.0.1'


def test_pinned_firmware_skips_lookup(gateway_config):
    gateway_config.set('gateway', 'firmware', '5.0.16.30689')
    device = Device(gateway_config)
    with patch('devices.fetch_latest_firmware') as fetch:
        assert device.firmware() == '5.0.16.30689'
    fetch.assert_not_called()
