# -*- coding: utf-8 -*-
"""Both AES backends must agree with each other and with NIST vectors."""
import os
from binascii import a2b_hex

import pytest

import aes


def _backends():
    found = []
    for name in ('pycryptodome', 'openssl'):
        try:
            found.append(aes.load_backend(name))
        except ImportError:
            pass
    return found


BACKENDS = _backends()


@pytest.fixture(params=BACKENDS, ids=lambda b: b.name)
def backend(request):
    return request.param


def test_at_least_one_backend():
    assert BACKENDS


def test_cbc_nist_vector(backend):
    # NIST SP 800-38A F.2.1, first block
    key = a2b_hex('2b7e151628aed2a6abf7158809cf4f3c')
    iv = a2b_hex('000102030405060708090a0b0c0d0e0f')
    plain = a2b_hex('6bc1bee22e409f96e93d7e117393172a')
    cipher = a2b_hex('7649abac8119b246cee98e9b12e9197d')
    assert backend.cbc_encrypt(key, iv, plain) == cipher
    assert backend.cbc_decrypt(key, iv, cipher) == plain


def test_gcm_round_trip_with_16_byte_iv(backend):
    key, iv, aad = os.urandom(16), os.urandom(16), b'TNBU header bytes'
    data = os.urandom(1000)
    sealed = backend.gcm_encrypt(key, iv, aad, data)
    assert len(sealed) == len(data) + aes.TAG_SIZE
    assert backend.gcm_decrypt(key, iv, aad, sealed) == data


def test_gcm_rejects_tampering(backend):
    key, iv = os.urandom(16), os.urandom(16)
    sealed = bytearray(backend.gcm_encrypt(key, iv, b'aad', b'secret payload'))
    sealed[3] ^= 1
    with pytest.raises(aes.AESError):
        backend.gcm_decrypt(key, iv, b'aad', bytes(sealed))
    with pytest.raises(aes.AESError):
        backend.gcm_decrypt(key, iv, b'other aad', backend.gcm_encrypt(key, iv, b'aad', b'x'))


@pytest.mark.skipif(len(BACKENDS) < 2, reason='needs both backends')
def test_backends_interoperate():
    a, b = BACKENDS
    key, iv, aad, data = os.urandom(16), os.urandom(16), os.urandom(40), os.urandom(333)
    assert a.gcm_encrypt(key, iv, aad, data) == b.gcm_encrypt(key, iv, aad, data)
    assert b.gcm_decrypt(key, iv, aad, a.gcm_encrypt(key, iv, aad, data)) == data
    padded = data[:320]
    assert a.cbc_encrypt(key, iv, padded) == b.cbc_encrypt(key, iv, padded)
