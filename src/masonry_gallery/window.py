"""Главное окно «Эрмитажа».

Устройство интерфейса — по гайдлайнам GNOME (HIG):

* всё управление видом живёт в шапке: поиск, сортировка и размер плиток —
  отдельные кнопки-меню (как в Nautilus), а не пункты главного меню;
* главное меню содержит только уровень приложения (открыть папку, обновить,
  оформление, справка) — действия над конкретными файлами переехали в
  контекстное меню;
* нижней панели состояния нет: количество изображений и выделение показывает
  подзаголовок окна, сведения о файле — сама плитка при наведении;
* пока папка не выбрана, вместо пустой сетки показывается `Adw.StatusPage`
  с предложением открыть папку (а не содержимое каталога запуска).
"""
from __future__ import annotations

import gc
import os
import time

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Gdk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gdk, Gio, GLib, GObject, Gtk  # noqa: E402

from .masonry import MasonryView  # noqa: E402
from .model import GalleryModel  # noqa: E402
from .settings import Settings  # noqa: E402
from .theme_switcher import ThemeSwitcher  # noqa: E402
from .shortcuts import present_shortcuts  # noqa: E402
from .utils import plural_images  # noqa: E402
from .viewer import ViewerWindow  # noqa: E402

THUMB_SIZES = {"s": 150, "m": 240, "l": 340, "xl": 480}
THUMB_ORDER = ("s", "m", "l", "xl")

SORT_MODES = (
    ("name", "По имени"),
    ("date", "По дате"),
    ("size", "По размеру"),
    ("random", "Перемешать"),
)

THUMB_LABELS = (
    ("s", "Мелкие"),
    ("m", "Средние"),
    ("l", "Крупные"),
    ("xl", "Очень крупные"),
)


class GalleryWindow(Adw.ApplicationWindow):
    __gtype_name__ = "HermitageWindow"

    def __init__(self, application, loader, settings: Settings | None = None) -> None:
        super().__init__(application=application, title="Эрмитаж")

        self.settings = settings or Settings()
        self.loader = loader
        self.model = GalleryModel()

        self._current_path: str | None = None
        self._loading = False
        self._cached_view = False
        self._load_started = 0.0
        self._first_paint_at = 0.0
        self._viewer: ViewerWindow | None = None
        self._search_source = 0
        self._thumb_key = self.settings.get_string("thumb-size")
        if self._thumb_key not in THUMB_SIZES:
            self._thumb_key = "m"
        self._sort_key = self.settings.get_string("sort-mode")
        if self._sort_key not in dict(SORT_MODES):
            self._sort_key = "name"

        # окно должно оставаться рабочим и на маленьком экране
        self.set_size_request(360, 300)
        width = max(360, self.settings.get_int("window-width"))
        height = max(300, self.settings.get_int("window-height"))
        self.set_default_size(width, height)

        self.model.set_sort(self._sort_key)
        self._build_ui()
        self._install_actions()
        self._apply_thumb_key(self._thumb_key, save=False)
        self._update_title()

    # ================================================================== UI
    def _build_ui(self) -> None:
        # --- сетка --------------------------------------------------------
        self.view = MasonryView(self.model, self.loader)
        self.view.on_activate = self._open_viewer
        self.view.on_selection_changed = self._on_selection_changed
        self.view.on_context_menu = self._on_context_menu
        self.view.on_zoom_request = self._on_zoom_request
        self.view.set_min_col_width(THUMB_SIZES[self._thumb_key])

        self._scroller = Gtk.ScrolledWindow()
        self._scroller.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        self._scroller.set_child(self.view)
        self._scroller.set_vexpand(True)
        self._scroller.set_hexpand(True)
        self._scroller.add_css_class("gallery-scroll")
        self.view.attach_adjustment(self._scroller.get_vadjustment())

        # --- статусная страница (приветствие / индексация / пусто) ---------
        self._status_page = Adw.StatusPage()
        self._status_page.set_icon_name("folder-pictures-symbolic")
        self._status_page.set_title("Откройте папку с фотографиями")
        self._status_page.set_description(
            "Перетащите папку в окно или выберите её на диске — "
            "миниатюры появятся сразу"
        )

        self._open_button_large = Gtk.Button(label="Открыть папку…")
        self._open_button_large.add_css_class("pill")
        self._open_button_large.add_css_class("suggested-action")
        self._open_button_large.set_action_name("win.open")
        self._open_button_large.set_halign(Gtk.Align.CENTER)

        self._reset_search_button = Gtk.Button(label="Сбросить поиск")
        self._reset_search_button.add_css_class("pill")
        self._reset_search_button.set_action_name("win.clear-search")
        self._reset_search_button.set_halign(Gtk.Align.CENTER)

        self._status_spinner = Adw.Spinner()
        self._status_spinner.set_size_request(32, 32)
        self._status_spinner.set_halign(Gtk.Align.CENTER)

        self._status_page.set_child(self._open_button_large)

        self._stack = Gtk.Stack()
        self._stack.set_transition_type(Gtk.StackTransitionType.CROSSFADE)
        self._stack.set_transition_duration(150)
        self._stack.add_named(self._status_page, "status")
        self._stack.add_named(self._scroller, "grid")

        # --- шапка --------------------------------------------------------
        header = Adw.HeaderBar()

        self._window_title = Adw.WindowTitle(title="Эрмитаж", subtitle="")
        header.set_title_widget(self._window_title)

        self._open_button = Gtk.Button(icon_name="folder-open-symbolic")
        self._open_button.set_tooltip_text("Открыть папку (Ctrl+O)")
        self._open_button.set_action_name("win.open")
        header.pack_start(self._open_button)

        self._spinner = Adw.Spinner()
        self._spinner.set_tooltip_text("Индексация папки…")
        self._spinner.set_visible(False)

        self._search_button = Gtk.ToggleButton()
        self._search_button.set_icon_name("system-search-symbolic")
        self._search_button.set_tooltip_text("Поиск (Ctrl+F)")

        self._sort_button = self._menu_button(
            "view-sort-descending-symbolic", "Сортировка", self._build_sort_menu()
        )
        self._size_button = self._menu_button(
            "view-grid-symbolic", "Размер плиток", self._build_size_menu()
        )
        self._theme_switcher = ThemeSwitcher(on_change=self._on_scheme_selected)
        self._menu_button_primary = self._menu_button(
            "open-menu-symbolic", "Главное меню", self._build_primary_menu()
        )
        self._attach_theme_switcher()

        # Adw.HeaderBar кладёт каждый следующий pack_end левее предыдущего,
        # поэтому порядок обратный желаемому: [поиск][сортировка][размер][меню]
        header.pack_end(self._menu_button_primary)
        header.pack_end(self._size_button)
        header.pack_end(self._sort_button)
        header.pack_end(self._search_button)
        header.pack_end(self._spinner)

        # --- поиск --------------------------------------------------------
        self._search_entry = Gtk.SearchEntry()
        self._search_entry.set_placeholder_text("Поиск по имени файла…")
        self._search_entry.connect("search-changed", self._on_search_changed)
        self._search_entry.connect(
            "stop-search", lambda *_: self._search_button.set_active(False)
        )

        self._search_bar = Gtk.SearchBar()
        self._search_bar.set_child(self._search_entry)
        self._search_bar.connect_entry(self._search_entry)
        self._search_bar.set_key_capture_widget(self)
        self._search_button.bind_property(
            "active",
            self._search_bar,
            "search-mode-enabled",
            GObject.BindingFlags.BIDIRECTIONAL | GObject.BindingFlags.SYNC_CREATE,
        )

        # --- сборка -------------------------------------------------------
        toolbar = Adw.ToolbarView()
        toolbar.add_top_bar(header)
        toolbar.add_top_bar(self._search_bar)
        toolbar.set_content(self._stack)

        self._toast_overlay = Adw.ToastOverlay()
        self._toast_overlay.set_child(toolbar)
        self.set_content(self._toast_overlay)

        # --- drag & drop ---------------------------------------------------
        drop = Gtk.DropTarget.new(Gdk.FileList, Gdk.DragAction.COPY)
        drop.connect("drop", self._on_drop)
        self._stack.add_controller(drop)

        # --- клавиатура ----------------------------------------------------
        # CAPTURE: Enter/Space должны открывать изображение даже когда фокус
        # оказался на кнопке шапки (иначе срабатывала бы «Открыть папку»)
        keys = Gtk.EventControllerKey()
        keys.set_propagation_phase(Gtk.PropagationPhase.CAPTURE)
        keys.connect("key-pressed", self._on_key)
        self.add_controller(keys)

        self._context_popover = self._build_context_popover()

    # ------------------------------------------------------------ меню
    def _attach_theme_switcher(self) -> None:
        """Ставит переключатель оформления первым пунктом в поповер меню."""
        popover = self._menu_button_primary.get_popover()
        if popover is None:
            return
        try:
            popover.add_child(self._theme_switcher.widget, "theme-switcher")
        except Exception:  # noqa: BLE001 — GTK без поддержки custom-пунктов
            return
        application = self.get_application()
        settings = getattr(application, "settings", None)
        if settings is not None:
            self._theme_switcher.set_scheme(settings.get_string("color-scheme"))

    def _on_scheme_selected(self, name: str, _scheme) -> None:
        application = self.get_application()
        if application is not None and hasattr(application, "set_color_scheme"):
            application.set_color_scheme(name)

    def sync_color_scheme(self, name: str) -> None:
        """Вызывается приложением: оформление сменили не из переключателя."""
        self._theme_switcher.set_scheme(name)

    def _menu_button(self, icon: str, tooltip: str, menu: Gio.Menu) -> Gtk.MenuButton:
        button = Gtk.MenuButton()
        button.set_icon_name(icon)
        button.set_tooltip_text(tooltip)
        button.set_menu_model(menu)
        button.set_always_show_arrow(False)
        return button

    def _build_sort_menu(self) -> Gio.Menu:
        menu = Gio.Menu()
        for value, label in SORT_MODES:
            menu.append(label, f"win.sort::{value}")
        return menu

    def _build_size_menu(self) -> Gio.Menu:
        menu = Gio.Menu()
        for value, label in THUMB_LABELS:
            menu.append(label, f"win.thumb-size::{value}")
        return menu

    def _build_primary_menu(self) -> Gio.Menu:
        menu = Gio.Menu()

        # «custom»-пункт: в него Gtk.PopoverMenu вставит живой виджет
        # (см. _attach_theme_switcher) — так переключатель оформления живёт
        # прямо в меню, как в GNOME Builder
        custom = Gio.MenuItem.new(None, None)
        custom.set_attribute_value("custom", GLib.Variant.new_string("theme-switcher"))
        menu.append_item(custom)

        files = Gio.Menu()
        files.append("Открыть папку…", "win.open")
        files.append("Обновить", "win.refresh")
        menu.append_section(None, files)

        help_section = Gio.Menu()
        help_section.append("Горячие клавиши", "win.shortcuts")
        menu.append_section(None, help_section)

        about = Gio.Menu()
        about.append("О программе «Эрмитаж»", "app.about")
        menu.append_section(None, about)
        return menu

    def _build_context_popover(self) -> Gtk.PopoverMenu:
        model = Gio.Menu()
        model.append("Открыть", "win.activate-item")
        model.append("Открыть в приложении по умолчанию", "win.open-external")
        section = Gio.Menu()
        section.append("Скопировать путь", "win.copy-path")
        section.append("Показать в файловом менеджере", "win.show-in-files")
        model.append_section(None, section)
        selection = Gio.Menu()
        selection.append("Выбрать все", "win.select-all")
        selection.append("Снять выбор", "win.clear-selection")
        model.append_section(None, selection)

        popover = Gtk.PopoverMenu.new_from_model(model)
        popover.set_parent(self._scroller)
        popover.set_has_arrow(True)
        return popover

    # ============================================================== экшены
    def _install_actions(self) -> None:
        for name, handler in (
            ("open", self._action_open),
            ("refresh", self._action_refresh),
            ("activate-item", self._action_activate_item),
            ("open-external", self._action_open_external),
            ("select-all", self._action_select_all),
            ("clear-selection", self._action_clear_selection),
            ("clear-search", self._action_clear_search),
            ("copy-path", self._action_copy_path),
            ("show-in-files", self._action_show_in_files),
            ("zoom-in", self._action_zoom_in),
            ("zoom-out", self._action_zoom_out),
            ("escape", self._action_escape),
            ("shortcuts", self._action_shortcuts),
        ):
            action = Gio.SimpleAction.new(name, None)
            action.connect("activate", handler)
            self.add_action(action)

        sort_action = Gio.SimpleAction.new_stateful(
            "sort",
            GLib.VariantType.new("s"),
            GLib.Variant.new_string(self._sort_key),
        )
        sort_action.connect("activate", self._action_sort)
        self.add_action(sort_action)

        thumb_action = Gio.SimpleAction.new_stateful(
            "thumb-size",
            GLib.VariantType.new("s"),
            GLib.Variant.new_string(self._thumb_key),
        )
        thumb_action.connect("activate", self._action_thumb_size)
        self.add_action(thumb_action)

        self._set_selection_actions(False)

    def _set_selection_actions(self, enabled: bool) -> None:
        for name in (
            "activate-item",
            "open-external",
            "copy-path",
            "show-in-files",
            "clear-selection",
        ):
            action = self.lookup_action(name)
            if action is not None:
                action.set_enabled(enabled)

    def _action_open(self, *_args) -> None:
        dialog = Gtk.FileDialog()
        dialog.set_title("Выберите папку с изображениями")
        if self._current_path:
            dialog.set_initial_folder(Gio.File.new_for_path(self._current_path))
        dialog.select_folder(self, None, self._on_folder_selected)

    def _on_folder_selected(self, dialog, result) -> None:
        try:
            folder = dialog.select_folder_finish(result)
        except GLib.Error:
            return
        if folder is not None and folder.get_path():
            self.load_paths([folder.get_path()])

    def _action_refresh(self, *_args) -> None:
        if self._current_path:
            self.load_paths([self._current_path])

    def _action_activate_item(self, *_args) -> None:
        item = self.view.cursor_item()
        if item is None:
            selected = self.view.selected_items()
            item = selected[0] if selected else None
        if item is not None:
            self._open_viewer(item)

    def _action_open_external(self, *_args) -> None:
        items = self.view.selected_items()
        item = items[0] if items else self.view.cursor_item()
        if item is None:
            return
        try:
            Gio.AppInfo.launch_default_for_uri(GLib.filename_to_uri(item.path, None), None)
        except GLib.Error:
            self._toast("Не удалось открыть изображение в другом приложении")

    def _action_select_all(self, *_args) -> None:
        if len(self.model):
            self.view.select_all()

    def _action_clear_selection(self, *_args) -> None:
        self.view.clear_selection()

    def _action_clear_search(self, *_args) -> None:
        self._search_entry.set_text("")
        self._search_button.set_active(False)
        self._apply_search()

    def _action_copy_path(self, *_args) -> None:
        items = self.view.selected_items()
        if not items:
            return
        text = "\n".join(item.path for item in items)
        clipboard = self.get_clipboard()
        clipboard.set(text)
        self._toast(
            "Путь скопирован" if len(items) == 1 else f"Скопировано путей: {len(items)}"
        )

    def _action_show_in_files(self, *_args) -> None:
        items = self.view.selected_items()
        if not items:
            return
        folder = items[0].folder
        try:
            Gio.AppInfo.launch_default_for_uri(GLib.filename_to_uri(folder, None), None)
        except GLib.Error:
            self._toast("Не удалось открыть файловый менеджер")

    def _action_escape(self, *_args) -> None:
        if self._search_bar.get_search_mode():
            self._action_clear_search()
            return
        self.view.clear_selection()

    def _action_shortcuts(self, *_args) -> None:
        present_shortcuts(self)

    def _action_sort(self, action, parameter) -> None:
        if parameter is None:
            return
        value = parameter.get_string()
        if value not in dict(SORT_MODES):
            return
        action.set_state(parameter)
        self._sort_key = value
        self.settings.set_string("sort-mode", value)
        self.model.set_sort(value)
        self._after_reorder()

    def _action_thumb_size(self, action, parameter) -> None:
        if parameter is None:
            return
        action.set_state(parameter)
        self._apply_thumb_key(parameter.get_string())

    def _apply_thumb_key(self, key: str, save: bool = True) -> None:
        if key not in THUMB_SIZES:
            return
        self._thumb_key = key
        self.view.set_min_col_width(THUMB_SIZES[key])
        if save:
            self.settings.set_string("thumb-size", key)

    def _action_zoom_in(self, *_args) -> None:
        index = THUMB_ORDER.index(self._thumb_key) if self._thumb_key in THUMB_ORDER else 1
        new_key = THUMB_ORDER[min(len(THUMB_ORDER) - 1, index + 1)]
        self._sync_thumb_action(new_key)

    def _action_zoom_out(self, *_args) -> None:
        index = THUMB_ORDER.index(self._thumb_key) if self._thumb_key in THUMB_ORDER else 1
        new_key = THUMB_ORDER[max(0, index - 1)]
        self._sync_thumb_action(new_key)

    def _sync_thumb_action(self, key: str) -> None:
        action = self.lookup_action("thumb-size")
        if action is not None:
            action.set_state(GLib.Variant.new_string(key))
        self._apply_thumb_key(key)

    def _on_zoom_request(self, delta: int) -> None:
        if delta > 0:
            self._action_zoom_in()
        else:
            self._action_zoom_out()

    # =========================================================== загрузка
    def is_loading(self) -> bool:
        return self._loading

    def _set_view_controls_visible(self, visible: bool) -> None:
        for widget in (self._search_button, self._sort_button, self._size_button):
            widget.set_visible(visible)

    def start(self) -> None:
        """Первый экран: последняя папка, а если её нет — приветствие."""
        last = self.settings.get_string("last-folder")
        if last and os.path.isdir(last):
            self.load_paths([last])
        else:
            self.show_landing()

    def show_landing(self) -> None:
        self._status_page.set_icon_name("folder-pictures-symbolic")
        self._status_page.set_title("Откройте папку с фотографиями")
        self._status_page.set_description(
            "Перетащите папку в окно или выберите её на диске — "
            "миниатюры появятся сразу"
        )
        self._status_page.set_child(self._open_button_large)
        self._stack.set_visible_child_name("status")
        self._current_path = None
        # на приветственном экране сортировать и искать нечего
        self._set_view_controls_visible(False)
        self._update_title()

    def load_paths(self, paths: list[str]) -> None:
        if not paths:
            return
        target = os.path.abspath(os.path.expanduser(paths[0]))
        if not os.path.exists(target):
            self._toast(f"Путь не найден: {target}")
            return
        if os.path.isfile(target):
            target = os.path.dirname(target)

        self._current_path = target
        self._cached_view = False
        self._loading = True
        self._load_started = time.monotonic()
        self._first_paint_at = 0.0
        self.model.clear()
        self.view.reset()
        self._scroller.get_vadjustment().set_value(0.0)
        self._spinner.set_visible(True)
        self._status_page.set_icon_name("folder-pictures-symbolic")
        self._status_page.set_title("Индексация папки…")
        self._status_page.set_description(target)
        self._status_page.set_child(self._status_spinner)
        self._status_spinner.set_visible(True)
        self._stack.set_visible_child_name("status")
        self._set_view_controls_visible(True)
        self._update_title()

        self.settings.set_string("last-folder", target)
        self.model.load(
            [target],
            on_batch=self._on_batch,
            on_done=self._on_done,
            on_cached=self._on_cached,
        )

    def _on_cached(self, items) -> None:
        """Индекс из дискового кэша: сетку показываем сразу, без ожидания обхода.

        Обход каталога при этом продолжается в фоне — если состав папки
        изменился, `_on_done` заменит список целиком.
        """
        self._cached_view = True
        self.model.push_batch(items)
        self.model.finish_load()
        self.view.reset()
        self._stack.set_visible_child_name("grid")
        self._update_title()
        if os.environ.get("HERMITAGE_TIMING"):
            print(f"[эрмитаж] индекс из кэша: {len(items)} файлов за "
                  f"{time.monotonic() - self._load_started:.2f} c", flush=True)

    def _on_batch(self, batch, done: int) -> None:
        self.model.push_batch(batch)
        self.view.append_items()
        if len(self.model) and self._stack.get_visible_child_name() != "grid":
            self._stack.set_visible_child_name("grid")
        if not self._first_paint_at:
            self._first_paint_at = time.monotonic()
            if os.environ.get("HERMITAGE_TIMING"):
                print(f"[эрмитаж] первые плитки на экране: "
                      f"{self._first_paint_at - self._load_started:.2f} c", flush=True)
        self._update_title()

    def _on_done(self, replace=None, count: int = 0) -> None:
        self._loading = False
        if replace is not None:
            # папка изменилась с прошлого запуска — показываем свежий список
            self.model.clear()
            self.model.push_batch(replace)
            self.view.reset()
            if os.environ.get("HERMITAGE_TIMING"):
                print(f"[эрмитаж] кэш устарел, список заменён: {len(replace)} файлов",
                      flush=True)
        cached_view, self._cached_view = self._cached_view, False
        # модель — долгоживущие объекты: выносим их из-под циклического GC,
        # чтобы он не перебирал десятки тысяч элементов на каждой сборке
        gc.collect()
        gc.freeze()
        self._spinner.set_visible(False)
        self._status_spinner.set_visible(False)
        self.model.finish_load()
        self.view.reset()

        if len(self.model) == 0:
            self._status_page.set_icon_name("folder-open-symbolic")
            self._status_page.set_title("Здесь нет изображений")
            self._status_page.set_description(
                "Поддерживаются JPEG, PNG, WebP, GIF, TIFF, AVIF, HEIC и SVG"
            )
            self._status_page.set_child(None)
            self._stack.set_visible_child_name("status")
        else:
            self._stack.set_visible_child_name("grid")
            self.view.grab_focus()
            # тост только когда действительно шёл обход, а не показ из кэша
            if count > 400 and not cached_view:
                self._toast(f"Проиндексировано {plural_images(count)}", timeout=3)
        self._update_title()
        if os.environ.get("HERMITAGE_TIMING"):
            print(f"[эрмитаж] индексация {count} файлов: "
                  f"{time.monotonic() - self._load_started:.2f} c", flush=True)

    def _on_drop(self, target, value, x, y) -> bool:
        paths: list[str] = []
        try:
            files = value.get_files()
        except AttributeError:
            files = []
        for file in files:
            path = file.get_path()
            if path:
                paths.append(path)
        if not paths:
            return False
        self.load_paths(paths)
        return True

    # ========================================================== интерфейс
    def _on_search_changed(self, entry) -> None:
        if self._search_source:
            GLib.source_remove(self._search_source)
            self._search_source = 0
        self._search_source = GLib.timeout_add(140, self._apply_search)

    def _apply_search(self) -> bool:
        self._search_source = 0
        query = self._search_entry.get_text()
        self.model.set_filter(query)
        self._scroller.get_vadjustment().set_value(0.0)
        self._after_reorder()
        if len(self.model) == 0 and len(self.model.all_items):
            self._status_page.set_icon_name("edit-find-symbolic")
            self._status_page.set_title("Ничего не найдено")
            self._status_page.set_description(f"По запросу «{query}» изображений нет")
            self._status_page.set_child(self._reset_search_button)
            self._stack.set_visible_child_name("status")
        elif len(self.model):
            self._stack.set_visible_child_name("grid")
        elif self._current_path:
            self._status_page.set_icon_name("folder-open-symbolic")
            self._status_page.set_title("Здесь нет изображений")
            self._status_page.set_child(None)
            self._stack.set_visible_child_name("status")
        return False

    def _after_reorder(self) -> None:
        self.view.reset()
        self.view.invalidate()
        self._update_title()

    def _on_selection_changed(self, items) -> None:
        self._set_selection_actions(bool(items))
        self._update_title()

    def _update_title(self) -> None:
        if self._current_path:
            name = os.path.basename(self._current_path.rstrip("/")) or "Эрмитаж"
            self._window_title.set_title(name)
        else:
            self._window_title.set_title("Эрмитаж")

        total = len(self.model)
        filtered = len(self.model.all_items)
        if self.model.get_filter() and filtered != total:
            subtitle = f"{total} из {filtered} изображений"
        elif total:
            subtitle = plural_images(total)
        else:
            subtitle = "" if not self._current_path else plural_images(0)
        selected = self.view.selection_count()
        # «выбрано 1» ничего не сообщает: рамка плитки уже видна. Счётчик
        # нужен только для группового выбора (Ctrl+клик, Shift+клик, Ctrl+A)
        if selected > 1:
            subtitle += (" · " if subtitle else "") + f"выбрано {selected}"
        self._window_title.set_subtitle(subtitle)

    def _toast(self, text: str, timeout: int = 2) -> None:
        toast = Adw.Toast.new(text)
        toast.set_timeout(timeout)
        self._toast_overlay.add_toast(toast)

    # ============================================================ события
    def _on_context_menu(self, item, tile) -> None:
        bounds = None
        try:
            result = tile.compute_bounds(self._scroller)
            # PyGObject отдаёт (bool, Graphene.Rect) — поддерживаем оба варианта
            if isinstance(result, tuple):
                if result and result[0]:
                    bounds = result[1]
            else:
                bounds = result
        except Exception:  # noqa: BLE001
            bounds = None
        if bounds is not None:
            rect = Gdk.Rectangle()
            rect.x = int(bounds.origin.x)
            rect.y = int(bounds.origin.y)
            rect.width = int(bounds.size.width)
            rect.height = int(bounds.size.height)
            self._context_popover.set_pointing_to(rect)
        self._context_popover.popup()

    def _open_viewer(self, item) -> None:
        index = self.model.index_of(item.path)
        if index < 0:
            return
        if self._viewer is not None:
            self._viewer.close()
        self._viewer = ViewerWindow(
            self,
            self.loader,
            self.model.items,
            index,
            on_index_changed=self._on_viewer_index_changed,
        )
        self._viewer.connect("close-request", self._on_viewer_closed)
        self._viewer.present()

    def _on_viewer_index_changed(self, item) -> None:
        self.view.select_path(item.path, scroll=True)

    def _on_viewer_closed(self, *_args) -> bool:
        self._viewer = None
        return False

    def _on_key(self, controller, keyval, keycode, state) -> bool:
        if self._search_bar.get_search_mode():
            return False
        focus = self.get_focus()
        if isinstance(focus, (Gtk.Entry, Gtk.SearchEntry, Gtk.Text)):
            return False
        # Если фокус на кнопке шапки, Enter/Space должны нажимать именно её —
        # иначе клавиатурная навигация по интерфейсу ломается. А когда фокус
        # в сетке (обычное состояние), Enter/Space открывают изображение.
        if focus is not None and focus is not self.view:
            return False
        if keyval in (Gdk.KEY_Left, Gdk.KEY_Right, Gdk.KEY_Up, Gdk.KEY_Down):
            if keyval == Gdk.KEY_Left:
                self.view.move_cursor(-1, 0)
            elif keyval == Gdk.KEY_Right:
                self.view.move_cursor(1, 0)
            elif keyval == Gdk.KEY_Up:
                self.view.move_cursor(0, -1)
            else:
                self.view.move_cursor(0, 1)
            return True
        if keyval in (Gdk.KEY_Return, Gdk.KEY_KP_Enter, Gdk.KEY_space):
            # Enter/Space открывают изображение, а не папку
            if self.view.cursor_item() is not None:
                self._action_activate_item()
                return True
            return False
        return False

    # ============================================================ закрытие
    def save_window_state(self) -> None:
        """Запоминает размер окна (вызывается и при закрытии, и при выходе)."""
        width, height = self.get_width(), self.get_height()
        if width > 0 and height > 0 and not self.is_maximized():
            self.settings.set_int("window-width", width)
            self.settings.set_int("window-height", height)

    def do_close_request(self) -> bool:  # noqa: N802
        if self._search_source:
            GLib.source_remove(self._search_source)
            self._search_source = 0
        self.model.cancel_load()
        self.save_window_state()
        return False
