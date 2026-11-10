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


def _fernet_for(secret: str) -> Fernet:
    key = base64.urlsafe_b64encode(hashlib.sha256(secret.encode()).digest())
    return Fernet(key)


def reencrypt_provider_secrets(db, *, old_secret: str, new_secret: str) -> int:
    """Rewrite stored credentials with a new key. A bad old key changes nothing."""
    from backend.models import DiscoveryProviderSecret

    rows = db.query(DiscoveryProviderSecret).all()
    updated = []
    old = _fernet_for(old_secret)
    new = _fernet_for(new_secret)
    for row in rows:
        plain = old.decrypt(row.ciphertext.encode()).decode()
        updated.append((row, new.encrypt(plain.encode()).decode()))
    for row, token in updated:
        row.ciphertext = token
        row.updated_at = __import__("datetime").datetime.utcnow()
    db.commit()
    return len(updated)
