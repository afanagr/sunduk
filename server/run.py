"""Sunduk entry point.

Two Uvicorn servers run in separate processes so the admin interface and the
public share endpoint bind to different ports:

* admin  -> ``admin_port``  (LAN only, authenticated)
* public -> ``public_port`` (internet, share links only)

Every setting comes from ``/config/config.yml``: this program reads no
environment variable at all.
"""
from __future__ import annotations

import logging
import multiprocessing
import os
import signal
import sys
import tempfile
import time

import uvicorn

from app.admin_app import create_admin_app
from app.config import AppConfig, load_config
from app.db import Database
from app.directories import DirectoryService
from app.public_app import create_public_app
from app.shares import ShareService
from app.users import DEFAULT_PASSWORD, DEFAULT_USERNAME, UserService

log = logging.getLogger("sunduk")


def prepare_spool_dir(config: AppConfig) -> None:
    """Send temporary files to the data volume instead of the tiny tmpfs /tmp.

    The ASGI server spools every uploaded file larger than ~1 MB into a real
    temporary file, and ``/tmp`` is a 64 MB tmpfs inside the container (see the
    hardening section of docker-compose.yml).  Without this, any upload above
    that size dies with ``No space left on device`` no matter what
    ``max_upload_mb`` allows.  ``/data`` is the only writable volume, so the
    spool directory lives there and is created here — the container root
    filesystem is read-only.
    """
    spool = config.tmp_dir
    try:
        os.makedirs(spool, exist_ok=True)
    except OSError as exc:
        log.warning(
            "cannot use the upload spool directory %s (%s) — falling back to %s; "
            "uploads larger than that tmpfs will fail (check the /data volume "
            "permissions, see docker-compose.yml)",
            spool, exc, tempfile.gettempdir(),
        )
        return
    tempfile.tempdir = spool
    log.info("upload spool directory: %s", spool)


def build_services(config: AppConfig, *, init_db: bool = True):
    """Open the database and hand out the shared services."""
    db = Database(config.db_path)
    if init_db:
        db.init()
        UserService(db).ensure_default()
    service = ShareService(db, config.secret_key)
    service.purge_expired()
    if init_db:
        # Links of a directory that is no longer in the panel (removed earlier,
        # or left over from an older release) can never resolve again.
        known = [directory.id for directory in DirectoryService(db, config).list()]
        revoked = service.revoke_orphans(known)
        if revoked:
            log.info("revoked %s link(s) of removed directories", revoked)
    return db, service


def _report_state(config: AppConfig) -> None:
    if not os.path.isdir(config.host_root):
        log.warning(
            "host_root %s is missing — mount the server filesystem there "
            "(docker-compose.yml: /:/host) or no directory can be added",
            config.host_root,
        )
    log.info(
        "starting Sunduk — admin :%s (LAN) | public :%s | base_url=%s",
        config.admin_port, config.public_port, config.base_url,
    )


def _serve_admin() -> None:
    config = load_config()
    prepare_spool_dir(config)
    db, service = build_services(config, init_db=False)
    app = create_admin_app(config, db, service)
    log.info("exposed directories: %s",
             [d.host_path for d in app.state.directories.list()] or "none — add them in the panel")
    uvicorn.run(
        app, host=config.host, port=config.admin_port,
        log_level="info", server_header=False, proxy_headers=False, access_log=True,
    )


def _serve_public() -> None:
    config = load_config()
    prepare_spool_dir(config)
    db, service = build_services(config, init_db=False)
    app = create_public_app(config, db, service)
    uvicorn.run(
        app, host=config.host, port=config.public_port,
        log_level="info", server_header=False, proxy_headers=False, access_log=True,
    )


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )
    config = load_config()
    prepare_spool_dir(config)
    _report_state(config)

    # Create/upgrade the schema and seed the default account once, in the
    # parent, before either server opens the database: this removes the
    # WAL-switch race between the two children.
    db, _ = build_services(config)
    users = UserService(db)
    if users.must_change(DEFAULT_USERNAME):
        log.warning(
            "the panel still uses the default credentials %s/%s — "
            "the first login will ask for a new password",
            DEFAULT_USERNAME, DEFAULT_PASSWORD,
        )

    processes = [
        multiprocessing.Process(target=_serve_admin, name="admin"),
        multiprocessing.Process(target=_serve_public, name="public"),
    ]

    def _terminate(_signum, _frame):
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, _terminate)
    signal.signal(signal.SIGINT, _terminate)

    for process in processes:
        process.start()

    exit_code = 0
    try:
        # Supervise: if either server dies, bring the whole container down so
        # docker-compose (restart: unless-stopped) can start it again cleanly.
        while True:
            dead = next((p for p in processes if not p.is_alive()), None)
            if dead is not None:
                exit_code = dead.exitcode or 1
                log.error("process '%s' exited (code %s) — stopping", dead.name, exit_code)
                break
            time.sleep(0.5)
    except KeyboardInterrupt:
        log.info("shutting down")
    finally:
        for process in processes:
            if process.is_alive():
                process.terminate()
        for process in processes:
            process.join()
    sys.exit(exit_code)


if __name__ == "__main__":
    main()

