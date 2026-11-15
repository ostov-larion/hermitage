"""Плитка галереи.

`MgTile` — это Gtk.Widget без детей и без CSS-оформления: вся отрисовка
происходит в `do_snapshot()` через Gsk-узлы (скруглённый клип + одна текстура).
Это самый дешёвый вариант плитки в GTK4: на кадр — 2–4 узла render node,
никаких вложенных виджетов, layout-менеджеров и style-контекстов.

Плитки переиспользуются контейнером (пул), поэтому bind/unbind должны быть
дешёвыми и полностью отменять незавершённые запросы миниатюр.
"""
from __future__ import annotations

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Gdk", "4.0")
gi.require_version("Gsk", "4.0")
gi.require_version("Pango", "1.0")
from gi.repository import Gdk, GLib, Graphene, Gsk, Gtk, Pango  # noqa: E402

from .utils import human_size  # noqa: E402

RADIUS = 10.0


def _shorten(text: str, limit: int) -> str:
    """Обрезает имя файла по центру, сохраняя расширение."""
    if len(text) <= limit:
        return text
    head = (limit - 3) // 2
    tail = limit - 3 - head
    return f"{text[:head]}…{text[-tail:]}"



class Tile(Gtk.Widget):
    """Одна плитка: миниатюра, подпись по наведению, рамка выбора."""

    __gtype_name__ = "MgTile"

    def __init__(self, view) -> None:
        super().__init__()
        self._view = view
        self._item = None
        self._texture = None
        self._request = None
        self._hover = False
        self._selected = False
        self._failed = False
        self._layout: Pango.Layout | None = None
        self._layout_width = -1.0

        self.set_overflow(Gtk.Overflow.HIDDEN)
        self.set_cursor(Gdk.Cursor.new_from_name("pointer", None))

        motion = Gtk.EventControllerMotion()
        motion.connect("enter", self._on_enter)
        motion.connect("leave", self._on_leave)
        self.add_controller(motion)

        click = Gtk.GestureClick()
        click.set_button(0)
        click.connect("pressed", self._on_pressed)
        self.add_controller(click)

    # --- привязка к элементу модели --------------------------------------
    @property
    def item(self):
        return self._item

    def bind(self, item) -> None:
        if self._item is not None and self._item.path == item.path:
            return
        self._cancel_request()
        self._item = item
        self._texture = None
        self._failed = False
        self._layout = None
        self._layout_width = -1.0
        self._hover = False
        self._request = self._view.loader.request(
            item, self._view.thumb_px(), self._on_thumb
        )
        self.queue_draw()

    def refresh_thumbnail(self) -> None:
        """Перезапрашивает миниатюру под изменившуюся ширину плитки.

        Старая текстура остаётся на месте до прихода новой, поэтому при
        ресайзе окна плитка не мигает пустотой.
        """
        if self._item is None:
            return
        self._cancel_request()
        self._failed = False
        self._request = self._view.loader.request(
            self._item, self._view.thumb_px(), self._on_thumb
        )

    def unbind(self) -> None:
        self._cancel_request()
        self._item = None
        self._texture = None
        self._hover = False
        self._selected = False
        self._layout = None
        self.queue_draw()

    def _cancel_request(self) -> None:
        if self._request is not None:
            self._request.cancel()
            self._request = None

    def _on_thumb(self, path: str, texture) -> None:
        self._request = None
        if self._item is None or self._item.path != path:
            return  # плитку уже переиспользовали — результат не наш
        if texture is None:
            self._failed = True
        else:
            self._texture = texture
        self.queue_draw()

    def set_selected(self, selected: bool) -> None:
        if self._selected != selected:
            self._selected = selected
            self.queue_draw()

    # --- события ----------------------------------------------------------
    def _on_enter(self, *_args) -> None:
        self._hover = True
        self.queue_draw()
        self._view.on_tile_hover(self._item)

    def _on_leave(self, *_args) -> None:
        self._hover = False
        self.queue_draw()

    def _on_pressed(self, gesture, n_press: int, x: float, y: float) -> None:
        item = self._item
        if item is None:
            return
        button = gesture.get_current_button()
        state = gesture.get_current_event_state()
        if button == 3:
            self._view.context_menu(item, self)
        elif button == 1:
            if n_press >= 2:
                self._view.activate(item)
            else:
                self._view.click(item, ctrl=bool(state & Gdk.ModifierType.CONTROL_MASK),
                                 shift=bool(state & Gdk.ModifierType.SHIFT_MASK))

    # --- отрисовка --------------------------------------------------------
    def do_snapshot(self, snapshot: Gtk.Snapshot) -> None:  # noqa: N802
        width = self.get_width()
        height = self.get_height()
        if width <= 0 or height <= 0:
            return

        palette = self._view.palette
        bounds = Graphene.Rect().init(0, 0, width, height)
        rounded = Gsk.RoundedRect()
        rounded.init_from_rect(bounds, min(RADIUS, min(width, height) * 0.5))

        snapshot.push_rounded_clip(rounded)

        texture = self._texture
        if texture is not None:
            tw = texture.get_width()
            th = texture.get_height()
            if tw > 0 and th > 0:
                # object-fit: cover — заполняем плитку с обрезкой, без искажений
                scale = max(width / tw, height / th)
                draw_w = tw * scale
                draw_h = th * scale
                dest = Graphene.Rect().init(
                    (width - draw_w) * 0.5, (height - draw_h) * 0.5, draw_w, draw_h
                )
                snapshot.append_texture(texture, dest)
        else:
            fill = palette.placeholder_hover if self._hover else palette.placeholder
            snapshot.append_color(fill, bounds)

        if self._hover and self._item is not None:
            self._snapshot_caption(snapshot, width, height, palette)

        snapshot.pop()  # конец скруглённого клипа

        if self._selected or self._hover:
            color = palette.accent if self._selected else palette.hairline
            size = 2.5 if self._selected else 1.0
            snapshot.append_border(rounded, [size] * 4, [color] * 4)

    def _snapshot_caption(self, snapshot, width: float, height: float, palette) -> None:
        layout = self._ensure_layout(width)
        if layout is None:
            return
        text_w, text_h = layout.get_pixel_size()
        scrim_h = min(height, text_h + 18.0)
        snapshot.append_color(
            palette.scrim, Graphene.Rect().init(0, height - scrim_h, width, scrim_h)
        )
        snapshot.save()
        snapshot.translate(Graphene.Point().init(10.0, height - scrim_h + 9.0))
        snapshot.append_layout(layout, palette.text)
        snapshot.restore()

    def _ensure_layout(self, width: float) -> Pango.Layout | None:
        if self._item is None:
            return None
        if self._layout is not None and abs(self._layout_width - width) < 1.0:
            return self._layout
        item = self._item
        layout = Pango.Layout(self.get_pango_context())
        name = GLib.markup_escape_text(_shorten(item.name, 34))
        meta = f"{item.width}×{item.height} · {human_size(item.size)}"
        layout.set_markup(
            f"<b>{name}</b>\n<span size='smaller' alpha='72%'>{meta}</span>", -1
        )
        layout.set_width(int(max(16.0, width - 20.0)) * Pango.SCALE)
        layout.set_wrap(Pango.WrapMode.WORD_CHAR)
        layout.set_single_paragraph_mode(False)
        self._layout = layout
        self._layout_width = width
        return layout

    # --- измерение --------------------------------------------------------
    def do_measure(self, orientation, for_size):  # noqa: N802
        # размер задаётся контейнером сверху, собственных предпочтений нет
        return 0, 0, -1, -1

    def do_size_allocate(self, width, height, baseline):  # noqa: N802
        # дети отсутствуют, но сбрасываем кэш подписи при смене ширины
        if abs(width - self._layout_width) > 1.0:
            self._layout = None
