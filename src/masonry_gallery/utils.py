"""Мелкие утилиты форматирования."""
from __future__ import annotations

import time


def human_size(num: int) -> str:
    value = float(num)
    for unit in ("Б", "КБ", "МБ", "ГБ"):
        if value < 1024 or unit == "ГБ":
            if unit == "Б":
                return f"{value:.0f} Б"
            return f"{value:.1f} {unit}"
        value /= 1024
    return f"{num} Б"


def human_date(timestamp: float) -> str:
    try:
        return time.strftime("%d.%m.%Y", time.localtime(timestamp))
    except (OSError, ValueError):
        return "—"


def plural(count: int, one: str, few: str, many: str) -> str:
    """Русские формы множественного числа: 1 изображение / 2 изображения / 5 изображений."""
    count = abs(count)
    if count % 10 == 1 and count % 100 != 11:
        return one
    if 2 <= count % 10 <= 4 and not 12 <= count % 100 <= 14:
        return few
    return many


def plural_images(count: int) -> str:
    return f"{count} {plural(count, 'изображение', 'изображения', 'изображений')}"
