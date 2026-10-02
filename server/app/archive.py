"""Streaming ZIP archive of a shared folder.

The archive is built while it is sent and never touches the disk: a shared
folder may hold tens of gigabytes of video and the container has no scratch
space to put a second copy in (``/tmp`` is a 64 MB tmpfs, ``/data`` holds the
database).  ``zipfile`` happily writes into a non-seekable sink — it switches to
data descriptors — so every file is read in 1 MB blocks, handed to the client
and forgotten.

Files keep the folder layout, the modification time and the unix permissions
inside the archive.  Media is *stored*, not compressed (it is already
compressed and the CPU would be spent for nothing); small text files are
deflated, where it pays off.

Nothing outside the shared folder can end up in the archive:

* symlinks are skipped instead of followed (a link to ``/etc/shadow`` — or to
  anything else outside the share — would otherwise leak it);
* every entry is re-checked against the resolved share root.
"""
from __future__ import annotations

import logging
import os
import stat
import time
import zipfile
from typing import Iterator, List, Tuple

from . import storage
from .security import is_within

log = logging.getLogger("sunduk.archive")

CHUNK = 1 << 20                      # 1 MiB blocks: read, hand over, forget
DEFLATE_MAX_MB = 64                  # above this even text is stored: not worth it
ZIP_DATE_FLOOR = 315532800           # 1980-01-01: the ZIP format cannot store less


class _Sink:
    """File object that collects what ``zipfile`` writes and hands it over.

    It is deliberately *not* seekable (no ``tell()``/``seek()``): ``zipfile``
    notices that and puts the sizes into data descriptors instead of going back
    to patch a local header, which is exactly what streaming needs.
    """

    def __init__(self) -> None:
        self._parts: List[bytes] = []

    def write(self, data) -> int:
        block = bytes(data)
        self._parts.append(block)
        return len(block)

    def flush(self) -> None:
        """Called by ``zipfile`` and the compressor; there is nothing to flush."""

    def drain(self) -> List[bytes]:
        parts, self._parts = self._parts, []
        return parts


def archive_name(folder: str, fallback: str = "archive") -> str:
    """Download name for the archive of a folder (``/host/Фото`` -> ``Фото.zip``)."""
    base = os.path.basename(folder.rstrip("/" + os.sep))
    base = "".join(ch for ch in base if ch.isprintable() and ch not in '"\\/')
    return f"{base or fallback}.zip"


def stream(folder: str, *, name: str = "") -> Iterator[bytes]:
    """Yield the ZIP archive of ``folder`` (an absolute path inside the share).

    Every entry is prefixed with ``name`` (the folder's own name by default) so
    that extracting the archive does not spray loose files into the target.
    """
    base = os.path.realpath(folder)
    prefix = name or os.path.basename(base.rstrip(os.sep)) or "archive"
    sink = _Sink()
    zipf = zipfile.ZipFile(sink, "w", zipfile.ZIP_STORED, allowZip64=True)
    try:
        for arcname, full, is_dir in _walk(base, prefix):
            yield from sink.drain()
            if is_dir:
                try:
                    zipf.writestr(_info(arcname, os.stat(full), is_dir=True), b"")
                except OSError as exc:                # vanished between walk and stat
                    log.warning("archive: skipping %s (%s)", full, exc)
            else:
                yield from _write_file(zipf, sink, full, arcname)
    finally:
        # The central directory is written here.  Nothing is yielded while
        # closing, so an early close (the client went away) stays clean.
        try:
            zipf.close()
        except Exception:  # noqa: BLE001 - never mask the original failure
            log.warning("archive: could not finish the zip of %s", base, exc_info=True)
    yield from sink.drain()


def _walk(base: str, prefix: str) -> Iterator[Tuple[str, str, bool]]:
    """Yield ``(arcname, path, is_dir)`` for everything below ``base``."""
    yield prefix, base, True
    for dirpath, dirnames, filenames in os.walk(base, followlinks=False, onerror=_walk_error):
        relative = os.path.relpath(dirpath, base).replace(os.sep, "/")
        relative = "" if relative == "." else relative
        # Symlinked folders are neither followed nor listed as empty folders:
        # they can point anywhere and an empty entry only confuses.
        dirnames[:] = sorted(
            item for item in dirnames
            if not os.path.islink(os.path.join(dirpath, item))
        )
        if relative and not dirnames and not filenames:
            yield f"{prefix}/{relative}", dirpath, True      # keep empty folders
        for item in sorted(filenames):
            full = os.path.join(dirpath, item)
            if os.path.islink(full) or not is_within(base, full):
                log.debug("archive: skipped %s (symlink or outside the share)", full)
                continue
            arcname = f"{prefix}/{relative}/{item}" if relative else f"{prefix}/{item}"
            yield arcname, full, False


def _write_file(zipf: zipfile.ZipFile, sink: _Sink, full: str, arcname: str) -> Iterator[bytes]:
    """Copy one file into the archive, flushing it to the client as it is read."""
    try:
        info_stat = os.stat(full)
        handle = open(full, "rb")
    except OSError as exc:                                  # unreadable / vanished
        log.warning("archive: skipping %s (%s)", full, exc)
        return
    if not stat.S_ISREG(info_stat.st_mode):                 # socket, fifo, device
        handle.close()
        return
    info = _info(arcname, info_stat, is_dir=False,
                 compress=_worth_deflating(arcname, info_stat))
    # The sizes travel in a data descriptor; a 32-bit one cannot describe a file
    # above 2 GiB, and zipfile would refuse to close such an entry.
    force_zip64 = info_stat.st_size >= zipfile.ZIP64_LIMIT
    with handle, zipf.open(info, "w", force_zip64=force_zip64) as destination:
        while True:
            block = handle.read(CHUNK)
            if not block:
                break
            destination.write(block)
            yield from sink.drain()


def _info(arcname: str, info_stat: os.stat_result, *, is_dir: bool,
          compress: bool = False) -> zipfile.ZipInfo:
    """A ZIP entry that keeps the unix mode and the modification time."""
    moment = time.localtime(max(info_stat.st_mtime, ZIP_DATE_FLOOR))[:6]
    info = zipfile.ZipInfo(arcname + "/" if is_dir else arcname, moment)
    info.create_system = 3                       # 3 = unix: external_attr is a mode
    info.external_attr = (info_stat.st_mode & 0xFFFF) << 16
    if is_dir:
        info.external_attr |= 0x10               # MS-DOS "this is a directory"
    info.compress_type = zipfile.ZIP_DEFLATED if compress else zipfile.ZIP_STORED
    return info


def _worth_deflating(arcname: str, info_stat: os.stat_result) -> bool:
    """Text compresses well; media is already compressed and only costs CPU."""
    return (
        storage.kind_of(arcname, False) == "text"
        and info_stat.st_size <= DEFLATE_MAX_MB * 1024 * 1024
    )


def _walk_error(exc: OSError) -> None:
    """A folder that cannot be listed is skipped, not fatal (root on a live FS)."""
    log.warning("archive: %s", exc)
