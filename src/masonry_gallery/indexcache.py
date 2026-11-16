"""Дисковый кэш индекса каталога.

Зачем: индекс папки (список файлов + их размеры и размеры картинки) до сих пор
строился заново при каждом запуске. Для 50 000 файлов это ~1 с на быстром SSD и
десятки секунд на HDD или сетевой папке — и пользователь каждый раз видел
«Индексация папки…». Здесь этот результат складывается рядом с миниатюрами:

    $XDG_CACHE_HOME/masonry-gallery/index-v1/<sha1 корней>.json.gz

Проверка актуальности намеренно дешёвая: сравниваются `mtime` самих каталогов,
а не всех файлов. Изменение набора файлов в каталоге всегда меняет его `mtime`,
поэтому добавленные и удалённые файлы видны сразу, а полный `stat` каждого файла
не нужен. Если файл изменили «на месте» (mtime каталога при этом не меняется),
кэш отдаст старые размеры: миниатюра пересчитается по ключу «размер+mtime», а
размеры картинки обновятся при следующем изменении каталога. Это приемлемая
плата за мгновенный старт.

Кэш — только оптимизация: любая ошибка чтения или записи означает «кэша нет»,
и приложение работает как раньше.
"""
from __future__ import annotations

import gzip
import hashlib
import json
import os

import gi

gi.require_version("GLib", "2.0")
from gi.repository import GLib  # noqa: E402

from .model import Item  # noqa: E402

VERSION = 1


def _cache_dir() -> str:
    return os.path.join(GLib.get_user_cache_dir(), "masonry-gallery", "index-v1")


def _cache_path(roots: list[str]) -> str:
    payload = "\x00".join(sorted(os.path.abspath(r) for r in roots))
    digest = hashlib.sha1(payload.encode("utf-8", "replace")).hexdigest()[:20]
    return os.path.join(_cache_dir(), digest + ".json.gz")


def _dir_mtime(directory: str) -> int | None:
    try:
        return os.stat(directory).st_mtime_ns
    except OSError:
        return None


def save(roots: list[str], items: list[Item]) -> None:
    """Сохраняет индекс. Вызывается из фонового потока — не блокирует UI."""
    if not items:
        return
    dirs: dict[str, int] = {}
    for item in items:
        folder = item.folder
        if folder in dirs:
            continue
        stamp = _dir_mtime(folder)
        if stamp is not None:
            dirs[folder] = stamp
    if not dirs:
        return

    payload = {
        "v": VERSION,
        "roots": [os.path.abspath(r) for r in roots],
        "dirs": dirs,
        "items": [
            [item.path, item.size, int(item.mtime), item.width, item.height]
            for item in items
        ],
    }
    target = _cache_path(roots)
    tmp = target + f".{os.getpid()}.tmp"
    try:
        os.makedirs(os.path.dirname(target), exist_ok=True)
        with gzip.open(tmp, "wt", encoding="utf-8", compresslevel=3) as handle:
            json.dump(payload, handle, ensure_ascii=False, separators=(",", ":"))
        os.replace(tmp, target)
    except (OSError, ValueError):
        try:
            os.unlink(tmp)
        except OSError:
            pass


def load(roots: list[str]) -> tuple[list[Item] | None, bool]:
    """Читает индекс: `(элементы, актуален ли)`.

    Элементы возвращаются даже для устаревшего кэша — их можно показать сразу,
    а свежий обход затем заменит список (см. `GalleryModel.load`). `None` —
    кэша нет вовсе.
    """
    target = _cache_path(roots)
    try:
        with gzip.open(target, "rt", encoding="utf-8") as handle:
            payload = json.load(handle)
    except (OSError, ValueError):
        return None, False
    if not isinstance(payload, dict) or payload.get("v") != VERSION:
        return None, False

    dirs = payload.get("dirs") or {}
    raw_items = payload.get("items") or []
    if not dirs or not raw_items:
        return None, False

    # каталоги дешевле проверить до распаковки элементов
    valid = True
    for directory, stamp in dirs.items():
        if _dir_mtime(directory) != stamp:
            valid = False
            break

    items: list[Item] = []
    try:
        for path, size, mtime, width, height in raw_items:
            items.append(
                Item(
                    path=path,
                    name=os.path.basename(path),
                    folder=os.path.dirname(path),
                    size=int(size),
                    mtime=float(mtime),
                    width=int(width),
                    height=int(height),
                )
            )
    except (TypeError, ValueError):
        return None, False
    return (items, valid) if items else (None, False)


def drop(roots: list[str]) -> None:
    """Удаляет кэш для набора корней (например, при явном обновлении)."""
    try:
        os.unlink(_cache_path(roots))
    except OSError:
        pass
