# -*- coding: utf-8 -*-
"""ISP lookup for the controller's ISP display. No real network."""
from unittest.mock import patch

import dns.resolver

import pytest

import isp
from isp import IspInfo, lookup

RECORDS = {
    ('4.3.2.81.origin.asn.cymru.com', 'TXT'): [
        '64500 | 81.0.0.0/8 | GB | ripencc | 2004-01-01',
        '64501 | 81.2.0.0/16 | GB | ripencc | 2006-09-08',  # more specific
    ],
    ('AS64501.asn.cymru.com', 'TXT'): [
        '64501 | GB | ripencc | 1995-06-08 | EXAMPLE-AS - Example Broadband Limited, GB'],
    ('myip.opendns.com', 'A'): ['81.2.3.4'],
}


def fake_dns(seen):
    def query(name, rtype, server=None):
        seen.append((name, server))
        return RECORDS.get((name, rtype), [])
    return query


class TestLookup:

    def test_looks_up_the_wan_address(self):
        seen = []
        with patch('isp.query', fake_dns(seen)):
            info = lookup('81.2.3.4')
        assert info == {
            'isp_name': 'Example Broadband Limited', 'isp_organization': 'Example Broadband Limited',
            'asn': 64501, 'address': '81.2.3.4', 'country_code': 'GB',
        }
        assert [name for name, _ in seen] == ['4.3.2.81.origin.asn.cymru.com', 'AS64501.asn.cymru.com']

    @pytest.mark.parametrize('wan_ip', ['100.64.1.2', '192.168.1.2', None, 'not-an-ip'])
    def test_private_wan_uses_own_public_address(self, wan_ip):
        """Behind CGNAT or another router the WAN address says nothing."""
        seen = []
        with patch('isp.query', fake_dns(seen)):
            assert lookup(wan_ip)['address'] == '81.2.3.4'
        assert seen[0] == ('myip.opendns.com', isp.OPENDNS)

    def test_ipv6_origin_name(self):
        name = isp._origin_name(isp.ipaddress.ip_address('2001:db8::1'))
        assert name == '1.0.0.0.' + '0.0.0.0.' * 5 + '8.b.d.0.1.0.0.2.origin6.asn.cymru.com'

    @pytest.mark.parametrize('description,name', [
        ('BT-UK-AS - British Telecommunications Limited, GB', 'British Telecommunications Limited'),
        ('unspecified - Vodafone Limited, GB', 'Vodafone Limited'),
        ('CLOUDFLARENET - Cloudflare, Inc., US', 'Cloudflare, Inc.'),
        ('EXAMPLE', 'EXAMPLE'),
    ])
    def test_provider_name(self, description, name):
        assert isp._provider_name(description, description[-2:]) == name

    def test_no_provider(self):
        with patch('isp.query', lambda *a, **k: []):
            with pytest.raises(ValueError):
                lookup('81.2.3.4')


class TestQuery:

    def _answer(self, *records):
        class Record:
            def __init__(self, strings):
                self.strings = strings

            def to_text(self):
                return self.strings[0].decode()
        return [Record(r) for r in records]

    def test_txt_strings_are_joined(self):
        answer = self._answer([b'64500 | GB', b' | xy'])
        with patch('isp.dns.resolver.Resolver.resolve', return_value=answer) as resolve:
            assert isp.query('AS64500.asn.cymru.com', 'TXT') == ['64500 | GB | xy']
        assert resolve.call_args.args[:2] == ('AS64500.asn.cymru.com', 'TXT')

    def test_asks_a_given_server(self):
        seen = []

        def resolve(resolver, name, rtype, lifetime=None):
            seen.append(resolver.nameservers)
            return self._answer([b'81.2.3.4'])
        with patch('isp.dns.resolver.Resolver.resolve', resolve):
            assert isp.query('myip.opendns.com', 'A', server=isp.OPENDNS) == ['81.2.3.4']
        assert seen == [[isp.OPENDNS]]

    @pytest.mark.parametrize('error', [dns.resolver.NXDOMAIN, dns.resolver.NoAnswer])
    def test_no_such_record(self, error):
        with patch('isp.dns.resolver.Resolver.resolve', side_effect=error):
            assert isp.query('nothing.example', 'TXT') == []


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
        answers = [{'isp_name': 'Example Broadband', 'asn': 64500}, OSError('offline')]

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
