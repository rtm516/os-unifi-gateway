# -*- coding: utf-8 -*-
"""The WAN speed test the controller starts.

The controller sends `{"cmd": "speed-test", "up_duration", "down_duration"}`
and reads `speedtest-status` from every inform after that. Each phase
(`status_ping`, `status_download`, `status_upload`, `status_summary`) is 0
idle, 1 running or 2 done. The first report with `status_summary` other than
1 after one with 1 is stored as a result: `xput_download` and `xput_upload` in
Mbps, `latency` in ms, keyed by `rundate` (epoch seconds). So the controller
must see the test running at least once, or it keeps the previous result.

Measurements use Ookla's speedtest.net servers (UniFi OS's own test uses
Ubiquiti's, listed at sp-dir.uwn.com), over the HTTPS interface their web
client uses: the lowest-latency server of the nearest few, then several
parallel download and upload streams. The controller marks a test still
running 90 s after `rundate` as failed (status 3).
"""
import http.client
import json
import logging
import os
import statistics
import threading
import time

import requests

logger = logging.getLogger('unifi-gateway')

IDLE, RUNNING, DONE = 0, 1, 2

# Nearest servers first, as seen from this address
SERVERS_URL = 'https://www.speedtest.net/api/js/servers?engine=js&https_functional=true&limit=10'
SERVER_HOST = 'server-%s.prod.hosts.ooklaserver.net'
SERVER_PORT = 8080
CANDIDATES = 5   # nearest servers pinged to pick the fastest

HEADERS = {'User-Agent': 'unifi-gateway', 'Accept': '*/*'}
DOWNLOAD_BYTES = 25000000   # per request
UPLOAD_BYTES = 10000000
BLOCK = 64 * 1024
STREAMS = 8
PING_COUNT = 10
TIMEOUT = 10

# Seconds per direction: the controller asks for 5, too short for TCP to
# reach full speed on fast links, so at least 10
DEFAULT_DURATION = 10
MIN_DURATION, MAX_DURATION = 10, 30
SAMPLE_INTERVAL = 0.5
# Throughput samples left out of the result, as speedtest.net does: the
# slowest (mostly ramp-up) and the fastest
DROP_SLOWEST, DROP_FASTEST = 0.3, 0.1


class SpeedTestError(Exception):
    pass


def idle_status():
    return {
        'status_summary': IDLE, 'status_ping': IDLE,
        'status_download': IDLE, 'status_upload': IDLE,
        'rundate': 0, 'runtime': 0, 'latency': 0,
        'xput_download': 0, 'xput_upload': 0,
    }


def _duration(value):
    try:
        seconds = int(value)
    except (TypeError, ValueError):
        return DEFAULT_DURATION
    if seconds <= 0:
        return DEFAULT_DURATION
    return max(MIN_DURATION, min(MAX_DURATION, seconds))


def summarise(samples):
    """Mbps from per-interval samples, without the slowest and fastest."""
    ordered = sorted(samples)
    low = int(len(ordered) * DROP_SLOWEST)
    high = len(ordered) - int(len(ordered) * DROP_FASTEST)
    kept = ordered[low:high] or ordered
    return sum(kept) / len(kept)


class _Counter:

    def __init__(self):
        self._lock = threading.Lock()
        self.value = 0

    def add(self, n):
        with self._lock:
            self.value += n


class OoklaBackend:
    """Latency and throughput against the best nearby speedtest.net server."""

    def __init__(self, streams=STREAMS, timeout=TIMEOUT):
        self.streams = streams
        self.timeout = timeout
        self.host = 'speedtest.net'
        self.server = None
        self._payload = os.urandom(BLOCK)

    def _connect(self, host=None):
        return http.client.HTTPSConnection(host or self.host, SERVER_PORT, timeout=self.timeout)

    def _check(self, response, host=None):
        if response.status != 200:
            raise SpeedTestError('%s answered HTTP %d' % (host or self.host, response.status))

    def servers(self):
        """speedtest.net's nearest servers for this address."""
        try:
            response = requests.get(SERVERS_URL, headers=HEADERS, timeout=self.timeout)
            response.raise_for_status()
            servers = response.json()
        except (requests.RequestException, ValueError) as e:
            raise SpeedTestError('cannot get the speedtest.net server list: %s' % e)
        if not isinstance(servers, list) or not servers:
            raise SpeedTestError('speedtest.net returned no servers')
        return servers

    def _pings(self, host, count):
        """Round trips in ms on one connection, not counting the first (which
        pays for the TCP and TLS handshakes)."""
        conn = self._connect(host)
        try:
            samples = []
            for i in range(count + 1):
                started = time.monotonic()
                conn.request('GET', '/latency.txt', headers=HEADERS)
                response = conn.getresponse()
                response.read()
                self._check(response, host)
                if i:
                    samples.append((time.monotonic() - started) * 1000)
            return samples
        finally:
            conn.close()

    def latency(self, count=PING_COUNT):
        """Pick the server with the lowest latency of the nearest few:
        (its median latency in ms, server details)."""
        best = None
        for server in self.servers()[:CANDIDATES]:
            host = SERVER_HOST % server.get('id')
            try:
                rtt = min(self._pings(host, 3))
            except (OSError, http.client.HTTPException, SpeedTestError) as e:
                logger.debug('Skipping speed test server %s: %s', host, e)
                continue
            if best is None or rtt < best[0]:
                best = (rtt, host, server)
        if best is None:
            raise SpeedTestError('no speedtest.net server answered')
        _, self.host, self.server = best
        return statistics.median(self._pings(self.host, count)), self._server_info(self.server)

    @staticmethod
    def _server_info(server):
        """The server as UniFi OS's own speed test reports it. The UI shows
        "<provider> to <country>" while testing (only "Connecting..."
        without both)."""
        info = {
            'provider': server.get('sponsor') or 'speedtest.net',
            'provider_url': 'https://www.speedtest.net/',
            'city': server.get('name', ''),
            'country': server.get('country') or server.get('name', ''),
            'countryCode': server.get('cc', ''),
            'id': str(server.get('id', '')),
        }
        for key in ('lat', 'lon'):
            try:
                info[key] = float(server[key])
            except (KeyError, TypeError, ValueError):
                pass
        return info

    def download(self, duration, progress=None):
        return self._measure(self._download, duration, progress)

    def upload(self, duration, progress=None):
        return self._measure(self._upload, duration, progress)

    def _download(self, conn, counter, stop):
        """One download request. True if the connection can be reused."""
        conn.request('GET', '/download?nocache=%s&size=%d' % (os.urandom(8).hex(), DOWNLOAD_BYTES),
                     headers=HEADERS)
        response = conn.getresponse()
        if response.status != 200:
            response.read()
            self._check(response)
        while not stop.is_set():
            data = response.read(BLOCK)
            if not data:
                return True
            counter.add(len(data))
        return False

    def _upload(self, conn, counter, stop):
        """One upload request. True if the connection can be reused."""
        conn.putrequest('POST', '/upload?nocache=%s' % os.urandom(8).hex())
        for name, value in HEADERS.items():
            conn.putheader(name, value)
        conn.putheader('Content-Type', 'application/octet-stream')
        conn.putheader('Content-Length', str(UPLOAD_BYTES))
        conn.endheaders()
        sent = 0
        while sent < UPLOAD_BYTES:
            if stop.is_set():
                return False
            block = self._payload[:min(BLOCK, UPLOAD_BYTES - sent)]
            conn.send(block)
            sent += len(block)
            counter.add(len(block))
        response = conn.getresponse()
        response.read()
        self._check(response)
        return True

    def _measure(self, transfer, duration, progress):
        """Mbps over `duration` seconds of parallel transfers, from
        half-second samples (see summarise). Calls progress(mbps) with each."""
        counter, stop, errors = _Counter(), threading.Event(), []

        def stream():
            conn = None
            try:
                while not stop.is_set():
                    conn = conn or self._connect()
                    if not transfer(conn, counter, stop):
                        conn.close()
                        conn = None
            except (OSError, http.client.HTTPException, SpeedTestError) as e:
                if not stop.is_set():
                    errors.append(e)
            finally:
                if conn:
                    conn.close()

        threads = [threading.Thread(target=stream, name='speedtest-%d' % i, daemon=True)
                   for i in range(self.streams)]
        for thread in threads:
            thread.start()

        samples = []
        last_time, last_bytes = time.monotonic(), 0
        end = last_time + duration
        while time.monotonic() < end and any(t.is_alive() for t in threads):
            stop.wait(min(SAMPLE_INTERVAL, max(0, end - time.monotonic())))
            now, total = time.monotonic(), counter.value
            if now > last_time:
                mbps = (total - last_bytes) * 8 / (now - last_time) / 1e6
                samples.append(mbps)
                if progress:
                    progress(mbps)
            last_time, last_bytes = now, total
        stop.set()
        for thread in threads:
            thread.join(self.timeout + 1)

        if not counter.value:
            raise SpeedTestError(str(errors[0]) if errors else 'no data received')
        return summarise(samples)


def _progress_field(samples):
    """Throughput samples as the UI reads them from `download-progress` and
    `upload-progress`: [{records: [[time in ms, Mbps x 100], ...]}], shown as
    the average of the records."""
    return [{'records': [[t, int(round(mbps * 100))] for t, mbps in samples]}]


class SpeedTest:
    """Runs one test at a time in the background and keeps the last result
    (as `speedtest-status`) in `results_file`.

    While a test runs, each status carries the throughput samples taken since
    the previous one, which the controller passes on to the UI as they come."""

    def __init__(self, results_file, backend=None):
        self.results_file = results_file
        self.backend = backend or OoklaBackend()
        self._lock = threading.Lock()
        self._thread = None
        self._progress = None   # status while a test runs
        self._reported = False  # whether status() has shown it running
        self._samples = {'download': [], 'upload': []}  # (ms, Mbps)
        self._sent = {'download': 0, 'upload': 0}       # samples already reported
        self._result = self._load()

    @property
    def active(self):
        """True while a test runs or its result hasn't been reported yet."""
        return self._progress is not None

    def start(self, down_duration=None, up_duration=None):
        with self._lock:
            if self._progress is not None:
                logger.info('Speed test already running')
                return False
            now = int(time.time())
            self._progress = dict(idle_status(), rundate=now, status_summary=RUNNING,
                                  status_ping=RUNNING)
            self._reported = False
            self._samples = {'download': [], 'upload': []}
            self._sent = {'download': 0, 'upload': 0}
            self._thread = threading.Thread(
                target=self._run, args=(_duration(down_duration), _duration(up_duration)),
                name='speedtest', daemon=True)
            self._thread.start()
        return True

    def status(self):
        """The `speedtest-status` to report now."""
        with self._lock:
            if self._progress is not None:
                status = dict(self._progress)
                status['runtime'] = int(time.time()) - status['rundate']
                status.update(self._new_samples())
                self._reported = True
                if self._progress['status_summary'] != RUNNING:
                    # finished before any inform showed it running: that was
                    # the one report it needed, the result follows next time
                    status['status_summary'] = RUNNING
                    self._progress = None
                return status
            if not self._result:
                return idle_status()
            return dict(self._result, **self._final_samples(self._result))

    def _new_samples(self):
        """Progress fields with the samples not reported yet (or the latest
        one again, so the UI keeps showing a value)."""
        fields = {}
        for phase, samples in self._samples.items():
            if samples:
                fields['%s-progress' % phase] = _progress_field(
                    samples[self._sent[phase]:] or samples[-1:])
                self._sent[phase] = len(samples)
        return fields

    @staticmethod
    def _final_samples(result):
        end = (result['rundate'] + result.get('runtime', 0)) * 1000
        return {
            'download-progress': _progress_field([(end, result['xput_download'])]),
            'upload-progress': _progress_field([(end, result['xput_upload'])]),
        }

    def _update(self, **fields):
        with self._lock:
            self._progress.update(fields)

    def _sample(self, phase, mbps):
        with self._lock:
            self._progress['xput_%s' % phase] = round(mbps, 2)
            self._samples[phase].append((int(time.time() * 1000), mbps))

    def _run(self, down_duration, up_duration):
        logger.info('Running speed test (%ds download, %ds upload)', down_duration, up_duration)
        started = time.monotonic()
        try:
            latency, server = self.backend.latency()
            logger.info('Speed test server: %s in %s, %.0f ms', server.get('provider', '?'),
                        server.get('city') or server.get('country', '?'), latency)
            self._update(latency=int(round(latency)), server=server,
                         status_ping=DONE, status_download=RUNNING)
            down = self.backend.download(down_duration, lambda mbps: self._sample('download', mbps))
            self._update(xput_download=round(down, 2), status_download=DONE,
                         status_upload=RUNNING)
            up = self.backend.upload(up_duration, lambda mbps: self._sample('upload', mbps))
        except Exception as e:
            logger.warning('Speed test failed: %s', e)
            with self._lock:
                self._progress = None
            return

        with self._lock:
            result = dict(self._progress, xput_upload=round(up, 2), status_upload=DONE,
                          status_summary=DONE, runtime=int(round(time.monotonic() - started)))
            self._result = result
            if self._reported:
                self._progress = None
            else:
                self._progress = dict(result)
        logger.info('Speed test done: %.1f Mbps down, %.1f Mbps up, %d ms',
                    result['xput_download'], result['xput_upload'], result['latency'])
        self._save(result)

    def _load(self):
        try:
            with open(self.results_file) as f:
                result = json.load(f)
        except (IOError, OSError, ValueError):
            return None
        if not isinstance(result, dict) or not result.get('rundate'):
            return None  # not ours, e.g. speedtest-cli output from older versions
        return dict(idle_status(), **result)

    def _save(self, result):
        tmp = self.results_file + '.tmp'
        try:
            with open(tmp, 'w') as f:
                json.dump(result, f)
            os.replace(tmp, self.results_file)
        except (IOError, OSError) as e:
            logger.warning('Could not save speed test result to %s: %s', self.results_file, e)
