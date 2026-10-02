"""SQLite layer.

One database file on the ``/data`` volume is shared by the two listeners the
appliance runs (admin + public); WAL mode plus a busy timeout keep concurrent
access safe without a database server.  Everything Sunduk has to remember
lives here:

* ``users``       — admin accounts (scrypt hash + "must change password" flag)
* ``directories`` — the server directories exposed in the interface
* ``share_links`` — external links: token, expiry, password, download counters

There is no ORM: plain ``sqlite3`` with explicit SQL keeps the code auditable.
"""
from __future__ import annotations

import logging
import os
import sqlite3
import time
from typing import Iterable, Optional, Tuple

log = logging.getLogger("sunduk.db")

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    username             TEXT PRIMARY KEY,
    password_hash        TEXT NOT NULL,
    must_change_password INTEGER NOT NULL DEFAULT 0,
    created_at           INTEGER NOT NULL,
    changed_at           INTEGER
);

CREATE TABLE IF NOT EXISTS directories (
    id         TEXT PRIMARY KEY,
    name       TEXT NOT NULL,
    host_path  TEXT NOT NULL UNIQUE,
    created_at INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS share_links (
    id            TEXT PRIMARY KEY,
    token_hash    TEXT UNIQUE NOT NULL,
    token_enc     TEXT,
    directory_id  TEXT NOT NULL,
    rel_path      TEXT NOT NULL,
    is_dir        INTEGER NOT NULL DEFAULT 0,
    name          TEXT,
    password_hash TEXT,
    password_enc  TEXT,
    expires_at    INTEGER NOT NULL,
    max_downloads INTEGER NOT NULL DEFAULT 0,
    downloads     INTEGER NOT NULL DEFAULT 0,
    created_by    TEXT,
    created_at    INTEGER NOT NULL,
    last_access   INTEGER,
    revoked       INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_share_token ON share_links(token_hash);
CREATE INDEX IF NOT EXISTS idx_share_directory ON share_links(directory_id);
"""

# Columns added after the first release; ``init()`` adds whatever is missing so
# an existing /data volume keeps working without a manual migration.
ADDED_COLUMNS: Tuple[Tuple[str, str, str], ...] = (
    ("share_links", "password_enc", "TEXT"),
    ("share_links", "token_enc", "TEXT"),
)

# Old name -> new name.  The concept was renamed from "share" to "directory"
# when the interface stopped being configured by hand.
RENAMED_COLUMNS: Tuple[Tuple[str, str, str], ...] = (
    ("share_links", "share_id", "directory_id"),
)


def _columns(conn: sqlite3.Connection, table: str) -> Iterable[str]:
    return {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}


def _migrate(conn: sqlite3.Connection) -> None:
    """Bring an existing database up to the current schema (idempotent)."""
    for table, old, new in RENAMED_COLUMNS:
        columns = _columns(conn, table)
        if not columns:
            # No such table yet — this is a brand new /data volume.  SCHEMA
            # below creates the table with the final column names, so there is
            # nothing to rename (ALTER on a missing table would fail with
            # "no such table" and stop a fresh install from booting).
            continue
        if old in columns and new not in columns:
            conn.execute(f"ALTER TABLE {table} RENAME COLUMN {old} TO {new}")
            log.info("schema migration: renamed %s.%s to %s", table, old, new)
    for table, column, decl in ADDED_COLUMNS:
        columns = _columns(conn, table)
        if not columns:
            continue  # fresh volume: the schema brings the full table
        if column not in columns:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")
            log.info("schema migration: added %s.%s", table, column)


def _unwritable(directory: str) -> Optional[str]:
    """Return why ``directory`` cannot be written to, or ``None`` if it can.

    The container drops every capability and adds a few back (see
    docker-compose.yml).  Without ``CAP_DAC_OVERRIDE`` the kernel does not let
    even root write into a directory owned by somebody else — the normal
    situation for ``/mnt/media`` and for a ``/data`` volume created by an older
    release.  Checking up front turns a long "database is locked" retry loop
    into one actionable line.
    """
    try:
        os.makedirs(directory, exist_ok=True)
    except OSError as exc:
        return f"cannot create it ({exc})"
    probe = os.path.join(directory, ".sunduk-write-test")
    try:
        with open(probe, "w", encoding="utf-8") as handle:
            handle.write("")
        os.unlink(probe)
    except OSError as exc:
        stat = os.stat(directory)
        uid = os.getuid() if hasattr(os, "getuid") else -1
        return (
            f"no write permission ({exc}); directory owner uid:gid="
            f"{stat.st_uid}:{stat.st_gid}, process uid={uid}"
        )
    return None


class Database:
    def __init__(self, path: str):
        self.path = path

    def connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=30)
        conn.row_factory = sqlite3.Row
        # busy_timeout must be set before anything that may take a lock.
        conn.execute("PRAGMA busy_timeout=30000")
        try:
            conn.execute("PRAGMA journal_mode=WAL")
        except sqlite3.OperationalError as exc:
            # A parallel process is switching the journal mode at this very
            # moment.  WAL is a persistent property of the file, so if it is
            # already active (or about to be) the connection is still fine.
            log.warning("could not switch to WAL (continuing): %s", exc)
        conn.execute("PRAGMA foreign_keys=ON")
        return conn

    def init(self, attempts: int = 10) -> None:
        """Create/upgrade the schema.  Safe to call from several processes."""
        directory = os.path.dirname(self.path) or "."
        problem = _unwritable(directory)
        if problem is not None:
            raise RuntimeError(
                f"data directory {directory} is unusable: {problem}. "
                "Fix the ownership of the volume (chown 0:0 /data) or keep the "
                "CAP_DAC_OVERRIDE capability — see docker-compose.yml."
            )
        last_error: Optional[sqlite3.OperationalError] = None
        for attempt in range(1, attempts + 1):
            try:
                with self.connect() as conn:
                    # Renames come first: the schema below creates indexes over
                    # the new column names, so a table left over from an older
                    # release must be fixed before ``CREATE TABLE IF NOT
                    # EXISTS`` (a no-op on an existing table) lets
                    # ``CREATE INDEX`` run into a column that does not exist.
                    _migrate(conn)
                    conn.executescript(SCHEMA)
                    # ... and again for whatever the schema just changed.
                    _migrate(conn)
                last_error = None
                break
            except sqlite3.OperationalError as exc:
                # Only a concurrent writer is worth retrying; anything else
                # (unreadable file, read-only filesystem, ...) needs the
                # operator, not patience.
                text = str(exc).lower()
                if "locked" not in text and "busy" not in text:
                    raise
                last_error = exc
                log.warning("database busy, retrying schema init (%s/%s)", attempt, attempts)
                time.sleep(min(0.25 * attempt, 2.0))
        if last_error is not None:
            raise last_error
        # Keep the data directory private.
        try:
            os.chmod(directory, 0o700)
        except OSError:
            pass

    @staticmethod
    def now() -> int:
        return int(time.time())
