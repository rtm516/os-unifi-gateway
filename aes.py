# -*- coding: utf-8 -*-
"""AES-128-CBC and AES-128-GCM (16-byte IV) for the inform protocol.

Uses the `cryptography` package. OPNsense core doesn't list it, but it comes
with every install as a dependency of core's dnspython (its DNSSEC option),
so the plugin needn't declare it.
"""
from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

TAG_SIZE = 16


class AESError(ValueError):
    pass


def _cbc(key, iv):
    return Cipher(algorithms.AES(key), modes.CBC(iv))


def cbc_encrypt(key, iv, data):
    """No padding is added: the data must already be a multiple of 16 bytes."""
    encryptor = _cbc(key, iv).encryptor()
    try:
        return encryptor.update(data) + encryptor.finalize()
    except ValueError as e:
        raise AESError(str(e)) from None


def cbc_decrypt(key, iv, data):
    decryptor = _cbc(key, iv).decryptor()
    try:
        return decryptor.update(data) + decryptor.finalize()
    except ValueError as e:
        raise AESError(str(e)) from None


def gcm_encrypt(key, iv, aad, data):
    """Ciphertext followed by the 16-byte tag."""
    return AESGCM(key).encrypt(iv, data, aad)


def gcm_decrypt(key, iv, aad, data):
    if len(data) < TAG_SIZE:
        raise AESError('ciphertext shorter than the GCM tag')
    try:
        return AESGCM(key).decrypt(iv, data, aad)
    except InvalidTag:
        raise AESError('authentication check failed') from None
