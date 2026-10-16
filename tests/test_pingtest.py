# -*- coding: utf-8 -*-
"""The controller's client Ping Test: sessions, both router sources, and how
the daemon takes commands and reports back. No real pings."""
import io
import json
import sys
import time
from unittest.mock import patch

import pytest

import pingtest
from collectors import local_source
from collectors.api_source import APISource
from collectors.local_source import LocalSource
from collectors.opnsense_api import OPNsenseAPIError
from collectors.source import PingJob, SourceError
from pingtest import CANCELLED, FAILED, FINISHED, RUNNING, PingSession, PingTests


class ScriptedJob(PingJob):
    """Answers poll() from a list of (transmitted, replies, done)."""

    def __init__(self, steps):
        self.steps = list(steps)
        self.stopped = False

    def poll(self):
        if len(self.steps) > 1:
            return self.steps.pop(0)
        return self.steps[0]

    def stop(self):
        self.stopped = True


class ScriptedSource:

    def __init__(self, steps=None, error=None):
        self.steps = steps
        self.error = error
        self.jobs = []
        self.calls = []

    def ping(self, destination, count, interval, packet_size):
        self.calls.append((destination, count, interval, packet_size))
        if self.error:
            raise self.error
        job = ScriptedJob(self.steps)
        self.jobs.append(job)
        return job


def _wait_until(condition, timeout=5):
    deadline = time.monotonic() + timeout
    while not condition():
        assert time.monotonic() < deadline, 'timed out'
        time.sleep(0.01)


@pytest.fixture(autouse=True)
def fast_polls():
    with patch.object(pingtest, 'POLL_INTERVAL', 0.01):
        yield


class TestSession:

    def test_report_counts_lost_packets(self):
        session = PingSession('192.168.1.10', 30, 1, 56)
        session.add(3, [{'id': 0, 'time': 0.5, 'ttl': 64}, {'id': 2, 'time': 1.5, 'ttl': 63}])
        report = session.report()
        assert report == {
            'address': '192.168.1.10', 'status': RUNNING,
            'transmittedCount': 3, 'receivedCount': 2,
            'timeMin': 0.5, 'timeMax': 1.5, 'timeAvg': 1.0,
            'replies': [
                {'id': 0, 'address': '192.168.1.10', 'time': 0.5, 'ttl': 64},
                {'id': 2, 'address': '192.168.1.10', 'time': 1.5, 'ttl': 63},
            ],
        }

    def test_no_times_before_the_first_reply(self):
        session = PingSession('192.168.1.10', 30, 1, 56)
        session.add(1, [])
        report = session.report()
        assert report['transmittedCount'] == 1 and report['receivedCount'] == 0
        assert 'timeAvg' not in report and report['replies'] == []

    def test_transmitted_never_goes_back_or_past_count(self):
        session = PingSession('192.168.1.10', 3, 1, 56)
        session.add(2, [])
        session.add(1, [{'id': 2, 'time': 1.0, 'ttl': 64}])
        assert session.transmitted == 3
        session.add(9, [])
        assert session.transmitted == 3


class TestPingTests:

    def test_runs_to_the_end_and_reports_the_result_once(self):
        source = ScriptedSource([
            (1, [{'id': 0, 'time': 0.4, 'ttl': 64}], False),
            (2, [{'id': 1, 'time': 0.6, 'ttl': 64}], True),
        ])
        tests = PingTests(source)
        tests.start('192.168.1.10', count=2, interval=1, packet_size=56)
        assert tests.active
        _wait_until(lambda: tests._sessions['192.168.1.10'].status != RUNNING)
        assert source.calls == [('192.168.1.10', 2, 1, 56)]
        assert source.jobs[0].stopped
        [report] = tests.reports()
        assert report['status'] == FINISHED
        assert report['transmittedCount'] == 2 and report['receivedCount'] == 2
        assert report['timeAvg'] == 0.5
        assert tests.reports() == [] and not tests.active

    def test_running_tests_are_reported_every_time(self):
        tests = PingTests(ScriptedSource([(1, [{'id': 0, 'time': 0.4, 'ttl': 64}], False)]))
        tests.start('192.168.1.10')
        _wait_until(lambda: tests._sessions['192.168.1.10'].transmitted == 1)
        assert tests.reports()[0]['status'] == RUNNING
        assert tests.reports()[0]['status'] == RUNNING
        tests.stop('192.168.1.10')
        _wait_until(lambda: tests._sessions['192.168.1.10'].status != RUNNING)
        assert tests.reports()[0]['status'] == CANCELLED
        assert not tests.active

    def test_defaults_and_limits_for_odd_commands(self):
        source = ScriptedSource([(0, [], True)])
        tests = PingTests(source)
        tests.start('192.168.1.10', count='lots', interval=0, packet_size=None)
        tests.start('')
        _wait_until(lambda: source.calls)
        assert source.calls == [('192.168.1.10', 30, 1, 56)]

    def test_source_error_fails_the_test(self):
        error = OPNsenseAPIError('/api/diagnostics/ping/set', 403, 'Forbidden')
        error.hint = 'grant the "Diagnostics: Ping" privilege'
        tests = PingTests(ScriptedSource(error=error))
        tests.start('192.168.1.10')
        _wait_until(lambda: tests._sessions['192.168.1.10'].status != RUNNING)
        assert tests.reports()[0]['status'] == FAILED

    def test_gives_up_on_a_ping_that_never_ends(self):
        tests = PingTests(ScriptedSource([(1, [], False)]))
        with patch.object(pingtest, 'GRACE', -1):
            tests.start('192.168.1.10', count=1, interval=1)
            _wait_until(lambda: tests._sessions['192.168.1.10'].status != RUNNING)
        assert tests.reports()[0]['status'] == FINISHED


# ── LocalSource: /sbin/ping ──────────────────────────────────────────────

PING_OUTPUT = """PING 192.168.1.10 (192.168.1.10): 56 data bytes
64 bytes from 192.168.1.10: icmp_seq=0 ttl=64 time=0.412 ms
64 bytes from 192.168.1.10: icmp_seq=2 ttl=64 time=1.250 ms

--- 192.168.1.10 ping statistics ---
3 packets transmitted, 2 packets received, 33.3% packet loss
round-trip min/avg/max/stddev = 0.412/0.831/1.250/0.419 ms
"""


class FakePopen:

    def __init__(self, output, returncode=0):
        self.stdout = io.StringIO(output)
        self.returncode = returncode
        self.args = None
        self.terminated = False

    def __call__(self, args, **kwargs):
        self.args = args
        return self

    def poll(self):
        return self.returncode

    def wait(self, timeout=None):
        return self.returncode

    def terminate(self):
        self.terminated = True


def _local_ping(popen, *args):
    with patch.object(local_source.subprocess, 'Popen', popen):
        job = LocalSource().ping(*args)
    job._reader.join(5)
    return job


def test_local_ping_reads_replies_and_summary():
    popen = FakePopen(PING_OUTPUT, returncode=2)
    job = _local_ping(popen, '192.168.1.10', 3, 1, 56)
    assert popen.args == ['/sbin/ping', '-n', '-c', '3', '-i', '1', '-s', '56', '192.168.1.10']
    transmitted, replies, done = job.poll()
    assert (transmitted, done) == (3, True)
    assert replies == [{'id': 0, 'ttl': 64, 'time': 0.412}, {'id': 2, 'ttl': 64, 'time': 1.25}]
    job.stop()
    assert not popen.terminated   # already over


def test_local_ping_counts_unanswered_packets_by_time():
    job = _local_ping(FakePopen('PING 192.168.1.10 (192.168.1.10): 56 data bytes\n'),
                      '192.168.1.10', 30, 1, 56)
    job._reader = type('Alive', (), {'is_alive': lambda self: True})()
    job.started = time.monotonic() - 3.5   # packets 0-2 are over a second old
    transmitted, replies, done = job.poll()
    assert (transmitted, replies, done) == (3, [], False)


def test_local_ping_that_cannot_start_is_an_error():
    job = _local_ping(FakePopen('ping: cannot resolve nowhere: Unknown host\n', returncode=68),
                      'nowhere', 30, 1, 56)
    with pytest.raises(SourceError, match='cannot resolve'):
        job.poll()


def test_local_ping_ipv6_replies():
    line = '16 bytes from 2001:db8::10, icmp_seq=4 hlim=64 time=0.301 ms\n'
    job = _local_ping(FakePopen(line), '2001:db8::10', 30, 1, 56)
    job._reader = type('Alive', (), {'is_alive': lambda self: True})()
    assert job.poll()[1] == [{'id': 4, 'ttl': 64, 'time': 0.301}]


# ── APISource: Diagnostics > Ping jobs ───────────────────────────────────

class PingAPI:
    """The /api/diagnostics/ping endpoints, with job totals from `totals`."""

    def __init__(self, totals, forbidden=False):
        self.totals = list(totals)
        self.forbidden = forbidden
        self.calls = []

    def post(self, path, body=None):
        self.calls.append((path, body))
        if self.forbidden:
            raise OPNsenseAPIError(path, 403, 'Forbidden')
        if path == '/api/diagnostics/ping/set':
            return {'result': 'ok', 'uuid': 'job-1'}
        if path == '/api/diagnostics/ping/start/job-1':
            return {'status': 'ok'}
        if path == '/api/diagnostics/ping/search_jobs':
            sent, received, avg = self.totals.pop(0) if len(self.totals) > 1 else self.totals[0]
            return {'rows': [
                {'id': 'other', 'status': 'running', 'send': 99},
                {'id': 'job-1', 'status': 'running', 'send': sent, 'received': received,
                 'avg': avg, 'hostname': '192.168.1.10'},
            ]}
        return {'status': 'ok'}

    def get(self, path):
        return self.post(path)


def test_api_ping_creates_and_cleans_up_a_job():
    api = PingAPI([(None, None, None)])
    job = APISource(api).ping('192.168.1.10', 30, 1, 56)
    assert api.calls[0] == ('/api/diagnostics/ping/set', {'ping': {'settings': {
        'hostname': '192.168.1.10', 'fam': 'ip', 'packetsize': '56', 'interval': '1',
        'description': 'UniFi ping test'}}})
    assert api.calls[1][0] == '/api/diagnostics/ping/start/job-1'
    assert job.poll() == (0, [], False)
    job.stop()
    assert [path for path, _ in api.calls[-2:]] == [
        '/api/diagnostics/ping/stop/job-1', '/api/diagnostics/ping/remove/job-1']


def test_api_ping_works_out_each_reply_from_the_totals():
    api = PingAPI([(1, 1, 0.5), (3, 1, 0.5), (4, 3, 1.0)])
    job = APISource(api).ping('192.168.1.10', 4, 1, 56)
    assert job.poll() == (1, [{'id': 0, 'time': 0.5, 'ttl': 64}], False)
    # one packet unanswered besides the one in flight: it's lost
    assert job.poll() == (3, [], False)
    # the average moved from 0.5 over 1 reply to 1.0 over 3
    sent, replies, done = job.poll()
    assert (sent, done) == (4, False)
    assert replies == [{'id': 2, 'time': 1.25, 'ttl': 64}, {'id': 3, 'time': 1.25, 'ttl': 64}]


def test_api_ping_late_reply_fills_a_lost_slot():
    api = PingAPI([(3, 1, 0.5), (3, 3, 0.5)])
    job = APISource(api).ping('192.168.1.10', 3, 1, 56)
    assert [r['id'] for r in job.poll()[1]] == [1]   # id 0 lost, so far
    sent, replies, done = job.poll()
    assert [r['id'] for r in replies] == [2, 0] and done


def test_api_ping_waits_briefly_for_the_last_reply():
    api = PingAPI([(2, 1, 0.5)])
    job = APISource(api).ping('192.168.1.10', 2, 1, 56)
    assert [job.poll()[2] for _ in range(3)] == [False, False, True]


def test_api_ping_without_privilege_names_it():
    with pytest.raises(SourceError) as e:
        APISource(PingAPI([], forbidden=True)).ping('192.168.1.10', 30, 1, 56)
    assert 'Diagnostics: Ping' in e.value.hint


def test_api_ping_rejected_settings():
    class Rejecting(PingAPI):
        def post(self, path, body=None):
            return {'result': 'failed', 'validations': {'ping.settings.hostname': 'invalid'}}
    with pytest.raises(SourceError, match='hostname'):
        APISource(Rejecting([])).ping('not a host', 30, 1, 56)


# ── The daemon ───────────────────────────────────────────────────────────

@pytest.fixture
def gateway_module():
    sys.modules.pop('unifi_gateway', None)
    import unifi_gateway
    yield unifi_gateway
    sys.modules.pop('unifi_gateway', None)


class TestGateway:

    def _gateway(self, gateway_module, tmp_path):
        from tests.test_gateway_lifecycle import _collector, _gateway
        conf = tmp_path / 'unifi-gateway.conf'
        conf.write_text('[gateway]\nis_adopted = True\nurl = http://192.0.2.10:8080/inform\n')
        gw = _gateway(gateway_module, conf)
        gw.datacollector = _collector(gw)
        gw.config.set('gateway', 'lan_mac', gw.datacollector.lan_mac)
        gw.config.set('gateway', 'lan_ip', gw.datacollector.lan_ip)
        return gw

    def test_commands_start_and_stop_tests(self, tmp_path, gateway_module):
        gw = self._gateway(gateway_module, tmp_path)
        with patch.object(gw.pingtest, 'start') as start, \
             patch.object(gw.pingtest, 'stop') as stop:
            gw._handle_response({'_type': 'cmd', 'cmd': 'ping-test-start', 'count': 30,
                                 'destination': '192.168.1.10', 'interval': 1, 'packetSize': 56})
            gw._handle_response({'_type': 'cmd', 'cmd': 'ping-test-stop',
                                 'destination': '192.168.1.10'})
        start.assert_called_once_with('192.168.1.10', count=30, interval=1, packet_size=56)
        stop.assert_called_once_with('192.168.1.10')
        assert 'command/ping-test-start' not in gw._unhandled

    def test_progress_goes_out_as_an_event_inform(self, tmp_path, gateway_module):
        gw = self._gateway(gateway_module, tmp_path)
        report = {'address': '192.168.1.10', 'status': RUNNING, 'transmittedCount': 1,
                  'receivedCount': 0, 'replies': []}
        sent = []
        with patch.object(gw.pingtest, 'reports', return_value=[report]), \
             patch.object(gw, '_send_inform', side_effect=lambda data: sent.append(data) or
                          {'_type': 'cmd', 'cmd': 'ping-test-stop', 'destination': '192.168.1.10'}), \
             patch.object(gw.pingtest, 'stop') as stop, \
             patch('devices.fetch_latest_firmware', return_value='9.9.9.99999'):
            gw._report_ping_tests()
        payload = json.loads(sent[0])
        assert payload['inform_as_notif'] is True and payload['notif_reason'] == 'event'
        assert payload['notif_payload'] == {'event_string': 'EVT_GW_PingtestStatus',
                                            'pingtest-status': [report]}
        assert payload['mac'] and payload['model'] == 'UXGPRO'
        stop.assert_called_once_with('192.168.1.10')   # its reply is handled too

    def test_nothing_sent_without_tests(self, tmp_path, gateway_module):
        gw = self._gateway(gateway_module, tmp_path)
        with patch.object(gw, '_send_inform') as send:
            gw._report_ping_tests()
        send.assert_not_called()

    def test_capability_and_faster_informs(self, tmp_path, gateway_module):
        gw = self._gateway(gateway_module, tmp_path)
        with patch('devices.fetch_latest_firmware', return_value='9.9.9.99999'):
            payload = json.loads(gw._create_inform())
        assert payload['usg2_caps'] & 1   # the controller's supportsPingTest()
        assert 'pingtest-status' not in payload
        assert gw.pingtest.source is None   # set when the collector starts
        with patch.object(type(gw.pingtest), 'active', new=True):
            assert gw._inform_delay() == gateway_module.TEST_INFORM_INTERVAL
