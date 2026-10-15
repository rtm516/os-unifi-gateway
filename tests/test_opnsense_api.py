# -*- coding: utf-8 -*-
"""Tests for the OPNsense API client."""
import io
import json
import urllib.error
from unittest.mock import patch

import pytest

from collectors.opnsense_api import OPNsenseAPI, OPNsenseAPIError, read_api_key_file


class FakeResponse(io.BytesIO):

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()


def _capture(body):
    seen = {}

    def urlopen(request, context=None, timeout=None):
        seen['request'] = request
        return FakeResponse(body)
    return seen, urlopen


class TestClient:

    def test_get_sends_basic_auth_without_content_type(self):
        seen, urlopen = _capture(b'{"ok": true}')
        api = OPNsenseAPI('router', 'key', 'secret')
        with patch('urllib.request.urlopen', urlopen):
            assert api.get('/api/x') == {'ok': True}
        req = seen['request']
        assert req.full_url == 'https://router/api/x'
        assert req.get_method() == 'GET'
        assert req.get_header('Authorization') == 'Basic a2V5OnNlY3JldA=='
        # OPNsense answers 400 "Invalid JSON syntax" to an empty JSON body
        assert req.get_header('Content-type') is None

    def test_post_sends_json(self):
        seen, urlopen = _capture(b'{"rows": []}')
        api = OPNsenseAPI('https://router:4443', 'k', 's')
        with patch('urllib.request.urlopen', urlopen):
            api.post('/api/y', {'current': 1})
        req = seen['request']
        assert req.full_url == 'https://router:4443/api/y'
        assert req.get_method() == 'POST'
        assert json.loads(req.data) == {'current': 1}
        assert req.get_header('Content-type') == 'application/json'

    def test_http_error(self):
        def urlopen(request, context=None, timeout=None):
            raise urllib.error.HTTPError(request.full_url, 403, 'Forbidden', {}, None)
        api = OPNsenseAPI('router', 'k', 's')
        with patch('urllib.request.urlopen', urlopen), pytest.raises(OPNsenseAPIError) as exc:
            api.get('/api/z')
        assert exc.value.status == 403 and exc.value.forbidden

    def test_connection_error(self):
        def urlopen(request, context=None, timeout=None):
            raise urllib.error.URLError('refused')
        api = OPNsenseAPI('router', 'k', 's')
        with patch('urllib.request.urlopen', urlopen), pytest.raises(OPNsenseAPIError) as exc:
            api.get('/api/z')
        assert exc.value.status is None and not exc.value.forbidden

    def test_first_event(self):
        stream = b': keepalive\n\ndata: {"total": 7.5}\n\ndata: {"total": 9}\n\n'
        seen, urlopen = _capture(stream)
        api = OPNsenseAPI('router', 'k', 's')
        with patch('urllib.request.urlopen', urlopen):
            assert api.first_event('/api/diagnostics/cpu_usage/stream') == {'total': 7.5}


def test_read_api_key_file(tmp_path):
    path = tmp_path / 'apikey.txt'
    path.write_text('key=AbC+/1=\nsecret=dEf=2\n')
    assert read_api_key_file(str(path)) == ('AbC+/1=', 'dEf=2')
