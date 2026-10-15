# -*- coding: utf-8 -*-
"""Tests for the OPNsense API client."""
import json
from unittest.mock import patch

import pytest
import requests
from requests.adapters import HTTPAdapter

from collectors.opnsense_api import OPNsenseAPI, OPNsenseAPIError, read_api_key_file


def fake_response(body=b'', status=200, reason='OK'):
    response = requests.Response()
    response.status_code, response.reason = status, reason
    response._content, response._content_consumed = body, True
    return response


def _capture(body, status=200, reason='OK'):
    """Stands in for the transport: records the prepared request (with auth
    and headers applied) and answers with `body`."""
    seen = {}

    def send(adapter, request, **kwargs):
        seen['request'] = request
        return fake_response(body, status, reason)
    return seen, patch.object(HTTPAdapter, 'send', send)


class TestClient:

    def test_get_sends_basic_auth_without_content_type(self):
        seen, transport = _capture(b'{"ok": true}')
        api = OPNsenseAPI('router', 'key', 'secret')
        with transport:
            assert api.get('/api/x') == {'ok': True}
        req = seen['request']
        assert req.url == 'https://router/api/x'
        assert req.method == 'GET'
        assert req.headers['Authorization'] == 'Basic a2V5OnNlY3JldA=='
        # OPNsense answers 400 "Invalid JSON syntax" to an empty JSON body
        assert 'Content-Type' not in req.headers
        assert req.body is None

    def test_post_sends_json(self):
        seen, transport = _capture(b'{"rows": []}')
        api = OPNsenseAPI('https://router:4443', 'k', 's')
        with transport:
            api.post('/api/y', {'current': 1})
        req = seen['request']
        assert req.url == 'https://router:4443/api/y'
        assert req.method == 'POST'
        assert json.loads(req.body) == {'current': 1}
        assert req.headers['Content-Type'] == 'application/json'

    def test_post_without_body_sends_empty_object(self):
        seen, transport = _capture(b'{}')
        with transport:
            OPNsenseAPI('router', 'k', 's').post('/api/y')
        assert json.loads(seen['request'].body) == {}

    def test_http_error(self):
        _, transport = _capture(b'', 403, 'Forbidden')
        api = OPNsenseAPI('router', 'k', 's')
        with transport, pytest.raises(OPNsenseAPIError) as exc:
            api.get('/api/z')
        assert exc.value.status == 403 and exc.value.forbidden

    def test_connection_error(self):
        def send(adapter, request, **kwargs):
            raise requests.ConnectionError('refused')
        api = OPNsenseAPI('router', 'k', 's')
        with patch.object(HTTPAdapter, 'send', send), pytest.raises(OPNsenseAPIError) as exc:
            api.get('/api/z')
        assert exc.value.status is None and not exc.value.forbidden

    def test_invalid_json(self):
        _, transport = _capture(b'<html>')
        with transport, pytest.raises(OPNsenseAPIError) as exc:
            OPNsenseAPI('router', 'k', 's').get('/api/z')
        assert 'invalid JSON' in str(exc.value)

    def test_unverified_ssl(self):
        assert OPNsenseAPI('router', 'k', 's', verify_ssl=False).session.verify is False

    def test_first_event(self):
        stream = b': keepalive\n\ndata: {"total": 7.5}\n\ndata: {"total": 9}\n\n'
        _, transport = _capture(stream)
        api = OPNsenseAPI('router', 'k', 's')
        with transport:
            assert api.first_event('/api/diagnostics/cpu_usage/stream') == {'total': 7.5}


def test_read_api_key_file(tmp_path):
    path = tmp_path / 'apikey.txt'
    path.write_text('key=AbC+/1=\nsecret=dEf=2\n')
    assert read_api_key_file(str(path)) == ('AbC+/1=', 'dEf=2')
