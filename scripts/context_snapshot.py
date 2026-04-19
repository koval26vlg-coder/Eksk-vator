#!/usr/bin/env python3
"""
Снимок контекста для чата с ИИ: последние коммиты + свежие файлы логов.
Запуск из корня репозитория: python scripts/context_snapshot.py
"""

from __future__ import annotations

import subprocess
import sys
from datetime import datetime
from pathlib import Path


def main() -> int:
    root = Path(__file__).resolve().parent.parent
    print("## Ekskavator — снимок контекста\n")

    print("### Git (последние 20 коммитов)\n")
    try:
        r = subprocess.run(
            ["git", "-C", str(root), "log", "--oneline", "-20"],
            capture_output=True,
            text=True,
            check=False,
        )
        print((r.stdout or r.stderr or "(git недоступен)").rstrip() or "(пусто)")
    except OSError as e:
        print(f"(ошибка запуска git: {e})")

    print("\n### Каталог logs/ (до 12 последних по времени изменения)\n")
    log_dir = root / "logs"
    if not log_dir.is_dir():
        print("_Каталог `logs/` ещё не создан — запустите бота хотя бы раз._\n")
        print(f"Абсолютный путь к логам после первого запуска: `{log_dir}`\n")
        return 0

    paths = sorted(log_dir.glob("run-*.log"), key=lambda p: p.stat().st_mtime, reverse=True)[:12]
    if not paths:
        print("_Нет файлов `run-*.log`._\n")
        return 0

    for p in paths:
        st = p.stat()
        ts = datetime.fromtimestamp(st.st_mtime).strftime("%Y-%m-%d %H:%M:%S")
        print(f"- `{p.name}` — {st.st_size} bytes, изменён {ts}")
        print(f"  Полный путь: `{p.resolve()}`")
    print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
