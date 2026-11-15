"""Модель данных галереи.

Модель намеренно сделана «плоской»: два python-списка (полный набор и
отображаемый срез). Это даёт O(1) доступ по индексу для виртуализированного
представления и предсказуемое поведение памяти — никаких цепочек
GtkFilterListModel → GtkSortListModel → GtkSelectionModel, которые на десятках
тысяч элементов дают ощутимый оверхед на каждый запрос индекса.

Индексация каталога выполняется в фоновом потоке, размеры изображений читаются
из заголовков файлов параллельно (только заголовок, без декодирования пикселей).
"""
from __future__ import annotations

import os
import random
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass

from gi.repository import GLib

from .imageinfo import dimensions as read_dimensions

# Расширения, которые умеет декодировать gdk-pixbuf (+ svg через librsvg)
IMAGE_SUFFIXES = {
    ".jpg", ".jpeg", ".jpe", ".png", ".webp", ".gif", ".bmp", ".tif", ".tiff",
    ".avif", ".heic", ".heif", ".jxl", ".svg", ".ico", ".tga", ".ppm", ".pgm",
}

# Каталоги, в которые не стоит заходить при рекурсивном обходе
SKIP_DIRS = {
    "node_modules", ".git", ".cache", "__pycache__", "venv", ".venv",
    "site-packages", ".thumbnails", "lost+found",
}


@dataclass(slots=True)
class Item:
    """Один файл изображения."""

    path: str
    name: str
    folder: str
    size: int
    mtime: float
    width: int = 0
    height: int = 0

    @property
    def aspect(self) -> float:
        """Соотношение сторон; 1.0, если размеры неизвестны/битые."""
        if self.width > 0 and self.height > 0:
            return self.width / self.height
        return 1.0

    @property
    def ext(self) -> str:
        return os.path.splitext(self.name)[1].lstrip(".").upper()


def _probe_dimensions(item: Item) -> Item:
    """Читает только заголовок файла: ~15 мкс вместо декодирования пикселей."""
    dims = read_dimensions(item.path)
    if dims is not None:
        item.width, item.height = dims
    return item


def scan_directory(root: str, max_depth: int = 6, limit: int = 50_000) -> list[Item]:
    """Рекурсивный обход каталога. Возвращает список файлов-изображений."""
    root = os.path.abspath(os.path.expanduser(root))
    out: list[Item] = []
    stack: list[tuple[str, int]] = [(root, 0)]
    while stack:
        directory, depth = stack.pop()
        try:
            entries = os.scandir(directory)
        except OSError:
            continue
        with entries:
            for entry in entries:
                if entry.name.startswith("."):
                    continue
                try:
                    if entry.is_dir(follow_symlinks=False):
                        if depth < max_depth and entry.name not in SKIP_DIRS:
                            stack.append((entry.path, depth + 1))
                    elif entry.is_file(follow_symlinks=False):
                        ext = os.path.splitext(entry.name)[1].lower()
                        if ext in IMAGE_SUFFIXES:
                            st = entry.stat()
                            out.append(
                                Item(
                                    path=entry.path,
                                    name=entry.name,
                                    # каталог один и тот же у тысяч файлов —
                                    # интернируем строку, экономим память
                                    folder=sys.intern(directory),
                                    size=st.st_size,
                                    mtime=st.st_mtime,
                                )
                            )
                            if len(out) >= limit:
                                return out
                except OSError:
                    continue
    return out


SORT_MODES = ("name", "date", "size", "random")

# Пределы «растяжения» плитки: панорама не должна стать полоской, а очень
# высокий кадр — бесконечной колонной. Ограничение применяется один раз при
# сборке кэша пропорций, чтобы внутренний цикл раскладки не знал о ветвлениях.
ASPECT_MIN = 0.34
ASPECT_MAX = 3.2


def _clamped_aspect(item: "Item") -> float:
    aspect = item.aspect
    if aspect < ASPECT_MIN:
        return ASPECT_MIN
    if aspect > ASPECT_MAX:
        return ASPECT_MAX
    return aspect


class GalleryModel:
    """Полный набор элементов + отображаемый срез (фильтр/сортировка)."""

    __slots__ = (
        "all_items", "items", "aspects", "_index_map", "_sort", "_reverse",
        "_filter", "_load_token", "_pool",
    )

    def __init__(self) -> None:
        self.all_items: list[Item] = []
        self.items: list[Item] = []
        # параллельный списку items кэш пропорций: раскладка читает только его
        self.aspects: list[float] = []
        self._index_map: dict[str, int] = {}
        self._sort = "name"
        self._reverse = False
        self._filter = ""
        self._load_token = 0
        self._pool: ThreadPoolExecutor | None = None

    # --- доступ -----------------------------------------------------------
    def __len__(self) -> int:
        return len(self.items)

    def __getitem__(self, index: int) -> Item:
        return self.items[index]

    def index_of(self, path: str) -> int:
        """O(1): индекс элемента по пути (для 50 000 файлов это важно)."""
        return self._index_map.get(path, -1)

    # --- загрузка ---------------------------------------------------------
    def cancel_load(self) -> None:
        self._load_token += 1
        if self._pool is not None:
            self._pool.shutdown(wait=False, cancel_futures=True)
            self._pool = None

    def load(
        self,
        paths: list[str],
        on_batch=None,
        on_done=None,
        on_error=None,
    ) -> None:
        """Индексирует каталоги в фоне, отдавая результаты порциями.

        on_batch(items, total_so_far) — вызывается в главном потоке;
        on_done(count) — по завершении.
        """
        self.cancel_load()
        token = self._load_token
        roots = list(paths)

        def worker() -> None:
            found: list[Item] = []
            for root in roots:
                if token != self._load_token:
                    return
                if os.path.isdir(root):
                    found.extend(scan_directory(root))
                elif os.path.isfile(root):
                    st = os.stat(root)
                    found.append(
                        Item(
                            path=root,
                            name=os.path.basename(root),
                            folder=os.path.dirname(root),
                            size=st.st_size,
                            mtime=st.st_mtime,
                        )
                    )
            if not found:
                GLib.idle_add(_safe, on_done, 0)
                return

            # Заголовки читаются в один поток: это ~15 мкс на файл, а GIL всё
            # равно не даёт распараллелить парсинг (потоки только раздувают RSS
            # и замедляют работу). Зато каждая порция отдаётся главному потоку
            # через рукопожатие: индексатор ждёт, пока UI её отрисует, поэтому
            # очередь не растёт и окно остаётся отзывчивым.
            batch: list[Item] = []
            done = 0
            gate = threading.Event()

            for item in found:
                if token != self._load_token:
                    return
                _probe_dimensions(item)
                batch.append(item)
                done += 1
                if len(batch) >= 300:
                    gate.clear()
                    GLib.idle_add(_deliver_batch, on_batch, list(batch), done, gate)
                    gate.wait(timeout=5.0)
                    batch.clear()

            if batch:
                gate.clear()
                GLib.idle_add(_deliver_batch, on_batch, list(batch), done, gate)
                gate.wait(timeout=5.0)
            GLib.idle_add(_safe, on_done, done)

        self._pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="indexer")
        self._pool.submit(worker)

    def push_batch(self, batch: list[Item], sort_now: bool = False) -> None:
        """Добавляет порцию найденных элементов в отображаемый срез."""
        self.all_items.extend(batch)
        if sort_now:
            self.apply()
            return
        base = len(self.items)
        self.items.extend(batch)
        self.aspects.extend(_clamped_aspect(item) for item in batch)
        index_map = self._index_map
        for offset, item in enumerate(batch):
            index_map[item.path] = base + offset

    def finish_load(self) -> None:
        self.apply()

    def clear(self) -> None:
        self.all_items.clear()
        self.items.clear()
        self.aspects.clear()
        self._index_map.clear()

    # --- фильтр и сортировка ---------------------------------------------
    def set_filter(self, text: str) -> None:
        self._filter = text.strip().lower()
        self.apply()

    def get_filter(self) -> str:
        return self._filter

    def set_sort(self, mode: str, reverse: bool = False) -> None:
        if mode in SORT_MODES:
            self._sort = mode
        self._reverse = reverse
        self.apply()

    def get_sort(self) -> tuple[str, bool]:
        return self._sort, self._reverse

    def apply(self) -> None:
        """Пересобирает отображаемый срез: фильтр → сортировка."""
        items = self.all_items
        needle = self._filter
        if needle:
            items = [it for it in items if needle in it.name.lower()]
        mode, reverse = self._sort, self._reverse
        if mode == "date":
            items = sorted(items, key=lambda it: it.mtime, reverse=not reverse)
        elif mode == "size":
            items = sorted(items, key=lambda it: it.size, reverse=not reverse)
        elif mode == "random":
            items = list(items)
            random.Random(0xC0FFEE).shuffle(items)
        else:
            items = sorted(items, key=lambda it: it.name.lower(), reverse=reverse)
        self.items = items
        self.aspects = [_clamped_aspect(item) for item in items]
        self._index_map = {item.path: index for index, item in enumerate(items)}


def _safe(callback, *args):
    if callback is not None:
        callback(*args)
    return False


def _deliver_batch(callback, batch, done, gate):
    try:
        if callback is not None:
            callback(batch, done)
    finally:
        gate.set()
    return False
