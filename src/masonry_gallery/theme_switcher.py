"""Переключатель оформления — три кружка «система / светлая / тёмная».

Это тот же контрол, что в главном меню GNOME Builder: `Adw.ToggleGroup` из трёх
`Adw.Toggle`, в каждом — нарисованный кружок-превью, у выбранного акцентное
кольцо и галочка в углу. `Adw.ToggleGroup` — финальный тип, наследовать его
нельзя, поэтому класс ниже его *содержит* и отдаёт через `.widget`.

Кружки рисуются средствами CSS (`border-radius` + `linear-gradient`), а не
картинками: так они остаются чёткими на любом масштабе и автоматически
подхватывают цвета темы.

Семантика цветовых схем — из рекомендаций libadwaita для приложений с
переключателем из трёх состояний: «система» → `PREFER_LIGHT`,
«светлая» → `FORCE_LIGHT`, «тёмная» → `FORCE_DARK`. Если система не умеет
сообщать предпочтение (X11 без портала), кружок «система» отключается — тоже
по рекомендации.
"""
from __future__ import annotations

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gtk  # noqa: E402

SCHEMES = (
    ("system", "Система", "Как в системе", Adw.ColorScheme.PREFER_LIGHT),
    ("light", "Светлая", "Светлое оформление", Adw.ColorScheme.FORCE_LIGHT),
    ("dark", "Тёмная", "Тёмное оформление", Adw.ColorScheme.FORCE_DARK),
)

_NAMES = {item[0] for item in SCHEMES}


def _swatch(name: str) -> Gtk.Box:
    """Кружок-превью оформления (внешний вид задаётся в style.css)."""
    box = Gtk.Box()
    box.add_css_class("appearance-swatch")
    box.add_css_class(f"appearance-{name}")
    box.set_size_request(28, 28)
    return box


class ThemeSwitcher:
    """`Adw.ToggleGroup` с тремя состояниями оформления (см. `.widget`)."""

    def __init__(self, on_change=None) -> None:
        self._on_change = on_change
        self._applying = False
        self._badges: dict[str, Gtk.Widget] = {}

        group = Adw.ToggleGroup()
        group.set_homogeneous(False)
        group.set_halign(Gtk.Align.CENTER)
        group.set_margin_top(8)
        group.set_margin_bottom(8)
        group.add_css_class("appearance-switcher")
        self.widget = group

        for name, label, tooltip, _scheme in SCHEMES:
            toggle = Adw.Toggle(name=name, tooltip=tooltip)
            # слот чуть больше кружка: галочка должна лечь на его край и не
            # обрезаться границами overlay (дети overlay клипуются)
            slot = Gtk.Box()
            slot.add_css_class("appearance-slot")
            slot.append(_swatch(name))
            overlay = Gtk.Overlay()
            overlay.set_child(slot)
            badge = Gtk.Label(label="\u2713")
            badge.add_css_class("appearance-check")
            badge.set_halign(Gtk.Align.END)
            badge.set_valign(Gtk.Align.END)
            badge.set_visible(False)
            overlay.add_overlay(badge)
            self._badges[name] = badge
            toggle.set_child(overlay)
            try:  # доступное имя: метода нет во всех версиях PyGObject
                toggle.update_property([Gtk.AccessibleProperty.LABEL], [label])
            except Exception:  # noqa: BLE001
                pass
            group.add(toggle)

        # без портала система не сообщает предпочтение: кружок остаётся, но
        # честно предупреждаем, что он ведёт себя как «светлая»
        manager = Adw.StyleManager.get_default()
        if not manager.get_system_supports_color_schemes():
            first = group.get_toggle_by_name("system")
            if first is not None:
                first.set_tooltip("Система не сообщает оформление — будет светлое")

        group.connect("notify::active-name", self._on_active_changed)

    # ------------------------------------------------------------------ API
    def set_scheme(self, name: str) -> None:
        """Показывает состояние, не вызывая обработчик."""
        if name not in _NAMES:
            name = "system"
        self._applying = True
        self.widget.set_active_name(name)
        self._applying = False
        self._sync_badges()

    def _sync_badges(self) -> None:
        active = self.widget.get_active_name()
        for key, badge in self._badges.items():
            badge.set_visible(key == active)

    def _on_active_changed(self, *_args) -> None:
        self._sync_badges()
        if self._applying or self._on_change is None:
            return
        name = self.widget.get_active_name() or "system"
        for key, _label, _tooltip, scheme in SCHEMES:
            if key == name:
                self._on_change(key, scheme)
                break
