# -*- coding: utf-8 -*-
"""The controller-driven WAN speed test. No real network: HTTP is faked."""
import io
import json
from unittest.mock import patch

import pytest

import speedtest
from speedtest import (
    DONE, IDLE, RUNNING, OoklaBackend, SpeedTest, SpeedTestError, summarise,
)

# What speedtest.net's server list returns (nearest first)
SERVERS = [
    {'url': 'http://speedtest.example-hosting.net:8080/speedtest/upload.php', 'lat': '51.1000', 'lon': '-1.1000',
     'distance': 3, 'name': 'Otherton', 'country': 'United Kingdom', 'cc': 'GB',
     'sponsor': 'Example Hosting', 'id': '1001', 'https_functional': 1},
    {'url': 'http://speedtest.example-isp.net:8080/speedtest/upload.php', 'lat': '51.0000',
     'lon': '-1.0000', 'distance': 37, 'name': 'Exampleton', 'country': 'United Kingdom', 'cc': 'GB',
     'sponsor': 'Example ISP', 'id': '1002', 'https_functional': 1},
]


class FakeResponse:

    def __init__(self, status=200, body=b''):
        self.status = status
        self._body = io.BytesIO(body)

    def read(self, n=-1):
        return self._body.read(n)


class FakeConnection:
    """Stands in for http.client.HTTPSConnection to a speedtest.net server."""
    instances = []
    status = 200
    broken = set()   # hosts that refuse connections

    def __init__(self, host, port=None, timeout=None):
        if host in self.broken:
            raise OSError('connection refused')
        self.host, self.port = host, port
        self.requests = []
        self.sent = 0
        self.closed = False
        FakeConnection.instances.append(self)

    def request(self, method, url, headers=None):
        self.requests.append((method, url))

    def putrequest(self, method, url):
        self.requests.append((method, url))

    def putheader(self, name, value):
        pass

    def endheaders(self):
        pass

    def send(self, data):
        self.sent += len(data)

    def getresponse(self):
        method, url = self.requests[-1]
        if self.status != 200:
            return FakeResponse(self.status)
        if url == '/latency.txt':
            return FakeResponse(body=b'test=test\n')
        if url.startswith('/download?'):
            size = int(url.rsplit('size=', 1)[1])
            return FakeResponse(body=b'\0' * size)
        return FakeResponse(body=b'size=%d\n' % self.sent)

    def close(self):
        self.closed = True


def _server_list(servers):
    def urlopen(request, timeout=None):
        assert request.full_url == speedtest.SERVERS_URL
        assert request.get_header('User-agent') == 'unifi-gateway'  # 403 without one
        return io.BytesIO(json.dumps(servers).encode())
    return urlopen


@pytest.fixture
def fake_http():
    FakeConnection.instances = []
    FakeConnection.status = 200
    FakeConnection.broken = set()
    with patch('speedtest.http.client.HTTPSConnection', FakeConnection), \
            patch('speedtest.urllib.request.urlopen', _server_list(SERVERS)), \
            patch.object(speedtest, 'DOWNLOAD_BYTES', 256 * 1024), \
            patch.object(speedtest, 'UPLOAD_BYTES', 256 * 1024):
        yield FakeConnection


class FakeBackend:
    host = 'fake'

    def __init__(self, fail=None):
        self.fail = fail
        self.durations = []

    def latency(self):
        return 4.4, {'provider': 'Example ISP', 'country': 'Exampleton'}

    def download(self, duration, progress=None):
        if self.fail == 'download':
            raise SpeedTestError('no route to host')
        progress(400.0)
        self.durations.append(duration)
        return 912.345

    def upload(self, duration, progress=None):
        progress(50.0)
        self.durations.append(duration)
        return 105.678


def _finish(test):
    test._thread.join(5)
    assert not test._thread.is_alive()


class TestHelpers:

    @pytest.mark.parametrize('value,expected', [
        (None, 10), ('', 10), (0, 10), (-5, 10), ('abc', 10),
        (5, 10), (15, 15), ('20', 20), (3600, 30),
    ])
    def test_duration_is_clamped(self, value, expected):
        """The controller asks for 5 s, too short for fast links."""
        assert speedtest._duration(value) == expected

    def test_summarise_drops_ramp_up_and_peaks(self):
        # 10 samples: the 3 slowest (TCP ramp-up) and the fastest go
        samples = [100, 300, 600, 900, 900, 910, 905, 895, 900, 2000]
        assert summarise(samples) == pytest.approx((895 + 900 + 900 + 900 + 905 + 910) / 6)
        assert summarise([500]) == 500


class TestSpeedTest:

    def test_idle_without_result(self, tmp_path):
        test = SpeedTest(str(tmp_path / 'speedtest.json'), FakeBackend())
        status = test.status()
        assert status['status_summary'] == IDLE
        assert (status['xput_download'], status['rundate']) == (0, 0)

    def test_reports_running_then_result(self, tmp_path):
        results = tmp_path / 'speedtest.json'
        backend = FakeBackend()
        test = SpeedTest(str(results), backend)
        with patch.object(test, '_run'):  # hold it in the running state
            assert test.start(down_duration=15, up_duration=5)
            running = test.status()
        assert running['status_summary'] == RUNNING
        assert running['status_ping'] == RUNNING
        assert running['rundate'] > 0
        assert test.start() is False  # one at a time

        test._run(15, 5)
        status = test.status()
        assert status['status_summary'] == DONE
        assert [status[k] for k in ('status_ping', 'status_download', 'status_upload')] == [DONE] * 3
        assert (status['xput_download'], status['xput_upload'], status['latency']) == (912.35, 105.68, 4)
        assert status['rundate'] == running['rundate']
        assert status['server']['provider'] == 'Example ISP'
        assert backend.durations == [15, 5]

        end = (status['rundate'] + status['runtime']) * 1000
        assert status.pop('download-progress') == [{'records': [[end, 91235]]}]
        assert status.pop('upload-progress') == [{'records': [[end, 10568]]}]
        saved = json.loads(results.read_text())
        assert saved == status
        reloaded = SpeedTest(str(results), FakeBackend()).status()
        reloaded.pop('download-progress')
        reloaded.pop('upload-progress')
        assert reloaded == status

    def test_progress_while_running(self, tmp_path):
        test = SpeedTest(str(tmp_path / 'speedtest.json'), FakeBackend())
        seen = []

        def download(duration, progress=None):
            progress(321.0)
            seen.append(test.status())
            return 900.0
        test.backend.download = download
        test.start()
        _finish(test)
        assert seen[0]['status_summary'] == RUNNING
        assert (seen[0]['status_ping'], seen[0]['status_download']) == (DONE, RUNNING)
        assert seen[0]['status_upload'] == IDLE
        assert (seen[0]['latency'], seen[0]['xput_download']) == (4, 321.0)
        assert test.status()['status_summary'] == DONE

    def test_live_progress_samples(self, tmp_path):
        """Each status while running carries the samples taken since the last
        one, as the UI reads them: [{records: [[ms, Mbps x 100]]}]."""
        test = SpeedTest(str(tmp_path / 'speedtest.json'), FakeBackend())
        seen = []

        def download(duration, progress=None):
            progress(100.0)
            progress(200.0)
            seen.append(test.status())
            seen.append(test.status())  # nothing new: repeats the latest
            progress(300.0)
            seen.append(test.status())
            return 250.0
        test.backend.download = download
        assert not test.active
        test.start()
        _finish(test)

        values = [[v for _, v in s['download-progress'][0]['records']] for s in seen]
        assert values == [[10000, 20000], [20000], [30000]]
        assert 'upload-progress' not in seen[0]
        assert all(s['status_summary'] == RUNNING for s in seen)
        assert not test.active  # done, and it was reported running
        final = test.status()
        assert final['status_summary'] == DONE
        assert final['download-progress'][0]['records'][0][1] == 25000

    def test_reports_running_once_even_if_it_finished_first(self, tmp_path):
        """The controller only stores a result after it has seen the test running."""
        test = SpeedTest(str(tmp_path / 'speedtest.json'), FakeBackend())
        test.start()
        _finish(test)
        first, second = test.status(), test.status()
        assert first['status_summary'] == RUNNING
        assert first['xput_download'] == 912.35
        assert second['status_summary'] == DONE
        assert second['rundate'] == first['rundate']

    def test_failure_keeps_previous_result(self, tmp_path):
        results = tmp_path / 'speedtest.json'
        test = SpeedTest(str(results), FakeBackend())
        test.start()
        _finish(test)
        test.status()
        previous = test.status()

        test.backend = FakeBackend(fail='download')
        test.start()
        _finish(test)
        assert test.status() == previous
        saved = json.loads(results.read_text())
        assert saved == {k: v for k, v in previous.items() if not k.endswith('-progress')}

    def test_failure_without_result_goes_back_to_idle(self, tmp_path):
        results = tmp_path / 'speedtest.json'
        test = SpeedTest(str(results), FakeBackend(fail='download'))
        test.start()
        _finish(test)
        assert test.status()['status_summary'] == IDLE
        assert not results.exists()

    def test_ignores_speedtest_cli_results(self, tmp_path):
        results = tmp_path / 'speedtest.json'
        results.write_text(json.dumps({'ping': 9, 'download': 1e8, 'upload': 1e7}))
        assert SpeedTest(str(results), FakeBackend()).status()['status_summary'] == IDLE

    def test_unwritable_results_file_is_not_fatal(self, tmp_path):
        test = SpeedTest(str(tmp_path / 'missing' / 'speedtest.json'), FakeBackend())
        test.start()
        _finish(test)
        test.status()
        assert test.status()['xput_upload'] == 105.68


class TestOoklaBackend:

    def test_picks_lowest_latency_server(self, fake_http):
        """speedtest.net lists by distance; the test uses the fastest to reach."""
        backend = OoklaBackend()
        rtts = {'server-1001.prod.hosts.ooklaserver.net': [16.0] * 3,
                'server-1002.prod.hosts.ooklaserver.net': [4.0] * 3}
        real_pings = backend._pings

        def pings(host, count):
            real_pings(host, count)  # exercise the real requests too
            return rtts[host] if count == 3 else [4.2, 4.0, 4.1]
        with patch.object(backend, '_pings', side_effect=pings):
            latency, server = backend.latency()
        assert latency == 4.1
        assert backend.host == 'server-1002.prod.hosts.ooklaserver.net'
        assert server == {
            'provider': 'Example ISP', 'provider_url': 'https://www.speedtest.net/',
            'city': 'Exampleton', 'country': 'Exampleton', 'cc': 'GB', 'id': '1002',
            'lat': 51.0, 'lon': -1.0,
        }
        conn = fake_http.instances[0]
        assert conn.port == 8080
        assert conn.requests == [('GET', '/latency.txt')] * 4  # first one is the handshake

    def test_skips_servers_that_fail(self, fake_http):
        fake_http.broken = {'server-1001.prod.hosts.ooklaserver.net'}
        _, server = OoklaBackend().latency(count=2)
        assert server['provider'] == 'Example ISP'

    def test_no_server_answers(self, fake_http):
        fake_http.status = 503
        with pytest.raises(SpeedTestError, match='no speedtest.net server answered'):
            OoklaBackend().latency()

    def test_server_list_unavailable(self, fake_http):
        def urlopen(request, timeout=None):
            raise OSError('network is unreachable')
        with patch('speedtest.urllib.request.urlopen', urlopen):
            with pytest.raises(SpeedTestError, match='server list'):
                OoklaBackend().latency()
        with patch('speedtest.urllib.request.urlopen', _server_list([])):
            with pytest.raises(SpeedTestError, match='no servers'):
                OoklaBackend().latency()

    def test_download(self, fake_http):
        backend = OoklaBackend(streams=2)
        backend.host = 'server-1002.prod.hosts.ooklaserver.net'
        progress = []
        assert backend.download(1, progress.append) > 0
        assert len(progress) >= 2  # a sample every half second
        urls = {url for conn in fake_http.instances for _, url in conn.requests}
        assert all(u.startswith('/download?nocache=') and u.endswith('&size=%d' % (256 * 1024))
                   for u in urls)
        assert len(urls) > 1  # nocache differs per request
        assert {conn.host for conn in fake_http.instances} == {backend.host}
        assert all(conn.closed for conn in fake_http.instances)

    def test_upload(self, fake_http):
        backend = OoklaBackend(streams=2)
        backend.host = 'server-1002.prod.hosts.ooklaserver.net'
        assert backend.upload(1) > 0
        conns = fake_http.instances
        assert all(url.startswith('/upload?nocache=') for conn in conns for _, url in conn.requests)
        assert sum(conn.sent for conn in conns) >= 256 * 1024

    def test_http_error_raises(self, fake_http):
        backend = OoklaBackend(streams=1)
        backend.host = 'server-1002.prod.hosts.ooklaserver.net'
        fake_http.status = 403
        with pytest.raises(SpeedTestError, match='403'):
            backend.download(1)

    def test_no_data_is_a_failure_not_zero(self, fake_http):
        backend = OoklaBackend(streams=1)
        with patch.object(speedtest, 'DOWNLOAD_BYTES', 0):
            with pytest.raises(SpeedTestError, match='no data'):
                backend.download(1)

    def test_unreachable_raises(self, fake_http):
        fake_http.broken = {'server-1002.prod.hosts.ooklaserver.net'}
        backend = OoklaBackend(streams=2)
        backend.host = 'server-1002.prod.hosts.ooklaserver.net'
        with pytest.raises(SpeedTestError, match='refused'):
            backend.download(1)
