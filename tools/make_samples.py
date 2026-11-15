#!/usr/bin/env python3
"""Генератор демонстрационного набора изображений.

Создаёт N процедурных «фотографий» с разными пропорциями (пейзаж, портрет,
панорама, квадрат), чтобы было на чём проверить masonry-раскладку и
производительность загрузки миниатюр.

    python3 tools/make_samples.py samples --count 180
"""
from __future__ import annotations

import math
import os
import random
import sys

from PIL import Image, ImageDraw, ImageFilter

# (ширина, высота) — типичные пропорции фотоаппарата/телефона
RATIOS = [
    (3000, 2000),  # 3:2 пейзаж
    (2000, 3000),  # 2:3 портрет
    (2400, 2400),  # квадрат
    (3840, 1080),  # панорама
    (1080, 1920),  # телефон
    (4000, 2250),  # 16:9
    (1600, 1200),  # 4:3
    (1200, 1600),
]


def make_photo(path: str, size: tuple[int, int], rng: random.Random) -> None:
    w, h = size
    small = (max(8, w // 24), max(8, h // 24))
    base = Image.new("RGB", small)
    px = base.load()

    # палитра: два цвета + плавный градиент с шумом
    hue_a = rng.uniform(0, 360)
    hue_b = (hue_a + rng.uniform(40, 180)) % 360
    ca = _hsv(hue_a, rng.uniform(0.35, 0.75), rng.uniform(0.45, 0.95))
    cb = _hsv(hue_b, rng.uniform(0.35, 0.8), rng.uniform(0.3, 0.9))
    angle = rng.uniform(0, math.tau)
    ca_x, sa_x = math.cos(angle), math.sin(angle)

    for y in range(small[1]):
        for x in range(small[0]):
            t = (x / small[0] * ca_x + y / small[1] * sa_x + 1) / 2
            t = min(1.0, max(0.0, t))
            n = rng.uniform(-14, 14)
            px[x, y] = tuple(
                int(max(0, min(255, ca[i] * (1 - t) + cb[i] * t + n))) for i in range(3)
            )

    img = base.resize((w, h), Image.BICUBIC)
    draw = ImageDraw.Draw(img, "RGBA")

    # геометрические акценты, чтобы плитки визуально различались
    for _ in range(rng.randint(1, 4)):
        cx, cy = rng.uniform(0, w), rng.uniform(0, h)
        r = rng.uniform(0.08, 0.35) * min(w, h)
        col = _hsv(rng.uniform(0, 360), rng.uniform(0.2, 0.9), rng.uniform(0.6, 1.0))
        alpha = rng.randint(40, 150)
        kind = rng.choice(("ellipse", "band", "poly"))
        if kind == "ellipse":
            draw.ellipse([cx - r, cy - r, cx + r, cy + r], fill=col + (alpha,))
        elif kind == "band":
            yy = rng.uniform(0, h)
            draw.rectangle([0, yy, w, yy + r * 0.4], fill=col + (alpha // 2,))
        else:
            pts = [
                (rng.uniform(0, w), rng.uniform(0, h)) for _ in range(rng.randint(3, 5))
            ]
            draw.polygon(pts, fill=col + (alpha // 2,))

    img = img.filter(ImageFilter.GaussianBlur(0.6))
    img.save(path, "JPEG", quality=88, optimize=False)


def _hsv(h: float, s: float, v: float) -> tuple[int, int, int]:
    import colorsys

    r, g, b = colorsys.hsv_to_rgb(h / 360.0, s, v)
    return int(r * 255), int(g * 255), int(b * 255)


def main() -> None:
    out = sys.argv[1] if len(sys.argv) > 1 else "samples"
    count = int(sys.argv[2]) if len(sys.argv) > 2 else 180
    os.makedirs(out, exist_ok=True)
    rng = random.Random(2026)
    for i in range(count):
        w, h = RATIOS[i % len(RATIOS)] if i % 3 else rng.choice(RATIOS)
        name = f"photo_{i:04d}.jpg"
        make_photo(os.path.join(out, name), (w, h), rng)
        if (i + 1) % 20 == 0:
            print(f"  … {i + 1}/{count}", flush=True)
    print(f"готово: {count} изображений в {out}")


if __name__ == "__main__":
    main()
