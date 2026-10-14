#!/usr/bin/env python3
"""Сброс пароля admin (или создание, если пользователей ещё нет)."""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.auth import bootstrap_admin  # noqa: E402
from app.config import load_settings  # noqa: E402
from app.crypto_util import hash_password  # noqa: E402
from app.db import Database, new_id, utc_now  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="Reset Test Stand Manager admin password")
    parser.add_argument("password", nargs="?", help="новый пароль (иначе из config bootstrap_admin_password)")
    parser.add_argument(
        "--user",
        default=None,
        help="имя пользователя (по умолчанию manager.bootstrap_admin_user)",
    )
    args = parser.parse_args()

    settings = load_settings()
    username = (args.user or settings.bootstrap_admin_user).strip()
    password = args.password or settings.bootstrap_admin_password
    if not password:
        print("Укажите пароль аргументом или bootstrap_admin_password в config.yaml", file=sys.stderr)
        return 1

    db = Database(settings.db_path)
    with db.connection() as conn:
        row = conn.execute("SELECT id FROM users WHERE username=?", (username,)).fetchone()
        if row:
            conn.execute(
                "UPDATE users SET password_hash=?, updated_at=? WHERE username=?",
                (hash_password(password), utc_now(), username),
            )
            print(f"Пароль обновлён для {username!r} ({settings.db_path})")
        else:
            bootstrap_admin(db, username, password)
            n = conn.execute("SELECT COUNT(*) AS c FROM users").fetchone()["c"]
            if n:
                print(f"Создан пользователь {username!r} ({settings.db_path})")
            else:
                print("Не удалось создать пользователя", file=sys.stderr)
                return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
