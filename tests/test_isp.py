# -*- coding: utf-8 -*-
"""ISP lookup for the controller's ISP display. No real network."""
import io
import json
import urllib.error
from unittest.mock import patch

import pytest

import isp
from isp import IspInfo, lookup

IPINFO = {'ip': '81.2.3.4', 'city': 'Exampleton', 'region': 'England', 'country': 'GB',
          'loc': '51.0000,-1.0000', 'org': 'AS64500 Example Broadband', 'timezone': 'Europe/London'}


def _ipinfo(body, seen):
    def urlopen(request, timeout=None):
        seen.append(request.full_url)
        assert request.get_header('User-agent') == 'unifi-gateway'
        return io.BytesIO(json.dumps(body).encode())
    return urlopen


class TestLookup:

    def test_looks_up_the_wan_address(self):
        seen = []
        with patch('isp.urllib.request.urlopen', _ipinfo(IPINFO, seen)):
            info = lookup('81.2.3.4')
        assert seen == ['https://ipinfo.io/81.2.3.4/json']
        assert info == {
            'isp_name': 'Example Broadband', 'isp_organization': 'Example Broadband',
            'asn': 64500, 'address': '81.2.3.4', 'city': 'Exampleton',
            'country_code': 'GB', 'timezone': 'Europe/London',
        }

    @pytest.mark.parametrize('wan_ip', ['100.64.1.2', '192.168.1.2', None, 'not-an-ip'])
    def test_private_wan_uses_own_public_address(self, wan_ip):
        """Behind CGNAT or another router the WAN address says nothing."""
        seen = []
        with patch('isp.urllib.request.urlopen', _ipinfo(IPINFO, seen)):
            assert lookup(wan_ip)['isp_name'] == 'Example Broadband'
        assert seen == ['https://ipinfo.io/json']

    def test_no_provider(self):
        with patch('isp.urllib.request.urlopen', _ipinfo({'ip': '81.2.3.4', 'bogon': True}, [])):
            with pytest.raises(ValueError):
                lookup('81.2.3.4')


class TestIspInfo:

    def _wait(self, info):
        info._thread.join(5)

    def test_looks_up_in_the_background_and_caches(self):
        calls = []

        def fetch(ip):
            calls.append(ip)
            return {'isp_name': 'Example Broadband', 'asn': 64500}
        info = IspInfo(fetch)
        assert info.current('81.2.3.4') is None  # nothing yet: informs don't wait
        self._wait(info)
        assert info.current('81.2.3.4')['isp_name'] == 'Example Broadband'
        assert calls == ['81.2.3.4']

    def test_new_wan_address_looks_up_again(self):
        calls = []
        info = IspInfo(lambda ip: calls.append(ip) or {'isp_name': ip, 'asn': 1})
        info.current('81.2.3.4')
        self._wait(info)
        info.current('81.2.3.5')
        self._wait(info)
        assert calls == ['81.2.3.4', '81.2.3.5']
        assert info.current('81.2.3.5')['isp_name'] == '81.2.3.5'

    def test_refreshes_when_old(self):
        calls = []
        info = IspInfo(lambda ip: calls.append(ip) or {'isp_name': 'A', 'asn': 1})
        info.current('81.2.3.4')
        self._wait(info)
        info._next = 0
        info.current('81.2.3.4')
        self._wait(info)
        assert len(calls) == 2

    def test_failure_keeps_last_answer_and_retries_later(self):
        answers = [{'isp_name': 'Example Broadband', 'asn': 64500}, urllib.error.URLError('offline')]

        def fetch(ip):
            answer = answers.pop(0)
            if isinstance(answer, Exception):
                raise answer
            return answer
        info = IspInfo(fetch)
        info.current('81.2.3.4')
        self._wait(info)
        info._next = 0
        info.current('81.2.3.4')
        self._wait(info)
        with patch('isp.time.time', return_value=0):
            assert info.current('81.2.3.4')['isp_name'] == 'Example Broadband'
        assert info._next - isp.time.time() <= isp.RETRY
