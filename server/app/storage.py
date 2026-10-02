"""Filesystem operations, all confined to an exposed directory.

Every public function takes a :class:`~app.config.Directory` and a
directory-relative path and goes through :func:`resolve_within`, so callers can
never escape the root.  All directories are read/write: the appliance is a LAN
tool for a trusted user, so there is no per-directory permission model.
"""
from __future__ import annotations

import os
import shutil
from typing import Dict, List

from .config import Directory
from .security import resolve_within

# Extension -> logical kind (used by the frontend for icons / previews).
_KIND_BY_EXT = {
    "jpg": "image", "jpeg": "image", "png": "image", "gif": "image", "webp": "image",
    "bmp": "image", "tif": "image", "tiff": "image", "svg": "image", "heic": "image",
    "heif": "image", "avif": "image", "jxl": "image", "ico": "image",
    "mp4": "video", "m4v": "video", "mkv": "video", "avi": "video", "mov": "video",
    "webm": "video", "ogv": "video", "flv": "video", "wmv": "video", "mpg": "video",
    "mpeg": "video", "3gp": "video", "ts": "video", "m2ts": "video", "vob": "video",
    "mp3": "audio", "flac": "audio", "wav": "audio", "aac": "audio", "ogg": "audio",
    "oga": "audio", "m4a": "audio", "mka": "audio", "wma": "audio", "opus": "audio",
    "weba": "audio", "aif": "audio", "aiff": "audio",
    "zip": "archive", "rar": "archive", "7z": "archive", "tar": "archive",
    "gz": "archive", "bz2": "archive", "xz": "archive", "iso": "archive",
    "pdf": "pdf", "doc": "doc", "docx": "doc", "xls": "doc", "xlsx": "doc",
    "ppt": "doc", "pptx": "doc", "odt": "doc", "ods": "doc", "rtf": "doc",
    "epub": "doc", "fb2": "doc",
    "txt": "text", "md": "text", "log": "text", "csv": "text", "json": "text",
    "xml": "text", "yml": "text", "yaml": "text", "ini": "text", "conf": "text",
    "nfo": "text", "m3u": "text", "m3u8": "text", "srt": "text", "vtt": "text",
    "sub": "text", "ass": "text", "cue": "text", "py": "text", "sh": "text",
}


def kind_of(path: str, is_dir: bool) -> str:
    if is_dir:
        return "dir"
    ext = os.path.splitext(path)[1].lstrip(".").lower()
    return _KIND_BY_EXT.get(ext, "file")


def _entry(full_path: str, rel_dir: str) -> Dict[str, object]:
    try:
        st = os.stat(full_path)
    except OSError:
        return {}
    is_dir = os.path.isdir(full_path)
    name = os.path.basename(full_path)
    rel = f"{rel_dir}/{name}" if rel_dir else name
    return {
        "name": name,
        "path": rel,
        "is_dir": is_dir,
        "size": 0 if is_dir else int(st.st_size),
        "mtime": int(st.st_mtime),
        "kind": kind_of(full_path, is_dir),
        "ext": os.path.splitext(name)[1].lstrip(".").lower(),
    }


def list_dir(directory: Directory, rel: str) -> List[Dict[str, object]]:
    return list_dir_abs(directory.root, rel)


def list_dir_abs(root: str, rel: str) -> List[Dict[str, object]]:
    """List a directory given an explicit root (used by the public share view)."""
    target = resolve_within(root, rel)
    if not os.path.isdir(target):
        raise FileNotFoundError("not a directory")
    rel_dir = rel.strip("/")
    entries: List[Dict[str, object]] = []
    with os.scandir(target) as it:
        for item in it:
            entry = _entry(item.path, rel_dir)
            if entry:
                entries.append(entry)
    entries.sort(key=lambda e: (not e["is_dir"], str(e["name"]).lower()))
    return entries


def stat_entry(directory: Directory, rel: str) -> Dict[str, object]:
    target = resolve_within(directory.root, rel)
    if not os.path.exists(target):
        raise FileNotFoundError("not found")
    return _entry(target, os.path.dirname(rel.strip("/")))


def _safe_name(name: str) -> str:
    name = (name or "").strip().replace("\\", "/")
    if not name or "/" in name or name in {".", ".."} or "\x00" in name:
        raise ValueError("invalid name")
    return name


def create_dir(directory: Directory, rel: str, name: str) -> None:
    parent = resolve_within(directory.root, rel)
    if not os.path.isdir(parent):
        raise NotADirectoryError("parent is not a directory")
    target = resolve_within(directory.root, os.path.join(rel.strip("/"), _safe_name(name)))
    os.mkdir(target)


def rename(directory: Directory, rel: str, new_name: str) -> None:
    target = resolve_within(directory.root, rel)
    if not os.path.exists(target):
        raise FileNotFoundError("not found")
    safe_name = _safe_name(new_name)
    parent_rel = os.path.dirname(rel.strip("/"))
    destination = resolve_within(directory.root, os.path.join(parent_rel, safe_name))
    if os.path.exists(destination):
        raise FileExistsError("an item with that name already exists")
    os.rename(target, destination)


def move(src_dir: Directory, src_rel: str, dst_dir: Directory, dst_rel: str) -> None:
    source = resolve_within(src_dir.root, src_rel)
    if not os.path.exists(source):
        raise FileNotFoundError("source not found")
    dest_dir = resolve_within(dst_dir.root, dst_rel)
    if not os.path.isdir(dest_dir):
        raise NotADirectoryError("destination is not a directory")
    destination = resolve_within(
        dst_dir.root, os.path.join(dst_rel.strip("/"), os.path.basename(source))
    )
    if os.path.isdir(source) and (destination == source or destination.startswith(source + os.sep)):
        raise ValueError("cannot move a folder into itself")
    if os.path.exists(destination):
        raise FileExistsError("an item with that name already exists at the destination")
    shutil.move(source, destination)


def copy(src_dir: Directory, src_rel: str, dst_dir: Directory, dst_rel: str) -> None:
    source = resolve_within(src_dir.root, src_rel)
    if not os.path.exists(source):
        raise FileNotFoundError("source not found")
    dest_dir = resolve_within(dst_dir.root, dst_rel)
    if not os.path.isdir(dest_dir):
        raise NotADirectoryError("destination is not a directory")
    destination = resolve_within(
        dst_dir.root, os.path.join(dst_rel.strip("/"), os.path.basename(source))
    )
    if os.path.exists(destination):
        raise FileExistsError("an item with that name already exists at the destination")
    if os.path.isdir(source):
        shutil.copytree(source, destination)
    else:
        shutil.copy2(source, destination)


def delete(directory: Directory, rel: str) -> None:
    target = resolve_within(directory.root, rel)
    if os.path.normpath(target) == os.path.normpath(directory.root):
        raise PermissionError("refusing to delete the directory root")
    if os.path.isdir(target):
        shutil.rmtree(target)
    elif os.path.exists(target):
        os.remove(target)
    else:
        raise FileNotFoundError("not found")


def search(directory: Directory, query: str, limit: int = 200) -> List[Dict[str, object]]:
    query_lower = query.lower()
    results: List[Dict[str, object]] = []
    root = directory.root
    for dirpath, dirnames, filenames in os.walk(root):
        if not dirpath.startswith(root):
            continue
        for name in list(dirnames) + list(filenames):
            if query_lower in name.lower():
                rel_dir = os.path.relpath(dirpath, root)
                rel_dir = "" if rel_dir == "." else rel_dir.replace(os.sep, "/")
                entry = _entry(os.path.join(dirpath, name), rel_dir)
                if entry:
                    results.append(entry)
                if len(results) >= limit:
                    return results
    return results


def disk_usage(directory: Directory) -> Dict[str, int]:
    try:
        usage = shutil.disk_usage(directory.root)
        return {"total": usage.total, "used": usage.used, "free": usage.free}
    except OSError:
        return {"total": 0, "used": 0, "free": 0}

