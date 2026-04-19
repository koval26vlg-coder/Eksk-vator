#!/usr/bin/env python3
"""
Краткая сводка для восстановления контекста: последние коммиты + список и хвост свежих логов запусков.

Запуск из корня репозитория:
  python scripts/dev_context.py
  python scripts/dev_context.py --no-tail   # только коммиты и список файлов
"""
from __future__ import annotations

import argparse
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
LOG_DIR = REPO_ROOT / "logs"


def _git_recent(n: int) -> str:
    try:
        r = subprocess.run(
            ["git", "-C", str(REPO_ROOT), "log", f"-{n}", "--oneline", "--no-decorate"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=15,
            check=False,
        )
        if r.returncode != 0:
            return (r.stderr or r.stdout or "").strip() or f"(git exit {r.returncode})"
        return r.stdout.strip() or "(пусто)"
    except FileNotFoundError:
        return "(git не найден в PATH)"
    except Exception as e:
        return f"(git ошибка: {e})"


def _fmt_mtime(path: Path) -> str:
    try:
        ts = path.stat().st_mtime
        dt = datetime.fromtimestamp(ts, tz=timezone.utc).astimezone()
        return dt.strftime("%Y-%m-%d %H:%M %z")
    except OSError:
        return "?"


def main() -> int:
    p = argparse.ArgumentParser(description="Сводка git + logs/ для контекста разработки бота.")
    p.add_argument("--commits", type=int, default=20, help="Сколько последних коммитов показать (по умолчанию 20)")
    p.add_argument("--logs", type=int, default=8, help="Сколько последних run-*.log перечислить")
    p.add_argument("--tail", type=int, default=30, help="Сколько последних строк хвоста у самого свежего лога (0 — не показывать)")
    p.add_argument("--no-tail", action="store_true", help="Не печатать хвост лога")
    args = p.parse_args()
    tail_n = 0 if args.no_tail else args.tail

    print(f"Репозиторий: {REPO_ROOT}")
    print()
    print("=== Git: последние коммиты ===")
    print(_git_recent(args.commits))
    print()
    print(f"=== Каталог логов: {LOG_DIR} ===")
    if not LOG_DIR.is_dir():
        print("(папки нет — запустите бота хотя бы раз: python main.py)")
        return 0

    files = sorted(LOG_DIR.glob("run-*.log"), key=lambda x: x.stat().st_mtime, reverse=True)
    if not files:
        print("(файлов run-*.log пока нет)")
        return 0

    for path in files[: args.logs]:
        try:
            sz = path.stat().st_size
        except OSError:
            sz = -1
        print(f"  {path.name}  |  {sz} bytes  |  {_fmt_mtime(path)}")

    if tail_n <= 0:
        return 0

    newest = files[0]
    print()
    print(f"=== Хвост: {newest.name} (последние {tail_n} строк) ===")
    try:
        text = newest.read_text(encoding="utf-8", errors="replace")
    except OSError as e:
        print(f"(не удалось прочитать: {e})")
        return 1
    lines = text.splitlines()
    for line in lines[-tail_n:]:
        print(line)
    return 0


if __name__ == "__main__":
    sys.exit(main())
