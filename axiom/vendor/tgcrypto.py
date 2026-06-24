"""Чистый-Python шим вместо C-расширения tgcrypto.

opentele.td.storage использует лишь AES-256-IGE (ige256_encrypt/decrypt) для
локального tdata. На Windows без MSVC tgcrypto не собирается, поэтому
подменяем его реализацией IGE из telethon (pyaes под капотом). Сигнатуры и
семантика совпадают: (data, key, iv) -> bytes.
"""
from __future__ import annotations

from telethon.crypto import AES


def _b(x) -> bytes:
    # opentele передаёт QByteArray (Qt); его индексация даёт bytes, а не int,
    # и ломает чистый-Python AES из telethon. Приводим к настоящим bytes.
    return x if isinstance(x, (bytes, bytearray)) else bytes(x)


def ige256_encrypt(data, key, iv) -> bytes:
    return AES.encrypt_ige(_b(data), _b(key), _b(iv))


def ige256_decrypt(data, key, iv) -> bytes:
    return AES.decrypt_ige(_b(data), _b(key), _b(iv))
