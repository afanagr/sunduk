"""Admin application: the LAN-only file manager and external-link manager.

Security layers applied to every request, outermost first:

1. :class:`LanGuardMiddleware` — rejects clients outside ``lan_allowlist``.
2. :class:`SecurityHeadersMiddleware` — CSP and hardening headers.
3. signed session cookie — who is logged in.
4. :class:`PasswordChangeGuardMiddleware` — nothing but the password change and
   logout works while the shipped ``admin``/``admin`` password is still in use.

There is no per-directory permission model: every directory exposed in the
interface is fully read/write for the trusted, LAN-local admin.  The only
confinement left is path safety — ``resolve_within`` keeps every request inside
the directory it belongs to.
"""
from __future__ import annotations

import logging
import os
import shutil
import time
from typing import List, Optional

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import auth, storage
from .auth import PasswordChangeGuardMiddleware, require_csrf, require_user
from .common import (
    LanGuardMiddleware,
    SecurityHeadersMiddleware,
    file_response,
    install_exception_handlers,
    preview_size_limit_hit,
)
from .config import AppConfig, Directory
from .security import RateLimiter, client_ip, resolve_within
from .shares import ShareService

log = logging.getLogger("sunduk.admin")

STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")

# Errors from path handling / the filesystem that mean "the client asked for
# something impossible" — never a 500.
REQUEST_ERRORS = (OSError, ValueError, shutil.Error)


# ---------------------------------------------------------------------------
# Request models
# ---------------------------------------------------------------------------
class LoginModel(BaseModel):
    username: str
    password: str


class PasswordModel(BaseModel):
    current_password: str
    new_password: str


class DirectoryAddModel(BaseModel):
    host_path: str
    name: Optional[str] = None
    create: bool = False


class DirectoryUpdateModel(BaseModel):
    id: str
    host_path: Optional[str] = None
    name: Optional[str] = None
    create: bool = False


class IdModel(BaseModel):
    id: str


class PathModel(BaseModel):
    directory: str
    path: str = ""


class MkdirModel(PathModel):
    name: str


class RenameModel(PathModel):
    new_name: str


class TransferModel(BaseModel):
    """Copy/move between two exposed directories (possibly the same one)."""

    directory: str
    path: str
    dest_directory: str
    dest_path: str = ""


class ShareCreateModel(BaseModel):
    directory: str
    path: str = ""
    expiry_hours: Optional[int] = None
    password: Optional[str] = None
    max_downloads: Optional[int] = None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _directory(request: Request, directory_id: str, *, must_exist: bool = True) -> Directory:
    """Resolve an exposed directory or fail with a message the UI can show."""
    service = request.app.state.directories
    directory = service.get(directory_id)
    if directory is None:
        raise HTTPException(status_code=404, detail="Каталог не найден")
    if must_exist and not os.path.isdir(directory.root):
        raise HTTPException(
            status_code=409,
            detail=f"Каталог недоступен на сервере: {directory.host_path}",
        )
    return directory


def _payload(directory: Directory) -> dict:
    return {
        "id": directory.id,
        "name": directory.name,
        "host_path": directory.host_path,
        "available": os.path.isdir(directory.root),
        "created_at": directory.created_at,
        "usage": storage.disk_usage(directory),
    }


# ---------------------------------------------------------------------------
# Application factory
# ---------------------------------------------------------------------------
def create_admin_app(config: AppConfig, db, shares_service: ShareService) -> FastAPI:
    from .directories import DirectoryService
    from .users import UserService

    app = FastAPI(
        title="Sunduk Admin",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    app.state.config = config
    app.state.db = db
    app.state.shares = shares_service
    app.state.directories = DirectoryService(db, config)
    app.state.users = UserService(db)
    app.state.login_limiter = RateLimiter(config.security.login_rate_limit_per_minute)

    # Middleware: last added runs first.  Order below (outer -> inner):
    # LAN guard -> security headers -> session cookie -> password guard.
    app.add_middleware(PasswordChangeGuardMiddleware)
    auth.install_session(app, config)
    app.add_middleware(SecurityHeadersMiddleware)
    app.add_middleware(
        LanGuardMiddleware,
        allowlist=config.lan_allowlist,
        trusted_proxies=config.trusted_proxies,
    )
    install_exception_handlers(app)

    _register_routes(app, config)
    return app


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------
def _register_routes(app: FastAPI, config: AppConfig) -> None:

    # ---- static assets & page ------------------------------------------------
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

    @app.get("/healthz")
    async def healthz() -> dict:
        return {"status": "ok"}

    @app.get("/", include_in_schema=False)
    async def index() -> FileResponse:
        return FileResponse(os.path.join(STATIC_DIR, "index.html"), media_type="text/html")

    @app.get("/favicon.ico", include_in_schema=False)
    async def favicon() -> Response:
        return Response(status_code=204)

    # ---- authentication ------------------------------------------------------
    @app.post("/api/login")
    async def api_login(request: Request, body: LoginModel) -> dict:
        ip = client_ip(request, config.trusted_proxies)
        if not request.app.state.login_limiter.allow(ip):
            raise HTTPException(status_code=429, detail="Too many attempts, please try again later")
        users = request.app.state.users
        if not auth.authenticate(users, body.username, body.password):
            log.warning("failed login user=%r ip=%s", body.username, ip)
            raise HTTPException(status_code=401, detail="Неверный логин или пароль")
        csrf = auth.start_session(request, body.username)
        must_change = users.must_change(body.username)
        log.info("login ok user=%s ip=%s must_change=%s", body.username, ip, must_change)
        return {"user": body.username, "csrf": csrf, "must_change_password": must_change}

    @app.post("/api/logout")
    async def api_logout(request: Request) -> dict:
        require_user(request)
        require_csrf(request)
        auth.end_session(request)
        return {"ok": True}

    @app.get("/api/me")
    async def api_me(request: Request) -> dict:
        user = auth.current_user(request)
        users = request.app.state.users
        return {
            "authenticated": bool(user),
            "user": user,
            "csrf": auth.current_csrf(request),
            "must_change_password": bool(user and users.must_change(user)),
        }

    @app.post("/api/password")
    async def api_password(request: Request, body: PasswordModel) -> dict:
        user = require_user(request)
        require_csrf(request)
        try:
            request.app.state.users.change_password(user, body.current_password, body.new_password)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        log.info("password changed user=%s ip=%s", user, client_ip(request, config.trusted_proxies))
        return {"ok": True, "must_change_password": False}

    # ---- exposed directories -------------------------------------------------
    @app.get("/api/directories")
    async def api_directories(request: Request) -> dict:
        require_user(request)
        service = request.app.state.directories
        return {
            "directories": [_payload(directory) for directory in service.list()],
            "host_root": config.host_root,
        }

    @app.post("/api/directories/add")
    async def api_directory_add(request: Request, body: DirectoryAddModel) -> dict:
        require_user(request)
        require_csrf(request)
        service = request.app.state.directories
        try:
            directory = service.add(body.host_path, body.name, create=body.create)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        log.info("directory add user=%s path=%s id=%s",
                 auth.current_user(request), directory.host_path, directory.id)
        return _payload(directory)

    @app.post("/api/directories/update")
    async def api_directory_update(request: Request, body: DirectoryUpdateModel) -> dict:
        require_user(request)
        require_csrf(request)
        service = request.app.state.directories
        try:
            directory = service.update(body.id, name=body.name, host_path=body.host_path,
                                       create=body.create)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        log.info("directory update user=%s id=%s path=%s name=%r",
                 auth.current_user(request), directory.id, directory.host_path, directory.name)
        return _payload(directory)

    @app.post("/api/directories/remove")
    async def api_directory_remove(request: Request, body: IdModel) -> dict:
        require_user(request)
        require_csrf(request)
        service = request.app.state.directories
        directory = service.remove(body.id)
        if directory is None:
            raise HTTPException(status_code=404, detail="Каталог не найден")
        revoked = request.app.state.shares.revoke_for_directory(body.id)
        log.info("directory remove user=%s id=%s path=%s links_revoked=%s",
                 auth.current_user(request), body.id, directory.host_path, revoked)
        return {"ok": True, "host_path": directory.host_path, "revoked_links": revoked}

    @app.get("/api/host/list")
    async def api_host_list(request: Request, path: str = "/") -> dict:
        """Folder picker: list the sub-directories of a path on the server."""
        require_user(request)
        try:
            return request.app.state.directories.browse(path)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    # ---- browsing & reading ---------------------------------------------------
    @app.get("/api/list")
    async def api_list(request: Request, directory: str, path: str = "") -> dict:
        require_user(request)
        target = _directory(request, directory)
        try:
            entries = storage.list_dir(target, path)
        except REQUEST_ERRORS as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {
            "directory": target.id,
            "name": target.display_name,
            "host_path": target.host_path,
            "path": path.strip("/"),
            "entries": entries,
        }

    @app.get("/api/download")
    async def api_download(request: Request, directory: str, path: str = "") -> FileResponse:
        require_user(request)
        target = _directory(request, directory)
        full = resolve_within(target.root, path)
        if not os.path.isfile(full):
            raise HTTPException(status_code=404, detail="File not found")
        return file_response(full, os.path.basename(full), inline=False)

    @app.get("/api/preview")
    async def api_preview(request: Request, directory: str, path: str = "") -> FileResponse:
        require_user(request)
        target = _directory(request, directory)
        full = resolve_within(target.root, path)
        if not os.path.isfile(full):
            raise HTTPException(status_code=404, detail="File not found")
        # Media is streamed (range requests), so only text-ish previews are capped.
        if preview_size_limit_hit(full, os.path.getsize(full), config.security.max_preview_mb):
            raise HTTPException(status_code=413, detail="File is too large to preview")
        return file_response(full, os.path.basename(full), inline=True)

    @app.get("/api/search")
    async def api_search(request: Request, directory: str, q: str = "") -> dict:
        require_user(request)
        target = _directory(request, directory)
        query = (q or "").strip()
        if len(query) < 2:
            return {"entries": []}
        try:
            entries = storage.search(target, query)
        except REQUEST_ERRORS as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {"directory": target.id, "entries": entries}

    # ---- mutating operations --------------------------------------------------
    @app.post("/api/upload")
    async def api_upload(
        request: Request,
        directory: str = Form(...),
        path: str = Form(""),
        files: List[UploadFile] = File(...),
    ) -> dict:
        require_user(request)
        require_csrf(request)
        target = _directory(request, directory)
        max_bytes = config.security.max_upload_mb * 1024 * 1024
        parent = resolve_within(target.root, path)
        if not os.path.isdir(parent):
            raise HTTPException(status_code=400, detail="Target is not a directory")
        saved: List[str] = []
        for upload in files:
            name = os.path.basename((upload.filename or "").replace("\\", "/")).strip()
            if not name or name in {".", ".."}:
                continue
            destination = resolve_within(target.root, os.path.join(path.strip("/"), name))
            tmp = f"{destination}.part-{os.getpid()}-{int(time.time() * 1000)}"
            written = 0
            try:
                with open(tmp, "wb") as handle:
                    while True:
                        chunk = await upload.read(1024 * 1024)
                        if not chunk:
                            break
                        written += len(chunk)
                        if written > max_bytes:
                            raise HTTPException(
                                status_code=413,
                                detail=f"'{name}' превышает лимит "
                                       f"{config.security.max_upload_mb} МБ",
                            )
                        handle.write(chunk)
                os.replace(tmp, destination)
                saved.append(name)
            except HTTPException:
                if os.path.exists(tmp):
                    os.remove(tmp)
                raise
            except OSError as exc:
                if os.path.exists(tmp):
                    os.remove(tmp)
                raise HTTPException(status_code=500, detail=f"Could not store '{name}': {exc}")
        log.info("upload user=%s directory=%s path=%s files=%s",
                 auth.current_user(request), directory, path, saved)
        return {"uploaded": saved}

    @app.post("/api/mkdir")
    async def api_mkdir(request: Request, body: MkdirModel) -> dict:
        require_user(request)
        require_csrf(request)
        target = _directory(request, body.directory)
        try:
            storage.create_dir(target, body.path, body.name)
        except REQUEST_ERRORS as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {"ok": True}

    @app.post("/api/rename")
    async def api_rename(request: Request, body: RenameModel) -> dict:
        require_user(request)
        require_csrf(request)
        target = _directory(request, body.directory)
        try:
            storage.rename(target, body.path, body.new_name)
        except REQUEST_ERRORS as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {"ok": True}

    @app.post("/api/move")
    async def api_move(request: Request, body: TransferModel) -> dict:
        require_user(request)
        require_csrf(request)
        source = _directory(request, body.directory)
        destination = _directory(request, body.dest_directory)
        try:
            storage.move(source, body.path, destination, body.dest_path)
        except REQUEST_ERRORS as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        log.info("move user=%s %s/%s -> %s/%s", auth.current_user(request),
                 body.directory, body.path, body.dest_directory, body.dest_path)
        return {"ok": True}

    @app.post("/api/copy")
    async def api_copy(request: Request, body: TransferModel) -> dict:
        require_user(request)
        require_csrf(request)
        source = _directory(request, body.directory)
        destination = _directory(request, body.dest_directory)
        try:
            storage.copy(source, body.path, destination, body.dest_path)
        except REQUEST_ERRORS as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        log.info("copy user=%s %s/%s -> %s/%s", auth.current_user(request),
                 body.directory, body.path, body.dest_directory, body.dest_path)
        return {"ok": True}

    @app.post("/api/delete")
    async def api_delete(request: Request, body: PathModel) -> dict:
        require_user(request)
        require_csrf(request)
        target = _directory(request, body.directory)
        try:
            storage.delete(target, body.path)
        except REQUEST_ERRORS as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        log.info("delete user=%s directory=%s path=%s",
                 auth.current_user(request), body.directory, body.path)
        return {"ok": True}

    # ---- external links -------------------------------------------------------
    @app.post("/api/share/create")
    async def api_share_create(request: Request, body: ShareCreateModel) -> dict:
        require_user(request)
        require_csrf(request)
        target = _directory(request, body.directory)
        full = resolve_within(target.root, body.path)
        if not os.path.exists(full):
            raise HTTPException(status_code=404, detail="Path not found")
        is_dir = os.path.isdir(full)
        name = os.path.basename(full.rstrip("/")) or target.display_name

        hours = body.expiry_hours if body.expiry_hours is not None else config.sharing.default_expiry_hours
        hours = max(1, min(int(hours), config.sharing.max_expiry_hours))
        expires_at = int(time.time()) + hours * 3600

        max_dl = body.max_downloads if body.max_downloads is not None else config.sharing.default_max_downloads
        max_dl = max(0, int(max_dl))

        password = (body.password or "").strip() or None
        if password and len(password) < 4:
            raise HTTPException(status_code=400, detail="Пароль должен быть не короче 4 символов")

        result = request.app.state.shares.create(
            directory_id=target.id,
            rel_path=body.path.strip("/"),
            is_dir=is_dir,
            name=name,
            expires_at=expires_at,
            password=password,
            max_downloads=max_dl,
            created_by=auth.current_user(request),
        )
        url = f"{config.base_url}/s/{result['token']}"
        log.info("share create user=%s directory=%s path=%s dir=%s",
                 auth.current_user(request), target.id, body.path, is_dir)
        return {
            "id": result["id"],
            "url": url,
            "expires_at": expires_at,
            "is_dir": is_dir,
            "has_password": bool(password),
            "password": password,
        }

    @app.get("/api/share/list")
    async def api_share_list(request: Request) -> dict:
        require_user(request)
        service: ShareService = request.app.state.shares
        directories = request.app.state.directories
        now = int(time.time())
        items = []
        downloads = 0
        active = 0
        for link in service.list_links():
            directory = directories.get(str(link["directory_id"]))
            token = service.reveal_token(link)
            is_active = service.is_active(link, now=now)
            downloads += int(link["downloads"] or 0)
            active += 1 if is_active else 0
            items.append({
                "id": link["id"],
                "directory": link["directory_id"],
                "directory_name": directory.display_name if directory else link["directory_id"],
                "host_path": directory.host_path if directory else None,
                "path": link["rel_path"],
                "name": link["name"],
                "is_dir": bool(link["is_dir"]),
                "url": f"{config.base_url}/s/{token}" if token else None,
                "has_password": bool(link["password_hash"]),
                # Only this authenticated, LAN-only endpoint ever exposes the
                # plaintext password (stored encrypted for exactly this purpose).
                "password": service.reveal_password(link),
                "expires_at": link["expires_at"],
                "max_downloads": link["max_downloads"],
                "downloads": link["downloads"],
                "created_at": link["created_at"],
                "created_by": link["created_by"],
                "active": is_active,
            })
        return {
            "links": items,
            "summary": {
                "total": len(items),
                "active": active,
                "downloads": downloads,
                "base_url": config.base_url,
            },
        }

    @app.post("/api/share/revoke")
    async def api_share_revoke(request: Request, body: IdModel) -> dict:
        require_user(request)
        require_csrf(request)
        removed = request.app.state.shares.revoke(body.id)
        log.info("share revoke user=%s id=%s ok=%s", auth.current_user(request), body.id, removed)
        return {"ok": removed}

    @app.post("/api/share/revoke-all")
    async def api_share_revoke_all(request: Request) -> dict:
        require_user(request)
        require_csrf(request)
        count = request.app.state.shares.revoke_all()
        log.info("share revoke-all user=%s count=%s", auth.current_user(request), count)
        return {"ok": True, "revoked": count}




