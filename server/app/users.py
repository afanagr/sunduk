"""Admin accounts.

The appliance ships with one built-in account (``admin`` / ``admin``) that has
to be replaced on the first login: the row carries a ``must_change_password``
flag, the panel answers every other call with HTTP 428 until the password is
changed (see :class:`app.auth.PasswordChangeGuardMiddleware`), and
``tools/reset_admin.py`` puts the account back into that state if the password is
ever lost.  Hashes are scrypt, the same format the share-link passwords use.
"""
from __future__ import annotations

import logging
import secrets
from typing import Dict, List, Optional

from .db import Database
from .security import hash_password, verify_password

log = logging.getLogger("sunduk.users")

DEFAULT_USERNAME = "admin"
DEFAULT_PASSWORD = "admin"
MIN_PASSWORD_LENGTH = 4

# Pre-computed dummy hash so unknown usernames cost roughly the same as known
# ones (defeats user enumeration through response timing).
_DUMMY_HASH = hash_password(secrets.token_hex(16))


class UserService:
    def __init__(self, db: Database):
        self.db = db

    # ------------------------------------------------------------------ reads
    def get(self, username: str) -> Optional[Dict[str, object]]:
        if not username:
            return None
        with self.db.connect() as conn:
            row = conn.execute(
                "SELECT * FROM users WHERE username=?", (username,)
            ).fetchone()
        return dict(row) if row else None

    def list_users(self) -> List[Dict[str, object]]:
        with self.db.connect() as conn:
            rows = conn.execute("SELECT * FROM users ORDER BY username").fetchall()
        return [dict(row) for row in rows]

    def must_change(self, username: str) -> bool:
        """True while the account still uses the shipped default password."""
        with self.db.connect() as conn:
            row = conn.execute(
                "SELECT must_change_password FROM users WHERE username=?", (username,)
            ).fetchone()
        return bool(row and row[0])

    def authenticate(self, username: str, password: str) -> bool:
        user = self.get(username)
        if user is None:
            verify_password(password or "", _DUMMY_HASH)
            return False
        if not password:
            return False
        return verify_password(password, str(user["password_hash"]))

    # ----------------------------------------------------------------- writes
    def ensure_default(self) -> bool:
        """Create the shipped account when the database has no user yet."""
        with self.db.connect() as conn:
            count = conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]
            if count:
                return False
            conn.execute(
                """INSERT INTO users (username, password_hash, must_change_password, created_at)
                   VALUES (?,?,1,?)""",
                (DEFAULT_USERNAME, hash_password(DEFAULT_PASSWORD), Database.now()),
            )
        log.warning(
            "created the default account %s/%s — the panel will ask for a new password "
            "on the first login", DEFAULT_USERNAME, DEFAULT_PASSWORD,
        )
        return True

    def set_password(self, username: str, password: str, *, must_change: bool = False) -> None:
        """Overwrite a password (used by the reset tool and by ensure_default)."""
        self._validate(password)
        with self.db.connect() as conn:
            exists = conn.execute(
                "SELECT 1 FROM users WHERE username=?", (username,)
            ).fetchone()
            if exists:
                conn.execute(
                    """UPDATE users SET password_hash=?, must_change_password=?, changed_at=?
                       WHERE username=?""",
                    (hash_password(password), 1 if must_change else 0, Database.now(), username),
                )
            else:
                conn.execute(
                    """INSERT INTO users (username, password_hash, must_change_password, created_at)
                       VALUES (?,?,?,?)""",
                    (username, hash_password(password), 1 if must_change else 0, Database.now()),
                )
        log.info("password updated for %s (must_change=%s)", username, must_change)

    def change_password(self, username: str, current: str, new_password: str) -> None:
        """Self-service change: verify the current password, store the new one.

        Raises ``ValueError`` with a message meant for the UI.
        """
        if not self.authenticate(username, current):
            raise ValueError("текущий пароль неверен")
        if current == new_password:
            raise ValueError("новый пароль совпадает с текущим")
        self.set_password(username, new_password, must_change=False)

    @staticmethod
    def _validate(password: str) -> None:
        if not isinstance(password, str) or len(password) < MIN_PASSWORD_LENGTH:
            raise ValueError(
                f"пароль должен быть не короче {MIN_PASSWORD_LENGTH} символов"
            )
        if len(password) > 256:
            raise ValueError("пароль слишком длинный")
