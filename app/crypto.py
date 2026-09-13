"""저장용 문자열 암복호화(AES-GCM)와 앱 비밀번호 해시(scrypt)."""

from __future__ import annotations

import base64
import hashlib
import hmac
import os

from Crypto.Cipher import AES

from app.config import secret_key

_NONCE_LEN = 12


def encrypt(plain: str) -> str:
    if not plain:
        return ""
    nonce = os.urandom(_NONCE_LEN)
    cipher = AES.new(secret_key(), AES.MODE_GCM, nonce=nonce)
    ct, tag = cipher.encrypt_and_digest(plain.encode("utf-8"))
    return base64.b64encode(nonce + tag + ct).decode("ascii")


def decrypt(token: str) -> str:
    if not token:
        return ""
    raw = base64.b64decode(token)
    nonce, tag, ct = raw[:_NONCE_LEN], raw[_NONCE_LEN:_NONCE_LEN + 16], raw[_NONCE_LEN + 16:]
    cipher = AES.new(secret_key(), AES.MODE_GCM, nonce=nonce)
    return cipher.decrypt_and_verify(ct, tag).decode("utf-8")


def hash_password(password: str) -> str:
    salt = os.urandom(16)
    digest = hashlib.scrypt(password.encode("utf-8"), salt=salt, n=2**14, r=8, p=1)
    return f"scrypt${salt.hex()}${digest.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        _, salt_hex, digest_hex = stored.split("$")
        digest = hashlib.scrypt(password.encode("utf-8"), salt=bytes.fromhex(salt_hex), n=2**14, r=8, p=1)
        return hmac.compare_digest(digest.hex(), digest_hex)
    except (ValueError, TypeError):
        return False
