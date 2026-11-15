#!/usr/bin/env python3
"""Переименование приложения под свой GitHub-аккаунт (подготовка к Flathub).

Flathub требует, чтобы app-id в схеме `io.github.<логин>.<Имя>` соответствовал
реальному репозиторию на GitHub. Этот скрипт меняет app-id сразу везде, где он
зашит: метаданные, .desktop, иконка, схема GSettings и её путь, константы в
коде, манифесты Flatpak, meson-сборка, скрипты и README.

    ./tools/setup-app-id.sh io.github.my-login.Ermitage https://github.com/my-login/ermitage

Без второго аргумента URL репозитория остаётся прежним (заглушка).
"""
from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OLD_ID = "io.github.ermitage.Ermitage"
OLD_DEV = "io.github.ermitage"
OLD_URL = "https://github.com/ermitage/ermitage"
TEXT_SUFFIXES = {".py", ".xml", ".json", ".build", ".desktop", ".sh", ".md",
                 ".css", ".toml", ".in"}
SKIP_DIRS = {"build", "dist", ".git", "samples", "build-flatpak",
             ".flatpak-builder-state", "flatpak-repo", "node_modules"}


def fail(message: str) -> "None":
    print(f"ошибка: {message}", file=sys.stderr)
    raise SystemExit(1)


def main() -> None:
    if len(sys.argv) < 2:
        fail("укажите новый app-id, например io.github.my-login.Ermitage")
    new_id = sys.argv[1].strip()
    if not re.fullmatch(r"[A-Za-z][\w-]*(\.[A-Za-z][\w-]*){2,}", new_id):
        fail(f"«{new_id}» не похоже на app-id: нужны ≥3 компонента через точку")
    if new_id == OLD_ID:
        fail("app-id не изменился")

    new_url = (sys.argv[2].strip().rstrip("/") if len(sys.argv) > 2 else "")
    match = re.fullmatch(r"https://github\.com/([\w.-]+)/([\w.-]+)", new_url or "")
    if len(sys.argv) > 2 and not match:
        fail("URL должен быть вида https://github.com/<логин>/<репозиторий>")

    old_path = OLD_ID.replace(".", "/")
    new_path = new_id.replace(".", "/")
    new_dev = new_id.rsplit(".", 1)[0]

    subs: list[tuple[str, str]] = [(OLD_ID, new_id), (old_path, new_path)]
    if match:
        login, repo = match.groups()
        raw_old = "https://raw.githubusercontent.com/ermitage/ermitage"
        raw_new = f"https://raw.githubusercontent.com/{login}/{repo}"
        subs += [(OLD_URL, new_url), (raw_old, raw_new)]
    subs.append((OLD_DEV, new_dev))

    changed = []
    for path in sorted(ROOT.rglob("*")):
        if not path.is_file() or path.suffix not in TEXT_SUFFIXES:
            continue
        if SKIP_DIRS & set(path.relative_to(ROOT).parts):
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        updated = text
        for old, new in subs:
            updated = updated.replace(old, new)
        if updated != text:
            path.write_text(updated, encoding="utf-8")
            changed.append(str(path.relative_to(ROOT)))

    renamed = []
    for path in list(ROOT.rglob(f"*{OLD_ID}*")):
        if not path.is_file() or SKIP_DIRS & set(path.relative_to(ROOT).parts):
            continue
        target = path.with_name(path.name.replace(OLD_ID, new_id))
        path.rename(target)
        renamed.append(f"{path.relative_to(ROOT)} → {target.relative_to(ROOT)}")

    print(f"app-id:  {OLD_ID} → {new_id}")
    if match:
        print(f"репозиторий: {OLD_URL} → {new_url}")
    print(f"изменено файлов: {len(changed)}" + ("".join(f"\n  {c}" for c in changed)))
    if renamed:
        print("переименовано:" + "".join(f"\n  {r}" for r in renamed))

    # схема GSettings: пересобираем, чтобы локальный запуск видел новый id
    schema_dir = ROOT / "data"
    if (schema_dir / f"{new_id}.gschema.xml").exists():
        subprocess.run(["glib-compile-schemas", str(schema_dir)], check=False)

    print(
        "\nДальше:\n"
        "  1. git add -A && git commit -m 'Переименование под …'\n"
        "  2. git tag -a v1.0.0 -m 'Эрмитаж 1.0.0' && git push --tags\n"
        "  3. git rev-parse v1.0.0^{commit} → подставить в\n"
        f"     flatpak/flathub/{new_id}.json (поле commit)\n"
        "  4. ./tools/build-flatpak.sh --from-git   # проверить сборку из git\n"
    )


if __name__ == "__main__":
    main()
