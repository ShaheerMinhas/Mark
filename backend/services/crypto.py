from __future__ import annotations

import base64
import hashlib

from cryptography.fernet import Fernet

from backend.config import SESSION_ENCRYPTION_KEY


def _fernet() -> Fernet:
    key = base64.urlsafe_b64encode(hashlib.sha256(SESSION_ENCRYPTION_KEY.encode()).digest())
    return Fernet(key)


def encrypt_value(value: str) -> str:
    return _fernet().encrypt(value.encode()).decode()


def decrypt_value(token: str) -> str:
    return _fernet().decrypt(token.encode()).decode()
