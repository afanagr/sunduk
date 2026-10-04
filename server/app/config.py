"""Configuration model and loader for Sunduk.

Settings come from two sources:

1. the optional YAML file ``/config/config.yml`` — mounted read-only by the
   ``docker-compose.yml`` in the repository root and by ``deploy/``;
2. environment variables, which win over the file (and over the built-in
   defaults) whenever they are set.  The minimal stack
   ``deploy/docker-compose.dockge.yml`` uses only these, so a container can be
   configured without any file at all.

An environment variable is the upper-case spelling of the config key
(``ADMIN_PORT``); the lower-case spelling (``admin_port``) and the ``SUNDUK_``
prefix are accepted as well.  Every setting has a working default, so the two
ports plus the public address (``PUBLIC_ADDRESS`` — an IP or host name, or a
full URL when it carries a scheme) are enough to start.

The directories exposed in the interface are *not* part of these settings.
They are added from the admin panel (server path + display name) and stored in
the database — see :mod:`app.directories`.  The server filesystem itself is
mounted once at ``host_root`` (``/host``), so adding a directory never requires
touching docker-compose or restarting the container.
"""
from __future__ import annotations

import logging
import os
import re
import secrets
from typing import List, Optional

import yaml
from pydantic import BaseModel, Field, model_validator

log = logging.getLogger("sunduk.config")

DEFAULT_CONFIG_PATH = "/config/config.yml"
DEFAULT_DATA_DIR = "/data"
DEFAULT_HOST_ROOT = "/host"
SECRET_KEY_FILE = "secret.key"

# Settings that may be given as environment variables.  The key itself is
# enough for every one of them (``ADMIN_PORT``, ``admin_port`` or
# ``SUNDUK_ADMIN_PORT``); the extra names are conveniences for values people
# look for under another name.
ENV_KEYS = {
    "host": (),
    "admin_port": (),
    "public_port": (),
    "public_ip": ("PUBLIC_ADDRESS", "PUBLIC_IP"),
    "base_url": (),
    "data_dir": (),
    "host_root": (),
    "secret_key": (),
    "lan_allowlist": ("ALLOWLIST",),
    "trusted_proxies": (),
}
LIST_KEYS = {"lan_allowlist", "trusted_proxies"}


def env_setting(key: str) -> Optional[str]:
    """Value of the environment variable for one config key, if any is set.

    Names are tried in this order: ``KEY``, ``key``, ``SUNDUK_KEY`` — and the
    same three for every alias of the key.  An empty value counts as unset, so
    ``PUBLIC_ADDRESS=`` falls back to the file/default.
    """
    for name in (key, *ENV_KEYS.get(key, ())):
        for candidate in (name.upper(), name.lower(), "SUNDUK_" + name.upper()):
            value = os.environ.get(candidate)
            if value is not None and value.strip():
                return value.strip()
    return None


def apply_env(config: dict) -> dict:
    """Overlay environment variables on top of the file's settings.

    ``config`` is updated in place; the overrides are returned so the caller
    can log them and tell where the settings came from.
    """
    overrides: dict = {}
    for key in ENV_KEYS:
        value = env_setting(key)
        if value is None:
            continue
        if key in LIST_KEYS:
            overrides[key] = [part for part in re.split(r"[,\s]+", value) if part]
        elif key == "public_ip" and "://" in value:
            # A full URL (https://files.example.com) is a base_url: it carries
            # the scheme, which public_ip deliberately never does.  The file's
            # public_ip is dropped on purpose — otherwise it would win over this
            # base_url (see AppConfig._sane).
            overrides["base_url"] = value
            overrides["public_ip"] = ""
        else:
            overrides[key] = value
    config.update(overrides)
    return overrides


class SecurityConfig(BaseModel):
    max_upload_mb: int = 2048              # per-file upload limit
    session_max_age_hours: int = 12        # admin session lifetime
    cookie_secure: bool = False            # forced on when base_url is https
    cookie_name: str = "sunduk_session"
    csrf_enabled: bool = True              # X-CSRF-Token on state-changing calls
    login_rate_limit_per_minute: int = 10  # brute-force protection for /api/login
    public_rate_limit_per_minute: int = 240  # per-IP limit on the public endpoint
    max_preview_mb: int = 64               # inline text preview cap


class SharingConfig(BaseModel):
    default_expiry_hours: int = 24         # default lifetime of a share link
    max_expiry_hours: int = 8760           # hard cap (1 year)
    default_max_downloads: int = 0         # 0 = unlimited


class Directory(BaseModel):
    """One server directory exposed in the interface.

    * ``host_path``   — the path **on the server**, exactly what the admin typed
      in the panel (``/mnt/media``).  It is what the user sees and edits.
    * ``root``        — the path **inside the container** where that directory is
      reachable (``/host/mnt/media``); every filesystem operation uses it.
    """

    id: str
    name: str
    host_path: str
    root: str
    created_at: int = 0

    @property
    def display_name(self) -> str:
        return self.name or self.host_path


class AppConfig(BaseModel):
    host: str = "0.0.0.0"
    admin_port: int = 8080
    public_port: int = 8081
    # IP (или имя хоста) для формирования внешних ссылок. Если задан, ссылки
    # строятся как http://<public_ip>:<public_port> — указывать порт в base_url
    # вручную не нужно.
    public_ip: str = ""
    base_url: str = "http://localhost:8081"
    data_dir: str = DEFAULT_DATA_DIR
    # Prefix under which the server filesystem is mounted in the container.
    host_root: str = DEFAULT_HOST_ROOT
    # Empty -> Sunduk generates <data_dir>/secret.key on the first start.
    secret_key: str = ""
    lan_allowlist: List[str] = Field(
        default_factory=lambda: ["127.0.0.1/32", "::1/128", "10.0.0.0/8",
                                 "172.16.0.0/12", "192.168.0.0/16"]
    )
    trusted_proxies: List[str] = Field(default_factory=list)
    security: SecurityConfig = Field(default_factory=SecurityConfig)
    sharing: SharingConfig = Field(default_factory=SharingConfig)

    @model_validator(mode="after")
    def _sane(self) -> "AppConfig":
        if self.admin_port == self.public_port:
            raise ValueError("admin_port and public_port must differ")

        # public_ip — простой способ задать внешний адрес: из него и public_port
        # собирается base_url (обычный http). Полный base_url остаётся для HTTPS
        # и доменов за reverse-proxy.
        public_ip = (self.public_ip or "").strip()
        if "://" in public_ip:
            public_ip = public_ip.split("://", 1)[1]
        public_ip = public_ip.strip().strip("/")
        if public_ip:
            if "/" in public_ip:
                raise ValueError("public_ip must be an IP address or host name")
            self.public_ip = public_ip
            self.base_url = f"http://{public_ip}:{self.public_port}"

        self.base_url = self.base_url.rstrip("/") or f"http://localhost:{self.public_port}"
        if not self.host_root.startswith("/"):
            raise ValueError("host_root must be an absolute path, e.g. /host")
        self.host_root = self.host_root.rstrip("/") or "/"
        if self.base_url.startswith("https://"):
            self.security.cookie_secure = True
        return self

    @property
    def db_path(self) -> str:
        return os.path.join(self.data_dir, "sunduk.db")

    @property
    def tmp_dir(self) -> str:
        return os.path.join(self.data_dir, "tmp")


# ---------------------------------------------------------------------------
# Server paths
# ---------------------------------------------------------------------------
def normalize_host_path(path: str) -> str:
    """Clean a server path typed by the admin and reject impossible values.

    Returns an absolute path without duplicate slashes, ``.`` or ``..``
    components (``/mnt//media/./films/`` -> ``/mnt/media/films``).
    """
    if not isinstance(path, str):
        raise ValueError("путь не задан")
    cleaned = path.strip().replace("\\", "/")
    if "\x00" in cleaned:
        raise ValueError("в пути недопустимый символ")
    if not cleaned.startswith("/"):
        raise ValueError("путь должен быть абсолютным, например /mnt/media")
    parts: List[str] = []
    for part in cleaned.split("/"):
        if part in ("", "."):
            continue
        if part == "..":
            raise ValueError("в пути не должно быть '..'")
        parts.append(part)
    return "/" + "/".join(parts)


def container_path(host_root: str, host_path: str) -> str:
    """Where ``host_path`` lives inside the container."""
    if host_path == "/":
        return host_root
    return os.path.join(host_root, host_path.lstrip("/"))


def to_host_path(host_root: str, root: str) -> str:
    """Reverse of :func:`container_path` (used by the folder picker)."""
    root = os.path.normpath(root)
    prefix = os.path.normpath(host_root)
    if root == prefix:
        return "/"
    if not root.startswith(prefix + os.sep):
        raise ValueError("путь вне примонтированной файловой системы сервера")
    return "/" + os.path.relpath(root, prefix).replace(os.sep, "/")


# ---------------------------------------------------------------------------
# Secret key
# ---------------------------------------------------------------------------
def _load_or_create_secret_key(data_dir: str) -> str:
    """Read ``<data_dir>/secret.key`` or create it with fresh randomness.

    Sessions and the encrypted copies of link tokens/passwords survive a restart
    because the key is persisted on the data volume instead of living only in an
    environment variable (``SECRET_KEY`` is accepted for moving it between
    servers).
    """
    path = os.path.join(data_dir, SECRET_KEY_FILE)
    try:
        with open(path, "r", encoding="utf-8") as handle:
            key = handle.read().strip()
        if key:
            return key
    except FileNotFoundError:
        pass
    except OSError as exc:
        log.warning("cannot read %s (%s) — using a temporary key", path, exc)
        return secrets.token_hex(32)

    key = secrets.token_hex(32)
    try:
        os.makedirs(data_dir, exist_ok=True)
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(key + "\n")
        log.info("generated a new secret key: %s", path)
    except OSError as exc:
        log.warning("cannot write %s (%s) — sessions will not survive a restart", path, exc)
    return key


# ---------------------------------------------------------------------------
# Loader
# ---------------------------------------------------------------------------
def load_config(path: Optional[str] = None) -> AppConfig:
    """Read the settings: environment variables over ``/config/config.yml``.

    The file is optional — without it the built-in defaults apply, which is all
    the minimal stack needs.
    """
    config_path = path or DEFAULT_CONFIG_PATH
    try:
        with open(config_path, "r", encoding="utf-8") as handle:
            raw = yaml.safe_load(handle) or {}
    except FileNotFoundError:
        log.warning("%s not found — using built-in defaults", config_path)
        raw = {}
    except yaml.YAMLError as exc:
        raise SystemExit(f"некорректный YAML в {config_path}: {exc}") from exc
    if not isinstance(raw, dict):
        raise SystemExit(f"{config_path}: ожидается словарь верхнего уровня")

    # Keys from the old, file-based layout.  Say so loudly instead of silently
    # ignoring a configuration the admin believes is in effect.
    legacy = [key for key in ("users", "shares", "local_paths") if key in raw]
    if legacy:
        log.warning(
            "%s: разделы %s больше не используются — каталоги добавляются в панели, "
            "учётные записи хранятся в базе данных",
            config_path, ", ".join(legacy),
        )

    overrides = apply_env(raw)
    if overrides:
        log.info("environment overrides: %s", ", ".join(sorted(overrides)))

    try:
        config = AppConfig(**raw)
    except Exception as exc:  # pydantic ValidationError -> readable message
        raise SystemExit(f"{config_path}: {exc}") from exc

    if not config.secret_key:
        config.secret_key = _load_or_create_secret_key(config.data_dir)

    source = f"public_ip={config.public_ip}" if config.public_ip else "base_url"
    origin = f"{config_path} + environment" if overrides else config_path
    log.info(
        "config loaded from %s (host_root=%s, links via %s -> %s, admin :%s, public :%s)",
        origin, config.host_root, source, config.base_url,
        config.admin_port, config.public_port,
    )
    return config


