# Сундук — универсальный стек: Linux, Windows, macOS

Одна и та же установка на любом хосте с Docker: готовый образ, настройка
переменными окружения, каталоги подключаются уже в панели. Ни
`config/config.yml`, ни исходники для запуска из готового образа не нужны.

```
deploy/docker-compose.portable.yml  # стек: готовый образ + переменные (любая ОС)
deploy/docker-compose.build.yml     # стек: сборка из server/ этого репозитория
deploy/.env.example                 # значения стека: скопировать в .env
deploy/install.ps1                  # установка на Windows одной командой
deploy/README-portable.md           # этот файл
```

Файлы лежат в `deploy/` рядом с остальным боевым комплектом — копировать их
куда-то не нужно, все пути ниже указаны от корня репозитория. Исключение —
Dockge и Portainer в режиме Web editor: там в редактор стека вставляется
содержимое файла.

## Какой файл когда брать

| Способ запуска | Файл |
|----------------|------|
| Dockge | `deploy/docker-compose.portable.yml` — как `compose.yaml` стека, рядом `.env` из `deploy/.env.example` |
| Portainer, Web editor | содержимое `deploy/docker-compose.portable.yml`, значения — в «Environment variables» |
| Portainer, Repository (из git) | `deploy/docker-compose.build.yml` — путь в поле *Compose path*; Portainer склонирует репозиторий и соберёт образ |
| Windows «одной командой» | `deploy/install.ps1` (файл стека берётся рядом со скриптом) |
| Linux-сервер без панелей | `deploy/docker-compose.portable.yml` |
| Linux + firewall, без проброса портов | `deploy/docker-compose.dockge.yml` — минимальный стек из `ghcr.io` на `network_mode: host` |

## Вариант 1. Windows / macOS (Docker Desktop)

```powershell
# из корня репозитория — файл стека скрипт ищет рядом с собой
.\deploy\install.ps1 -HostRoot 'C:\Share' -PublicAddress '192.168.1.50'
```

Скрипт проверит Docker, создаст `C:\Share`, подберёт образ (архив `sunduk-*.tar`
→ локальный `sunduk:latest` → сборка из `server/` → GHCR), запустит стек,
дождётся ответа панели и напечатает адреса. Панель: `http://<адрес>:8080`,
первый вход `admin / admin`.

Без скрипта — то же самое вручную:

```powershell
$env:HOST_ROOT='C:\Share'; $env:PUBLIC_ADDRESS='192.168.1.50'
docker compose -f deploy\docker-compose.portable.yml up -d
```

## Вариант 2. Linux-сервер

```bash
cp deploy/.env.example .env   # HOST_ROOT=/ и остальные значения
docker compose -f deploy/docker-compose.portable.yml up -d
```

Если образ из GHCR тянуть нельзя — соберите его из клонированного репозитория
(`docker compose -f deploy/docker-compose.build.yml build`) либо загрузите
`sunduk-<версия>.tar` из релиза (`docker load -i`) и задайте
`SUNDUK_IMAGE=sunduk:latest`.

## Вариант 3. Dockge

1. **Создать стек** → имя `sunduk`.
2. В редактор вставить содержимое `deploy/docker-compose.portable.yml`.
3. Рядом (в каталоге стека) создать `.env` из `deploy/.env.example` — Dockge
   подхватит его сам. Вместо этого можно вписать значения прямо в секцию
   `environment:`.
4. **Deploy**.

Dockge не клонирует репозиторий, поэтому сборка из исходников делается заранее из
клона: `docker compose -f deploy/docker-compose.build.yml build` — образ получит
имя `sunduk:latest`, после чего в стеке достаточно `SUNDUK_IMAGE=sunduk:latest`.
Копировать `docker-compose.build.yml` в каталог стека Dockge нельзя: контекст
сборки `../server` там не найдётся.

Для Linux-сервера, где важна работа firewall, в репозитории есть
`deploy/docker-compose.dockge.yml` — тот же минимальный стек, но на
`network_mode: host`: контейнер слушает порты как обычная программа, и правила
хоста его закрывают. В Docker Desktop (Windows/macOS) этот вариант не работает:
там host-сеть живёт внутри его Linux-ВМ.

## Вариант 4. Portainer

### А. Web editor (готовый образ)

1. **Stacks → Add stack** → имя `sunduk`.
2. **Web editor** → вставить содержимое `deploy/docker-compose.portable.yml`.
3. **Environment variables**: `HOST_ROOT=C:\Share`, `PUBLIC_ADDRESS=192.168.1.50`,
   при необходимости `ADMIN_PORT`, `PUBLIC_PORT`, `LAN_ALLOWLIST`,
   `SUNDUK_IMAGE=sunduk:latest`.
4. Если пакет GHCR приватный — добавьте registry-доступ
   (**Registries → Add registry → GitHub Container Registry**), либо сделайте
   пакет Public, либо соберите образ локально и укажите `SUNDUK_IMAGE`.
5. **Deploy the stack**.

### Б. Repository — «универсальный git» (без GHCR и без логинов)

1. **Stacks → Add stack → Repository**:
   * *Repository URL* — `https://github.com/afanagr/sunduk` (или ваш форк);
   * *Repository reference* — ветка или тег, например `v1.1.2`;
   * *Compose path* — `deploy/docker-compose.build.yml`.
2. **Environment variables** — как в варианте А (достаточно `HOST_ROOT` и
   `PUBLIC_ADDRESS`).
3. **Deploy the stack**: Portainer склонирует репозиторий и соберёт образ из
   `server/` (`build: ../server`) — ни registry-логина, ни интернета на сервере,
   кроме GitHub, не требуется. Обновление — **Pull and redeploy** с новым тегом.
4. Если образ остался прежним (панель не обновилась), соберите его на хосте
   вручную из того же клона:
   `docker compose -f deploy/docker-compose.build.yml build`, а в
   portable-стеке укажите `SUNDUK_IMAGE=sunduk:latest`.

## Переменные стека

| Переменная | По умолчанию | Что делает |
|-----------|--------------|------------|
| `ADMIN_PORT` | `8080` | порт панели; публикуется и передаётся приложению, чтобы ссылки были верными. Открывать только в LAN |
| `PUBLIC_PORT` | `8081` | порт внешних ссылок: его открывать наружу (или ставить HTTPS-прокси) |
| `PUBLIC_ADDRESS` | `localhost` | IP, домен или `https://домен` — из него строятся все внешние ссылки |
| `HOST_ROOT` | `/` | что панель видит как файловую систему: Linux `/`, Windows `C:\Share`, macOS `/Users/имя` |
| `LAN_ALLOWLIST` | локальные сети | кому отвечает панель, CIDR через запятую |
| `CONTAINER_NAME` | `sunduk` | имя контейнера (для второго экземпляра рядом) |
| `DATA_VOLUME` | `sunduk_data` | том с базой, ключом и буфером загрузок; то же имя, что у остальных стеков Сундука — данные не теряются при смене способа запуска |
| `SUNDUK_IMAGE` | `ghcr.io/afanagr/sunduk:latest` | образ; `sunduk:latest` — если он уже есть локально |

## Что важно знать про системы

| Система | `HOST_ROOT` | Особенности |
|---------|-------------|-------------|
| Linux | `/` (весь сервер) или `/mnt` | `docker compose up` — от root; опубликованные порты обходят `ufw`/`firewalld` — закрывайте доступ правилами `DOCKER-USER` либо используйте host-стек `deploy/docker-compose.dockge.yml` |
| Windows (Docker Desktop) | `C:\Share` | `HOST_ROOT=/` покажет **не диск, а файловую систему Linux-ВМ Docker Desktop** (видны `host_mnt`, `mutagen-file-shares`) — панель будет работать не с вашими файлами. Весь `C:\` подключить можно, но панель упрётся в системные файлы (`pagefile.sys`, `DumpStack.log.tmp` → «нет доступа»), поэтому лучше отдельная папка |
| macOS (Docker Desktop) | `/Users/имя` | корень `/` Desktop отдать не может; берите каталог в `/Users` (Desktop спросит доступ к папке) |

Ограничения Docker Desktop, о которых стоит знать заранее:

* контейнер видит адрес NAT-шлюза Desktop (`192.168.65.x`), а не реальный IP
  клиента — поэтому `LAN_ALLOWLIST` по умолчанию пропускает всех, а точные
  лимиты «по клиенту» и `trusted_proxies` работают только при запуске на Linux
  (там адрес виден как есть, особенно в host-стеке);
* `network_mode: host` в Desktop не публикует порты на хост-систему — используйте
  этот стек: он на пробросе портов и проверен на Windows.

## Порты и HTTPS

| Порт | Назначение | Наружу |
|------|-----------|--------|
| `8080` (панель) | вход по паролю, локальная сеть | **нет** |
| `8081` (ссылки) | страницы внешних ссылок | да, либо через прокси |

```caddy
files.example.com {
    reverse_proxy 127.0.0.1:8081
}
```

`PUBLIC_ADDRESS=https://files.example.com` включает `Secure`-cookie
автоматически. Если прокси передаёт `X-Forwarded-For`, укажите его сеть в
`trusted_proxies`: это единственная настройка, которой нет среди переменных
окружения, — её задают файлом `config/config.yml`, смонтировав его в стек
(`- ./config:/config:ro`). Иначе лимиты будут считаться по адресу прокси.

## Эксплуатация

```bash
docker compose -f deploy/docker-compose.portable.yml logs -f        # логи
docker compose -f deploy/docker-compose.portable.yml restart        # применить config.yml
docker compose -f deploy/docker-compose.portable.yml pull           # новая версия образа
docker compose -f deploy/docker-compose.portable.yml up -d          # обновиться
docker compose -f deploy/docker-compose.portable.yml down           # остановить (данные целы)
docker compose -f deploy/docker-compose.portable.yml down -v        # стереть базу/ключ/ссылки

docker exec -it sunduk python tools/reset_admin.py           # вернуть admin/admin
docker exec sunduk python tools/reset_admin.py 'новый-пароль'
docker exec -T sunduk python tools/smoke_test.py             # самопроверка

# Бэкап (в Windows PowerShell вместо "$PWD" подставьте свой путь)
docker run --rm -v sunduk_data:/data -v "$PWD:/backup" alpine \
    tar czf /backup/sunduk-backup.tgz -C /data .
```

В архиве важны `sunduk.db` (каталоги, пользователи, ссылки) и `secret.key` — без
него не расшифровать токены и пароли ссылок.

## Если что-то не так

| Симптом | Решение |
|---------|---------|
| `pull access denied for ghcr.io/afanagr/sunduk` | пакет приватный: сделать Public, выполнить `docker login ghcr.io`, либо `SUNDUK_IMAGE=sunduk:latest` с локально собранным/загруженным образом |
| Панель открывается, но «каталог не найден на сервере» | путь вне `HOST_ROOT`: на Windows задайте `C:\Share`, на macOS — `/Users/имя` |
| В списке папок видно `host_mnt`, `mutagen-file-shares` | `HOST_ROOT=/` на Docker Desktop: это файловая система его ВМ, укажите реальный каталог |
| «Нет доступа» к `pagefile.sys`, `DumpStack.log.tmp` | подключён весь `C:\`: ограничьте доступ папкой (`HOST_ROOT=C:\Share`) |
| Внешние ссылки ведут не на тот порт | порт публикации и `public_port` должны совпадать — в этом стеке оба берутся из `ADMIN_PORT`/`PUBLIC_PORT` |
| Панель недоступна с другого компьютера | панель отвечает только локальным сетям: проверьте `LAN_ALLOWLIST` и firewall (Windows спросит разрешение порта при первом запуске) |
| `address already in use` | порт занят: смените `ADMIN_PORT`/`PUBLIC_PORT` |
| Нужны данные прежней установки | укажите `DATA_VOLUME` от неё (по умолчанию `sunduk_data` — том всех стеков Сундука) |
| Контейнер сразу перезапускается | `docker compose ... logs` — обычно опечатка в `config/config.yml` (если он подключён) |
| В логах `data directory /data is unusable: no write permission` | том остался от старой версии и принадлежит другому пользователю: `docker run --rm -v sunduk_data:/data alpine chown 0:0 /data` |
| `failed to solve ... server: not found` при сборке | `docker-compose.build.yml` собирается только из клона репозитория (контекст `../server`): не копируйте этот файл в каталог стека панели, указывайте путь к нему внутри клона |
