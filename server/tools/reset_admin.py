"""Reset the administrator password from the shell.

    docker compose exec sunduk python tools/reset_admin.py
    docker compose exec sunduk python tools/reset_admin.py 'новый-пароль'
    docker compose exec sunduk python tools/reset_admin.py 'новый-пароль' --no-force-change

Without an argument the account goes back to ``admin``/``admin`` and the panel
asks for a new password on the next login (that is the default behaviour, and
``--no-force-change`` disables it).
"""
from __future__ import annotations

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.config import load_config          # noqa: E402
from app.db import Database                 # noqa: E402
from app.users import (                     # noqa: E402
    DEFAULT_PASSWORD,
    DEFAULT_USERNAME,
    UserService,
)


def main() -> int:
    parser = argparse.ArgumentParser(description="Сменить пароль администратора Сундука")
    parser.add_argument("password", nargs="?", default=DEFAULT_PASSWORD,
                        help=f"новый пароль (по умолчанию: {DEFAULT_PASSWORD})")
    parser.add_argument("--user", default=DEFAULT_USERNAME, help="имя пользователя")
    parser.add_argument("--no-force-change", action="store_true",
                        help="не требовать смены пароля при следующем входе")
    args = parser.parse_args()

    config = load_config()
    db = Database(config.db_path)
    db.init()
    users = UserService(db)
    try:
        users.set_password(args.user, args.password, must_change=not args.no_force_change)
    except ValueError as exc:
        print(f"ошибка: {exc}")
        return 1

    print(f"пароль пользователя «{args.user}» обновлён")
    if not args.no_force_change:
        print("панель потребует задать новый пароль при следующем входе")
    return 0


if __name__ == "__main__":
    sys.exit(main())
