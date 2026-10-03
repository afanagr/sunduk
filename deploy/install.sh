#!/usr/bin/env sh
# Установка Сундука на сервер: собирает (или загружает) образ и запускает
# контейнер. Запускать из каталога deploy/ с правами root (sudo ./install.sh).
set -eu
cd "$(dirname "$0")"

if ! command -v docker >/dev/null 2>&1; then
    echo "Не найден docker. Установите Docker Engine и повторите." >&2
    exit 1
fi

IMAGE_TAR="$(ls sunduk-*.tar 2>/dev/null | head -n 1 || true)"
if [ -n "$IMAGE_TAR" ]; then
    echo "==> загружаю образ из $IMAGE_TAR"
    docker load -i "$IMAGE_TAR"
else
    echo "==> собираю образ из исходников (../server)"
    docker compose build
fi

echo "==> запускаю контейнер"
docker compose up -d

cat <<'EOF'

Готово.

  Панель  : http://<ip-сервера>:8080   (первый вход: admin / admin)
  Ссылки  : http://<ip-сервера>:8081

Что сделать дальше:
  1. Зайдите в панель и задайте новый пароль (он спрашивается сразу).
  2. Нажмите «Добавить каталог» и укажите путь к папке на сервере.
  3. Если ссылки должны открываться извне — проверьте public_ip в
     config/config.yml (base_url нужен только для HTTPS/домена) и
     перезапустите: docker compose restart

Логи:  docker compose logs -f
Тест:  docker compose exec -T sunduk python tools/smoke_test.py
EOF
