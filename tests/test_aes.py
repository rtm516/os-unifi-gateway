# -*- coding: utf-8 -*-
"""AES must match NIST vectors and reject tampered GCM messages."""
import os
from binascii import a2b_hex

import pytest

import aes


def test_cbc_nist_vector():
    # NIST SP 800-38A F.2.1, first block
    key = a2b_hex('2b7e151628aed2a6abf7158809cf4f3c')
    iv = a2b_hex('000102030405060708090a0b0c0d0e0f')
    plain = a2b_hex('6bc1bee22e409f96e93d7e117393172a')
    cipher = a2b_hex('7649abac8119b246cee98e9b12e9197d')
    assert aes.cbc_encrypt(key, iv, plain) == cipher
    assert aes.cbc_decrypt(key, iv, cipher) == plain


def test_cbc_rejects_unpadded_data():
    with pytest.raises(aes.AESError):
        aes.cbc_encrypt(os.urandom(16), os.urandom(16), b'not 16 bytes')


def test_gcm_round_trip_with_16_byte_iv():
    key, iv, aad = os.urandom(16), os.urandom(16), b'TNBU header bytes'
    data = os.urandom(1000)
    sealed = aes.gcm_encrypt(key, iv, aad, data)
    assert len(sealed) == len(data) + aes.TAG_SIZE
    assert aes.gcm_decrypt(key, iv, aad, sealed) == data


def test_gcm_rejects_tampering():
    key, iv = os.urandom(16), os.urandom(16)
    sealed = bytearray(aes.gcm_encrypt(key, iv, b'aad', b'secret payload'))
    sealed[3] ^= 1
    with pytest.raises(aes.AESError):
        aes.gcm_decrypt(key, iv, b'aad', bytes(sealed))
    with pytest.raises(aes.AESError):
        aes.gcm_decrypt(key, iv, b'other aad', aes.gcm_encrypt(key, iv, b'aad', b'x'))
    with pytest.raises(aes.AESError):
        aes.gcm_decrypt(key, iv, b'aad', b'short')
