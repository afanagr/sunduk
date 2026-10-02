"""Share-link service.

Security model
--------------
* A share link token is 32 random bytes (url-safe).  Only its SHA-256 hash is
  stored, so a database leak does not yield usable links.
* Tokens carry an expiry, an optional password and an optional download limit;
  they can be revoked at any time.
* The link password is kept twice, mirroring the token itself: a scrypt hash
  (used for verification, never reversible) and a Fernet-encrypted copy so the
  authenticated LAN admin can display the password of an existing link again.
  Both rely on the secret key persisted in ``<data_dir>/secret.key``; without it
  neither tokens nor passwords can be revealed.
* Lookups are constant work and always re-validate expiry/limit/revocation.
"""
from __future__ import annotations

import time
from typing import Dict, Iterable, List, Optional

from .crypto import TokenCipher
from .db import Database
from .security import hash_password, new_token, sha256_hex, verify_password


class ShareService:
    def __init__(self, db: Database, secret_key: str):
        self.db = db
        self.cipher = TokenCipher(secret_key)

    # ------------------------------------------------------------------ create
    def create(
        self,
        *,
        directory_id: str,
        rel_path: str,
        is_dir: bool,
        name: str,
        expires_at: int,
        password: Optional[str] = None,
        max_downloads: int = 0,
        created_by: Optional[str] = None,
    ) -> Dict[str, object]:
        token = new_token(32)
        link_id = new_token(9)
        now = int(time.time())
        pw_hash = hash_password(password) if password else None
        pw_enc = self.cipher.encrypt(password) if password else None
        with self.db.connect() as conn:
            conn.execute(
                """
                INSERT INTO share_links
                    (id, token_hash, token_enc, directory_id, rel_path, is_dir, name, password_hash,
                     password_enc, expires_at, max_downloads, downloads, created_by, created_at, revoked)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,0)
                """,
                (
                    link_id, sha256_hex(token), self.cipher.encrypt(token), directory_id, rel_path,
                    1 if is_dir else 0, name, pw_hash, pw_enc,
                    expires_at, max_downloads, 0, created_by, now,
                ),
            )
        return {"id": link_id, "token": token, "password": password}

    def reveal_token(self, link: Dict[str, object]) -> Optional[str]:
        """Return the raw token for an admin-created link (or None)."""
        blob = link.get("token_enc")
        if not blob:
            return None
        return self.cipher.decrypt(str(blob))

    def reveal_password(self, link: Dict[str, object]) -> Optional[str]:
        """Return the plaintext password of a link (admin/LAN only) or None.

        Links created before the ``password_enc`` column exists keep working:
        they simply cannot be revealed (the scrypt hash is one-way).
        """
        stored = link.get("password_enc")
        if not stored:
            return None
        return self.cipher.decrypt(str(stored))

    # -------------------------------------------------------------------- read
    def get_by_token(self, token: str) -> Optional[Dict[str, object]]:
        with self.db.connect() as conn:
            row = conn.execute(
                "SELECT * FROM share_links WHERE token_hash=?", (sha256_hex(token),)
            ).fetchone()
        return dict(row) if row else None

    def get_by_id(self, link_id: str) -> Optional[Dict[str, object]]:
        with self.db.connect() as conn:
            row = conn.execute(
                "SELECT * FROM share_links WHERE id=?", (link_id,)
            ).fetchone()
        return dict(row) if row else None

    def list_links(self) -> List[Dict[str, object]]:
        with self.db.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM share_links WHERE revoked=0 ORDER BY created_at DESC"
            ).fetchall()
        return [dict(r) for r in rows]

    # ---------------------------------------------------------------- validate
    @staticmethod
    def is_active(link: Dict[str, object], *, now: Optional[int] = None) -> bool:
        now = now if now is not None else int(time.time())
        if int(link.get("revoked") or 0):
            return False
        if int(link["expires_at"]) and int(link["expires_at"]) < now:
            return False
        max_dl = int(link.get("max_downloads") or 0)
        if max_dl and int(link.get("downloads") or 0) >= max_dl:
            return False
        return True

    def check_password(self, link: Dict[str, object], password: Optional[str]) -> bool:
        stored = link.get("password_hash")
        if not stored:
            return True
        if not password:
            return False
        return verify_password(password, str(stored))

    # ---------------------------------------------------------------- mutators
    def register_download(self, link_id: str) -> None:
        with self.db.connect() as conn:
            conn.execute(
                "UPDATE share_links SET downloads = downloads + 1, last_access=? WHERE id=?",
                (int(time.time()), link_id),
            )

    def touch(self, link_id: str) -> None:
        with self.db.connect() as conn:
            conn.execute(
                "UPDATE share_links SET last_access=? WHERE id=?",
                (int(time.time()), link_id),
            )

    def revoke(self, link_id: str) -> bool:
        with self.db.connect() as conn:
            cur = conn.execute(
                "UPDATE share_links SET revoked=1 WHERE id=? AND revoked=0", (link_id,)
            )
            return cur.rowcount > 0

    def revoke_all(self) -> int:
        with self.db.connect() as conn:
            cur = conn.execute("UPDATE share_links SET revoked=1 WHERE revoked=0")
            return cur.rowcount

    def revoke_orphans(self, known_directory_ids: Iterable[str]) -> int:
        """Revoke links whose directory is gone.

        A link outlives its directory when the panel entry is removed while the
        link was created earlier, or when an existing ``/data`` volume is opened
        by a release that keeps its directories in the database: the public page
        can only answer 404 and the panel would offer a URL that never works.
        Links are revoked, never deleted, so the history stays readable.
        """
        ids = sorted({str(item) for item in known_directory_ids if item})
        with self.db.connect() as conn:
            if ids:
                marks = ",".join("?" * len(ids))
                cur = conn.execute(
                    "UPDATE share_links SET revoked=1 "
                    f"WHERE revoked=0 AND directory_id NOT IN ({marks})",
                    ids,
                )
            else:
                cur = conn.execute("UPDATE share_links SET revoked=1 WHERE revoked=0")
            return cur.rowcount

    def revoke_for_directory(self, directory_id: str) -> int:
        """Kill every link of a directory that is being removed from the panel."""
        with self.db.connect() as conn:
            cur = conn.execute(
                "UPDATE share_links SET revoked=1 WHERE directory_id=? AND revoked=0",
                (directory_id,),
            )
            return cur.rowcount

    def count_active(self) -> int:
        with self.db.connect() as conn:
            row = conn.execute(
                "SELECT COUNT(*) FROM share_links WHERE revoked=0"
            ).fetchone()
        return int(row[0])

    def purge_expired(self) -> int:
        with self.db.connect() as conn:
            cur = conn.execute(
                "DELETE FROM share_links WHERE expires_at>0 AND expires_at < ?",
                (int(time.time()) - 86400,),
            )
            return cur.rowcount
