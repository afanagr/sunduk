"""End-to-end smoke test for Sunduk — runs *inside* the container:

    docker compose exec -T sunduk python tools/smoke_test.py
    docker compose exec -T sunduk python tools/smoke_test.py --password 'мой-пароль'
    docker compose exec -T sunduk python tools/smoke_test.py --big-mb 96

Everything is exercised over real HTTP against both listeners (admin + public),
and the test brings its own fixtures: it creates two directories on the server
under ``/tmp/sunduk-smoke``, adds them to the panel through the API, runs the
whole feature set and removes them again (files included).  Nothing else on the
server is touched, and the exit code is 1 as soon as one check fails.

Covered: health, login with CSRF, the forced first-login password change and the
428 gate it leaves behind, adding/renaming/removing directories (with the host
folder picker and every path validation), listing, search, the full write cycle
(mkdir, upload, rename, move, copy, delete), path-traversal containment, media
preview (inline MIME types, PDF framing, HTTP Range streaming, HTML forced to
download), external links (file, folder, password, download limit, plaintext
password for the admin), the public page (metadata, download, raw, unlock,
range) and revocation.  Optional: --big-mb N uploads a file larger than N MB.
"""
from __future__ import annotations

import argparse
import http.cookiejar
import json
import os
import shutil
import sys
import urllib.error
import urllib.parse
import urllib.request

import yaml

CONFIG_PATH = "/config/config.yml"
FIXTURE_BASE = "/tmp/sunduk-smoke"        # server path (below host_root)

FAILURES: list = []
PASSED = 0
ARGS = None
SETTINGS: dict = {}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def check(name, ok, detail=""):
    global PASSED
    if ok:
        PASSED += 1
    else:
        FAILURES.append(name)
    print(f"{'PASS' if ok else 'FAIL'}  {name}{('  -> ' + detail) if detail else ''}")
    return ok


def note(text):
    """Informational line; does not count as a check."""
    print(f"NOTE  {text}")


def lower_headers(headers):
    """HTTP header lookup must be case-insensitive (Starlette emits lowercase)."""
    return {key.lower(): value for key, value in dict(headers).items()}


def payload(body):
    try:
        return json.loads(body.decode("utf-8") or "{}")
    except (ValueError, UnicodeDecodeError):
        return {}


def load_settings():
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as handle:
            loaded = yaml.safe_load(handle) or {}
        return loaded if isinstance(loaded, dict) else {}
    except (OSError, yaml.YAMLError):
        return {}


class Client:
    """Tiny urllib-based client with its own cookie jar."""

    def __init__(self, base):
        self.base = base.rstrip("/")
        self.jar = http.cookiejar.CookieJar()
        self.opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(self.jar))
        self.csrf = None

    def request(self, method, path, body=None, headers=None, raw=None, content_type=None):
        url = self.base + path
        data = None
        hdrs = dict(headers or {})
        if raw is not None:
            data = raw
            if content_type:
                hdrs["Content-Type"] = content_type
        elif body is not None:
            data = json.dumps(body).encode("utf-8")
            hdrs["Content-Type"] = "application/json"
        if self.csrf and method.upper() not in ("GET", "HEAD"):
            hdrs["X-CSRF-Token"] = self.csrf
        req = urllib.request.Request(url, data=data, headers=hdrs, method=method)
        try:
            with self.opener.open(req, timeout=30) as resp:
                return resp.status, lower_headers(resp.headers), resp.read()
        except urllib.error.HTTPError as exc:
            return exc.code, lower_headers(exc.headers), exc.read()

    def get(self, path, headers=None):
        return self.request("GET", path, headers=headers)

    def post_json(self, path, body):
        return self.request("POST", path, body=body)

    def post_multipart(self, path, fields, file_field, filename, content):
        boundary = "----sundukSmokeBoundary"
        chunks = []
        for name, value in fields.items():
            chunks.append(
                f"--{boundary}\r\nContent-Disposition: form-data; name=\"{name}\"\r\n\r\n{value}\r\n"
            )
        chunks.append(
            f"--{boundary}\r\nContent-Disposition: form-data; name=\"{file_field}\"; "
            f"filename=\"{filename}\"\r\nContent-Type: application/octet-stream\r\n\r\n"
        )
        data = "".join(chunks).encode("utf-8") + content + f"\r\n--{boundary}--\r\n".encode("utf-8")
        return self.request(
            "POST", path, raw=data, content_type=f"multipart/form-data; boundary={boundary}"
        )


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------
def container_fixture_path(host_path):
    """Where a server path lives inside this container."""
    host_root = str(SETTINGS.get("host_root") or "/host").rstrip("/") or "/"
    if host_path == "/":
        return host_root
    return os.path.join(host_root, host_path.lstrip("/"))


def build_fixtures():
    """Create the two directories the test exposes, with a little content."""
    root = container_fixture_path(FIXTURE_BASE)
    alpha = os.path.join(root, "alpha")
    beta = os.path.join(root, "beta")
    try:
        shutil.rmtree(root, ignore_errors=True)
        os.makedirs(os.path.join(alpha, "Docs"), exist_ok=True)
        os.makedirs(beta, exist_ok=True)
        with open(os.path.join(alpha, "Docs", "note.txt"), "w", encoding="utf-8") as handle:
            handle.write("Sunduk smoke fixture\n")
        with open(os.path.join(alpha, "user-note.txt"), "w", encoding="utf-8") as handle:
            handle.write("inline text preview\n")
    except OSError as exc:
        print(f"Не удалось подготовить тестовые каталоги в {root}: {exc}")
        print("Проверьте, что файловая система сервера смонтирована в host_root "
              "(docker-compose.yml: /:/host).")
        return None
    return alpha, beta


def remove_directory_entry(client, directory_id):
    try:
        client.post_json("/api/directories/remove", {"id": directory_id})
    except Exception:  # noqa: BLE001 - cleanup must never fail the run
        pass


# ---------------------------------------------------------------------------
# Health & authentication
# ---------------------------------------------------------------------------
def test_health_and_auth():
    admin = Client(ADMIN)
    status, _, body = Client(ADMIN).get("/healthz")
    check("admin /healthz", status == 200 and payload(body).get("status") == "ok", f"status={status}")
    status, _, body = Client(PUBLIC).get("/healthz")
    check("public /healthz", status == 200, f"status={status}")

    status, _, _ = Client(ADMIN).post_json(
        "/api/login", {"username": ARGS.user, "password": "wrong-password"}
    )
    check("неверный пароль отклонён", status == 401, f"status={status}")

    # A signed-in client that does not send the CSRF token must be refused
    # (checked after the password gate, see test_csrf_gate).
    status, _, body = admin.post_json(
        "/api/login", {"username": ARGS.user, "password": ARGS.password}
    )
    data = payload(body)
    if not check("вход в панель", status == 200 and bool(data.get("csrf")),
                 f"status={status}"):
        print("\nПодсказка: пароль мог быть изменён при первом входе — укажите его "
              "через --password, либо сбросьте: python tools/reset_admin.py")
        return None, None
    admin.csrf = data["csrf"]

    status, _, body = Client(ADMIN).get("/api/me")
    check("/api/me без сессии", payload(body).get("authenticated") is False, f"status={status}")

    status, _, body = admin.get("/api/me")
    check("/api/me с сессией", payload(body).get("authenticated") is True, f"status={status}")

    status, _, _ = Client(ADMIN).get("/api/directories")
    check("API без входа отклонён", status == 401, f"status={status}")
    return admin, data


def test_csrf_gate():
    """A signed-in client that omits X-CSRF-Token must not be able to mutate."""
    client = Client(ADMIN)
    status, _, _ = client.post_json(
        "/api/login", {"username": ARGS.user, "password": ARGS.password}
    )
    if not check("вход для проверки CSRF", status == 200, f"status={status}"):
        return
    status, _, _ = client.post_json("/api/directories/add", {"host_path": FIXTURE_BASE})
    check("изменение без CSRF отклонено", status == 403, f"status={status}")
    status, _, _ = client.get("/api/directories")
    check("чтение без CSRF разрешено", status == 200, f"status={status}")


def test_password_flow(admin, login_data):
    """The shipped default password must be replaced before anything else works."""
    if login_data.get("must_change_password"):
        note("первый вход с паролем по умолчанию — проверяем принудительную смену")
        status, _, _ = admin.get("/api/directories")
        check("панель закрыта до смены пароля (428)", status == 428, f"status={status}")

        status, _, _ = admin.post_json(
            "/api/password", {"current_password": ARGS.password, "new_password": "123"}
        )
        check("короткий пароль отклонён", status == 400, f"status={status}")

        status, _, _ = admin.post_json(
            "/api/password", {"current_password": "не-тот", "new_password": ARGS.new_password}
        )
        check("неверный текущий пароль отклонён", status == 400, f"status={status}")

        status, _, _ = admin.post_json(
            "/api/password", {"current_password": ARGS.password, "new_password": ARGS.new_password}
        )
        check("смена пароля принята", status == 200, f"status={status}")

        status, _, body = admin.get("/api/me")
        check("флаг смены снят", payload(body).get("must_change_password") is False,
              f"status={status}")

        status, _, _ = admin.get("/api/directories")
        check("панель открылась после смены пароля", status == 200, f"status={status}")

        status, _, _ = Client(ADMIN).post_json(
            "/api/login", {"username": ARGS.user, "password": ARGS.password}
        )
        check("старый пароль больше не действует", status == 401, f"status={status}")

        # Return the account to the documented credentials so the deployment is
        # left exactly as it was found (the "must change" flag stays cleared).
        status, _, _ = admin.post_json(
            "/api/password", {"current_password": ARGS.new_password, "new_password": ARGS.password}
        )
        check("возврат к исходному паролю", status == 200, f"status={status}")
        status, _, body = admin.get("/api/me")
        check("флаг смены остаётся снятым", payload(body).get("must_change_password") is False)
    else:
        note("пароль уже сменён ранее — проверка первого входа пропущена")
        status, _, _ = admin.get("/api/directories")
        check("панель доступна без смены пароля", status == 200, f"status={status}")
        status, _, _ = admin.post_json(
            "/api/password", {"current_password": "не-тот", "new_password": ARGS.new_password}
        )
        check("неверный текущий пароль отклонён", status == 400, f"status={status}")


# ---------------------------------------------------------------------------
# Directory management (the panel workflow)
# ---------------------------------------------------------------------------
def test_directories(admin):
    fixtures = build_fixtures()
    if fixtures is None:
        return None
    alpha_path, _beta_path = fixtures
    alpha_server = f"{FIXTURE_BASE}/alpha"
    beta_server = f"{FIXTURE_BASE}/beta"

    status, _, body = admin.get("/api/directories")
    data = payload(body)
    check("/api/directories отвечает", status == 200 and "directories" in data, f"status={status}")
    check("host_root отдан панели", bool(data.get("host_root")), str(data.get("host_root")))

    # --- validation ---------------------------------------------------------
    status, _, _ = admin.post_json("/api/directories/add", {"host_path": "relative/path"})
    check("относительный путь отклонён", status == 400, f"status={status}")

    status, _, _ = admin.post_json("/api/directories/add", {"host_path": f"{FIXTURE_BASE}/../etc"})
    check("путь с '..' отклонён", status == 400, f"status={status}")

    status, _, _ = admin.post_json("/api/directories/add", {"host_path": f"{FIXTURE_BASE}/нет-такого"})
    check("несуществующий каталог отклонён", status == 400, f"status={status}")

    # --- folder picker ------------------------------------------------------
    status, _, body = admin.get("/api/host/list?path=/")
    listing = payload(body)
    check("проводник сервера отвечает", status == 200 and listing.get("path") == "/", f"status={status}")
    check("проводник отдаёт папки", any(d["name"] == "tmp" for d in listing.get("dirs", [])),
          str([d["name"] for d in listing.get("dirs", [])][:12]))

    status, _, body = admin.get("/api/host/list?path=" + urllib.parse.quote(f"{FIXTURE_BASE}/alpha"))
    listing = payload(body)
    check("проводник входит в подкаталог", status == 200 and listing.get("path") == alpha_server,
          f"status={status}")
    check("проводник видит вложенную папку",
          [d["name"] for d in listing.get("dirs", [])] == ["Docs"],
          str(listing.get("dirs")))

    status, _, _ = admin.get("/api/host/list?path=" + urllib.parse.quote(f"{FIXTURE_BASE}/нет"))
    check("проводник отвергает несуществующий путь", status == 400, f"status={status}")

    # --- add ----------------------------------------------------------------
    status, _, body = admin.post_json(
        "/api/directories/add", {"host_path": alpha_server, "name": "Смоук-Альфа"}
    )
    alpha = payload(body)
    check("каталог добавлен", status == 200 and bool(alpha.get("id")), f"status={status}")
    check("каталог доступен", alpha.get("available") is True, str(alpha))
    check("имя задано вручную", alpha.get("name") == "Смоук-Альфа", str(alpha.get("name")))
    check("путь сохранён как на сервере", alpha.get("host_path") == alpha_server,
          str(alpha.get("host_path")))
    check("занятое место посчитано", bool((alpha.get("usage") or {}).get("total")), str(alpha.get("usage")))

    status, _, _ = admin.post_json("/api/directories/add", {"host_path": alpha_server})
    check("повторное добавление отклонено", status == 400, f"status={status}")

    status, _, body = admin.post_json(
        "/api/directories/add", {"host_path": beta_server, "name": "Смоук-Бета", "create": True}
    )
    beta = payload(body)
    check("каталог создан на сервере при добавлении",
          status == 200 and os.path.isdir(_beta_path), f"status={status}")
    check("каталог для переноса доступен", beta.get("available") is True, str(beta))

    status, _, body = admin.get("/api/directories")
    ids = [d["id"] for d in payload(body).get("directories", [])]
    check("оба каталога в списке", alpha.get("id") in ids and beta.get("id") in ids, str(ids))

    # --- update -------------------------------------------------------------
    status, _, body = admin.post_json(
        "/api/directories/update", {"id": alpha["id"], "name": "Альфа-2"}
    )
    updated = payload(body)
    check("каталог переименован", status == 200 and updated.get("name") == "Альфа-2",
          f"status={status} name={updated.get('name')}")
    check("id не изменился", updated.get("id") == alpha["id"], str(updated.get("id")))

    status, _, _ = admin.post_json(
        "/api/directories/update", {"id": alpha["id"], "host_path": f"{FIXTURE_BASE}/нет-такого"}
    )
    check("смена пути на несуществующий отклонена", status == 400, f"status={status}")

    return alpha, beta


# ---------------------------------------------------------------------------
# Browsing, the write cycle and path confinement
# ---------------------------------------------------------------------------
def test_browsing_and_writes(admin, alpha, beta):
    a, b = alpha["id"], beta["id"]

    status, _, body = admin.get(f"/api/list?directory={a}&path=")
    data = payload(body)
    names = [e["name"] for e in data.get("entries", [])]
    check("список каталога", status == 200 and "Docs" in names and "user-note.txt" in names,
          f"entries={names}")
    check("папки идут первыми", names and names[0] == "Docs", str(names))
    check("отдан путь сервера", str(data.get("host_path", "")).startswith("/"),
          str(data.get("host_path")))

    status, _, body = admin.get(f"/api/list?directory={a}&path=Docs")
    check("навигация в подпапку",
          status == 200 and [e["name"] for e in payload(body).get("entries", [])] == ["note.txt"],
          f"status={status}")

    status, _, _ = admin.get("/api/list?directory=no-such-directory&path=")
    check("неизвестный каталог отвергнут", status == 404, f"status={status}")

    status, _, body = admin.get(f"/api/search?directory={a}&q=note")
    check("поиск по каталогу",
          status == 200 and any(e["name"] == "note.txt" for e in payload(body).get("entries", [])),
          f"status={status}")

    # --- write cycle --------------------------------------------------------
    status, _, _ = admin.post_json("/api/mkdir", {"directory": a, "path": "", "name": "SmokeDir"})
    check("создание папки", status == 200, f"status={status}")

    status, _, body = admin.post_multipart(
        "/api/upload", {"directory": a, "path": "SmokeDir"}, "files",
        "smoke-upload.txt", b"hello sunduk smoke test\n",
    )
    check("загрузка файла",
          status == 200 and "smoke-upload.txt" in payload(body).get("uploaded", []),
          f"status={status} {payload(body)}")

    status, _, body = admin.get(f"/api/list?directory={a}&path=SmokeDir")
    names = [e["name"] for e in payload(body).get("entries", [])]
    check("загруженный файл в списке", names == ["smoke-upload.txt"], f"entries={names}")

    status, _, _ = admin.post_json(
        "/api/rename",
        {"directory": a, "path": "SmokeDir/smoke-upload.txt", "new_name": "renamed.txt"},
    )
    check("переименование", status == 200, f"status={status}")

    status, _, body = admin.get(f"/api/list?directory={a}&path=SmokeDir")
    names = [e["name"] for e in payload(body).get("entries", [])]
    check("переименование применено", names == ["renamed.txt"], f"entries={names}")

    # --- move / copy across directories -------------------------------------
    status, _, _ = admin.post_json(
        "/api/move",
        {"directory": a, "path": "SmokeDir/renamed.txt", "dest_directory": b, "dest_path": ""},
    )
    check("перенос в другой каталог", status == 200, f"status={status}")
    status, _, body = admin.get(f"/api/list?directory={b}&path=")
    names = [e["name"] for e in payload(body).get("entries", [])]
    check("файл оказался в каталоге назначения", names == ["renamed.txt"], f"entries={names}")

    status, _, _ = admin.post_json(
        "/api/copy",
        {"directory": b, "path": "renamed.txt", "dest_directory": a, "dest_path": "SmokeDir"},
    )
    check("копирование обратно", status == 200, f"status={status}")
    status, _, body = admin.get(f"/api/list?directory={a}&path=SmokeDir")
    names = [e["name"] for e in payload(body).get("entries", [])]
    check("копия на месте", names == ["renamed.txt"], f"entries={names}")

    status, _, _ = admin.post_json(
        "/api/move",
        {"directory": a, "path": "SmokeDir", "dest_directory": a, "dest_path": "SmokeDir"},
    )
    check("перенос папки внутрь себя запрещён", status == 400, f"status={status}")

    # --- delete -------------------------------------------------------------
    status, _, _ = admin.post_json("/api/delete", {"directory": a, "path": "SmokeDir"})
    check("удаление папки", status == 200, f"status={status}")
    status, _, body = admin.get(f"/api/list?directory={a}&path=")
    names = [e["name"] for e in payload(body).get("entries", [])]
    check("удаление применено", "SmokeDir" not in names, f"entries={names}")

    status, _, _ = admin.post_json("/api/delete", {"directory": a, "path": ""})
    check("корень каталога удалить нельзя", status == 400, f"status={status}")

    # --- path confinement ---------------------------------------------------
    probes = ["../../../../etc/passwd", "/etc/passwd", "Docs/../../../../etc/passwd"]
    for probe in probes:
        quoted = urllib.parse.quote(probe, safe="/")
        status, _, content = admin.get(f"/api/download?directory={a}&path={quoted}")
        check(f"обход каталога заблокирован ({probe})",
              status in (400, 403, 404) and b"root:" not in content, f"status={status}")

    status, _, content = admin.get(
        f"/api/preview?directory={a}&path=" + urllib.parse.quote("../../../../etc/passwd", safe="/")
    )
    check("предпросмотр вне каталога заблокирован",
          status in (400, 403, 404) and b"root:" not in content, f"status={status}")

    status, _, _ = admin.get(f"/api/list?directory={a}&path=" + urllib.parse.quote("../../..", safe="/"))
    check("листинг вне каталога заблокирован", status in (400, 403, 404), f"status={status}")

    status, _, content = admin.get(
        f"/api/download?directory={a}&path=" + urllib.parse.quote("user-note.txt", safe="/")
    )
    check("обычный файл после проверок читается", status == 200 and len(content) > 0, f"status={status}")


# ---------------------------------------------------------------------------
# Media preview & streaming
# ---------------------------------------------------------------------------
MEDIA_DIR = "SmokeMedia"
MEDIA_NAME = "smoke-clip.mp4"
MKV_NAME = "smoke-clip.mkv"
PDF_NAME = "smoke.pdf"
HTML_NAME = "smoke-page.html"
MEDIA_BYTES = bytes(range(256)) * 16            # 4096 bytes
PDF_BYTES = b"%PDF-1.4\n% sunduk smoke test\n%%EOF\n"
HTML_BYTES = b"<html><body><script>alert(1)</script></body></html>\n"


def test_media(admin, alpha):
    a = alpha["id"]
    status, _, _ = admin.post_json("/api/mkdir", {"directory": a, "path": "", "name": MEDIA_DIR})
    check("медиа: тестовая папка создана", status == 200, f"status={status}")

    for name, content in ((MEDIA_NAME, MEDIA_BYTES), (MKV_NAME, MEDIA_BYTES),
                          (PDF_NAME, PDF_BYTES), (HTML_NAME, HTML_BYTES)):
        status, _, body = admin.post_multipart(
            "/api/upload", {"directory": a, "path": MEDIA_DIR}, "files", name, content,
        )
        check(f"медиа: загружен {name}",
              status == 200 and name in payload(body).get("uploaded", []), f"status={status}")

    status, _, body = admin.get(f"/api/list?directory={a}&path={MEDIA_DIR}")
    kinds = {e["name"]: e["kind"] for e in payload(body).get("entries", [])}
    check("медиа: распознан видеофайл", kinds.get(MEDIA_NAME) == "video", str(kinds))
    check("медиа: распознан pdf", kinds.get(PDF_NAME) == "pdf", str(kinds))

    url = f"/api/preview?directory={a}&path={MEDIA_DIR}/{MEDIA_NAME}"
    status, headers, content = admin.get(url)
    check("медиа: mp4 отдаётся как video/mp4",
          status == 200 and headers.get("content-type", "").startswith("video/mp4"),
          f"status={status} type={headers.get('content-type')}")
    check("медиа: mp4 открывается в браузере",
          "inline" in headers.get("content-disposition", "").lower(),
          headers.get("content-disposition", ""))
    check("медиа: поддержка Range объявлена", headers.get("accept-ranges") == "bytes",
          str(headers.get("accept-ranges")))
    check("медиа: mp4 отдан целиком", len(content) == len(MEDIA_BYTES), f"len={len(content)}")

    status, headers, content = admin.get(url, headers={"Range": "bytes=0-99"})
    check("медиа: запрос диапазона -> 206", status == 206, f"status={status}")
    check("медиа: Content-Range корректен",
          headers.get("content-range") == f"bytes 0-99/{len(MEDIA_BYTES)}",
          str(headers.get("content-range")))
    check("медиа: тело диапазона 100 байт", len(content) == 100, f"len={len(content)}")

    status, headers, _ = admin.get(f"/api/preview?directory={a}&path={MEDIA_DIR}/{MKV_NAME}")
    check("медиа: mkv получает video/x-matroska",
          status == 200 and headers.get("content-type") == "video/x-matroska",
          f"status={status} type={headers.get('content-type')}")

    status, headers, content = admin.get(f"/api/preview?directory={a}&path={MEDIA_DIR}/{PDF_NAME}")
    check("медиа: pdf открывается браузером",
          status == 200 and headers.get("content-type") == "application/pdf",
          f"status={status} type={headers.get('content-type')}")
    check("медиа: pdf разрешено встраивать своему источнику",
          headers.get("x-frame-options") == "SAMEORIGIN", str(headers.get("x-frame-options")))
    check("медиа: csp pdf допускает frame-ancestors 'self'",
          "frame-ancestors 'self'" in headers.get("content-security-policy", ""),
          str(headers.get("content-security-policy")))
    check("медиа: pdf отдан целиком", content == PDF_BYTES, f"len={len(content)}")

    status, headers, _ = admin.get(f"/api/preview?directory={a}&path={MEDIA_DIR}/{HTML_NAME}")
    check("медиа: html всегда скачивается",
          status == 200 and "attachment" in headers.get("content-disposition", "").lower(),
          headers.get("content-disposition", ""))
    check("медиа: html не получает text/html",
          headers.get("content-type") == "application/octet-stream",
          str(headers.get("content-type")))

    if ARGS.big_mb > 0:
        test_large_upload(admin, a)
    else:
        note("крупная загрузка пропущена (включить: --big-mb 96)")


def test_large_upload(admin, directory_id):
    """A file larger than the spool threshold and the 64 MB tmpfs must survive."""
    name = "smoke-large.bin"
    blob = b"\0" * (ARGS.big_mb * 1024 * 1024)
    try:
        status, _, body = admin.post_multipart(
            "/api/upload", {"directory": directory_id, "path": MEDIA_DIR}, "files", name, blob,
        )
    except Exception as exc:  # noqa: BLE001 - surface urllib failures as a FAIL
        check(f"крупная загрузка ({ARGS.big_mb} МБ)", False, f"{type(exc).__name__}: {exc}")
        return
    check(f"крупная загрузка ({ARGS.big_mb} МБ)",
          status == 200 and name in payload(body).get("uploaded", []), f"status={status}")

    status, _, body = admin.get(f"/api/list?directory={directory_id}&path={MEDIA_DIR}")
    entry = next((e for e in payload(body).get("entries", []) if e["name"] == name), {})
    check("крупный файл сохранён целиком", entry.get("size") == len(blob), f"size={entry.get('size')}")

    status, _, _ = admin.post_json(
        "/api/delete", {"directory": directory_id, "path": f"{MEDIA_DIR}/{name}"}
    )
    check("крупный файл удалён", status == 200, f"status={status}")


# ---------------------------------------------------------------------------
# External links: admin side
# ---------------------------------------------------------------------------
SHARE_PASSWORD = "secret1"


def test_links_admin(admin, alpha):
    a = alpha["id"]
    file_path = f"{MEDIA_DIR}/{MEDIA_NAME}"

    status, _, body = admin.post_json(
        "/api/share/create", {"directory": a, "path": file_path, "expiry_hours": 2}
    )
    info = payload(body)
    file_token = (info.get("url") or "").rsplit("/s/", 1)[-1]
    check("создана ссылка на файл",
          status == 200 and bool(file_token) and info.get("is_dir") is False, f"status={status}")
    check("ссылка построена из base_url",
          (info.get("url") or "").startswith(BASE_URL + "/s/"),
          f"url={info.get('url')} base_url={BASE_URL}")

    status, _, body = admin.post_json(
        "/api/share/create",
        {"directory": a, "path": "", "expiry_hours": 2,
         "password": SHARE_PASSWORD, "max_downloads": 3},
    )
    folder = payload(body)
    folder_token = (folder.get("url") or "").rsplit("/s/", 1)[-1]
    check("создана ссылка на папку с паролем",
          status == 200 and bool(folder_token) and folder.get("is_dir") is True, f"status={status}")
    check("пароль ссылки вернулся в ответе", folder.get("password") == SHARE_PASSWORD,
          str(folder.get("password")))

    status, _, body = admin.post_json(
        "/api/share/create", {"directory": a, "path": "user-note.txt", "expiry_hours": 2}
    )
    inline_token = (payload(body).get("url") or "").rsplit("/s/", 1)[-1]
    check("создана ссылка на текстовый файл", status == 200 and bool(inline_token), f"status={status}")

    status, _, _ = admin.post_json("/api/share/create", {"directory": a, "path": "", "password": "ab"})
    check("короткий пароль ссылки отклонён", status == 400, f"status={status}")

    status, _, _ = admin.post_json("/api/share/create", {"directory": "no-such-directory", "path": ""})
    check("ссылка в неизвестный каталог отклонена", status == 404, f"status={status}")

    status, _, _ = admin.post_json("/api/share/create", {"directory": a, "path": "нет-файла"})
    check("ссылка на несуществующий путь отклонена", status == 404, f"status={status}")

    status, _, body = admin.get("/api/share/list")
    data = payload(body)
    links = data.get("links", [])
    summary = data.get("summary", {})
    check("список ссылок получен", status == 200 and len(links) >= 3, f"links={len(links)}")
    check("сводка по ссылкам",
          summary.get("total") == len(links) and summary.get("active") == len(links), str(summary))
    protected = [link for link in links if link.get("has_password")]
    check("пароль ссылки виден администратору",
          bool(protected) and protected[0].get("password") == SHARE_PASSWORD,
          str([link.get("password") for link in protected]))
    check("ссылки содержат имя каталога", all(link.get("directory_name") for link in links))
    check("ссылки содержат готовый url", all(link.get("url") for link in links))
    return file_token, folder_token, inline_token


# ---------------------------------------------------------------------------
# External links: public side, revocation, logout
# ---------------------------------------------------------------------------
def test_links_public(admin, file_token, folder_token, inline_token):
    anon = Client(PUBLIC)
    status, _, _ = anon.get("/api/share/does-not-exist")
    check("неизвестный токен отвергнут", status == 404, f"status={status}")
    status, _, content = anon.get("/s/does-not-exist")
    check("страница ссылки отдаётся", status == 200 and b"share.js" in content, f"status={status}")

    status, _, body = anon.get(f"/api/share/{file_token}")
    meta = payload(body)
    check("публичные метаданные файла",
          status == 200 and meta.get("is_dir") is False and bool((meta.get("file") or {}).get("name")),
          f"status={status}")

    status, headers, content = anon.get(f"/api/share/{file_token}/download")
    check("публичное скачивание", status == 200 and len(content) == len(MEDIA_BYTES),
          f"status={status} len={len(content)}")
    check("скачивание как вложение",
          "attachment" in headers.get("content-disposition", "").lower(),
          headers.get("content-disposition", ""))
    check("заголовок nosniff", headers.get("x-content-type-options") == "nosniff")
    check("запрет кеширования", "no-store" in headers.get("cache-control", ""))

    status, headers, _ = anon.get(f"/api/share/{file_token}/raw")
    check("публичное видео открывается браузером",
          status == 200 and headers.get("content-type") == "video/mp4"
          and "inline" in headers.get("content-disposition", "").lower(),
          f"status={status} type={headers.get('content-type')}")

    status, headers, content = anon.get(f"/api/share/{file_token}/raw", headers={"Range": "bytes=100-199"})
    check("публичный запрос диапазона -> 206", status == 206, f"status={status}")
    check("публичный Content-Range",
          headers.get("content-range") == f"bytes 100-199/{len(MEDIA_BYTES)}",
          str(headers.get("content-range")))
    check("публичный диапазон 100 байт", len(content) == 100, f"len={len(content)}")

    status, headers, _ = anon.get(f"/api/share/{inline_token}/raw")
    check("текстовый файл отдаётся inline",
          status == 200 and "inline" in headers.get("content-disposition", "").lower(),
          f"status={status} {headers.get('content-disposition', '')}")

    status, _, body = anon.get(f"/api/share/{folder_token}")
    check("ссылка с паролем просит пароль",
          status == 200 and payload(body).get("requires_password") is True, f"status={status}")
    status, _, _ = anon.get(f"/api/share/{folder_token}/download")
    check("до ввода пароля скачивание закрыто", status == 401, f"status={status}")
    status, _, _ = anon.post_json(f"/api/share/{folder_token}/unlock", {"password": "nope"})
    check("неверный пароль ссылки отклонён", status == 401, f"status={status}")
    status, _, _ = anon.post_json(f"/api/share/{folder_token}/unlock", {"password": SHARE_PASSWORD})
    check("верный пароль ссылки принят", status == 200, f"status={status}")

    status, _, body = anon.get(f"/api/share/{folder_token}")
    check("папка по ссылке отдаёт содержимое",
          status == 200 and len(payload(body).get("entries", [])) > 0, f"status={status}")
    status, _, body = anon.get(f"/api/share/{folder_token}?path={MEDIA_DIR}")
    check("навигация внутри ссылки",
          status == 200 and len(payload(body).get("entries", [])) > 0, f"status={status}")
    status, _, content = anon.get(
        f"/api/share/{folder_token}/download?path={MEDIA_DIR}/{MEDIA_NAME}"
    )
    check("скачивание внутри ссылки", status == 200 and len(content) == len(MEDIA_BYTES),
          f"status={status}")

    for probe in ("../../../../etc/passwd", "/etc/passwd"):
        status, _, content = anon.get(
            f"/api/share/{folder_token}/download?path=" + urllib.parse.quote(probe, safe="/%")
        )
        check(f"публичный обход каталога заблокирован ({probe})",
              status in (400, 403, 404) and b"root:" not in content, f"status={status}")

    status, _, body = admin.get("/api/share/list")
    target = next((link for link in payload(body).get("links", []) if link.get("name") == MEDIA_NAME), None)
    if check("ссылка для отзыва найдена", target is not None):
        status, _, _ = admin.post_json("/api/share/revoke", {"id": target["id"]})
        check("отзыв ссылки", status == 200, f"status={status}")
        status, _, _ = anon.get(f"/api/share/{file_token}")
        check("отозванная ссылка не работает", status == 404, f"status={status}")

    status, _, _ = admin.post_json("/api/logout", {})
    check("выход из панели", status == 200, f"status={status}")
    status, _, body = admin.get("/api/me")
    check("сессия очищена", payload(body).get("authenticated") is False, f"status={status}")

    status, _, body = admin.post_json("/api/login", {"username": ARGS.user, "password": ARGS.password})
    admin.csrf = payload(body).get("csrf")
    check("повторный вход", status == 200 and bool(admin.csrf), f"status={status}")


# ---------------------------------------------------------------------------
# Removing a directory from the panel
# ---------------------------------------------------------------------------
def test_directory_removal(admin, alpha, beta):
    b = beta["id"]
    status, _, body = admin.post_json(
        "/api/share/create", {"directory": b, "path": "", "expiry_hours": 1}
    )
    info = payload(body)
    token = (info.get("url") or "").rsplit("/s/", 1)[-1]
    check("подготовлена ссылка в убираемом каталоге", status == 200 and bool(token), f"status={status}")

    beta_root = container_fixture_path(f"{FIXTURE_BASE}/beta")
    status, _, body = admin.post_json("/api/directories/remove", {"id": b})
    result = payload(body)
    check("каталог убран из панели", status == 200, f"status={status}")
    check("ссылки убранного каталога отозваны", int(result.get("revoked_links") or 0) >= 1, str(result))
    check("файлы на сервере не тронуты", os.path.isdir(beta_root), beta_root)

    status, _, _ = admin.get(f"/api/list?directory={b}&path=")
    check("убранный каталог недоступен", status == 404, f"status={status}")

    status, _, body = admin.get("/api/directories")
    ids = [d["id"] for d in payload(body).get("directories", [])]
    check("каталога нет в списке", b not in ids, str(ids))

    status, _, _ = Client(PUBLIC).get(f"/api/share/{token}")
    check("ссылка убранного каталога не работает", status == 404, f"status={status}")

    status, _, _ = admin.post_json("/api/directories/remove", {"id": "нет-такого"})
    check("уборка несуществующего каталога -> 404", status == 404, f"status={status}")


def cleanup(alpha, beta_id):
    """Remove both panel entries and the fixture directories from the server."""
    client = Client(ADMIN)
    status, _, body = client.post_json(
        "/api/login", {"username": ARGS.user, "password": ARGS.password}
    )
    client.csrf = payload(body).get("csrf")
    if status != 200:
        note("не удалось войти для очистки — уберите тестовые каталоги в панели вручную")
    else:
        for directory_id in (alpha.get("id") if alpha else None, beta_id):
            if directory_id:
                remove_directory_entry(client, directory_id)
    root = container_fixture_path(FIXTURE_BASE)
    shutil.rmtree(root, ignore_errors=True)
    note(f"тестовые каталоги удалены: {root}")


def main():
    global ARGS, SETTINGS, ADMIN, PUBLIC, BASE_URL

    parser = argparse.ArgumentParser(description="Сквозной тест «Сундук» (запускается в контейнере)")
    parser.add_argument("--user", default="admin", help="логин администратора")
    parser.add_argument("--password", default="admin", help="текущий пароль администратора")
    parser.add_argument("--new-password", default="sunduk-smoke",
                        help="пароль, на который проверяется смена при первом входе")
    parser.add_argument("--big-mb", type=int, default=0,
                        help="дополнительно загрузить файл такого размера (МБ)")
    ARGS = parser.parse_args()
    SETTINGS = load_settings()

    ADMIN = f"http://127.0.0.1:{SETTINGS.get('admin_port', 8080)}"
    PUBLIC = f"http://127.0.0.1:{SETTINGS.get('public_port', 8081)}"
    BASE_URL = str(SETTINGS.get("base_url") or PUBLIC).rstrip("/")

    print(f"панель    : {ADMIN}")
    print(f"ссылки    : {PUBLIC}")
    print(f"base_url  : {BASE_URL}")
    print(f"фикстуры  : {FIXTURE_BASE} (путь на сервере)\n")

    admin, login_data = test_health_and_auth()
    if admin is None:
        return 1
    test_password_flow(admin, login_data)
    test_csrf_gate()

    directories = test_directories(admin)
    if directories is None:
        return 1
    alpha, beta = directories

    try:
        test_browsing_and_writes(admin, alpha, beta)
        test_media(admin, alpha)
        file_token, folder_token, inline_token = test_links_admin(admin, alpha)
        test_links_public(admin, file_token, folder_token, inline_token)
        test_directory_removal(admin, alpha, beta)
        beta = None                                    # removed inside the test
    finally:
        cleanup(alpha, beta["id"] if beta else None)

    total = PASSED + len(FAILURES)
    print(f"\n{'-' * 60}\n{PASSED}/{total} проверок пройдено")
    if FAILURES:
        print("Провалено: " + ", ".join(FAILURES))
        return 1
    print("Все проверки пройдены")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except urllib.error.URLError as exc:
        print(f"connection error: {exc}")
        sys.exit(2)








