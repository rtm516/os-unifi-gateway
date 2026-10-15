# -*- coding: utf-8 -*-
"""Tests for UnifiGateway lifecycle and config handling."""
import configparser
import json
import sys
from unittest.mock import patch, MagicMock

import pytest

from tests.conftest import FakeAPI


@pytest.fixture
def _mock_daemon():
    """Replace Daemon base class with a stub that works on Windows."""
    class FakeDaemon:
        def __init__(self, pidfile='_.pid', **kw):
            self.pidfile = pidfile

        def stop(self):
            import os
            from signal import SIGTERM
            try:
                with open(self.pidfile, 'r') as pf:
                    pid = int(pf.read().strip())
            except (IOError, FileNotFoundError):
                pid = None
            if not pid:
                return
            try:
                os.kill(pid, SIGTERM)
            except OSError:
                if os.path.exists(self.pidfile):
                    os.remove(self.pidfile)

        def restart(self):
            self.stop()
            self.start()

        def start(self):
            pass

    with patch.dict(sys.modules, {'daemon': MagicMock(Daemon=FakeDaemon)}):
        if 'unifi_gateway' in sys.modules:
            del sys.modules['unifi_gateway']
        import unifi_gateway
        yield unifi_gateway
    if 'unifi_gateway' in sys.modules:
        del sys.modules['unifi_gateway']


def _gateway(module, conf):
    with patch.object(module, 'CONFIG_FILE', str(conf)):
        return module.UnifiGateway()


def _read(path):
    cfg = configparser.RawConfigParser()
    cfg.read(str(path))
    return cfg


class TestStopWithoutRouter:
    """stop/restart only need a pid file; they never contact the router."""

    def test_init_with_empty_config(self, tmp_path, _mock_daemon):
        conf = tmp_path / 'empty.conf'
        conf.write_text('')
        gw = _gateway(_mock_daemon, conf)
        assert gw.datacollector is None
        assert gw.config.get('global', 'pid_file') == 'unifi-gateway.pid'
        assert gw.device.model == 'UXGPRO'

    def test_stop_reads_pidfile_and_kills(self, tmp_path, _mock_daemon):
        pid_file = tmp_path / 'test.pid'
        conf = tmp_path / 'minimal.conf'
        conf.write_text('[global]\npid_file = %s\n' % pid_file)
        pid_file.write_text('99999\n')
        gw = _gateway(_mock_daemon, conf)

        with patch('os.kill') as mock_kill:
            mock_kill.side_effect = OSError('No such process')
            gw.stop()

        mock_kill.assert_called_once_with(99999, 15)  # SIGTERM = 15

    def test_restart_does_not_collect(self, tmp_path, _mock_daemon):
        conf = tmp_path / 'minimal.conf'
        conf.write_text('[global]\npid_file = %s\n' % (tmp_path / 'test.pid'))
        gw = _gateway(_mock_daemon, conf)
        with patch.object(gw, 'start'):
            gw.restart()
        assert gw.datacollector is None


class TestConfigFile:

    def test_created_when_missing(self, tmp_path, _mock_daemon, monkeypatch):
        monkeypatch.setenv('OPNSENSE_HOST', '192.168.48.254')
        conf = tmp_path / 'sub' / 'unifi-gateway.conf'
        _mock_daemon.ensure_config(str(conf))
        cfg = _read(conf)
        assert cfg.sections() == ['opnsense']
        assert cfg.get('opnsense', 'host') == '192.168.48.254'

    def test_existing_file_untouched(self, tmp_path, _mock_daemon):
        conf = tmp_path / 'unifi-gateway.conf'
        conf.write_text('[opnsense]\nhost = 1.2.3.4\n')
        _mock_daemon.ensure_config(str(conf))
        assert conf.read_text() == '[opnsense]\nhost = 1.2.3.4\n'

    def test_save_keeps_runtime_values_out(self, tmp_path, _mock_daemon, monkeypatch):
        monkeypatch.setenv('OPNSENSE_API_SECRET', 'from-env')
        conf = tmp_path / 'unifi-gateway.conf'
        conf.write_text('[opnsense]\nhost = 192.168.48.254\napi_key = k\napi_secret =\n')
        gw = _gateway(_mock_daemon, conf)
        assert gw.config.get('opnsense', 'api_secret') == 'from-env'

        with patch('unifi_gateway.create_collector', return_value=_collector(gw)):
            gw._init_collector()
        assert gw.config.get('gateway', 'lan_mac') == '00:00:5e:00:53:01'

        gw.config.set('gateway', 'is_adopted', 'True')
        gw.config.set('gateway', 'url', 'http://controller:8080/inform')
        with patch.object(_mock_daemon, 'CONFIG_FILE', str(conf)):
            gw._save_config()

        saved = _read(conf)
        assert saved.get('opnsense', 'api_secret') == ''
        assert saved.get('gateway', 'is_adopted') == 'True'
        assert saved.get('gateway', 'url') == 'http://controller:8080/inform'
        for option in ('lan_mac', 'lan_ip', 'use_aes_gcm'):
            assert not saved.has_option('gateway', option)
        assert not saved.has_section('global')


class TestSetparam:

    def test_system_cfg_is_not_persisted(self, tmp_path, _mock_daemon):
        import json
        conf = tmp_path / 'unifi-gateway.conf'
        conf.write_text('[gateway]\nis_adopted = True\n')
        gw = _gateway(_mock_daemon, conf)
        system_cfg = json.dumps({'udapi': {'token': 'secret' * 200}, 'unifi': {}})
        with patch.object(_mock_daemon, 'CONFIG_FILE', str(conf)), \
             patch.object(_mock_daemon, 'UNHANDLED_LOG', str(tmp_path / 'unhandled.json')):
            gw._handle_response({'_type': 'setparam', 'cfgversion': 'abc',
                                 'system_cfg': system_cfg})
        saved = _read(conf)
        assert saved.get('provisioned', 'cfgversion') == 'abc'
        assert not saved.has_option('provisioned', 'system_cfg')
        logged = json.loads((tmp_path / 'unhandled.json').read_text())
        assert logged['setparam/system_cfg']['payload']['value'] == {
            '_truncated': len(system_cfg), 'keys': ['udapi', 'unifi']}

    def test_summarise_keeps_short_values(self, _mock_daemon):
        assert _mock_daemon._summarise('short') == 'short'
        assert _mock_daemon._summarise('{not json' + 'x' * 600)['keys'] == []


def _collector(gw):
    from collectors.api_source import APISource
    from collectors.opnsense import OPNsenseCollector
    return OPNsenseCollector(gw.config, APISource(FakeAPI()), gw.device.wan_port, gw.device.lan_port)


class TestRun:

    def test_run_initializes_collector(self, tmp_path, _mock_daemon):
        conf = tmp_path / 'full.conf'
        conf.write_text('[global]\npid_file = %s\n' % (tmp_path / 'test.pid'))
        gw = _gateway(_mock_daemon, conf)

        def stop_after_init():
            gw.running = False
        with patch.object(gw, '_init_collector', side_effect=stop_after_init) as mock_init:
            gw.run()
        mock_init.assert_called_once()

    def test_config_error_stops_run(self, tmp_path, _mock_daemon):
        conf = tmp_path / 'full.conf'
        conf.write_text('[opnsense]\nhost =\n')
        gw = _gateway(_mock_daemon, conf)
        with patch.object(_mock_daemon, 'CONFIG_FILE', str(conf)):
            assert gw._wait_for_collector() is False

    def test_api_error_retries(self, tmp_path, _mock_daemon):
        from collectors import SourceError
        conf = tmp_path / 'full.conf'
        conf.write_text('')
        gw = _gateway(_mock_daemon, conf)
        attempts = [SourceError('/api/x', 403, 'Forbidden'), None]

        def init():
            error = attempts.pop(0)
            if error:
                raise error
        with patch.object(gw, '_init_collector', side_effect=init), \
             patch('unifi_gateway.time.sleep') as sleep:
            assert gw._wait_for_collector() is True
        sleep.assert_called_once_with(30)


class TestSpeedTest:

    def test_results_live_next_to_config(self, tmp_path, _mock_daemon):
        conf = tmp_path / 'unifi-gateway.conf'
        conf.write_text('')
        gw = _gateway(_mock_daemon, conf)
        assert gw.speedtest.results_file == str(tmp_path / 'speedtest.json')

    def test_results_file_override(self, tmp_path, _mock_daemon):
        conf = tmp_path / 'unifi-gateway.conf'
        conf.write_text('[gateway]\nspeedtest_file = %s\n' % (tmp_path / 'st.json'))
        gw = _gateway(_mock_daemon, conf)
        assert gw.speedtest.results_file == str(tmp_path / 'st.json')

    def test_command_starts_test(self, tmp_path, _mock_daemon):
        conf = tmp_path / 'unifi-gateway.conf'
        conf.write_text('')
        gw = _gateway(_mock_daemon, conf)
        with patch.object(gw.speedtest, 'start') as start:
            gw._handle_response({'_type': 'cmd', 'cmd': 'speed-test', 'use_alert': False,
                                 'up_duration': 7, 'down_duration': 12})
        start.assert_called_once_with(down_duration=12, up_duration=7)

    def test_inform_carries_status(self, tmp_path, _mock_daemon):
        conf = tmp_path / 'unifi-gateway.conf'
        conf.write_text('[gateway]\nis_adopted = True\nurl = http://192.0.2.10:8080/inform\n')
        gw = _gateway(_mock_daemon, conf)
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

    def test_informs_faster_while_testing(self, tmp_path, _mock_daemon):
        conf = tmp_path / 'unifi-gateway.conf'
        conf.write_text('')
        gw = _gateway(_mock_daemon, conf)
        assert gw._inform_delay() == 10
        with patch.object(type(gw.speedtest), 'active', new=True):
            assert gw._inform_delay() == _mock_daemon.SPEEDTEST_INFORM_INTERVAL
            gw.interval = 1
            assert gw._inform_delay() == 1


class TestAdoptedModel:
    """Gateways adopted as a UXG Lite by older versions stay Lites in the
    controller until they're forgotten and adopted again."""

    def _warnings(self, module, conf, caplog):
        gw = _gateway(module, conf)
        caplog.clear()
        with caplog.at_level('WARNING', logger='unifi-gateway'):
            gw._check_adopted_model()
        return [r.getMessage() for r in caplog.records]

    def test_warns_for_gateway_adopted_by_older_version(self, tmp_path, _mock_daemon, caplog):
        conf = tmp_path / 'unifi-gateway.conf'
        conf.write_text('[gateway]\nis_adopted = True\n')
        messages = self._warnings(_mock_daemon, conf, caplog)
        assert len(messages) == 1
        assert 'adopted as a UXG' in messages[0] and 'adopt it again' in messages[0]

    def test_quiet_when_adopted_as_current_model(self, tmp_path, _mock_daemon, caplog):
        conf = tmp_path / 'unifi-gateway.conf'
        conf.write_text('[gateway]\nis_adopted = True\nadopted_model = UXGPRO\n')
        assert self._warnings(_mock_daemon, conf, caplog) == []

    def test_adoption_records_model_and_reset_clears_it(self, tmp_path, _mock_daemon):
        conf = tmp_path / 'unifi-gateway.conf'
        conf.write_text('[gateway]\nis_adopted = False\nurl = http://192.0.2.10:8080/inform\n')
        gw = _gateway(_mock_daemon, conf)
        gw.datacollector = _collector(gw)
        with patch.object(_mock_daemon, 'CONFIG_FILE', str(conf)), \
             patch.object(gw, '_create_inform', return_value='{}'), \
             patch.object(gw, '_send_inform', return_value={'_type': 'setparam'}):
            gw.set_adopt('http://192.0.2.10:8080/inform', None)
            assert _read(conf).get('gateway', 'adopted_model') == 'UXGPRO'
            gw._handle_response({'_type': 'setdefault'})
        assert not _read(conf).has_option('gateway', 'adopted_model')
