"""Запуск: python -m app (host/port из config.yaml → manager.host, manager.port)."""
from __future__ import annotations

import argparse
import os

import uvicorn

from app.config import load_settings
from app.degraded import FORCE_DEGRADED_ENV


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="NTA Test Stand Manager")
    parser.add_argument(
        "--force-degraded",
        action="store_true",
        help="Принудительный ограниченный режим (или переменная TSM_FORCE_DEGRADED=1)",
    )
    args = parser.parse_args(argv)
    if args.force_degraded:
        os.environ[FORCE_DEGRADED_ENV] = "1"

    settings = load_settings()
    uvicorn.run(
        "app.main:app",
        host=settings.host,
        port=settings.port,
    )


if __name__ == "__main__":
    main()