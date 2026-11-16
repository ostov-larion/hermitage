#!/usr/bin/env python3
"""Диагностика одного файла: как «Эрмитаж» его декодирует и кэширует.

Проверяет три пути, которыми картинка попадает на экран:

1. **свежий декод** — `thumbnails._decode()` в нужном масштабе;
2. **контейнер** — пишет миниатюру в `ThumbPack` и читает обратно
   (именно этот путь работает при повторном показе папки);
3. **текстура** — то, что видит GTK (`GdkTexture`), с проверкой цвета.

Для каждой ступени печатается размер, число каналов и насыщенность: если
цветная картинка где-то превращается в серую, видно, на каком шаге.

    ./tools/check-image.py ~/Фото/картинка.jpg
    ./tools/check-image.py ~/Фото            # все картинки в папке
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src"))

import gi  # noqa: E402

gi.require_version("Gdk", "4.0")
gi.require_version("GdkPixbuf", "2.0")
gi.require_version("Gtk", "4.0")
from gi.repository import Gdk, GdkPixbuf, GLib, Gtk  # noqa: E402

from masonry_gallery.imageinfo import dimensions, fast_dimensions  # noqa: E402
from masonry_gallery.thumbnails import BUCKETS, _decode  # noqa: E402
from masonry_gallery.thumbpack import ThumbPack  # noqa: E402

IMAGE_EXT = {
    ".jpg", ".jpeg", ".jpe", ".png", ".webp", ".gif", ".bmp", ".tif", ".tiff",
    ".avif", ".heic", ".heif", ".svg", ".jxl",
}


def saturation(pixbuf: GdkPixbuf.Pixbuf) -> float:
    """Средняя насыщенность (max−min по RGB): 0 — картинка серая."""
    channels = pixbuf.get_n_channels()
    width, height = pixbuf.get_width(), pixbuf.get_height()
    data = pixbuf.get_pixels()
    stride = pixbuf.get_rowstride()
    total = 0
    count = 0
    step = max(1, min(width, height) // 64)
    for y in range(0, height, step):
        row = y * stride
        for x in range(0, width, step):
            i = row + x * channels
            total += max(data[i], data[i + 1], data[i + 2]) - min(
                data[i], data[i + 1], data[i + 2]
            )
            count += 1
    return total / count if count else 0.0


def describe(label: str, width: int, height: int, channels: int, sat: float) -> None:
    verdict = "серая" if sat < 6 else "цветная"
    print(f"  {label:<22} {width:>5}×{height:<5} каналов {channels}  "
          f"насыщенность {sat:6.1f}  → {verdict}")


def check(path: str, pack_path: str | None = None) -> bool:
    print(f"\n{path}")
    size = os.path.getsize(path)
    print(f"  размер файла         {size} байт")
    with open(path, "rb") as handle:
        head = handle.read(16)
    print(f"  первые байты         {head[:8].hex(' ')}")

    fast = fast_dimensions(path)
    full = dimensions(path)
    print(f"  размеры (свой парсер) {fast}")
    print(f"  размеры (gdk-pixbuf)  {full}")
    if fast != full:
        print("  ! парсер заголовков расходится с gdk-pixbuf — вероятная причина")

    pixbuf = _decode(path, 256, fast)
    if pixbuf is None:
        print("  декод: НЕ УДАЛОСЬ (файл не поддерживается или повреждён)")
        return False
    describe("свежий декод", pixbuf.get_width(), pixbuf.get_height(),
             pixbuf.get_n_channels(), saturation(pixbuf))
    for name in ("icc-profile-data",):
        option = pixbuf.get_option(name)
        if option:
            print(f"  профиль в pixbuf     {name}: {len(option)} байт")

    ok, data = pixbuf.save_to_bufferv("jpeg", ["quality"], ["85"])
    if not ok or not data:
        print("  сохранение в контейнер не удалось")
        return False
    pack_path = pack_path or os.path.join(
        GLib.get_user_cache_dir(), "masonry-gallery", "check-image.pack"
    )
    pack = ThumbPack(pack_path)
    key = b"check-image-probe"
    pack.put(key, data)
    back = pack.get(key)
    pack.close()
    texture = Gdk.Texture.new_from_bytes(GLib.Bytes.new(back))
    loader = GdkPixbuf.PixbufLoader()
    loader.write(back)
    loader.close()
    from_cache = loader.get_pixbuf()
    describe("из контейнера", from_cache.get_width(), from_cache.get_height(),
             from_cache.get_n_channels(), saturation(from_cache))
    print(f"  текстура GTK         {texture.get_width()}×{texture.get_height()}")

    # масштабы, которые использует приложение
    grey_buckets = []
    for bucket in BUCKETS:
        scaled = _decode(path, bucket, fast)
        if scaled is not None and saturation(scaled) < 6:
            grey_buckets.append(bucket)
    print(f"  серые при масштабах  {grey_buckets or 'нет'}")
    return not grey_buckets


def main(argv: list[str]) -> int:
    if len(argv) < 2:
        print(__doc__)
        return 2
    targets: list[str] = []
    for argument in argv[1:]:
        if os.path.isdir(argument):
            for name in sorted(os.listdir(argument)):
                if os.path.splitext(name)[1].lower() in IMAGE_EXT:
                    targets.append(os.path.join(argument, name))
        else:
            targets.append(argument)
    if not targets:
        print("нечего проверять")
        return 2
    print("проверка декодирования «Эрмитажа»")
    print(f"GTK {Gtk.get_major_version()}.{Gtk.get_minor_version()}.{Gtk.get_micro_version()}")
    problems = 0
    for path in targets:
        try:
            if not check(path):
                problems += 1
        except Exception as error:  # noqa: BLE001
            problems += 1
            print(f"  ошибка проверки: {error}")
    print(f"\nфайлов: {len(targets)}, с подозрением на потерю цвета: {problems}")
    return 0 if problems == 0 else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
