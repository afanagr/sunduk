"""Security primitives: password hashing, path confinement, IP filtering,
rate limiting and client-IP resolution.

Everything here is dependency-free (standard library only) so it can be
audited easily and does not pull extra attack surface.
"""
from __future__ import annotations

import hashlib
import hmac
import ipaddress
import os
import secrets
import threading
import time
from collections import defaultdict, deque
from typing import Deque, Dict, Iterable, List, Optional

# ---------------------------------------------------------------------------
# Password hashing (scrypt)
# ---------------------------------------------------------------------------

_SCRYPT_N = 2 ** 14
_SCRYPT_R = 8
_SCRYPT_P = 1
_SCRYPT_DKLEN = 32


def hash_password(password: str) -> str:
    """Return a self-describing scrypt hash string."""
    salt = secrets.token_bytes(16)
    derived = hashlib.scrypt(
        password.encode("utf-8"), salt=salt,
        n=_SCRYPT_N, r=_SCRYPT_R, p=_SCRYPT_P, dklen=_SCRYPT_DKLEN,
    )
    return f"scrypt${_SCRYPT_N}${_SCRYPT_R}${_SCRYPT_P}${salt.hex()}${derived.hex()}"


def verify_password(password: str, stored: str) -> bool:
    """Constant-time verification of a password against a stored hash."""
    try:
        algo, n, r, p, salt_hex, hash_hex = stored.split("$")
        if algo != "scrypt":
            return False
        salt = bytes.fromhex(salt_hex)
        expected = bytes.fromhex(hash_hex)
        derived = hashlib.scrypt(
            password.encode("utf-8"), salt=salt,
            n=int(n), r=int(r), p=int(p), dklen=len(expected),
        )
        return hmac.compare_digest(derived, expected)
    except (ValueError, TypeError):
        return False


# ---------------------------------------------------------------------------
# Path confinement
# ---------------------------------------------------------------------------

def resolve_within(root: str, rel: str) -> str:
    """Resolve ``rel`` inside ``root`` or raise ``PermissionError``.

    Guards against ``..`` traversal and symlink escapes by comparing the
    fully resolved (realpath) target with the resolved root.
    """
    root_real = os.path.realpath(root)
    rel = (rel or "").replace("\\", "/").strip()
    rel = rel.lstrip("/")
    candidate = os.path.realpath(os.path.join(root_real, rel))
    if candidate != root_real and not candidate.startswith(root_real + os.sep):
        raise PermissionError("requested path escapes the share root")
    return candidate


def is_within(root: str, target: str) -> bool:
    root_real = os.path.realpath(root)
    target_real = os.path.realpath(target)
    return target_real == root_real or target_real.startswith(root_real + os.sep)


# ---------------------------------------------------------------------------
# IP / CIDR filtering
# ---------------------------------------------------------------------------

def ip_in_cidrs(ip: str, cidrs: Iterable[str]) -> bool:
    if not ip:
        return False
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return False
    for cidr in cidrs:
        try:
            if addr in ipaddress.ip_network(cidr, strict=False):
                return True
        except ValueError:
            continue
    return False


def client_ip(request, trusted_proxies: List[str]) -> str:
    """Return the real client IP, honouring X-Forwarded-For from trusted proxies."""
    peer = request.client.host if request.client else ""
    if trusted_proxies and ip_in_cidrs(peer, trusted_proxies):
        forwarded = request.headers.get("x-forwarded-for")
        if forwarded:
            return forwarded.split(",")[0].strip()
    return peer


# ---------------------------------------------------------------------------
# Rate limiting (in-memory sliding window, per process)
# ---------------------------------------------------------------------------

class RateLimiter:
    def __init__(self, limit: int, window_seconds: float = 60.0, max_keys: int = 10000):
        self.limit = max(1, int(limit))
        self.window = float(window_seconds)
        self.max_keys = max_keys
        self._hits: Dict[str, Deque[float]] = defaultdict(deque)
        self._lock = threading.Lock()

    def allow(self, key: str) -> bool:
        now = time.monotonic()
        with self._lock:
            if len(self._hits) > self.max_keys:
                self._hits.clear()
            hits = self._hits[key]
            while hits and now - hits[0] > self.window:
                hits.popleft()
            if len(hits) >= self.limit:
                return False
            hits.append(now)
            return True


# ---------------------------------------------------------------------------
# Token helpers
# ---------------------------------------------------------------------------

def new_token(nbytes: int = 32) -> str:
    return secrets.token_urlsafe(nbytes)


def sha256_hex(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def constant_time_eq(a: Optional[str], b: Optional[str]) -> bool:
    if a is None or b is None:
        return False
    return hmac.compare_digest(a.encode("utf-8"), b.encode("utf-8"))
