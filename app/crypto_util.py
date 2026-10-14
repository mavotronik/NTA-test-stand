from __future__ import annotations

import base64
import hashlib
import os

from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError
from cryptography.fernet import Fernet, InvalidToken

_ph = PasswordHasher()


def hash_password(password: str) -> str:
    return _ph.hash(password)


def verify_password(password_hash: str, password: str) -> bool:
    try:
        return _ph.verify(password_hash, password)
    except VerifyMismatchError:
        return False


def derive_fernet_key(secret_key: str) -> bytes:
    digest = hashlib.sha256(secret_key.encode("utf-8")).digest()
    return base64.urlsafe_b64encode(digest)


def get_fernet(settings_key: bytes | None, secret_key: str) -> Fernet:
    raw = settings_key or derive_fernet_key(secret_key)
    return Fernet(raw)


def encrypt_secret(fernet: Fernet, value: str) -> str:
    return fernet.encrypt(value.encode("utf-8")).decode("ascii")


def decrypt_secret(fernet: Fernet, token: str) -> str:
    try:
        return fernet.decrypt(token.encode("ascii")).decode("utf-8")
    except InvalidToken as err:
        raise ValueError("invalid secret token") from err


def redact(s: str, keep: int = 2) -> str:
    if not s:
        return ""
    if len(s) <= keep * 2:
        return "***"
    return s[:keep] + "***" + s[-keep:]
