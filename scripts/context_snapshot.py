#!/usr/bin/env python3
"""
Снимок контекста для чата с ИИ: git, remote, логи, хвост HANDOFF.md.
Запуск из корня репозитория: python scripts/context_snapshot.py
"""

from __future__ import annotations

import subprocess
import sys
from datetime import datetime
from pathlib import Path


def _github_web_url(remote: str) -> str | None:
    u = remote.strip()
    if u.startswith("git@github.com:"):
        path = u.split(":", 1)[1].removesuffix(".git")
        return f"https://github.com/{path}"
    if "github.com" in u and u.startswith("http"):
        return u.removesuffix(".git")
    return None


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

    print("\n### GitHub (remote `origin` — для Issues / Discussions / PR)\n")
    try:
        r = subprocess.run(
            ["git", "-C", str(root), "remote", "get-url", "origin"],
            capture_output=True,
            text=True,
            check=False,
        )
        raw = (r.stdout or "").strip()
        if raw:
            print(f"- clone URL: `{raw}`")
            web = _github_web_url(raw)
            if web:
                print(f"- веб-репозиторий: `{web}` (Issues: `{web}/issues`, Discussions: вкладка на GitHub при включении)")
        else:
            print("_Remote `origin` не настроен._")
    except OSError as e:
        print(f"(ошибка: {e})")

    print("\n### Каталог logs/ (до 12 последних по времени изменения)\n")
    log_dir = root / "logs"
    if not log_dir.is_dir():
        print("_Каталог `logs/` ещё не создан — запустите бота хотя бы раз._")
        print(f"Абсолютный путь после первого запуска: `{log_dir}`\n")
    else:
        paths = sorted(log_dir.glob("run-*.log"), key=lambda p: p.stat().st_mtime, reverse=True)[:12]
        if not paths:
            print("_Нет файлов `run-*.log`._\n")
        else:
            for p in paths:
                st = p.stat()
                ts = datetime.fromtimestamp(st.st_mtime).strftime("%Y-%m-%d %H:%M:%S")
                print(f"- `{p.name}` — {st.st_size} bytes, изменён {ts}")
                print(f"  Полный путь: `{p.resolve()}`")
            print()

    print("### HANDOFF.md (последние 50 строк)\n")
    handoff = root / "HANDOFF.md"
    if not handoff.is_file():
        print("_Файл `HANDOFF.md` не найден в корне репозитория._\n")
        return 0
    try:
        text = handoff.read_text(encoding="utf-8")
    except OSError as e:
        print(f"(не удалось прочитать HANDOFF.md: {e})\n")
        return 0
    lines = text.splitlines()
    tail = lines[-50:] if len(lines) > 50 else lines
    print(f"_Источник: `{handoff.resolve()}` ({len(lines)} строк, ниже хвост ≤50)_\n")
    print("\n".join(tail))
    print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
