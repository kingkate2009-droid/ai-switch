"""At-rest field encryption for API secrets (stdlib only).

Wire format for a secret string:
  ENC1.<b64(salt)>.<b64(nonce)>.<b64(ciphertext)>.<b64(mac)>

Master key: PBKDF2-HMAC-SHA256 from user password (or env), 32 bytes.
Per-secret: random nonce + SHA256 counter XOR + HMAC-SHA256 (same primitive family as crypto_backup).
"""
from __future__ import annotations

import hashlib
import hmac
import os
import struct
from typing import Optional

from core.crypto_backup import _b64d, _b64e, _derive_key, _keystream, _xor

PREFIX = "ENC1."
_ITERATIONS = 200_000
_SALT_LEN = 16
_NONCE_LEN = 16
_VERIFIER_MSG = b"ai-switch-secrets-v1"


def is_encrypted_secret(value: str) -> bool:
    s = str(value or "")
    return s.startswith(PREFIX) and s.count(".") >= 4


def make_verifier(master_key: bytes) -> str:
    return _b64e(hmac.new(master_key, _VERIFIER_MSG, hashlib.sha256).digest())


def verify_master_key(master_key: bytes, verifier: str) -> bool:
    if not verifier or not master_key:
        return False
    try:
        expect = hmac.new(master_key, _VERIFIER_MSG, hashlib.sha256).digest()
        got = _b64d(str(verifier))
        return hmac.compare_digest(expect, got)
    except Exception:
        return False


def derive_master_key(password: str, salt: bytes, iterations: int = _ITERATIONS) -> bytes:
    return _derive_key(password, salt, iterations)


def generate_salt() -> bytes:
    return os.urandom(_SALT_LEN)


def encrypt_secret(plaintext: str, master_key: bytes) -> str:
    """Encrypt a secret; empty string stays empty."""
    raw = (plaintext or "").encode("utf-8")
    if not raw:
        return ""
    if not master_key or len(master_key) < 16:
        raise ValueError("master key required")
    nonce = os.urandom(_NONCE_LEN)
    # use empty salt slot in wire for layout stability; key is master (already derived)
    salt = b"\x00" * 8
    ct = _xor(raw, _keystream(master_key, nonce, len(raw)))
    tag = hmac.new(master_key, nonce + ct, hashlib.sha256).digest()
    return f"{PREFIX}{_b64e(salt)}.{_b64e(nonce)}.{_b64e(ct)}.{_b64e(tag)}"


def decrypt_secret(value: str, master_key: bytes) -> str:
    if not value:
        return ""
    if not is_encrypted_secret(value):
        return value
    if not master_key or len(master_key) < 16:
        raise ValueError("master key required to decrypt")
    try:
        parts = value.split(".", 4)
        # ENC1 . salt . nonce . ct . mac  → after split on '.' : ['ENC1', salt, nonce, ct, mac]
        if len(parts) != 5 or parts[0] != "ENC1":
            raise ValueError("bad envelope")
        nonce = _b64d(parts[2])
        ct = _b64d(parts[3])
        mac = _b64d(parts[4])
    except Exception as e:
        raise ValueError("invalid encrypted secret") from e
    expect = hmac.new(master_key, nonce + ct, hashlib.sha256).digest()
    if not hmac.compare_digest(expect, mac):
        raise ValueError("wrong key or corrupted secret")
    plain = _xor(ct, _keystream(master_key, nonce, len(ct)))
    try:
        return plain.decode("utf-8")
    except Exception as e:
        raise ValueError("corrupted secret payload") from e


def encrypt_if_needed(value: str, master_key: Optional[bytes], enabled: bool) -> str:
    if not enabled or not master_key:
        return value or ""
    if not value:
        return ""
    if is_encrypted_secret(value):
        return value
    return encrypt_secret(value, master_key)


def decrypt_if_needed(value: str, master_key: Optional[bytes]) -> str:
    if not value:
        return ""
    if not is_encrypted_secret(value):
        return value
    if not master_key:
        raise ValueError("secrets locked")
    return decrypt_secret(value, master_key)
