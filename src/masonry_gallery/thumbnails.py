"""Асинхронная загрузка миниатюр: пул потоков + двухуровневый кэш.

Три вещи, которые дают основную производительность:

1. **Декодирование сразу в нужном масштабе.** Мы не декодируем 24-мегапиксельный
   JPEG целиком, чтобы потом сжать его в 240 px. `GdkPixbuf.PixbufLoader.set_size()`
   заставляет JPEG/PNG-загрузчики сжаться в 2/4/8 раз *во время* декодирования —
   это в 5–20 раз меньше работы и памяти на файл.
2. **Кэш на диске** (`$XDG_CACHE_HOME/masonry-gallery/thumbs`): повторный показ
   каталога не требует декодирования вообще. Ключ = путь+mtime+размер+бакет,
   бакеты по 160…1024 px, чтобы не пересоздавать миниатюры на каждый пиксель
   изменения размера плитки.
3. **Кэш в памяти (LRU)** уже в виде `Gdk.Texture` — то есть картинка лежит в
   виде готовой для GPU текстуры, и перерисовка это только создание нескольких
   узлов render node.

Вся работа с пикселями — в пуле потоков; GTK-объекты создаются только в главном
потоке (в idle-колбэках), поэтому никаких блокировок и гонок в UI нет.
"""
from __future__ import annotations

import hashlib
import os
import threading
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor

import gi

gi.require_version("Gdk", "4.0")
gi.require_version("GdkPixbuf", "2.0")
from gi.repository import Gdk, GdkPixbuf, GLib  # noqa: E402

from .imageinfo import fast_dimensions  # noqa: E402

# Бакеты размеров миниатюр: выбирается наименьший, покрывающий запрос.
# Шаг ~10 %, чтобы готовую текстуру не приходилось заметно сжимать при
# отрисовке: раньше между 256 и 384 был провал и плитка шириной 290 px
# показывала текстуру 384 px, сжатую одним линейным шагом, — это и читалось
# как «мыло». Цена — на несколько бакетов больше в дисковом кэше.
BUCKETS = (
    160, 176, 192, 208, 232, 256, 280, 304, 336, 368, 400, 440,
    480, 528, 576, 640, 704, 768, 848, 928, 1024,
)
# Декодировать ли крупнее цели перед финальным сжатием. Замеры (RMSE к эталону
# «полный декод + Lanczos» и визуальное сравнение) показали, что для связки
# «libjpeg-скейл → GSK» лишний промежуточный шаг только размывает: выигрыш даёт
# не суперсэмплинг, а плотная сетка бакетов выше. Оставляем 1 — дешевле и резче.
SUPERSAMPLE = 1
_CHUNK = 1 << 16


def bucket_for(px: int) -> int:
    for b in BUCKETS:
        if px <= b:
            return b
    return BUCKETS[-1]


def _cache_root() -> str:
    base = GLib.get_user_cache_dir()
    # версия в пути: при смене конвейера декодирования старые файлы не мешают
    return os.path.join(base, "masonry-gallery", "thumbs-v2")


def _target_size(
    path: str, max_px: int, dims: tuple[int, int] | None = None
) -> tuple[int, int] | None:
    """Целевой размер декодирования с сохранением пропорций.

    Важно: `GdkPixbuf.PixbufLoader.set_size()` — это не «максимум», а точный
    размер результата, он *растягивает* картинку в заданный прямоугольник.
    Поэтому пропорции считаем сами по заголовку файла (до EXIF-поворота:
    поворот меняет местами обе стороны, так что пропорции сохраняются).
    """
    src_w = src_h = 0
    # размеры уже известны из индекса: не дёргаем загрузчик лишний раз. В
    # Flatpak это экономит ~12 мс на каждой плитке (glycin запускается в
    # отдельном процессе песочницы), а на больших папках — заметные секунды.
    if dims is not None and dims[0] > 0 and dims[1] > 0:
        src_w, src_h = int(dims[0]), int(dims[1])
    if src_w <= 0 or src_h <= 0:
        try:
            info = GdkPixbuf.Pixbuf.get_file_info(path)
        except GLib.Error:
            info = None
        if info and info[0] is not None:
            src_w, src_h = info[1], info[2]
    if src_w <= 0 or src_h <= 0:
        fallback = fast_dimensions(path)
        if fallback is None:
            return None
        src_w, src_h = fallback
    longest = max(src_w, src_h)
    if longest <= max_px:  # не растягиваем маленькие картинки
        return src_w, src_h
    scale = max_px / longest
    return max(1, round(src_w * scale)), max(1, round(src_h * scale))


def _decode(
    path: str, max_px: int, dims: tuple[int, int] | None = None
) -> GdkPixbuf.Pixbuf | None:
    """Декодирует файл, сразу уменьшая его до max_px по длинной стороне.

    Декодируем вдвое крупнее (по возможности — насколько позволит сам файл) и
    затем сжимаем фильтром HYPER: так мелкие детали не теряются, а плитка не
    выглядит расфокусированной. Стоимость — единицы миллисекунд на файл, и
    только при первом показе: дальше работает дисковый кэш.
    """
    loader = GdkPixbuf.PixbufLoader()
    decode_px = max_px * SUPERSAMPLE
    target = _target_size(path, decode_px, dims)
    try:
        if target is not None:
            # подсказка загрузчику: сжаться в 1/2, 1/4 или 1/8 ещё на этапе декода
            loader.set_size(target[0], target[1])
        with open(path, "rb") as handle:
            while True:
                chunk = handle.read(_CHUNK)
                if not chunk:
                    break
                loader.write(chunk)
        loader.close()
    except (GLib.Error, OSError, ValueError):
        try:
            loader.close()
        except GLib.Error:
            pass
        return None

    pixbuf = loader.get_pixbuf()
    if pixbuf is None:
        return None

    # EXIF-ориентация (телефонные снимки иначе лежат на боку)
    try:
        rotated = pixbuf.apply_embedded_orientation()
        if rotated is not None:
            pixbuf = rotated
    except Exception:  # noqa: BLE001 — опция может отсутствовать
        pass

    width, height = pixbuf.get_width(), pixbuf.get_height()
    longest = max(width, height)
    if longest > max_px:
        scale = max_px / longest
        new_w = max(1, round(width * scale))
        new_h = max(1, round(height * scale))
        pixbuf = pixbuf.scale_simple(new_w, new_h, GdkPixbuf.InterpType.HYPER)
    return pixbuf


class ThumbRequest:
    """Ручка запроса: позволяет отменить доставку, если плитку переиспользовали."""

    __slots__ = ("key", "callback", "cancelled")

    def __init__(self, key: tuple, callback) -> None:
        self.key = key
        self.callback = callback
        self.cancelled = False

    def cancel(self) -> None:
        self.cancelled = True
        self.callback = None


class ThumbnailLoader:
    def __init__(
        self,
        cache_dir: str | None = None,
        workers: int | None = None,
        memory_budget: int | None = None,
        disk_cache: bool = True,
    ) -> None:
        self._root = cache_dir or _cache_root()
        self._disk_cache = disk_cache
        # бюджет кэша текстур: 192 МБ хватает на ~30 плиток экрана и десятки
        # экранов назад; настраивается через HERMITAGE_CACHE_MB
        if memory_budget is None:
            try:
                memory_budget = int(os.environ.get("HERMITAGE_CACHE_MB", "192")) << 20
            except ValueError:
                memory_budget = 192 << 20
        self._budget = max(32 << 20, memory_budget)
        self._pool = ThreadPoolExecutor(
            max_workers=workers or max(2, min(4, (os.cpu_count() or 2))),
            thread_name_prefix="thumb",
        )
        # key -> Gdk.Texture, порядок = LRU
        self._mem: OrderedDict[tuple, Gdk.Texture] = OrderedDict()
        self._mem_bytes = 0
        self._full: OrderedDict[tuple, Gdk.Texture] = OrderedDict()
        self._pending: dict[tuple, list[ThumbRequest]] = {}
        self._inflight: set[tuple] = set()
        self._closed = False
        # статистика (показывается в подвале окна)
        self.stats = {"hits": 0, "disk": 0, "decoded": 0, "errors": 0}

    # --- публичный API ----------------------------------------------------
    def request(self, item, px: int, callback) -> ThumbRequest:
        """Запрашивает миниатюру. callback(path, Gdk.Texture) — в главном потоке.

        Если текстура уже в памяти, callback вызывается синхронно (без задержки
        на перерисовку), иначе — по готовности из пула потоков.
        """
        px = bucket_for(int(px))
        key = ("t", item.path, int(item.mtime), item.size, px)

        texture = self._mem.get(key)
        if texture is not None:
            self._mem.move_to_end(key)
            self.stats["hits"] += 1
            callback(item.path, texture)
            request = ThumbRequest(key, None)
            return request

        request = ThumbRequest(key, callback)
        self._pending.setdefault(key, []).append(request)
        if key not in self._inflight:
            self._inflight.add(key)
            self._pool.submit(
                self._work, key, item.path, int(item.mtime), item.size, px,
                (item.width, item.height),
            )
        return request

    def load_full(self, item, callback, max_px: int = 4096) -> ThumbRequest:
        """Полноразмерное (но ограниченное max_px) изображение для просмотрщика."""
        key = ("f", item.path, int(item.mtime), item.size, max_px)
        texture = self._full.get(key)
        if texture is not None:
            self._full.move_to_end(key)
            callback(item.path, texture)
            return ThumbRequest(key, None)

        request = ThumbRequest(key, callback)
        self._pending.setdefault(key, []).append(request)
        if key not in self._inflight:
            self._inflight.add(key)
            self._pool.submit(
                self._work, key, item.path, int(item.mtime), item.size, max_px,
                (item.width, item.height),
            )
        return request

    def shutdown(self) -> None:
        self._closed = True
        self._pool.shutdown(wait=False, cancel_futures=True)
        self._pending.clear()
        self._mem.clear()
        self._full.clear()

    def cache_size_bytes(self) -> int:
        return self._mem_bytes

    # --- рабочая часть (пул потоков) --------------------------------------
    def _cache_path(self, path: str, mtime: int, size: int, bucket: int) -> str:
        digest = hashlib.sha1(
            f"{path}|{mtime}|{size}|{bucket}".encode("utf-8", "replace")
        ).hexdigest()
        return os.path.join(self._root, str(bucket), digest + ".jpg")

    def _work(
        self,
        key: tuple,
        path: str,
        mtime: int,
        size: int,
        px: int,
        dims: tuple[int, int] | None = None,
    ) -> None:
        # быстрая проверка: пока задача стояла в очереди, плитку могли
        # переиспользовать — тогда декодировать нечего
        pending = self._pending.get(key)
        if pending is not None and not any(not r.cancelled for r in pending):
            GLib.idle_add(self._deliver, key, None)
            return

        pixbuf = None
        is_thumb = key[0] == "t"

        if is_thumb and self._disk_cache:
            cache_file = self._cache_path(path, mtime, size, px)
            try:
                if os.path.exists(cache_file):
                    pixbuf = GdkPixbuf.Pixbuf.new_from_file(cache_file)
                    self.stats["disk"] += 1
            except GLib.Error:
                pixbuf = None

        if pixbuf is None:
            if not os.path.isfile(path):
                self.stats["errors"] += 1
                GLib.idle_add(self._deliver, key, None)
                return
            pixbuf = _decode(path, px, dims)
            if pixbuf is None:
                self.stats["errors"] += 1
                GLib.idle_add(self._deliver, key, None)
                return
            self.stats["decoded"] += 1
            if is_thumb and self._disk_cache:
                cache_file = self._cache_path(path, mtime, size, px)
                try:
                    os.makedirs(os.path.dirname(cache_file), exist_ok=True)
                    pixbuf.savev(cache_file, "jpeg", ["quality"], ["85"])
                except (GLib.Error, OSError):
                    pass

        GLib.idle_add(self._deliver, key, pixbuf)

    # --- главный поток ----------------------------------------------------
    def _deliver(self, key: tuple, pixbuf) -> bool:
        if self._closed:
            return False
        self._inflight.discard(key)
        requests = self._pending.pop(key, ())

        texture = None
        if pixbuf is not None:
            try:
                texture = Gdk.Texture.new_for_pixbuf(pixbuf)
            except Exception:  # noqa: BLE001
                texture = None

        if texture is not None:
            path = key[1]
            if key[0] == "t":
                self._mem[key] = texture
                self._mem_bytes += texture.get_width() * texture.get_height() * 4
                self._evict()
            else:
                self._full[key] = texture
                while len(self._full) > 6:
                    self._full.popitem(last=False)
            for request in requests:
                if not request.cancelled and request.callback is not None:
                    request.callback(path, texture)
        return False

    def _evict(self) -> None:
        while self._mem_bytes > self._budget and len(self._mem) > 1:
            key, texture = self._mem.popitem(last=False)
            self._mem_bytes -= texture.get_width() * texture.get_height() * 4


_shared: ThumbnailLoader | None = None
_shared_lock = threading.Lock()


def shared_loader() -> ThumbnailLoader:
    global _shared
    with _shared_lock:
        if _shared is None:
            _shared = ThumbnailLoader()
        return _shared
