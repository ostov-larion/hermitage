"""Палитра для ручной отрисовки плиток.

Плитки рисуются не через CSS, а напрямую в GtkSnapshot — так на кадр тратится
десяток узлов render node вместо полноценного CSS-каскада на каждый элемент.
Цвета берём у Adw.StyleManager, чтобы поддерживать тёмную тему и системный
акцент без перезапуска.
"""
from __future__ import annotations

from dataclasses import dataclass

import gi

gi.require_version("Gdk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gdk  # noqa: E402


@dataclass(slots=True)
class Palette:
    dark: bool
    placeholder: Gdk.RGBA        # фон ещё не загруженной плитки
    placeholder_hover: Gdk.RGBA
    hairline: Gdk.RGBA           # тонкая обводка при наведении
    accent: Gdk.RGBA             # системный акцент — рамка выбора
    scrim: Gdk.RGBA              # затемнение под подписью
    text: Gdk.RGBA               # цвет подписи
    text_dim: Gdk.RGBA


def _rgba(r: float, g: float, b: float, a: float) -> Gdk.RGBA:
    color = Gdk.RGBA()
    color.red, color.green, color.blue, color.alpha = r, g, b, a
    return color


def _accent_color() -> Gdk.RGBA:
    manager = Adw.StyleManager.get_default()
    getter = getattr(manager, "get_accent_color_rgba", None)
    if getter is not None:
        try:
            return getter()
        except Exception:  # noqa: BLE001
            pass
    return _rgba(0.20, 0.52, 0.96, 1.0)


def build_palette() -> Palette:
    dark = Adw.StyleManager.get_default().get_dark()
    if dark:
        placeholder = _rgba(1, 1, 1, 0.07)
        placeholder_hover = _rgba(1, 1, 1, 0.13)
        hairline = _rgba(1, 1, 1, 0.22)
    else:
        placeholder = _rgba(0, 0, 0, 0.06)
        placeholder_hover = _rgba(0, 0, 0, 0.10)
        hairline = _rgba(0, 0, 0, 0.20)
    return Palette(
        dark=dark,
        placeholder=placeholder,
        placeholder_hover=placeholder_hover,
        hairline=hairline,
        accent=_accent_color(),
        scrim=_rgba(0, 0, 0, 0.62),
        text=_rgba(1, 1, 1, 1.0),
        text_dim=_rgba(1, 1, 1, 0.72),
    )
