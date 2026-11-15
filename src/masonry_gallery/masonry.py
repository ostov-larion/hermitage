"""Masonry-сетка с виртуализацией — рассчитана на десятки тысяч элементов.

Идея
----
Классический masonry (Pinterest / Google Images) — упаковка «в самую короткую
колонку». Штатные контейнеры GTK4 так не умеют: `Gtk.FlowBox` и `Gtk.GridView`
требуют одинаковых ячеек, а колонки из `Gtk.Box` не умеют переиспользовать
виджеты. Поэтому контейнер написан свой, и он делает три вещи:

1. **Геометрия.** Один проход по элементам: высота плитки = ширина_колонки /
   aspect_ratio (aspect берётся из заголовка файла, без декодирования пикселей).
   Колонка выбирается через мини-кучу (`heapq`) — O(log k), где k — число
   колонок, поэтому полный пересчёт 50 000 элементов это десятки миллисекунд,
   а не секунды.
   Пропорции хранятся отдельным списком float в модели: внутренний цикл не
   трогает объекты Item и не вызывает property.
2. **Виртуализация.** Виджеты существуют только для элементов в видимой области
   плюс overscan. Поиск идёт через `bisect` по Y-координатам каждой колонки —
   O(log n + k), а не линейным сканом.
3. **Переиспользование и инкрементальность.** Освободившиеся плитки уходят в пул
   и перепривязываются к новым элементам (при быстром скролле создаётся 0
   виджетов). Догрузка каталога достраивает геометрию с последнего элемента,
   а не пересчитывает всё заново.

Итог: 50 000 изображений в каталоге, ~30–60 живых виджетов, постоянное
потребление памяти и стабильный скролл.
"""
from __future__ import annotations

import gc
import heapq
import time
from bisect import bisect_left, bisect_right

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Gdk", "4.0")
gi.require_version("Graphene", "1.0")
from gi.repository import Adw, Gdk, GLib, Graphene, Gsk, Gtk  # noqa: E402

from .palette import Palette, build_palette  # noqa: E402
from .tile import Tile  # noqa: E402

POOL_LIMIT = 48


class MasonryView(Gtk.Widget):
    __gtype_name__ = "MgMasonryView"

    def __init__(self, model, loader) -> None:
        super().__init__()
        self._model = model
        self.loader = loader
        self.palette: Palette = build_palette()

        # настройки вида
        self.min_col_width = 240
        self.spacing = 12
        self.padding = 12
        self.overscan = 400

        # кэш геометрии
        self._cols = 0
        self._col_w = 0.0
        self._pos: list[tuple[float, float, float, float]] = []
        # в колонках храним только индексы: создание кортежей на каждый элемент
        # удваивало стоимость полного пересчёта 50 000 элементов
        self._col_items: list[list[int]] = []
        self._col_ys: list[list[float]] = []
        self._heap: list[tuple[float, int]] = []
        self._height = 0.0
        self._layout_width = -1.0
        self._layout_count = -1
        self._dirty = True
        self._last_reflow = 0.0
        self._pending_width = 0.0
        self._reflow_source = 0

        # живые виджеты и пул
        self._active: dict[int, Tile] = {}
        # какие прямоугольники уже выданы плиткам: при ресайзе окна индекс
        # остаётся тем же, но геометрия меняется — плитку нужно переразместить
        self._alloc: dict[int, tuple[int, int, int, int]] = {}
        self._rebind_needed = False
        self._pool: list[Tile] = []
        self._idle_source = 0
        self._vadj: Gtk.Adjustment | None = None
        self._scroll_anim = None

        # выбор
        self._selected: set[str] = set()
        self._cursor: str | None = None

        # внешние колбэки (проставляет окно)
        self.on_activate = None            # (item) -> открыть просмотрщик
        self.on_selection_changed = None   # (list[Item]) -> обновить интерфейс
        self.on_context_menu = None        # (item, tile) -> контекстное меню
        self.on_hover = None               # (item | None) -> подсказка окна
        self.on_zoom_request = None        # (delta) -> смена размера плитки

        self.set_hexpand(True)
        self.set_focusable(True)
        self.add_css_class("masonry-view")

        scroll = Gtk.EventControllerScroll.new(
            Gtk.EventControllerScrollFlags.VERTICAL
            | Gtk.EventControllerScrollFlags.DISCRETE
        )
        scroll.set_propagation_phase(Gtk.PropagationPhase.CAPTURE)
        scroll.connect("scroll", self._on_scroll)
        self.add_controller(scroll)

    # ------------------------------------------------------------------ API
    def attach_adjustment(self, adjustment: Gtk.Adjustment) -> None:
        """Подключает вертикальную прокрутку: она определяет видимое окно."""
        self._vadj = adjustment
        adjustment.connect("value-changed", self._on_scroll_value)
        self._schedule_update()

    def set_min_col_width(self, width: int) -> None:
        width = max(80, min(640, int(width)))
        if width == self.min_col_width:
            return
        self.min_col_width = width
        # текстуры запрашивались под старый размер — перезапросим
        self._release_all()
        self._rebind_needed = True
        self.invalidate()

    def invalidate(self) -> None:
        """Полный пересчёт: сменились данные (сортировка/фильтр) или ширина."""
        self._dirty = True
        self.queue_resize()
        self._schedule_update()

    def append_items(self) -> None:
        """Догрузка в конец: геометрия достраивается, а не пересчитывается."""
        self.queue_resize()
        self._schedule_update()

    def reset(self) -> None:
        """Данные полностью сменились (фильтр/сортировка/новый каталог)."""
        self._release_all()
        self._selected.clear()
        self._cursor = None
        self._layout_count = -1
        self.invalidate()
        self._notify_selection()

    def refresh_palette(self) -> None:
        self.palette = build_palette()
        for tile in self._active.values():
            tile.queue_draw()

    def thumb_px(self) -> int:
        width = self._col_w if self._col_w > 0 else float(self.min_col_width)
        return int(width * max(1, self.get_scale_factor()))

    # ------------------------------------------------------- выделение
    def selection_count(self) -> int:
        return len(self._selected)

    def selected_items(self) -> list:
        if not self._selected:
            return []
        selected = self._selected
        return [item for item in self._model.items if item.path in selected]

    def cursor_item(self):
        if self._cursor is None:
            return None
        index = self._model.index_of(self._cursor)
        return self._model[index] if index >= 0 else None

    def click(self, item, ctrl: bool = False, shift: bool = False) -> None:
        if ctrl:
            if item.path in self._selected:
                self._selected.discard(item.path)
            else:
                self._selected.add(item.path)
        elif shift and self._cursor is not None:
            anchor = self._model.index_of(self._cursor)
            index = self._model.index_of(item.path)
            if anchor >= 0 and index >= 0:
                lo, hi = (anchor, index) if anchor <= index else (index, anchor)
                self._selected = {self._model[i].path for i in range(lo, hi + 1)}
            else:
                self._selected = {item.path}
        else:
            self._selected = {item.path}
        self._cursor = item.path
        self._notify_selection()

    def select_all(self) -> None:
        self._selected = {item.path for item in self._model.items}
        self._notify_selection()

    def clear_selection(self) -> None:
        if not self._selected:
            return
        self._selected.clear()
        self._notify_selection()

    def select_path(self, path: str, scroll: bool = True) -> None:
        self._selected = {path}
        self._cursor = path
        self._notify_selection()
        if scroll:
            index = self._model.index_of(path)
            if index >= 0:
                self.scroll_to_index(index)

    def activate(self, item) -> None:
        if self.on_activate is not None:
            self.on_activate(item)

    def context_menu(self, item, tile) -> None:
        if item.path not in self._selected:
            self.click(item)
        if self.on_context_menu is not None:
            self.on_context_menu(item, tile)

    def on_tile_hover(self, item) -> None:
        if self.on_hover is not None:
            self.on_hover(item)

    def _notify_selection(self) -> None:
        selected = self._selected
        for tile in self._active.values():
            tile_item = tile.item
            if tile_item is not None:
                tile.set_selected(tile_item.path in selected)
        if self.on_selection_changed is not None:
            self.on_selection_changed(self.selected_items())

    # ------------------------------------------------- перемещение курсора
    def _column_of(self, index: int) -> int:
        x = self._pos[index][0]
        step = self._col_w + self.spacing
        if step <= 0:
            return 0
        return int(round((x - self.padding) / step))

    def move_cursor(self, dx: float, dy: float) -> None:
        """Стрелки ходят по визуальному соседству: O(log n) вместо O(n)."""
        count = len(self._model)
        if count == 0 or len(self._pos) != count or self._col_w <= 0:
            return
        index = self._model.index_of(self._cursor) if self._cursor else -1
        if index < 0:
            self.select_path(self._model[0].path)
            return
        target = self._neighbor(index, dx, dy)
        if target is not None:
            self.select_path(self._model[target].path)

    def _neighbor(self, index: int, dx: float, dy: float) -> int | None:
        column = self._column_of(index)
        x, y, w, h = self._pos[index]
        center_y = y + h * 0.5

        if dy:
            items = self._col_items[column]
            ys = self._col_ys[column]
            position = bisect_left(ys, y)
            position += 1 if dy > 0 else -1
            if 0 <= position < len(items):
                return items[position]
            return None

        if not dx:
            return None
        new_column = column + (1 if dx > 0 else -1)
        if not 0 <= new_column < self._cols:
            return None
        items = self._col_items[new_column]
        ys = self._col_ys[new_column]
        if not items:
            return None

        start = max(0, bisect_right(ys, center_y) - 3)
        tolerance = y + h + h * 0.5
        positions = self._pos
        best = None
        best_distance = float("inf")
        for position in range(start, len(items)):
            other_index = items[position]
            _ox, other_y, _ow, other_h = positions[other_index]
            if other_y > tolerance:
                break
            distance = abs((other_y + other_h * 0.5) - center_y)
            if distance < best_distance:
                best_distance = distance
                best = other_index
        return best

    def scroll_to_index(self, index: int) -> None:
        if not self._pos or index >= len(self._pos) or self._vadj is None:
            return
        _x, y, _w, h = self._pos[index]
        value = self._vadj.get_value()
        page = self._vadj.get_page_size()
        if y < value + 8:
            target = y - 12
        elif y + h > value + page - 8:
            target = y + h - page + 12
        else:
            return
        target = max(self._vadj.get_lower(),
                     min(self._vadj.get_upper() - page, target))
        self._animate_scroll(target)

    def _animate_scroll(self, target: float) -> None:
        assert self._vadj is not None
        animation = getattr(Adw, "TimedAnimation", None)
        target_obj = getattr(Adw, "PropertyAnimationTarget", None)
        if animation is None or target_obj is None:
            self._vadj.set_value(target)
            return
        try:
            self._scroll_anim = animation(
                widget=self,
                value_from=self._vadj.get_value(),
                value_to=target,
                duration=160,
                target=target_obj(object=self._vadj, property="value"),
            )
            self._scroll_anim.play()
        except Exception:  # noqa: BLE001
            self._vadj.set_value(target)

    # ------------------------------------------------------- геометрия
    def _ensure_layout(self, width: float) -> None:
        count = len(self._model)
        if (not self._dirty and self._layout_count == count
                and abs(width - self._layout_width) < 0.5):
            return
        width = float(width)

        # 1) догрузка каталога: достраиваем с последнего элемента
        if (not self._dirty and self._cols > 0
                and abs(width - self._layout_width) < 0.5
                and count >= self._layout_count >= 0):
            self._compute(width, count, start=self._layout_count)
            self._dirty = False
            self._last_reflow = time.monotonic()
            return

        # 2) перетаскивание границы окна: не пересчитываем всю раскладку на
        #    каждый пиксель, а откладываем до «успокоения» размера
        if (not self._dirty and self._layout_width > 0
                and abs(width - self._layout_width) < 120
                and time.monotonic() - self._last_reflow < 0.14):
            self._pending_width = width
            self._schedule_reflow()
            return

        self._compute(width, count, start=0)
        self._dirty = False
        self._last_reflow = time.monotonic()

    def _schedule_reflow(self) -> None:
        if self._reflow_source:
            return
        self._reflow_source = GLib.timeout_add(150, self._on_reflow_timeout)

    def _on_reflow_timeout(self) -> bool:
        self._reflow_source = 0
        width = self._pending_width or self._layout_width
        count = len(self._model)
        if width > 0 and count:
            old_col_w = self._col_w
            self._compute(float(width), count, start=0)
            self._last_reflow = time.monotonic()
            self._dirty = False
            # плитки уже на экране: их геометрия поменялась, а миниатюры были
            # декодированы под прежнюю ширину колонки
            if abs(self._col_w - old_col_w) > 0.5:
                self._rebind_needed = True
            self.queue_resize()
            self._schedule_update()
        return False

    def _compute(self, width: float, count: int, start: int = 0) -> None:
        spacing = float(self.spacing)
        pad = float(self.padding)
        min_width = float(self.min_col_width)
        inner = max(1.0, width - pad * 2.0)
        cols = max(1, int((inner + spacing) // (min_width + spacing)))
        col_w = max(1.0, (inner - spacing * (cols - 1)) / cols)

        if start and (cols != self._cols or abs(col_w - self._col_w) > 0.01
                      or count < start):
            start = 0

        if start == 0:
            self._cols = cols
            self._col_w = col_w
            self._layout_width = width
            self._col_items = [[] for _ in range(cols)]
            self._col_ys = [[] for _ in range(cols)]
            self._pos = []
            heap = [(0.0, column) for column in range(cols)]
            heapq.heapify(heap)
            self._heap = heap
        else:
            heap = self._heap

        # 50 000 новых кортежей за проход — это заметная нагрузка на
        # циклический сборщик мусора, а циклов здесь нет. Отключаем его на время.
        gc_was_enabled = gc.isenabled()
        if gc_was_enabled:
            gc.disable()

        aspects = self._model.aspects
        positions = self._pos
        col_items = self._col_items
        col_ys = self._col_ys
        step = col_w + spacing
        heapreplace = heapq.heapreplace

        for index in range(start, count):
            height = col_w / aspects[index]
            y, column = heap[0]
            positions.append((pad + column * step, y, col_w, height))
            col_items[column].append(index)
            col_ys[column].append(y)
            heapreplace(heap, (y + height + spacing, column))

        if gc_was_enabled:
            gc.enable()

        self._layout_count = count
        if count:
            self._height = max(top for top, _column in heap) - spacing + pad
        else:
            self._height = 0.0

    def _visible(self, y0: float, y1: float) -> list[int]:
        out: list[int] = []
        positions = self._pos
        for column in range(self._cols):
            ys = self._col_ys[column]
            if not ys:
                continue
            # элементы бывают выше шага, поэтому отступаем назад с запасом
            start = bisect_right(ys, y0) - 2
            if start < 0:
                start = 0
            items = self._col_items[column]
            for position in range(start, len(items)):
                index = items[position]
                _x, y, _w, height = positions[index]
                if y > y1:
                    break
                if y + height >= y0:
                    out.append(index)
        return out

    # ------------------------------------------------------- виджеты
    def _obtain(self) -> Tile:
        if self._pool:
            tile = self._pool.pop()
            tile.set_visible(True)
            return tile
        tile = Tile(self)
        tile.set_parent(self)
        return tile

    def _recycle(self, index, tile) -> None:
        self._alloc.pop(index, None)
        tile.unbind()
        tile.set_visible(False)
        tile.set_selected(False)
        self._pool.append(tile)

    def _release_all(self) -> None:
        for tile in list(self._active.values()):
            tile.unbind()
            tile.set_visible(False)
            self._pool.append(tile)
        self._active.clear()
        self._alloc.clear()
        self._trim_pool()

    def _trim_pool(self) -> None:
        while len(self._pool) > POOL_LIMIT:
            tile = self._pool.pop()
            tile.unparent()

    def _schedule_update(self) -> None:
        if self._idle_source:
            return
        self._idle_source = GLib.idle_add(
            self._on_idle, priority=GLib.PRIORITY_HIGH_IDLE
        )

    def _on_idle(self) -> bool:
        self._idle_source = 0
        self._update()
        return False

    def _on_scroll_value(self, _adjustment) -> None:
        self._schedule_update()

    def _update(self) -> None:
        if self._dirty or self._layout_count != len(self._model):
            width = self._layout_width if self._layout_width > 0 else self.get_width()
            if width <= 0:
                return
            self._ensure_layout(float(width))

        count = len(self._model)
        if count == 0 or not self._pos:
            if self._active:
                self._release_all()
            return

        if self._vadj is not None:
            top = self._vadj.get_value()
            page = self._vadj.get_page_size() or self.get_height() or 800.0
        else:
            top, page = 0.0, float(self.get_height() or 800)

        y0 = max(-self.overscan, top - self.overscan)
        y1 = top + page + self.overscan
        needed = self._visible(y0, y1)
        needed_set = set(needed)

        for index in [i for i in self._active if i not in needed_set]:
            self._recycle(index, self._active.pop(index))

        model = self._model
        selected = self._selected
        positions = self._pos
        alloc = self._alloc
        rebind = self._rebind_needed
        for index in needed:
            x, y, w, h = positions[index]
            rect = (round(x), round(y), round(w), round(h))
            tile = self._active.get(index)
            if tile is None:
                tile = self._obtain()
                item = model[index]
                tile.bind(item)
                tile.set_selected(item.path in selected)
                self._active[index] = tile
                alloc.pop(index, None)
            elif rebind:
                # ширина плитки заметно изменилась — просим миниатюру под
                # новый размер (старая текстура видна, пока не придёт новая)
                tile.refresh_thumbnail()
            if alloc.get(index) == rect:
                continue
            transform = Gsk.Transform.new().translate(
                Graphene.Point().init(rect[0], rect[1])
            )
            tile.allocate(rect[2], rect[3], -1, transform)
            alloc[index] = rect
        self._rebind_needed = False

        self._trim_pool()

    # ------------------------------------------------------- события GTK
    def _on_scroll(self, controller, dx: float, dy: float) -> bool:
        state = controller.get_current_event_state()
        if state & Gdk.ModifierType.CONTROL_MASK:
            if self.on_zoom_request is not None and dy != 0:
                self.on_zoom_request(-int(dy))
            return True
        return False

    def do_measure(self, orientation, for_size):  # noqa: N802
        if orientation == Gtk.Orientation.HORIZONTAL:
            # минимум — уже одной «идеальной» колонки: при узком окне сетка
            # честно перестраивается в одну колонку, а не обрезается
            minimum = min(180.0, float(self.min_col_width))
            return minimum, minimum * 2.0, -1, -1
        width = float(for_size) if for_size > 0 else self._layout_width
        if width <= 0:
            return 0, 0, -1, -1
        self._ensure_layout(width)
        return self._height, self._height, -1, -1

    def do_size_allocate(self, width, height, baseline):  # noqa: N802
        self._ensure_layout(float(width))
        self._update()

    def do_unroot(self):  # noqa: N802
        if self._reflow_source:
            GLib.source_remove(self._reflow_source)
            self._reflow_source = 0
        if self._idle_source:
            GLib.source_remove(self._idle_source)
            self._idle_source = 0
        self._release_all()
        Gtk.Widget.do_unroot(self)

    def do_dispose(self):  # noqa: N802
        for tile in list(self._active.values()) + self._pool:
            try:
                tile.unparent()
            except Exception:  # noqa: BLE001
                pass
        self._active.clear()
        self._pool.clear()
        Gtk.Widget.do_dispose(self)
