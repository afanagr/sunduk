"""Shared HTTP helpers: security headers, LAN guard, rate limiting and
content-disposition-safe file responses.
"""
from __future__ import annotations

import logging
import mimetypes
import os
from typing import Dict, List, Optional
from urllib.parse import quote

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import FileResponse, JSONResponse

from .security import RateLimiter, client_ip, ip_in_cidrs

log = logging.getLogger("sunduk.http")

SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
    "Cross-Origin-Opener-Policy": "same-origin",
    "Cross-Origin-Resource-Policy": "same-origin",
    "Permissions-Policy": "geolocation=(), microphone=(), camera=(), interest-cohort=()",
    "X-XSS-Protection": "0",
}

CSP = (
    "default-src 'self'; "
    "img-src 'self' data: blob:; "
    "media-src 'self' blob:; "
    "style-src 'self' 'unsafe-inline'; "
    "script-src 'self'; "
    "connect-src 'self'; "
    "object-src 'none'; "
    "base-uri 'self'; "
    "frame-ancestors 'none'; "
    "form-action 'self'"
)


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request, call_next):
        response = await call_next(request)
        for key, value in SECURITY_HEADERS.items():
            response.headers.setdefault(key, value)
        response.headers.setdefault("Content-Security-Policy", CSP)
        return response


class LanGuardMiddleware(BaseHTTPMiddleware):
    """Reject any request to the admin surface from outside the LAN allowlist."""

    def __init__(self, app, allowlist: List[str], trusted_proxies: List[str]):
        super().__init__(app)
        self.allowlist = allowlist
        self.trusted_proxies = trusted_proxies

    async def dispatch(self, request, call_next):
        ip = client_ip(request, self.trusted_proxies)
        if not ip_in_cidrs(ip, self.allowlist):
            return JSONResponse(
                {"error": "forbidden",
                 "detail": "Admin interface is available only from the local network."},
                status_code=403,
            )
        return await call_next(request)


class RateLimitMiddleware(BaseHTTPMiddleware):
    def __init__(self, app, limiter: RateLimiter, trusted_proxies: Optional[List[str]] = None):
        super().__init__(app)
        self.limiter = limiter
        self.trusted_proxies = trusted_proxies or []

    async def dispatch(self, request, call_next):
        ip = client_ip(request, self.trusted_proxies)
        if not self.limiter.allow(ip):
            return JSONResponse(
                {"error": "rate_limited", "detail": "Too many requests, please slow down."},
                status_code=429,
                headers={"Retry-After": "60"},
            )
        return await call_next(request)


# ---------------------------------------------------------------------------
# Error handling
# ---------------------------------------------------------------------------
async def _permission_error_handler(request, exc) -> JSONResponse:
    """A path that escapes the share root is a client error, not a 500."""
    log.warning("blocked path outside the share root: %s", exc)
    return JSONResponse(
        {"error": "forbidden", "detail": "Requested path is outside the share root."},
        status_code=403,
    )


def install_exception_handlers(app) -> None:
    """Map path-confinement failures to a clean 403 instead of a 500."""
    app.add_exception_handler(PermissionError, _permission_error_handler)


# ---------------------------------------------------------------------------
# File responses
# ---------------------------------------------------------------------------

# Extensions that must never be rendered inline (XSS vectors).
_FORCE_DOWNLOAD_EXT = {"svg", "html", "htm", "xhtml", "xml", "js", "mjs", "json"}

# Extra text types that are safe to render inline (never executed as markup).
_INLINE_TEXT_TYPES = {"text/plain", "text/vtt", "text/markdown"}

# Explicit MIME map for popular media formats.  The stdlib table is incomplete
# on slim images (no ``.mkv``/``.m4a``/``.opus``/``.flac`` ...), and a wrong
# Content-Type makes browsers refuse to play an otherwise supported file.
_MEDIA_MIME = {
    # ---------------------------------------------------------------- video
    "mp4": "video/mp4", "m4v": "video/mp4", "mov": "video/quicktime",
    "webm": "video/webm", "mkv": "video/x-matroska", "ogv": "video/ogg",
    "avi": "video/x-msvideo", "wmv": "video/x-ms-wmv", "flv": "video/x-flv",
    "mpg": "video/mpeg", "mpeg": "video/mpeg", "3gp": "video/3gpp",
    "ts": "video/mp2t", "m2ts": "video/mp2t",
    # ---------------------------------------------------------------- audio
    "mp3": "audio/mpeg", "m4a": "audio/mp4", "aac": "audio/aac",
    "flac": "audio/flac", "ogg": "audio/ogg", "oga": "audio/ogg",
    "opus": "audio/ogg", "wav": "audio/wav", "weba": "audio/webm",
    "wma": "audio/x-ms-wma", "mka": "audio/x-matroska",
    "aif": "audio/aiff", "aiff": "audio/aiff",
    # --------------------------------------------------------------- images
    "jpg": "image/jpeg", "jpeg": "image/jpeg", "png": "image/png",
    "gif": "image/gif", "webp": "image/webp", "avif": "image/avif",
    "bmp": "image/bmp", "ico": "image/vnd.microsoft.icon",
    "tif": "image/tiff", "tiff": "image/tiff",
    "heic": "image/heic", "heif": "image/heif", "jxl": "image/jxl",
    # ------------------------------------------------------------------ doc
    "pdf": "application/pdf", "vtt": "text/vtt", "srt": "text/plain",
}


def _content_type(path: str) -> str:
    ext = os.path.splitext(path)[1].lstrip(".").lower()
    known = _MEDIA_MIME.get(ext)
    if known:
        return known
    ctype, _ = mimetypes.guess_type(path)
    return ctype or "application/octet-stream"


def is_inline_safe(path: str) -> bool:
    """Only media-ish types may be rendered inline; everything else downloads."""
    ext = os.path.splitext(path)[1].lstrip(".").lower()
    if ext in _FORCE_DOWNLOAD_EXT:
        return False
    ctype = _content_type(path)
    return (
        ctype.startswith("image/")
        or ctype.startswith("video/")
        or ctype.startswith("audio/")
        or ctype == "application/pdf"
        or ctype in _INLINE_TEXT_TYPES
    )


def is_media_type(path: str) -> bool:
    """True when the browser can render/stream the file without a download.

    Such files are served inline *without* the preview size cap: they are
    streamed (and seekable through HTTP range requests), so a multi-gigabyte
    movie never has to be buffered in memory.
    """
    ctype = _content_type(path)
    return (
        ctype.startswith("image/")
        or ctype.startswith("video/")
        or ctype.startswith("audio/")
        or ctype == "application/pdf"
    )


def preview_size_limit_hit(path: str, size: int, max_preview_mb: int) -> bool:
    """Text-like previews are read into the browser, so they keep the cap."""
    if is_media_type(path):
        return False
    return size > max_preview_mb * 1024 * 1024


def _content_disposition(filename: str, inline: bool) -> str:
    disposition = "inline" if inline else "attachment"
    ascii_fallback = filename.encode("ascii", "ignore").decode("ascii") or "download"
    ascii_fallback = ascii_fallback.replace('"', "")
    return f"{disposition}; filename=\"{ascii_fallback}\"; filename*=UTF-8''{quote(filename)}"


def attachment_headers(filename: str) -> Dict[str, str]:
    """Headers for a generated download (an archive built while it streams).

    It is never inline — there is nothing to render — and never cached.
    """
    return {
        "Content-Disposition": _content_disposition(os.path.basename(filename) or "download", False),
        "Cache-Control": "private, no-store",
        "X-Content-Type-Options": "nosniff",
    }


def file_response(path: str, download_name: str, *, inline: bool = False) -> FileResponse:
    """Return a hardened FileResponse.

    Only a small allowlist of media types may be rendered inline; everything
    else (and anything not in the allowlist, e.g. HTML/SVG/JS) is forced to
    download as ``application/octet-stream`` to neutralise stored-XSS.

    Inline responses are also streamable: ``Accept-Ranges`` is advertised and
    Starlette answers ``Range`` requests with ``206 Partial Content``, which is
    what lets the browser seek inside audio/video ("video online").
    """
    safe_inline = inline and is_inline_safe(path)
    media_type = _content_type(path) if safe_inline else "application/octet-stream"
    headers = {
        "X-Content-Type-Options": "nosniff",
        "Content-Disposition": _content_disposition(os.path.basename(download_name) or "download", safe_inline),
        "Cache-Control": "private, no-store",
        "Accept-Ranges": "bytes",
    }
    if safe_inline:
        if media_type == "application/pdf":
            # The built-in PDF viewer is a frame inside our own page, and a
            # ``sandbox`` CSP stops Chrome from rendering the plugin at all.
            headers["Content-Security-Policy"] = "default-src 'none'; frame-ancestors 'self'"
            headers["X-Frame-Options"] = "SAMEORIGIN"
        else:
            headers["Content-Security-Policy"] = (
                "default-src 'none'; sandbox; img-src 'self' data:; media-src 'self'"
            )
    return FileResponse(path, media_type=media_type, headers=headers)
