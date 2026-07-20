"""Optional password protection for backup files (stdlib only).

Format version 1:
  PBKDF2-HMAC-SHA256 (200k) → 32-byte key
  SHA256 counter keystream XOR + HMAC-SHA256 over ciphertext
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import struct
from typing import Any, Optional

FORMAT = "ai-switch-backup-encrypted"
VERSION = 1
_ITERATIONS = 200_000
_SALT_LEN = 16
_NONCE_LEN = 16


def _b64e(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).decode("ascii").rstrip("=")


def _b64d(s: str) -> bytes:
    pad = "=" * (-len(s) % 4)
    return base64.urlsafe_b64decode((s or "") + pad)


def _derive_key(password: str, salt: bytes, iterations: int = _ITERATIONS) -> bytes:
    if not password:
        raise ValueError("password required")
    return hashlib.pbkdf2_hmac(
        "sha256",
        password.encode("utf-8"),
        salt,
        int(iterations),
        dklen=32,
    )


def _keystream(key: bytes, nonce: bytes, length: int) -> bytes:
    out = bytearray()
    counter = 0
    while len(out) < length:
        block = hashlib.sha256(key + nonce + struct.pack(">I", counter)).digest()
        out.extend(block)
        counter += 1
    return bytes(out[:length])


def _xor(data: bytes, stream: bytes) -> bytes:
    return bytes(a ^ b for a, b in zip(data, stream))


def is_encrypted_backup(payload: Any) -> bool:
    return isinstance(payload, dict) and payload.get("format") == FORMAT


def encrypt_payload(payload: dict, password: str) -> dict:
    """Wrap a plain backup dict into an encrypted envelope."""
    if not isinstance(payload, dict):
        raise ValueError("payload must be a dict")
    raw = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    salt = os.urandom(_SALT_LEN)
    nonce = os.urandom(_NONCE_LEN)
    key = _derive_key(password, salt, _ITERATIONS)
    ct = _xor(raw, _keystream(key, nonce, len(raw)))
    tag = hmac.new(key, nonce + ct, hashlib.sha256).digest()
    return {
        "format": FORMAT,
        "version": VERSION,
        "kdf": "pbkdf2-sha256",
        "iterations": _ITERATIONS,
        "salt": _b64e(salt),
        "nonce": _b64e(nonce),
        "ciphertext": _b64e(ct),
        "mac": _b64e(tag),
    }


def decrypt_payload(envelope: dict, password: str) -> dict:
    """Decrypt envelope → original backup dict. Raises ValueError on failure."""
    if not is_encrypted_backup(envelope):
        raise ValueError("not an encrypted backup")
    try:
        iterations = int(envelope.get("iterations") or _ITERATIONS)
        salt = _b64d(str(envelope.get("salt") or ""))
        nonce = _b64d(str(envelope.get("nonce") or ""))
        ct = _b64d(str(envelope.get("ciphertext") or ""))
        mac = _b64d(str(envelope.get("mac") or ""))
    except Exception as e:
        raise ValueError("invalid encrypted backup fields") from e
    if len(salt) < 8 or len(nonce) < 8 or not ct or len(mac) < 16:
        raise ValueError("invalid encrypted backup fields")
    key = _derive_key(password, salt, iterations)
    expect = hmac.new(key, nonce + ct, hashlib.sha256).digest()
    if not hmac.compare_digest(expect, mac):
        raise ValueError("wrong password or corrupted backup")
    plain = _xor(ct, _keystream(key, nonce, len(ct)))
    try:
        data = json.loads(plain.decode("utf-8"))
    except Exception as e:
        raise ValueError("corrupted backup payload") from e
    if not isinstance(data, dict):
        raise ValueError("invalid backup payload")
    return data


def maybe_decrypt(payload: dict, password: Optional[str] = None) -> dict:
    """If encrypted, require password and decrypt; else return as-is."""
    if not is_encrypted_backup(payload):
        return payload
    if not password:
        raise ValueError("password required for encrypted backup")
    return decrypt_payload(payload, password)
