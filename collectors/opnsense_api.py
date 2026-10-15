# -*- coding: utf-8 -*-
"""Minimal OPNsense REST API client (stdlib only).

Authenticates with an API key/secret pair via HTTP basic auth, as described
in https://docs.opnsense.org/development/how-tos/api.html
"""
import base64
import json
import logging
import socket
import ssl
import urllib.error
import urllib.request

from .source import SourceError

logger = logging.getLogger('unifi-gateway')


class OPNsenseAPIError(SourceError):
    pass


def read_api_key_file(path):
    """Parse the apikey.txt file OPNsense offers for download (key=/secret=)."""
    creds = {}
    with open(path, 'r') as f:
        for line in f:
            if '=' in line:
                k, v = line.strip().split('=', 1)
                creds[k.strip().lower()] = v.strip()
    return creds.get('key', ''), creds.get('secret', '')


class OPNsenseAPI:

    def __init__(self, host, key, secret, verify_ssl=True, timeout=15):
        if '://' not in host:
            host = 'https://' + host
        self.base_url = host.rstrip('/')
        self.timeout = timeout
        token = base64.b64encode(('%s:%s' % (key, secret)).encode('utf-8'))
        self._auth = 'Basic ' + token.decode('ascii')
        self._ssl = ssl.create_default_context()
        if not verify_ssl:
            self._ssl.check_hostname = False
            self._ssl.verify_mode = ssl.CERT_NONE

    def _open(self, path, body=None, timeout=None):
        headers = {'Authorization': self._auth, 'Accept': 'application/json'}
        data = None
        if body is not None:
            # OPNsense rejects an empty body sent as application/json, so
            # only declare JSON when there is something to send
            data = json.dumps(body).encode('utf-8')
            headers['Content-Type'] = 'application/json'
        request = urllib.request.Request(
            self.base_url + path, data=data, headers=headers,
            method='POST' if data is not None else 'GET',
        )
        try:
            return urllib.request.urlopen(
                request, context=self._ssl, timeout=timeout or self.timeout
            )
        except urllib.error.HTTPError as e:
            raise OPNsenseAPIError(path, e.code, e.reason) from None
        except (urllib.error.URLError, socket.timeout, OSError) as e:
            raise OPNsenseAPIError(path, None, str(getattr(e, 'reason', e))) from None

    def get(self, path):
        return self._read_json(path, self._open(path))

    def post(self, path, body=None):
        return self._read_json(path, self._open(path, body if body is not None else {}))

    def first_event(self, path, timeout=5):
        """Read the first `data:` line of a server-sent event stream."""
        response = self._open(path, timeout=timeout)
        try:
            for raw in response:
                line = raw.decode('utf-8', 'replace').strip()
                if line.startswith('data:'):
                    return json.loads(line[5:].strip())
        except (socket.timeout, OSError, ValueError) as e:
            raise OPNsenseAPIError(path, None, str(e)) from None
        finally:
            response.close()
        raise OPNsenseAPIError(path, None, 'stream ended without data')

    @staticmethod
    def _read_json(path, response):
        try:
            with response:
                return json.loads(response.read().decode('utf-8'))
        except ValueError as e:
            raise OPNsenseAPIError(path, None, 'invalid JSON: %s' % e) from None
