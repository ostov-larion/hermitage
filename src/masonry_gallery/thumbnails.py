"""Асинхронная загрузка миниатюр: пул потоков + двухуровневый кэш.

Три вещи, которые дают основную производительность:

1. **Декодирование сразу в нужном масштабе.** Мы не декодируем 24-мегапиксельный
   JPEG целиком, чтобы потом сжать его в 240 px. `GdkPixbuf.PixbufLoader.set_size()`
   заставляет JPEG/PNG-загрузчики сжаться в 2/4/8 раз *во время* декодирования —
   это в 5–20 раз меньше работы и памяти на файл.
2. **Кэш на диске** — один бинарный контейнер
   `$XDG_CACHE_HOME/masonry-gallery/thumbs-v4.pack` (см. `thumbpack.py`):
   повторный показ каталога не требует декодирования вообще, а чтение — это
   срез `mmap` без единого системного вызова на миниатюру. Ключ =
   путь+mtime+размер+бакет, бакеты по 160…1024 px, чтобы не пересоздавать
   миниатюры на каждый пиксель изменения размера плитки.
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
from .thumbpack import ThumbPack  # noqa: E402

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
    # версия в имени: при смене конвейера декодирования старый файл не мешает
    return os.path.join(base, "masonry-gallery")


def _pack_path() -> str:
    return os.path.join(_cache_root(), "thumbs-v4.pack")


def _drop_legacy_cache() -> None:
    """Убирает кэши прошлых версий: каталог v2 и контейнер v3.

    v3 пришлось похоронить: в рантайме GNOME 51 gdk-pixbuf писал миниатюры
    PNG с альфа-каналом однокомпонентным JPEG — то есть серыми, и такие
    записи нельзя переиспользовать.
    """
    try:
        os.unlink(os.path.join(_cache_root(), "thumbs-v3.pack"))
    except OSError:
        pass
    legacy = os.path.join(_cache_root(), "thumbs-v2")
    if not os.path.isdir(legacy):
        return
    try:
        for bucket in os.listdir(legacy):
            folder = os.path.join(legacy, bucket)
            if os.path.isdir(folder):
                for name in os.listdir(folder):
                    try:
                        os.unlink(os.path.join(folder, name))
                    except OSError:
                        pass
                try:
                    os.rmdir(folder)
                except OSError:
                    pass
        os.rmdir(legacy)
    except OSError:
        pass


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


def _jpeg_components(data: bytes) -> int:
    """Число компонент в SOF-маркере JPEG: 1 — серый, 3 — цвет."""
    index = 2
    limit = len(data) - 9
    while index < limit:
        if data[index] != 0xFF:
            index += 1
            continue
        marker = data[index + 1]
        if marker in (0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB):
            return data[index + 9]
        try:
            index += 2 + int.from_bytes(data[index + 2:index + 4], "big")
        except ValueError:
            break
    return 0


def _encode_thumb(pixbuf: GdkPixbuf.Pixbuf) -> bytes | None:
    """Кодирует миниатюру для дискового контейнера.

    JPEG не умеет прозрачность, и в рантайме GNOME 51 (gdk-pixbuf с
    glycin-загрузчиками) сохранение RGBA-пиксбафа в JPEG даёт **одно-
    компонентный, то есть серый** файл: PNG с альфа-каналом после перезапуска
    показывались чёрно-белыми. Поэтому картинки с альфой кладём в контейнер
    как PNG (прозрачность сохраняется, читатель определяет формат сам), а для
    остальных страхуемся проверкой числа компонент.
    """
    try:
        if pixbuf.get_has_alpha() or pixbuf.get_n_channels() == 4:
            ok, data = pixbuf.save_to_bufferv("png", [], [])
            return bytes(data) if ok and data else None
        ok, data = pixbuf.save_to_bufferv("jpeg", ["quality"], ["85"])
    except (GLib.Error, TypeError):
        return None
    if not ok or not data:
        return None
    if pixbuf.get_n_channels() >= 3 and _jpeg_components(data) == 1:
        return None  # кодировщик потерял цвет — в кэш такое не кладём
    return bytes(data)


def _texture_from_bytes(data: bytes) -> Gdk.Texture | None:
    """Миниатюра из контейнера → текстура, минуя промежуточный pixbuf."""
    try:
        return Gdk.Texture.new_from_bytes(GLib.Bytes.new(data))
    except (GLib.Error, TypeError):
        return None


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
        self._pack_path = (
            os.path.join(cache_dir, "thumbs-v3.pack") if cache_dir else _pack_path()
        )
        self._disk_cache = disk_cache
        # контейнер открывается при первом обращении: не задерживаем старт
        # сканированием индекса, если миниатюры в этой сессии не понадобятся
        self._pack: ThumbPack | None = None
        self._pack_lock = threading.Lock()
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
        pack, self._pack = self._pack, None
        if pack is not None:
            try:
                pack.close()
            except OSError:
                pass
        self._pending.clear()
        self._mem.clear()
        self._full.clear()

    def cache_size_bytes(self) -> int:
        return self._mem_bytes

    # --- рабочая часть (пул потоков) --------------------------------------
    @staticmethod
    def _digest(path: str, mtime: int, size: int, bucket: int) -> bytes:
        return hashlib.sha1(
            f"{path}|{mtime}|{size}|{bucket}".encode("utf-8", "replace")
        ).digest()

    def _ensure_pack(self) -> ThumbPack | None:
        if not self._disk_cache:
            return None
        pack = self._pack
        if pack is not None:
            return pack
        with self._pack_lock:
            if self._pack is None:
                try:
                    self._pack = ThumbPack(self._pack_path)
                except OSError:
                    self._disk_cache = False
                    return None
                # чистим старое «файл на миниатюру» в фоне: на 100 000 файлов
                # это заметное время, а ждать его при старте незачем
                threading.Thread(
                    target=_drop_legacy_cache, name="cache-cleanup", daemon=True
                ).start()
            return self._pack

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
        texture = None
        is_thumb = key[0] == "t"

        if is_thumb and self._disk_cache:
            pack = self._ensure_pack()
            if pack is not None:
                cached = pack.get(self._digest(path, mtime, size, px))
                if cached:
                    # сразу текстура: Gdk.Texture.new_from_bytes в 1,5 раза
                    # быстрее связки «pixbuf.new_from_file + new_for_pixbuf»
                    texture = _texture_from_bytes(cached)
                    if texture is not None:
                        self.stats["disk"] += 1

        if texture is not None:
            GLib.idle_add(self._deliver, key, None, texture)
            return

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
                pack = self._ensure_pack()
                if pack is not None:
                    data = _encode_thumb(pixbuf)
                    if data:
                        try:
                            pack.put(self._digest(path, mtime, size, px), data)
                        except OSError:
                            pass

        GLib.idle_add(self._deliver, key, pixbuf)

    # --- главный поток ----------------------------------------------------
    def _deliver(self, key: tuple, pixbuf, texture=None) -> bool:
        if self._closed:
            return False
        self._inflight.discard(key)
        requests = self._pending.pop(key, ())

        if texture is None and pixbuf is not None:
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
