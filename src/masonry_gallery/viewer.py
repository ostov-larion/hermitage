"""Просмотрщик полноразмерного изображения.

`ImageStage` — маленький виджет-«сцена», который сам масштабирует и
панорамирует картинку через `Gsk.Transform` в `Gtk.Widget.allocate()`.
Это принципиально дешевле, чем менять size-request у `Gtk.Picture` внутри
`Gtk.ScrolledWindow`: зум и перетаскивание не вызывают ни одного лишнего
прохода measure/layout — только пересчёт одной матрицы.
"""
from __future__ import annotations

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Gdk", "4.0")
gi.require_version("Gsk", "4.0")
gi.require_version("Graphene", "1.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gdk, Graphene, Gsk, Gtk  # noqa: E402

from .utils import human_date, human_size  # noqa: E402

ZOOM_STEP = 1.25
MIN_SCALE = 0.02
MAX_SCALE = 16.0


class ImageStage(Gtk.Widget):
    """Область показа изображения с масштабом и панорамированием."""

    __gtype_name__ = "ErmitageImageStage"

    def __init__(self) -> None:
        super().__init__()
        self._picture = Gtk.Picture()
        self._picture.set_can_shrink(False)
        self._picture.set_visible(False)
        self._picture.set_parent(self)

        self._texture = None
        self._fit = True
        self._scale = 1.0
        self._effective_scale = 1.0
        self._pan = [0.0, 0.0]
        self._pan_start = (0.0, 0.0)

        self.set_hexpand(True)
        self.set_vexpand(True)
        self.set_overflow(Gtk.Overflow.HIDDEN)
        self.add_css_class("image-stage")
        self.set_cursor(Gdk.Cursor.new_from_name("grab", None))

        drag = Gtk.GestureDrag()
        drag.connect("drag-begin", self._on_drag_begin)
        drag.connect("drag-update", self._on_drag_update)
        self.add_controller(drag)

        self.on_scale_changed = None  # колбэк для индикатора масштаба

    # ------------------------------------------------------------- данные
    def set_texture(self, texture) -> None:
        self._texture = texture
        self._picture.set_paintable(texture)
        self._picture.set_visible(texture is not None)
        self._fit = True
        self._scale = 1.0
        self._pan = [0.0, 0.0]
        self._picture.set_margin_start(0)
        self._relayout()

    def has_image(self) -> bool:
        return self._texture is not None

    def is_fit(self) -> bool:
        return self._fit

    def effective_scale(self) -> float:
        return self._effective_scale

    # ------------------------------------------------------------ масштаб
    def _image_size(self) -> tuple[int, int]:
        if self._texture is None:
            return 1, 1
        return max(1, self._texture.get_width()), max(1, self._texture.get_height())

    def _fit_scale(self, width: float | None = None, height: float | None = None) -> float:
        if width is None:
            width = self.get_width()
        if height is None:
            height = self.get_height()
        width = width or 1
        height = height or 1
        image_w, image_h = self._image_size()
        scale = min(width / image_w, height / image_h)
        return min(scale, 1.0)  # мелкие картинки не растягиваем

    def set_fit(self) -> None:
        self._fit = True
        self._pan = [0.0, 0.0]
        self._relayout()

    def set_actual(self) -> None:
        self._fit = False
        self._scale = 1.0
        self._relayout()

    def zoom_by(self, factor: float) -> None:
        if self._texture is None:
            return
        base = self._effective_scale if not self._fit else self._fit_scale()
        self._scale = max(MIN_SCALE, min(MAX_SCALE, base * factor))
        self._fit = False
        self._relayout()

    def _relayout(self) -> None:
        queue = getattr(self, "queue_allocate", None)
        if queue is not None:
            queue()
        else:  # GTK < 4.12
            self.queue_resize()
        if self.on_scale_changed is not None:
            self.on_scale_changed(self)

    # ------------------------------------------------------- панорама
    def _on_drag_begin(self, gesture, start_x, start_y) -> None:
        self._pan_start = (self._pan[0], self._pan[1])
        self.set_cursor(Gdk.Cursor.new_from_name("grabbing", None))

    def _on_drag_update(self, gesture, offset_x, offset_y) -> None:
        if self._texture is None:
            return
        self._pan[0] = self._pan_start[0] + offset_x
        self._pan[1] = self._pan_start[1] + offset_y
        self._clamp_pan()
        self._relayout()

    def _clamp_pan(self, width: float | None = None, height: float | None = None) -> None:
        """Не даём утащить картинку за пределы сцены."""
        if width is None:
            width = self.get_width()
        if height is None:
            height = self.get_height()
        image_w, image_h = self._image_size()
        scale = self._effective_scale
        overflow_x = max(0.0, image_w * scale - (width or 0)) * 0.5
        overflow_y = max(0.0, image_h * scale - (height or 0)) * 0.5
        self._pan[0] = max(-overflow_x, min(overflow_x, self._pan[0]))
        self._pan[1] = max(-overflow_y, min(overflow_y, self._pan[1]))

    # ------------------------------------------------------- GTK-виртуалы
    def do_measure(self, orientation, for_size):  # noqa: N802
        # собственных предпочтений нет: сцена занимает всё доступное место
        return 0, 0, -1, -1

    def do_size_allocate(self, width, height, baseline):  # noqa: N802
        if self._texture is None:
            self._picture.set_visible(False)
            return
        image_w, image_h = self._image_size()
        # важно: get_width()/get_height() здесь ещё старые, поэтому считаем
        # масштаб по переданному размеру выделенной области
        scale = self._fit_scale(width, height) if self._fit else self._scale
        self._effective_scale = scale
        self._clamp_pan(width, height)

        draw_w = image_w * scale
        draw_h = image_h * scale
        x = (width - draw_w) * 0.5 + self._pan[0]
        y = (height - draw_h) * 0.5 + self._pan[1]

        # scale применяется первой, затем translate — так (x, y) задаёт
        # положение уже отмасштабированной картинки
        transform = (
            Gsk.Transform.new()
            .translate(Graphene.Point().init(x, y))
            .scale(scale, scale)
        )
        self._picture.allocate(image_w, image_h, -1, transform)


class ViewerWindow(Adw.Window):
    __gtype_name__ = "ErmitageViewer"

    def __init__(self, parent, loader, items: list, index: int, on_index_changed=None) -> None:
        super().__init__(transient_for=parent, modal=False)
        self.set_title("Эрмитаж — просмотр")
        self.set_default_size(1180, 820)
        self.add_css_class("viewer-window")

        self._loader = loader
        self._items = items
        self._index = max(0, min(index, len(items) - 1))
        self._request = None
        self._on_index_changed = on_index_changed

        self._build_ui()
        self._install_controllers()
        self._load_current()

    # ------------------------------------------------------------------ UI
    def _build_ui(self) -> None:
        self._title = Adw.WindowTitle(title="", subtitle="")
        # ширина индикатора фиксирована в CSS (.zoom-indicator): иначе при
        # смене текста «Вписано» ↔ «237%» шапка пересчитывается и текст
        # обрезается по краю
        self._zoom_label = Gtk.Label(label="")
        self._zoom_label.add_css_class("dim-label")
        self._zoom_label.add_css_class("zoom-indicator")
        self._zoom_label.set_xalign(0.5)
        self._zoom_label.set_valign(Gtk.Align.CENTER)

        header = Adw.HeaderBar()
        header.set_title_widget(self._title)

        self._prev_button = Gtk.Button(icon_name="go-previous-symbolic")
        self._prev_button.set_tooltip_text("Предыдущее (←)")
        self._prev_button.connect("clicked", lambda *_: self._step(-1))
        header.pack_start(self._prev_button)

        self._next_button = Gtk.Button(icon_name="go-next-symbolic")
        self._next_button.set_tooltip_text("Следующее (→)")
        self._next_button.connect("clicked", lambda *_: self._step(1))
        header.pack_start(self._next_button)

        self._zoom_out_button = Gtk.Button(icon_name="zoom-out-symbolic")
        self._zoom_out_button.set_tooltip_text("Уменьшить (−)")
        self._zoom_out_button.connect("clicked", lambda *_: self._stage.zoom_by(1 / ZOOM_STEP))
        header.pack_end(self._zoom_out_button)

        self._fit_button = Gtk.Button(icon_name="zoom-fit-best-symbolic")
        self._fit_button.set_tooltip_text("Вписать в окно (0)")
        self._fit_button.connect("clicked", lambda *_: self._stage.set_fit())
        header.pack_end(self._fit_button)

        self._zoom_in_button = Gtk.Button(icon_name="zoom-in-symbolic")
        self._zoom_in_button.set_tooltip_text("Увеличить (+)")
        self._zoom_in_button.connect("clicked", lambda *_: self._stage.zoom_by(ZOOM_STEP))
        header.pack_end(self._zoom_in_button)

        header.pack_end(self._zoom_label)

        self._stage = ImageStage()
        self._stage.on_scale_changed = self._on_scale_changed

        self._spinner = Adw.Spinner()
        self._spinner.set_size_request(48, 48)
        self._spinner.set_halign(Gtk.Align.CENTER)
        self._spinner.set_valign(Gtk.Align.CENTER)

        overlay = Gtk.Overlay()
        overlay.set_child(self._stage)
        overlay.add_overlay(self._spinner)

        toolbar = Adw.ToolbarView()
        toolbar.add_top_bar(header)
        toolbar.set_content(overlay)
        self.set_content(toolbar)

        # адаптивность по Adw.Breakpoint: сначала прячем индикатор масштаба,
        # затем кнопки «−/+» — в узком окне остаются только стрелки и «вписать»
        try:
            narrow = Adw.Breakpoint.new(Adw.BreakpointCondition.parse("max-width: 620sp"))
            narrow.add_setter(self._zoom_label, "visible", False)
            wider = Adw.Breakpoint.new(Adw.BreakpointCondition.parse("max-width: 460sp"))
            wider.add_setter(self._zoom_in_button, "visible", False)
            wider.add_setter(self._zoom_out_button, "visible", False)
            self.add_breakpoint(narrow)
            self.add_breakpoint(wider)
        except Exception:  # noqa: BLE001 — старый libadwaita: шапка просто плотнее
            pass

    def _install_controllers(self) -> None:
        keys = Gtk.EventControllerKey()
        keys.connect("key-pressed", self._on_key)
        self.add_controller(keys)

        scroll = Gtk.EventControllerScroll.new(
            Gtk.EventControllerScrollFlags.VERTICAL
            | Gtk.EventControllerScrollFlags.DISCRETE
        )
        scroll.set_propagation_phase(Gtk.PropagationPhase.CAPTURE)
        scroll.connect("scroll", self._on_scroll)
        self._stage.add_controller(scroll)

    # -------------------------------------------------------------- данные
    def _current_item(self):
        return self._items[self._index]

    def _load_current(self) -> None:
        item = self._current_item()
        if self._request is not None:
            self._request.cancel()
            self._request = None

        self._spinner.set_visible(True)
        self._title.set_title(item.name)
        self._title.set_subtitle(
            f"{item.width}×{item.height} · {human_size(item.size)} · {human_date(item.mtime)}"
        )
        self._prev_button.set_sensitive(self._index > 0)
        self._next_button.set_sensitive(self._index < len(self._items) - 1)

        self._request = self._loader.load_full(item, self._on_texture)
        if self._on_index_changed is not None:
            self._on_index_changed(item)

    def _on_texture(self, path: str, texture) -> None:
        self._request = None
        if self._current_item().path != path:
            return
        self._spinner.set_visible(False)
        self._stage.set_texture(texture)
        self._on_scale_changed(self._stage)

    def _on_scale_changed(self, stage) -> None:
        if not stage.has_image():
            self._zoom_label.set_text("")
            return
        if stage.is_fit():
            self._zoom_label.set_text("Вписано")
        else:
            self._zoom_label.set_text(f"{stage.effective_scale() * 100:.0f}%")

    # ------------------------------------------------------------ навигация
    def _step(self, delta: int) -> None:
        new_index = self._index + delta
        if 0 <= new_index < len(self._items):
            self._index = new_index
            self._load_current()

    def _on_key(self, controller, keyval, keycode, state) -> bool:
        if keyval == Gdk.KEY_Escape:
            self.close()
            return True
        if keyval in (Gdk.KEY_Left, Gdk.KEY_Up, Gdk.KEY_BackSpace):
            self._step(-1)
            return True
        if keyval in (Gdk.KEY_Right, Gdk.KEY_Down, Gdk.KEY_space):
            self._step(1)
            return True
        if keyval == Gdk.KEY_Home:
            self._index = 0
            self._load_current()
            return True
        if keyval == Gdk.KEY_End:
            self._index = len(self._items) - 1
            self._load_current()
            return True
        if keyval in (Gdk.KEY_plus, Gdk.KEY_equal, Gdk.KEY_KP_Add):
            self._stage.zoom_by(ZOOM_STEP)
            return True
        if keyval in (Gdk.KEY_minus, Gdk.KEY_KP_Subtract):
            self._stage.zoom_by(1 / ZOOM_STEP)
            return True
        if keyval in (Gdk.KEY_0, Gdk.KEY_f):
            self._stage.set_fit()
            return True
        if keyval == Gdk.KEY_1:
            self._stage.set_actual()
            return True
        if keyval == Gdk.KEY_F11:
            if self.is_fullscreen():
                self.unfullscreen()
            else:
                self.fullscreen()
            return True
        return False

    def _on_scroll(self, controller, dx: float, dy: float) -> bool:
        state = controller.get_current_event_state()
        if state & Gdk.ModifierType.CONTROL_MASK:
            if dy < 0:
                self._stage.zoom_by(ZOOM_STEP)
            elif dy > 0:
                self._stage.zoom_by(1 / ZOOM_STEP)
            return True
        return False

    def do_close_request(self) -> bool:  # noqa: N802
        if self._request is not None:
            self._request.cancel()
            self._request = None
        return False
