"""The server directories exposed in the interface.

A directory is added **from the admin panel**: the panel asks for the path on
the server (``/mnt/media``) and for the name shown in the sidebar, and the row
is stored in the database.  Nothing has to be mounted per directory any more —
the whole server filesystem is mounted once at ``host_root`` (``/host``), so
adding, renaming or removing a directory is immediate and needs no restart.

``root`` is derived from ``host_path`` by prefixing ``host_root``; every
filesystem operation then works on ``root`` with the usual confinement
(``resolve_within``), so ``..`` and symlinks still cannot escape a directory.
"""
from __future__ import annotations

import logging
import os
import re
from typing import Dict, List, Optional

from .config import AppConfig, Directory, container_path, normalize_host_path
from .db import Database

log = logging.getLogger("sunduk.directories")

# Cyrillic -> latin, so a Russian display name still yields a readable id.
_TRANSLIT = {
    "а": "a", "б": "b", "в": "v", "г": "g", "д": "d", "е": "e", "ё": "e", "ж": "zh",
    "з": "z", "и": "i", "й": "y", "к": "k", "л": "l", "м": "m", "н": "n", "о": "o",
    "п": "p", "р": "r", "с": "s", "т": "t", "у": "u", "ф": "f", "х": "h", "ц": "c",
    "ч": "ch", "ш": "sh", "щ": "sch", "ъ": "", "ы": "y", "ь": "", "э": "e", "ю": "yu",
    "я": "ya",
}


def slugify(name: str) -> str:
    """A short, URL/DB-safe identifier derived from a display name."""
    lowered = (name or "").strip().lower()
    latin = "".join(_TRANSLIT.get(char, char) for char in lowered)
    slug = re.sub(r"[^a-z0-9]+", "-", latin).strip("-")
    return slug[:40] or "dir"


class DirectoryService:
    def __init__(self, db: Database, config: AppConfig):
        self.db = db
        self.host_root = config.host_root

    # ------------------------------------------------------------- mapping
    def container_path(self, host_path: str) -> str:
        return container_path(self.host_root, host_path)

    def _row(self, row) -> Directory:
        return Directory(
            id=row["id"],
            name=row["name"],
            host_path=row["host_path"],
            root=self.container_path(row["host_path"]),
            created_at=int(row["created_at"] or 0),
        )

    # --------------------------------------------------------------- reads
    def list(self) -> List[Directory]:
        with self.db.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM directories ORDER BY created_at, name"
            ).fetchall()
        return [self._row(row) for row in rows]

    def get(self, directory_id: str) -> Optional[Directory]:
        if not directory_id:
            return None
        with self.db.connect() as conn:
            row = conn.execute(
                "SELECT * FROM directories WHERE id=?", (directory_id,)
            ).fetchone()
        return self._row(row) if row else None

    def find_by_host_path(self, host_path: str) -> Optional[Directory]:
        with self.db.connect() as conn:
            row = conn.execute(
                "SELECT * FROM directories WHERE host_path=?", (host_path,)
            ).fetchone()
        return self._row(row) if row else None

    def available(self, directory: Directory) -> bool:
        return os.path.isdir(directory.root)

    # -------------------------------------------------------------- writes
    def add(self, host_path: str, name: Optional[str] = None,
            *, create: bool = False) -> Directory:
        path = normalize_host_path(host_path)
        if self.find_by_host_path(path) is not None:
            raise ValueError("этот каталог уже добавлен")
        root = self.container_path(path)
        self._ensure_directory(path, root, create=create)

        display = (name or "").strip() or os.path.basename(path) or "Корень сервера"
        with self.db.connect() as conn:
            directory_id = self._unique_id(conn, display)
            conn.execute(
                "INSERT INTO directories (id, name, host_path, created_at) VALUES (?,?,?,?)",
                (directory_id, display, path, Database.now()),
            )
        log.info("directory added: %s -> %s (id=%s)", path, root, directory_id)
        return Directory(id=directory_id, name=display, host_path=path, root=root,
                         created_at=Database.now())

    def update(self, directory_id: str, *, name: Optional[str] = None,
               host_path: Optional[str] = None, create: bool = False) -> Directory:
        current = self.get(directory_id)
        if current is None:
            raise ValueError("каталог не найден")

        new_path = current.host_path
        if host_path is not None:
            new_path = normalize_host_path(host_path)
            if new_path != current.host_path:
                other = self.find_by_host_path(new_path)
                if other is not None and other.id != directory_id:
                    raise ValueError("этот каталог уже добавлен")
                self._ensure_directory(new_path, self.container_path(new_path), create=create)

        new_name = (name or "").strip() or current.name
        with self.db.connect() as conn:
            conn.execute(
                "UPDATE directories SET name=?, host_path=? WHERE id=?",
                (new_name, new_path, directory_id),
            )
        log.info("directory updated: id=%s name=%r path=%s", directory_id, new_name, new_path)
        return Directory(id=directory_id, name=new_name, host_path=new_path,
                         root=self.container_path(new_path), created_at=current.created_at)

    def remove(self, directory_id: str) -> Optional[Directory]:
        """Forget a directory.  The files themselves are never touched."""
        directory = self.get(directory_id)
        if directory is None:
            return None
        with self.db.connect() as conn:
            conn.execute("DELETE FROM directories WHERE id=?", (directory_id,))
        log.info("directory removed: id=%s path=%s", directory_id, directory.host_path)
        return directory

    # ------------------------------------------------------------ browsing
    def browse(self, host_path: str) -> Dict[str, object]:
        """List the sub-directories of a server path (folder picker)."""
        path = normalize_host_path(host_path)
        root = self.container_path(path)
        try:
            names = sorted(
                (entry.name for entry in os.scandir(root) if _is_dir(entry)),
                key=str.lower,
            )
        except FileNotFoundError as exc:
            raise ValueError(f"каталог не найден: {path}") from exc
        except NotADirectoryError as exc:
            raise ValueError(f"это не каталог: {path}") from exc
        except PermissionError as exc:
            raise ValueError(f"нет доступа к каталогу: {path}") from exc

        parent = None if path == "/" else (os.path.dirname(path) or "/")
        return {
            "path": path,
            "parent": parent,
            "dirs": [{"name": name, "path": _join(path, name)} for name in names],
        }

    # --------------------------------------------------------------- helpers
    def _ensure_directory(self, host_path: str, root: str, *, create: bool) -> None:
        if os.path.isdir(root):
            return
        if not create:
            raise ValueError(
                f"каталог не найден на сервере: {host_path} "
                "(проверьте путь и права доступа)"
            )
        try:
            os.makedirs(root, exist_ok=True)
        except OSError as exc:
            raise ValueError(f"не удалось создать каталог {host_path}: {exc}") from exc

    @staticmethod
    def _unique_id(conn, name: str) -> str:
        base = slugify(name)
        taken = {row[0] for row in conn.execute("SELECT id FROM directories")}
        candidate = base
        suffix = 2
        while candidate in taken:
            candidate = f"{base}-{suffix}"
            suffix += 1
        return candidate


def _is_dir(entry: os.DirEntry) -> bool:
    try:
        return entry.is_dir()
    except OSError:
        return False


def _join(parent: str, name: str) -> str:
    return f"/{name}" if parent == "/" else f"{parent}/{name}"


