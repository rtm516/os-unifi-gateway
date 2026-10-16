# -*- coding: utf-8 -*-
"""Tests for UnifiGateway lifecycle and config handling."""
import configparser
import json
import sys
from unittest.mock import patch

import pytest

from tests.conftest import FakeAPI


@pytest.fixture
def gateway_module():
    """A fresh import of unifi_gateway, so module-level state (CONFIG_FILE,
    logging) is set up per test."""
    sys.modules.pop('unifi_gateway', None)
    import unifi_gateway
    yield unifi_gateway
    sys.modules.pop('unifi_gateway', None)


def _gateway(module, conf):
    with patch.object(module, 'CONFIG_FILE', str(conf)):
        return module.UnifiGateway()


def _read(path):
    cfg = configparser.RawConfigParser()
    cfg.read(str(path))
    return cfg


class TestInit:

    def test_init_with_empty_config(self, tmp_path, gateway_module):
        conf = tmp_path / 'empty.conf'
        conf.write_text('')
        gw = _gateway(gateway_module, conf)
        assert gw.datacollector is None
        assert gw.device.model == 'UXGPRO'


class TestConfigFile:

    def test_created_when_missing(self, tmp_path, gateway_module, monkeypatch):
        monkeypatch.setenv('OPNSENSE_HOST', '192.168.48.254')
        conf = tmp_path / 'sub' / 'unifi-gateway.conf'
        gateway_module.ensure_config(str(conf))
        cfg = _read(conf)
        assert cfg.sections() == ['opnsense']
        assert cfg.get('opnsense', 'host') == '192.168.48.254'

    def test_existing_file_untouched(self, tmp_path, gateway_module):
        conf = tmp_path / 'unifi-gateway.conf'
        conf.write_text('[opnsense]\nhost = 1.2.3.4\n')
        gateway_module.ensure_config(str(conf))
        assert conf.read_text() == '[opnsense]\nhost = 1.2.3.4\n'

    def test_save_keeps_runtime_values_out(self, tmp_path, gateway_module, monkeypatch):
        monkeypatch.setenv('OPNSENSE_API_SECRET', 'from-env')
        conf = tmp_path / 'unifi-gateway.conf'
        conf.write_text('[opnsense]\nhost = 192.168.48.254\napi_key = k\napi_secret =\n')
        gw = _gateway(gateway_module, conf)
        assert gw.config.get('opnsense', 'api_secret') == 'from-env'

        with patch('unifi_gateway.create_collector', return_value=_collector(gw)):
            gw._init_collector()
        assert gw.config.get('gateway', 'lan_mac') == '00:00:5e:00:53:01'

        gw.config.set('gateway', 'is_adopted', 'True')
        gw.config.set('gateway', 'url', 'http://controller:8080/inform')
        with patch.object(gateway_module, 'CONFIG_FILE', str(conf)):
            gw._save_config()

        saved = _read(conf)
        assert saved.get('opnsense', 'api_secret') == ''
        assert saved.get('gateway', 'is_adopted') == 'True'
        assert saved.get('gateway', 'url') == 'http://controller:8080/inform'
        for option in ('lan_mac', 'lan_ip', 'use_aes_gcm'):
            assert not saved.has_option('gateway', option)
        assert not saved.has_section('global')


class TestSetparam:

    def test_system_cfg_is_not_persisted(self, tmp_path, gateway_module):
        import json
        conf = tmp_path / 'unifi-gateway.conf'
        conf.write_text('[gateway]\nis_adopted = True\n')
        gw = _gateway(gateway_module, conf)
        system_cfg = json.dumps({'udapi': {'token': 'secret' * 200}, 'unifi': {}})
        with patch.object(gateway_module, 'CONFIG_FILE', str(conf)), \
             patch.object(gateway_module, 'UNHANDLED_LOG', str(tmp_path / 'unhandled.json')):
            gw._handle_response({'_type': 'setparam', 'cfgversion': 'abc',
                                 'system_cfg': system_cfg})
        saved = _read(conf)
        assert saved.get('provisioned', 'cfgversion') == 'abc'
        assert not saved.has_option('provisioned', 'system_cfg')
        logged = json.loads((tmp_path / 'unhandled.json').read_text())
        assert logged['setparam/system_cfg']['payload']['value'] == {
            '_truncated': len(system_cfg), 'keys': ['udapi', 'unifi']}

    def test_cfgversion_and_interval_are_handled(self, tmp_path, gateway_module):
        import json
        conf = tmp_path / 'unifi-gateway.conf'
        conf.write_text('[gateway]\nis_adopted = True\n')
        gw = _gateway(gateway_module, conf)
        unhandled = tmp_path / 'unhandled.json'
        with patch.object(gateway_module, 'CONFIG_FILE', str(conf)), \
             patch.object(gateway_module, 'UNHANDLED_LOG', str(unhandled)):
            gw._handle_response({'_type': 'setparam', 'cfgversion': 'abc', 'interval': 30})
        assert gw.interval == 30
        assert _read(conf).get('provisioned', 'cfgversion') == 'abc'
        assert not unhandled.exists() or json.loads(unhandled.read_text()) == {}

    def test_summarise_keeps_short_values(self, gateway_module):
        assert gateway_module._summarise('short') == 'short'
        assert gateway_module._summarise('{not json' + 'x' * 600)['keys'] == []



class TestSendInform:

    def _send(self, tmp_path, module, post):
        conf = tmp_path / 'unifi-gateway.conf'
        conf.write_text('[gateway]\nurl = http://controller:8080/inform\n')
        gw = _gateway(module, conf)
        with patch.object(module.requests, 'post', post), \
             patch.object(module, 'encode_inform', return_value=b'packet'), \
             patch.object(module, 'decode_inform', side_effect=lambda config, body: {'body': body}):
            return gw._send_inform({})

    def test_posts_the_packet(self, tmp_path, gateway_module):
        import requests
        seen = {}

        def post(url, data=None, headers=None, timeout=None):
            seen.update(url=url, data=data, headers=headers)
            response = requests.Response()
            response.status_code, response._content = 200, b'reply'
            return response
        result = self._send(tmp_path, gateway_module, post)
        assert result == {'body': b'reply'}
        assert seen['url'] == 'http://controller:8080/inform'
        assert seen['data'] == b'packet'
        assert seen['headers']['Content-Type'] == 'application/x-binary'

    def test_http_error(self, tmp_path, gateway_module):
        import requests

        def post(url, **kwargs):
            response = requests.Response()
            response.status_code, response.reason, response._content = 404, 'Not Found', b''
            return response
        result = self._send(tmp_path, gateway_module, post)
        assert result == {'_type': 'httperror', 'code': '404', 'msg': 'Not Found'}

    def test_connection_error(self, tmp_path, gateway_module):
        import requests

        def post(url, **kwargs):
            raise requests.ConnectionError('refused')
        result = self._send(tmp_path, gateway_module, post)
        assert result == {'_type': 'urlerror', 'msg': 'refused'}


def _collector(gw):
    from collectors.api_source import APISource
    from collectors.opnsense import OPNsenseCollector
    return OPNsenseCollector(gw.config, APISource(FakeAPI()), gw.device.wan_port, gw.device.lan_port)


class TestRun:

    def test_run_initializes_collector(self, tmp_path, gateway_module):
        conf = tmp_path / 'full.conf'
        conf.write_text('')
        gw = _gateway(gateway_module, conf)

        def stop_after_init():
            gw.running = False
        with patch.object(gw, '_init_collector', side_effect=stop_after_init) as mock_init:
            gw.run()
        mock_init.assert_called_once()

    def test_config_error_stops_run(self, tmp_path, gateway_module):
        conf = tmp_path / 'full.conf'
        conf.write_text('[opnsense]\nhost =\n')
        gw = _gateway(gateway_module, conf)
        with patch.object(gateway_module, 'CONFIG_FILE', str(conf)):
            assert gw._wait_for_collector() is False

    def test_api_error_retries(self, tmp_path, gateway_module):
        from collectors import SourceError
        conf = tmp_path / 'full.conf'
        conf.write_text('')
        gw = _gateway(gateway_module, conf)
        attempts = [SourceError('/api/x', 403, 'Forbidden'), None]

        def init():
            error = attempts.pop(0)
            if error:
                raise error
        with patch.object(gw, '_init_collector', side_effect=init), \
             patch.object(gw, '_sleep') as sleep:
            assert gw._wait_for_collector() is True
        sleep.assert_called_once_with(30)

    def test_sleep_runs_full_interval(self, tmp_path, gateway_module):
        conf = tmp_path / 'full.conf'
        conf.write_text('')
        gw = _gateway(gateway_module, conf)
        clock = [0.0]

        def sleep(seconds):
            clock[0] += seconds
        with patch('unifi_gateway.time.monotonic', side_effect=lambda: clock[0]),              patch('unifi_gateway.time.sleep', side_effect=sleep):
            gw._sleep(10)
        assert clock[0] == 10

    def test_signal_ends_sleep(self, tmp_path, gateway_module):
        """SIGTERM must not wait out the interval: the rc.d script gives the
        daemon 10 s to exit."""
        conf = tmp_path / 'full.conf'
        conf.write_text('')
        gw = _gateway(gateway_module, conf)
        clock = [0.0]

        def sleep(seconds):
            clock[0] += seconds
            if clock[0] >= 2:
                gw._handle_signal(15, None)
        with patch('unifi_gateway.time.monotonic', side_effect=lambda: clock[0]),              patch('unifi_gateway.time.sleep', side_effect=sleep):
            gw._sleep(60)
        assert clock[0] <= 2.5


class TestSpeedTest:

    def test_results_live_next_to_config(self, tmp_path, gateway_module):
        conf = tmp_path / 'unifi-gateway.conf'
        conf.write_text('')
        gw = _gateway(gateway_module, conf)
        assert gw.speedtest.results_file == str(tmp_path / 'speedtest.json')

    def test_results_file_override(self, tmp_path, gateway_module):
        conf = tmp_path / 'unifi-gateway.conf'
        conf.write_text('[gateway]\nspeedtest_file = %s\n' % (tmp_path / 'st.json'))
        gw = _gateway(gateway_module, conf)
        assert gw.speedtest.results_file == str(tmp_path / 'st.json')

    def test_command_starts_test(self, tmp_path, gateway_module):
        conf = tmp_path / 'unifi-gateway.conf'
        conf.write_text('')
        gw = _gateway(gateway_module, conf)
        with patch.object(gw.speedtest, 'start') as start:
            gw._handle_response({'_type': 'cmd', 'cmd': 'speed-test', 'use_alert': False,
                                 'up_duration': 7, 'down_duration': 12})
        start.assert_called_once_with(down_duration=12, up_duration=7)

    def test_inform_carries_status(self, tmp_path, gateway_module):
        conf = tmp_path / 'unifi-gateway.conf'
        conf.write_text('[gateway]\nis_adopted = True\nurl = http://192.0.2.10:8080/inform\n')
        gw = _gateway(gateway_module, conf)
        gw.datacollector = _collector(gw)
        gw.config.set('gateway', 'lan_mac', gw.datacollector.lan_mac)
        gw.config.set('gateway', 'lan_ip', gw.datacollector.lan_ip)
        status = {'status_summary': 1, 'rundate': 1700000000, 'xput_download': 5}
        isp = {'isp_name': 'Example Broadband', 'isp_organization': 'Example Broadband', 'asn': 64500}
        with patch.object(gw.speedtest, 'status', return_value=status), \
             patch.object(gw.isp, 'current', return_value=isp) as current, \
             patch('devices.fetch_latest_firmware', return_value='9.9.9.99999'):
            payload = json.loads(gw._create_inform())
        assert payload['speedtest-status']['status_summary'] == 1
        assert payload['speedtest-status']['xput_download'] == 5
        current.assert_called_once_with('81.2.3.4')  # the WAN address, from OPNsense
        assert payload['geo_info'] == {'WAN': isp}

    def test_informs_faster_while_testing(self, tmp_path, gateway_module):
        conf = tmp_path / 'unifi-gateway.conf'
        conf.write_text('')
        gw = _gateway(gateway_module, conf)
        assert gw._inform_delay() == 10
        with patch.object(type(gw.speedtest), 'active', new=True):
            assert gw._inform_delay() == gateway_module.TEST_INFORM_INTERVAL
            gw.interval = 1
            assert gw._inform_delay() == 1

    def test_live_updates_while_the_ui_is_open(self, tmp_path, gateway_module):
        conf = tmp_path / 'unifi-gateway.conf'
        conf.write_text('')
        gw = _gateway(gateway_module, conf)
        gw._handle_response({'_type': 'noop', 'interval': 10, 'live_update': True})
        assert gw._inform_delay() == gateway_module.LIVE_INFORM_INTERVAL
        gw._handle_response({'_type': 'noop', 'interval': 10, 'live_update': False})
        assert gw._inform_delay() == 10
        gw._handle_response({'_type': 'noop', 'interval': 10})
        assert gw._inform_delay() == 10


class TestAdoptedModel:
    """Gateways adopted as a UXG Lite by older versions stay Lites in the
    controller until they're forgotten and adopted again."""

    def _warnings(self, module, conf, caplog):
        gw = _gateway(module, conf)
        caplog.clear()
        with caplog.at_level('WARNING', logger='unifi-gateway'):
            gw._check_adopted_model()
        return [r.getMessage() for r in caplog.records]

    def test_warns_for_gateway_adopted_by_older_version(self, tmp_path, gateway_module, caplog):
        conf = tmp_path / 'unifi-gateway.conf'
        conf.write_text('[gateway]\nis_adopted = True\n')
        messages = self._warnings(gateway_module, conf, caplog)
        assert len(messages) == 1
        assert 'adopted as a UXG' in messages[0] and 'adopt it again' in messages[0]

    def test_quiet_when_adopted_as_current_model(self, tmp_path, gateway_module, caplog):
        conf = tmp_path / 'unifi-gateway.conf'
        conf.write_text('[gateway]\nis_adopted = True\nadopted_model = UXGPRO\n')
        assert self._warnings(gateway_module, conf, caplog) == []

    def test_adoption_records_model_and_reset_clears_it(self, tmp_path, gateway_module):
        conf = tmp_path / 'unifi-gateway.conf'
        conf.write_text('[gateway]\nis_adopted = False\nurl = http://192.0.2.10:8080/inform\n')
        gw = _gateway(gateway_module, conf)
        gw.datacollector = _collector(gw)
        with patch.object(gateway_module, 'CONFIG_FILE', str(conf)), \
             patch.object(gw, '_create_inform', return_value='{}'), \
             patch.object(gw, '_send_inform', return_value={'_type': 'setparam'}):
            gw.set_adopt('http://192.0.2.10:8080/inform', None)
            assert _read(conf).get('gateway', 'adopted_model') == 'UXGPRO'
            gw._handle_response({'_type': 'setdefault'})
        assert not _read(conf).has_option('gateway', 'adopted_model')
