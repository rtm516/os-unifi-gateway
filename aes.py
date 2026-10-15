# -*- coding: utf-8 -*-
"""AES-128-CBC and AES-128-GCM (16-byte IV) for the inform protocol.

Uses pycryptodome when it is installed, otherwise calls the system OpenSSL
libcrypto through ctypes. Python's own ssl module needs libcrypto, so the
fallback works anywhere Python does -- including OPNsense, which ships no
Python AES library.
"""
import ctypes
import ctypes.util
import glob
import os
import sys

TAG_SIZE = 16


class AESError(ValueError):
    pass


# ── pycryptodome ─────────────────────────────────────────────────────────

class _PyCryptodome:
    name = 'pycryptodome'

    def __init__(self):
        from Crypto.Cipher import AES
        self.AES = AES

    def cbc_encrypt(self, key, iv, data):
        return self.AES.new(key, self.AES.MODE_CBC, iv).encrypt(data)

    def cbc_decrypt(self, key, iv, data):
        return self.AES.new(key, self.AES.MODE_CBC, iv).decrypt(data)

    def gcm_encrypt(self, key, iv, aad, data):
        cipher = self.AES.new(key, self.AES.MODE_GCM, iv)
        cipher.update(aad)
        ciphertext, tag = cipher.encrypt_and_digest(data)
        return ciphertext + tag

    def gcm_decrypt(self, key, iv, aad, data):
        cipher = self.AES.new(key, self.AES.MODE_GCM, iv)
        cipher.update(aad)
        try:
            return cipher.decrypt_and_verify(data[:-TAG_SIZE], data[-TAG_SIZE:])
        except ValueError as e:
            raise AESError(str(e)) from None


# ── OpenSSL libcrypto via ctypes ─────────────────────────────────────────

EVP_CTRL_GCM_SET_IVLEN = 0x9
EVP_CTRL_GCM_GET_TAG = 0x10
EVP_CTRL_GCM_SET_TAG = 0x11


def _find_libcrypto():
    candidates = []
    found = ctypes.util.find_library('crypto')
    if found:
        candidates.append(found)
    # Windows Python bundles its own OpenSSL next to the _ssl module
    for directory in (os.path.join(sys.base_prefix, 'DLLs'), os.path.dirname(sys.executable)):
        candidates += sorted(glob.glob(os.path.join(directory, 'libcrypto*.dll')))
    candidates += ['libcrypto.so', 'libcrypto.so.3', 'libcrypto.so.30', 'libcrypto.so.1.1']
    for candidate in candidates:
        try:
            lib = ctypes.CDLL(candidate)
            lib.EVP_CIPHER_CTX_new  # noqa: B018 -- raises if not libcrypto
            return lib
        except (OSError, AttributeError):
            continue
    raise ImportError('OpenSSL libcrypto not found')


class _OpenSSL:
    name = 'openssl'

    def __init__(self):
        lib = self.lib = _find_libcrypto()
        lib.EVP_CIPHER_CTX_new.restype = ctypes.c_void_p
        lib.EVP_CIPHER_CTX_free.argtypes = [ctypes.c_void_p]
        lib.EVP_aes_128_cbc.restype = ctypes.c_void_p
        lib.EVP_aes_128_gcm.restype = ctypes.c_void_p
        for name in ('EVP_CipherInit_ex',):
            getattr(lib, name).argtypes = [
                ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p,
                ctypes.c_char_p, ctypes.c_char_p, ctypes.c_int]
        lib.EVP_CipherUpdate.argtypes = [
            ctypes.c_void_p, ctypes.c_char_p, ctypes.POINTER(ctypes.c_int),
            ctypes.c_char_p, ctypes.c_int]
        lib.EVP_CipherFinal_ex.argtypes = [
            ctypes.c_void_p, ctypes.c_char_p, ctypes.POINTER(ctypes.c_int)]
        lib.EVP_CIPHER_CTX_set_padding.argtypes = [ctypes.c_void_p, ctypes.c_int]
        lib.EVP_CIPHER_CTX_ctrl.argtypes = [
            ctypes.c_void_p, ctypes.c_int, ctypes.c_int, ctypes.c_void_p]

    def _run(self, cipher, key, iv, data, encrypt, aad=b'', tag=None):
        lib = self.lib
        ctx = lib.EVP_CIPHER_CTX_new()
        if not ctx:
            raise AESError('EVP_CIPHER_CTX_new failed')
        try:
            enc = 1 if encrypt else 0
            gcm = cipher == lib.EVP_aes_128_gcm()
            self._check(lib.EVP_CipherInit_ex(ctx, cipher, None, None, None, enc))
            if gcm:
                self._check(lib.EVP_CIPHER_CTX_ctrl(ctx, EVP_CTRL_GCM_SET_IVLEN, len(iv), None))
            else:
                lib.EVP_CIPHER_CTX_set_padding(ctx, 0)
            self._check(lib.EVP_CipherInit_ex(ctx, None, None, key, iv, enc))

            n = ctypes.c_int(0)
            if aad:
                self._check(lib.EVP_CipherUpdate(ctx, None, ctypes.byref(n), aad, len(aad)))
            out = ctypes.create_string_buffer(len(data) + 32)
            self._check(lib.EVP_CipherUpdate(ctx, out, ctypes.byref(n), data, len(data)))
            total = n.value
            if tag is not None:
                tag_buf = ctypes.create_string_buffer(tag, TAG_SIZE)
                self._check(lib.EVP_CIPHER_CTX_ctrl(ctx, EVP_CTRL_GCM_SET_TAG, TAG_SIZE, tag_buf))
            final = ctypes.create_string_buffer(32)
            if lib.EVP_CipherFinal_ex(ctx, final, ctypes.byref(n)) != 1:
                raise AESError('authentication or padding check failed')
            result = out.raw[:total] + final.raw[:n.value]
            if gcm and encrypt:
                tag_buf = ctypes.create_string_buffer(TAG_SIZE)
                self._check(lib.EVP_CIPHER_CTX_ctrl(ctx, EVP_CTRL_GCM_GET_TAG, TAG_SIZE, tag_buf))
                result += tag_buf.raw
            return result
        finally:
            lib.EVP_CIPHER_CTX_free(ctx)

    @staticmethod
    def _check(ret):
        if ret != 1:
            raise AESError('OpenSSL call failed')

    def cbc_encrypt(self, key, iv, data):
        return self._run(self.lib.EVP_aes_128_cbc(), key, iv, data, True)

    def cbc_decrypt(self, key, iv, data):
        return self._run(self.lib.EVP_aes_128_cbc(), key, iv, data, False)

    def gcm_encrypt(self, key, iv, aad, data):
        return self._run(self.lib.EVP_aes_128_gcm(), key, iv, data, True, aad=aad)

    def gcm_decrypt(self, key, iv, aad, data):
        if len(data) < TAG_SIZE:
            raise AESError('ciphertext shorter than the GCM tag')
        return self._run(self.lib.EVP_aes_128_gcm(), key, iv, data[:-TAG_SIZE], False,
                         aad=aad, tag=data[-TAG_SIZE:])


def load_backend(preferred=None):
    order = [preferred] if preferred else ['pycryptodome', 'openssl']
    errors = []
    for name in order:
        try:
            return _PyCryptodome() if name == 'pycryptodome' else _OpenSSL()
        except ImportError as e:
            errors.append('%s: %s' % (name, e))
    raise ImportError('No AES implementation available (%s)' % '; '.join(errors))


backend = load_backend(os.environ.get('UNIFI_GW_AES_BACKEND') or None)

cbc_encrypt = backend.cbc_encrypt
cbc_decrypt = backend.cbc_decrypt
gcm_encrypt = backend.gcm_encrypt
gcm_decrypt = backend.gcm_decrypt
