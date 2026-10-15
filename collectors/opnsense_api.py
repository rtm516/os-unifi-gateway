# -*- coding: utf-8 -*-
"""Minimal OPNsense REST API client.

Authenticates with an API key/secret pair via HTTP basic auth, as described
in https://docs.opnsense.org/development/how-tos/api.html
"""
import json
import logging

import requests
import urllib3

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
        self.session = requests.Session()
        self.session.auth = (key, secret)
        self.session.headers['Accept'] = 'application/json'
        self.session.verify = verify_ssl
        if not verify_ssl:
            # otherwise urllib3 warns on every request
            urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

    def _request(self, path, body=None, timeout=None, stream=False):
        # OPNsense rejects an empty body sent as application/json, so GETs
        # send none and POSTs at least {}
        try:
            response = self.session.request(
                'POST' if body is not None else 'GET', self.base_url + path, json=body,
                timeout=timeout or self.timeout, stream=stream,
            )
        except requests.RequestException as e:
            raise OPNsenseAPIError(path, None, str(e)) from None
        if not response.ok:
            response.close()
            raise OPNsenseAPIError(path, response.status_code, response.reason)
        return response

    def get(self, path):
        return self._read_json(path, self._request(path))

    def post(self, path, body=None):
        return self._read_json(path, self._request(path, body if body is not None else {}))

    def first_event(self, path, timeout=5):
        """Read the first `data:` line of a server-sent event stream."""
        response = self._request(path, timeout=timeout, stream=True)
        try:
            for raw in response.iter_lines():
                line = raw.decode('utf-8', 'replace').strip()
                if line.startswith('data:'):
                    return json.loads(line[5:].strip())
        except (requests.RequestException, ValueError) as e:
            raise OPNsenseAPIError(path, None, str(e)) from None
        finally:
            response.close()
        raise OPNsenseAPIError(path, None, 'stream ended without data')

    @staticmethod
    def _read_json(path, response):
        try:
            return response.json()
        except (requests.RequestException, ValueError) as e:
            raise OPNsenseAPIError(path, None, 'invalid JSON: %s' % e) from None
