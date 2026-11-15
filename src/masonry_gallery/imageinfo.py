"""Быстрое чтение размеров изображения прямо из заголовка файла.

Зачем: для masonry-раскладки нужны пропорции **всех** файлов каталога, до того
как что-либо будет декодировано. `GdkPixbuf.Pixbuf.get_file_info()` создаёт
загрузчик, поднимает модули gdk-pixbuf и разбирает заголовок — это ~115 мкс на
файл, то есть ~6 секунд на 50 000 фотографий.

Здесь заголовки разбираются вручную по первым килобайтам: ~10–20 мкс на файл,
50 000 фотографий ≈ 0.6–1 с. Для форматов, которых нет в быстром пути
(TIFF, AVIF/HEIC, JXL, SVG, ICO), используется обычный gdk-pixbuf.

Дополнительно разбирается EXIF Orientation: у снимков с телефона кадр
повёрнут на 90°, и «настоящие» пропорции — обратные записанным в заголовке.
"""
from __future__ import annotations

import struct

import gi

gi.require_version("GdkPixbuf", "2.0")
from gi.repository import GdkPixbuf, GLib  # noqa: E402

HEADER_READ = 64 * 1024  # больше JPEG-заголовку не нужно

# маркеры, у которых нет поля длины
_SKIP_MARKERS = {0xD8, 0x01, 0xD0, 0xD1, 0xD2, 0xD3, 0xD4, 0xD5, 0xD6, 0xD7}


def _jpeg(data: bytes) -> tuple[int, int] | None:
    size = len(data)
    position = 2
    orientation = 1
    while position + 4 <= size:
        if data[position] != 0xFF:
            position += 1
            continue
        marker = data[position + 1]
        if marker in _SKIP_MARKERS:
            position += 2
            continue
        if marker == 0xDA:  # начало сжатых данных, SOF не нашли
            return None
        length = (data[position + 2] << 8) | data[position + 3]
        if length < 2:
            return None
        if marker == 0xE1 and data[position + 4:position + 10] == b"Exif\x00\x00":
            value = _exif_orientation(data, position + 10, length - 2)
            if value:
                orientation = value
        elif 0xC0 <= marker <= 0xCF and marker not in (0xC4, 0xC8, 0xCC):
            height = (data[position + 5] << 8) | data[position + 6]
            width = (data[position + 7] << 8) | data[position + 8]
            if width <= 0 or height <= 0:
                return None
            if orientation >= 5:  # 90°/270° — кадр показывается повёрнутым
                width, height = height, width
            return width, height
        position += 2 + length
    return None


def _exif_orientation(data: bytes, offset: int, length: int) -> int | None:
    end = min(len(data), offset + length)
    if offset + 8 > end:
        return None
    if data[offset:offset + 2] == b"II":
        fmt = "<"
    elif data[offset:offset + 2] == b"MM":
        fmt = ">"
    else:
        return None
    if struct.unpack_from(fmt + "H", data, offset + 2)[0] != 42:
        return None
    ifd = offset + struct.unpack_from(fmt + "I", data, offset + 4)[0]
    if ifd + 2 > end:
        return None
    entries = struct.unpack_from(fmt + "H", data, ifd)[0]
    for index in range(entries):
        entry = ifd + 2 + index * 12
        if entry + 12 > end:
            return None
        if struct.unpack_from(fmt + "H", data, entry)[0] == 0x0112:
            value = struct.unpack_from(fmt + "H", data, entry + 8)[0]
            return value if 1 <= value <= 8 else None
    return None


def _png(data: bytes) -> tuple[int, int] | None:
    if data[12:16] != b"IHDR":
        return None
    width, height = struct.unpack_from(">II", data, 16)
    return (width, height) if width and height else None


def _gif(data: bytes) -> tuple[int, int] | None:
    width, height = struct.unpack_from("<HH", data, 6)
    return (width, height) if width and height else None


def _bmp(data: bytes) -> tuple[int, int] | None:
    width, height = struct.unpack_from("<ii", data, 18)
    width, height = abs(width), abs(height)
    return (width, height) if width and height else None


def _webp(data: bytes) -> tuple[int, int] | None:
    chunk = data[12:16]
    if chunk == b"VP8 ":
        width, height = struct.unpack_from("<HH", data, 26)
        return width & 0x3FFF, height & 0x3FFF
    if chunk == b"VP8L":
        bits = int.from_bytes(data[21:25], "little")
        return (bits & 0x3FFF) + 1, ((bits >> 14) & 0x3FFF) + 1
    if chunk == b"VP8X":
        width = int.from_bytes(data[24:27], "little") + 1
        height = int.from_bytes(data[27:30], "little") + 1
        return width, height
    return None


_FAST = (
    (b"\xff\xd8", _jpeg),
    (b"\x89PNG", _png),
    (b"GIF8", _gif),
    (b"RIFF", _webp),
    (b"BM", _bmp),
)


def fast_dimensions(path: str) -> tuple[int, int] | None:
    """Возвращает (ширина, высота) из заголовка; None — формат не поддержан."""
    try:
        with open(path, "rb") as handle:
            data = handle.read(HEADER_READ)
    except OSError:
        return None
    if len(data) < 32:
        return None
    for signature, parser in _FAST:
        if data.startswith(signature):
            try:
                dims = parser(data)
            except (struct.error, IndexError, ValueError):
                return None
            if dims and dims[0] > 0 and dims[1] > 0:
                return dims
            return None
    return None


def gdk_dimensions(path: str) -> tuple[int, int] | None:
    """Медленный, но универсальный путь через gdk-pixbuf."""
    try:
        info = GdkPixbuf.Pixbuf.get_file_info(path)
    except GLib.Error:
        return None
    if not info or info[0] is None:
        return None
    width, height = info[1], info[2]
    return (width, height) if width > 0 and height > 0 else None


def dimensions(path: str) -> tuple[int, int] | None:
    return fast_dimensions(path) or gdk_dimensions(path)
