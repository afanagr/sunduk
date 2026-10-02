"""Authenticated reversible encryption for share tokens at rest.

Share tokens are stored twice in the database:
* ``token_hash``    — SHA-256, used for lookups (never reversible).
* ``token_enc``     — Fernet ciphertext, so the admin panel can re-display an
                      existing link.  Without the secret key from
                      ``<data_dir>/secret.key`` this is useless.
"""
from __future__ import annotations

import base64
import hashlib

from cryptography.fernet import Fernet, InvalidToken


class TokenCipher:
    def __init__(self, secret_key: str):
        key = base64.urlsafe_b64encode(hashlib.sha256(secret_key.encode("utf-8")).digest())
        self._fernet = Fernet(key)

    def encrypt(self, token: str) -> str:
        return self._fernet.encrypt(token.encode("utf-8")).decode("ascii")

    def decrypt(self, blob: str) -> str | None:
        try:
            return self._fernet.decrypt(blob.encode("ascii")).decode("utf-8")
        except (InvalidToken, ValueError, TypeError):
            return None
