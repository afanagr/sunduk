"""Public application: the internet-facing endpoint that only serves share links.

It is deliberately small and exposes *only* the shared path:
* No directory browsing outside the shared folder.
* No authentication APIs, no file-manager APIs.
* Rate limited per IP; password-protected links get an extra limiter.
"""
from __future__ import annotations

import logging
import os

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer
from pydantic import BaseModel

from . import archive, storage
from .common import (
    RateLimitMiddleware,
    SecurityHeadersMiddleware,
    attachment_headers,
    file_response,
    install_exception_handlers,
    preview_size_limit_hit,
)
from .config import AppConfig
from .security import RateLimiter, client_ip, resolve_within
from .shares import ShareService

log = logging.getLogger("sunduk.public")

STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")

# How long an unlocked password-protected link stays unlocked (seconds).
UNLOCK_TTL = 12 * 3600


class UnlockModel(BaseModel):
    password: str


def create_public_app(config: AppConfig, db, shares_service: ShareService) -> FastAPI:
    from .directories import DirectoryService

    app = FastAPI(title="Sunduk", docs_url=None, redoc_url=None, openapi_url=None)
    app.state.config = config
    app.state.db = db
    app.state.shares = shares_service
    app.state.directories = DirectoryService(db, config)
    app.state.serializer = URLSafeTimedSerializer(config.secret_key, salt="sunduk-share-unlock")
    app.state.unlock_limiter = RateLimiter(15)  # 15 password tries / minute / IP

    app.add_middleware(SecurityHeadersMiddleware)
    app.add_middleware(
        RateLimitMiddleware,
        limiter=RateLimiter(config.security.public_rate_limit_per_minute),
        trusted_proxies=config.trusted_proxies,
    )
    install_exception_handlers(app)

    _register_routes(app, config)
    return app


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _load_link(request: Request, token: str) -> dict:
    service: ShareService = request.app.state.shares
    link = service.get_by_token(token)
    if not link or not service.is_active(link):
        raise HTTPException(status_code=404, detail="Link not found or expired")
    return link


def _directory_for(request: Request, link: dict):
    """The exposed directory a link points at (404 when it was removed)."""
    directory = request.app.state.directories.get(str(link["directory_id"]))
    if directory is None:
        raise HTTPException(status_code=404, detail="Link not found or expired")
    return directory


def _unlock_cookie_name(link_id: str) -> str:
    return f"fs_unlock_{link_id}"


def _is_unlocked(request: Request, link: dict) -> bool:
    if not link.get("password_hash"):
        return True
    value = request.cookies.get(_unlock_cookie_name(str(link["id"])))
    if not value:
        return False
    try:
        data = request.app.state.serializer.loads(value, max_age=UNLOCK_TTL)
    except (BadSignature, SignatureExpired):
        return False
    return data == link["id"]


def _target_for(directory, link: dict, rel: str) -> str:
    """Resolve a requested path strictly inside the shared sub-tree."""
    subroot = resolve_within(directory.root, str(link["rel_path"]))
    if not link["is_dir"]:
        if rel:
            raise HTTPException(status_code=404, detail="Not found")
        return subroot
    return resolve_within(subroot, rel)


def _file_meta(full: str, name: str) -> dict:
    st = os.stat(full)
    return {
        "name": name,
        "size": int(st.st_size),
        "mtime": int(st.st_mtime),
        "kind": storage.kind_of(full, False),
    }


def _serve(request: Request, token: str, rel: str, *, inline: bool) -> FileResponse:
    config: AppConfig = request.app.state.config
    link = _load_link(request, token)
    directory = _directory_for(request, link)
    if link["password_hash"] and not _is_unlocked(request, link):
        raise HTTPException(status_code=401, detail="Password required")
    target = _target_for(directory, link, rel)
    if not os.path.isfile(target):
        raise HTTPException(status_code=404, detail="Not found")
    if inline and preview_size_limit_hit(target, os.path.getsize(target), config.security.max_preview_mb):
        raise HTTPException(status_code=413, detail="File is too large to preview")
    if not inline:
        request.app.state.shares.register_download(str(link["id"]))
        log.info("share download id=%s ip=%s", link["id"], client_ip(request, config.trusted_proxies))
    return file_response(target, os.path.basename(target) or str(link["name"]), inline=inline)


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------
def _register_routes(app: FastAPI, config: AppConfig) -> None:

    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

    @app.get("/healthz")
    async def healthz() -> dict:
        return {"status": "ok"}

    @app.get("/s/{token}", include_in_schema=False)
    async def share_page(token: str) -> FileResponse:
        return FileResponse(os.path.join(STATIC_DIR, "share.html"), media_type="text/html")

    @app.get("/api/share/{token}")
    async def share_info(request: Request, token: str, path: str = "") -> dict:
        link = _load_link(request, token)
        directory = _directory_for(request, link)
        if link["password_hash"] and not _is_unlocked(request, link):
            return {"name": link["name"], "is_dir": bool(link["is_dir"]), "requires_password": True}
        if link["is_dir"]:
            subroot = resolve_within(directory.root, str(link["rel_path"]))
            try:
                entries = storage.list_dir_abs(subroot, path)
            except (PermissionError, FileNotFoundError, NotADirectoryError):
                raise HTTPException(status_code=404, detail="Not found")
            return {
                "name": link["name"], "is_dir": True, "requires_password": False,
                "path": path.strip("/"), "entries": entries, "expires_at": link["expires_at"],
            }
        target = _target_for(directory, link, "")
        if not os.path.isfile(target):
            raise HTTPException(status_code=404, detail="Not found")
        return {
            "name": link["name"], "is_dir": False, "requires_password": False,
            "file": _file_meta(target, str(link["name"])), "expires_at": link["expires_at"],
        }

    @app.post("/api/share/{token}/unlock")
    async def share_unlock(request: Request, token: str, body: UnlockModel) -> JSONResponse:
        ip = client_ip(request, config.trusted_proxies)
        if not request.app.state.unlock_limiter.allow(ip):
            raise HTTPException(status_code=429, detail="Too many attempts, try again later")
        link = _load_link(request, token)
        if not request.app.state.shares.check_password(link, body.password):
            log.warning("share unlock failed id=%s ip=%s", link["id"], ip)
            raise HTTPException(status_code=401, detail="Invalid password")
        value = request.app.state.serializer.dumps(str(link["id"]))
        response = JSONResponse({"ok": True})
        response.set_cookie(
            _unlock_cookie_name(str(link["id"])), value,
            max_age=UNLOCK_TTL, httponly=True, samesite="lax",
            secure=config.security.cookie_secure, path="/",
        )
        return response

    @app.get("/api/share/{token}/download")
    async def share_download(request: Request, token: str, path: str = "") -> FileResponse:
        return _serve(request, token, path, inline=False)

    @app.get("/api/share/{token}/raw")
    async def share_raw(request: Request, token: str, path: str = "") -> FileResponse:
        return _serve(request, token, path, inline=True)

    @app.get("/api/share/{token}/archive")
    async def share_archive(request: Request, token: str, path: str = "") -> StreamingResponse:
        """The shared folder — or any folder inside it — as one ZIP download.

        The archive is produced while it is sent (see :mod:`app.archive`), so a
        folder of movies costs no disk space and no waiting time before the
        download starts.
        """
        link = _load_link(request, token)
        directory = _directory_for(request, link)
        if not link["is_dir"]:
            raise HTTPException(status_code=404, detail="Not found")
        if link["password_hash"] and not _is_unlocked(request, link):
            raise HTTPException(status_code=401, detail="Password required")
        subroot = resolve_within(directory.root, str(link["rel_path"]))
        folder = resolve_within(subroot, path)
        if not os.path.isdir(folder):
            raise HTTPException(status_code=404, detail="Not found")
        # One archive is one download: it counts against the link limit exactly
        # like a file does.
        request.app.state.shares.register_download(str(link["id"]))
        log.info("share archive id=%s path=%s ip=%s",
                 link["id"], path, client_ip(request, config.trusted_proxies))
        return StreamingResponse(
            archive.stream(folder),
            media_type="application/zip",
            headers=attachment_headers(archive.archive_name(folder, str(link["name"]))),
        )


