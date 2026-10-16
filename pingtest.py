# -*- coding: utf-8 -*-
"""The controller's client Ping Test (a client's side panel, next to Block App).

The controller only offers it when `usg2_caps` has bit 1. It sends
`{"cmd": "ping-test-start", "destination": <client IP>, "count": 30,
"interval": 1, "packetSize": 56}` (and a second one for the access point a
wireless client is on), and `{"cmd": "ping-test-stop", "destination"}`.

Progress goes back only as an event inform (`inform_as_notif`, `notif_reason:
event`) whose `notif_payload` is `{"event_string": "EVT_GW_PingtestStatus",
"pingtest-status": [...]}`, one entry per destination: `address` (the
destination as sent), `status` (running, finished, failed or cancelled),
`transmittedCount`, `receivedCount`, `timeMin`/`timeMax`/`timeAvg` in ms and
`replies` [{id, address, time, ttl}], where `id` is the 0-based sequence
number and missing ids (or `time: -1`) are lost packets. The controller
merges replies by id, and gives up on a test (status timeout) when
`transmittedCount` hasn't moved for 5 s, so a running test is reported at
least that often.
"""
import logging
import threading
import time

from collectors.source import SourceError

logger = logging.getLogger('unifi-gateway')

EVENT = 'EVT_GW_PingtestStatus'
RUNNING, FINISHED, FAILED, CANCELLED = 'running', 'finished', 'failed', 'cancelled'

DEFAULT_COUNT, DEFAULT_INTERVAL, DEFAULT_PACKET_SIZE = 30, 1, 56
MAX_COUNT = 1000
MAX_SESSIONS = 30   # the controller's own limit per gateway
POLL_INTERVAL = 0.5
# A ping that should be over by now is stopped, so a stuck job can't keep
# the daemon informing every 2 s
GRACE = 10


def _number(value, default, low, high):
    try:
        return min(max(int(value), low), high)
    except (TypeError, ValueError):
        return default


class PingSession:

    def __init__(self, destination, count, interval, packet_size):
        self.destination = destination
        self.count = count
        self.interval = interval
        self.packet_size = packet_size
        self.status = RUNNING
        self.transmitted = 0
        self.replies = {}   # id: (time ms, ttl)
        self.cancel = threading.Event()

    def add(self, transmitted, replies):
        for reply in replies:
            self.replies[reply['id']] = (reply['time'], reply.get('ttl', 64))
        self.transmitted = min(self.count, max(
            self.transmitted, transmitted, max(self.replies, default=-1) + 1))

    def report(self):
        times = [t for t, _ in self.replies.values() if t >= 0]
        entry = {
            'address': self.destination,
            'status': self.status,
            'transmittedCount': self.transmitted,
            'receivedCount': len(times),
            'replies': [{'id': i, 'address': self.destination, 'time': t, 'ttl': ttl}
                        for i, (t, ttl) in sorted(self.replies.items())],
        }
        if times:
            entry.update(timeMin=min(times), timeMax=max(times),
                         timeAvg=round(sum(times) / len(times), 3))
        return entry


class PingTests:
    """Ping tests by destination, each run in its own thread through the
    router source's `ping`."""

    def __init__(self, source=None):
        self.source = source
        self._lock = threading.Lock()
        self._sessions = {}

    @property
    def active(self):
        """A test is running or has a result the controller hasn't seen."""
        with self._lock:
            return bool(self._sessions)

    def start(self, destination, count=None, interval=None, packet_size=None):
        if not destination:
            return
        session = PingSession(
            str(destination),
            _number(count, DEFAULT_COUNT, 1, MAX_COUNT),
            _number(interval, DEFAULT_INTERVAL, 1, 60),
            _number(packet_size, DEFAULT_PACKET_SIZE, 1, 65000),
        )
        with self._lock:
            old = self._sessions.get(session.destination)
            if old is not None:
                old.cancel.set()
            elif len(self._sessions) >= MAX_SESSIONS:
                logger.warning('Too many ping tests running, not pinging %s', destination)
                return
            self._sessions[session.destination] = session
        logger.info('Ping test to %s started (%d x %d bytes)', session.destination,
                    session.count, session.packet_size)
        threading.Thread(target=self._run, args=(session,),
                         name='ping-%s' % session.destination, daemon=True).start()

    def stop(self, destination):
        with self._lock:
            session = self._sessions.get(str(destination))
        if session is not None:
            session.cancel.set()

    def reports(self):
        """pingtest-status entries for every test, dropping the finished
        ones: their final state is reported once."""
        with self._lock:
            reports = [session.report() for session in self._sessions.values()]
            for destination, session in list(self._sessions.items()):
                if session.status != RUNNING:
                    del self._sessions[destination]
        return reports

    def _finish(self, session, status):
        with self._lock:
            session.status = status
            if self._sessions.get(session.destination) is not session:
                return   # replaced by a new test to the same address
        logger.info('Ping test to %s %s: %d/%d replies', session.destination, status,
                    len([t for t, _ in session.replies.values() if t >= 0]),
                    session.transmitted)

    def _run(self, session):
        deadline = time.monotonic() + session.count * session.interval + GRACE
        job = None
        try:
            job = self.source.ping(session.destination, session.count,
                                   session.interval, session.packet_size)
            while not session.cancel.is_set():
                transmitted, replies, done = job.poll()
                with self._lock:
                    session.add(transmitted, replies)
                if done or time.monotonic() > deadline:
                    break
                session.cancel.wait(POLL_INTERVAL)
        except (SourceError, OSError, ValueError) as e:
            hint = getattr(e, 'hint', None)
            logger.warning('Ping test to %s failed: %s%s', session.destination, e,
                           ' -- %s' % hint if hint else '')
            self._finish(session, FAILED)
            return
        finally:
            if job is not None:
                try:
                    job.stop()
                except (SourceError, OSError) as e:
                    logger.debug('Stopping ping to %s: %s', session.destination, e)
        self._finish(session, CANCELLED if session.cancel.is_set() else FINISHED)
