"""Приложение «Эрмитаж» — Gtk.Application."""
from __future__ import annotations

import os

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gdk, Gio, GLib, Gtk  # noqa: E402

from . import __version__  # noqa: E402
from .gtktext import russify  # noqa: E402
from .settings import Settings  # noqa: E402
from .thumbnails import shared_loader  # noqa: E402
from .window import GalleryWindow  # noqa: E402

APP_ID = "io.github.hermitage.Hermitage"


class HermitageApplication(Adw.Application):
    def __init__(self) -> None:
        super().__init__(
            application_id=APP_ID,
            flags=Gio.ApplicationFlags.HANDLES_OPEN
            | Gio.ApplicationFlags.NON_UNIQUE,
        )
        self.set_version(__version__)
        self._window: GalleryWindow | None = None
        self._loader = shared_loader()
        self.settings = Settings()

    # ---------------------------------------------------------- жизненный цикл
    def do_startup(self) -> None:  # noqa: N802
        Adw.Application.do_startup(self)
        self._load_css()
        self._install_actions()
        self._install_accels()

        style_manager = Adw.StyleManager.get_default()
        style_manager.connect("notify::dark", self._on_theme_changed)
        self.set_color_scheme(self.settings.get_string("color-scheme"), save=False)

    def do_activate(self) -> None:  # noqa: N802
        window = self._ensure_window()
        window.present()
        # первый экран — последняя открытая папка, иначе StatusPage с
        # предложением выбрать папку (а не содержимое каталога запуска)
        if not window.model.all_items and not window.is_loading():
            window.start()

    def do_shutdown(self) -> None:  # noqa: N802
        if self._window is not None:
            # Ctrl+Q выходит мимо close-request — размер окна сохраняем здесь
            self._window.save_window_state()
        self._loader.shutdown()
        Adw.Application.do_shutdown(self)

    def do_open(self, files, n_files, hint) -> None:  # noqa: N802
        window = self._ensure_window()
        paths = []
        for file in files:
            path = file.get_path()
            if path:
                paths.append(path)
        if paths:
            window.load_paths(paths)
        window.present()

    # ------------------------------------------------------------------ окно
    def _ensure_window(self) -> GalleryWindow:
        if self._window is None:
            self._window = GalleryWindow(self, self._loader, self.settings)
        return self._window

    # ------------------------------------------------------------------- css
    def _load_css(self) -> None:
        display = Gdk.Display.get_default()
        if display is None:
            return
        provider = Gtk.CssProvider()
        css_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "style.css")
        try:
            if os.path.exists(css_path):
                provider.load_from_path(css_path)
            else:  # резерв: CSS внутри GResource (при установке через Flatpak)
                provider.load_from_resource("/io/github/hermitage/Hermitage/style.css")
        except GLib.Error as error:
            print(f"CSS не загружен: {error.message}")
            return
        Gtk.StyleContext.add_provider_for_display(
            display, provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION
        )

    # --------------------------------------------------------------- действия
    def _install_actions(self) -> None:
        quit_action = Gio.SimpleAction.new("quit", None)
        quit_action.connect("activate", lambda *_: self.quit())
        self.add_action(quit_action)

        about_action = Gio.SimpleAction.new("about", None)
        about_action.connect("activate", self._on_about)
        self.add_action(about_action)

        # состояние оформления: system / light / dark — его показывает
        # переключатель в главном меню (Adw.ToggleGroup)
        scheme_action = Gio.SimpleAction.new_stateful(
            "color-scheme",
            GLib.VariantType.new("s"),
            GLib.Variant.new_string(self.settings.get_string("color-scheme")),
        )
        scheme_action.connect("activate", self._on_color_scheme)
        self.add_action(scheme_action)

        toggle_dark = Gio.SimpleAction.new("toggle-dark", None)
        toggle_dark.connect("activate", self._on_toggle_dark)
        self.add_action(toggle_dark)

    def _install_accels(self) -> None:
        for action, accels in {
            "win.open": ["<Control>o"],
            "win.refresh": ["<Control>r", "F5"],
            "win.select-all": ["<Control>a"],
            "win.escape": ["Escape"],
            "win.copy-path": ["<Control><Shift>c"],
            "win.activate-item": ["<Control>Return"],
            "win.zoom-in": ["<Control>plus", "<Control>equal"],
            "win.zoom-out": ["<Control>minus"],
            "win.shortcuts": ["<Control>question"],
            "app.toggle-dark": ["<Control>d"],
            "win.open-external": ["<Control><Shift>o"],
            "app.quit": ["<Control>q"],
        }.items():
            self.set_accels_for_action(action, accels)

    SCHEMES = {
        "system": Adw.ColorScheme.PREFER_LIGHT,
        "light": Adw.ColorScheme.FORCE_LIGHT,
        "dark": Adw.ColorScheme.FORCE_DARK,
    }

    def set_color_scheme(self, name: str, save: bool = True) -> None:
        """Единая точка смены оформления: StyleManager + настройки + UI."""
        if name not in self.SCHEMES:
            name = "system"
        Adw.StyleManager.get_default().set_color_scheme(self.SCHEMES[name])
        if save:
            self.settings.set_string("color-scheme", name)
        action = self.lookup_action("color-scheme")
        if action is not None:
            action.set_state(GLib.Variant.new_string(name))
        if self._window is not None:
            self._window.sync_color_scheme(name)

    def _on_color_scheme(self, _action, parameter) -> None:
        if parameter is not None:
            self.set_color_scheme(parameter.get_string())

    def _on_toggle_dark(self, *_args) -> None:
        dark = not Adw.StyleManager.get_default().get_dark()
        self.set_color_scheme("dark" if dark else "light")

    def _on_theme_changed(self, *_args) -> None:
        if self._window is not None:
            self._window.view.refresh_palette()

    def _on_about(self, *_args) -> None:
        about = Adw.AboutDialog(
            application_name="Эрмитаж",
            application_icon=APP_ID,
            version=__version__,
            developer_name="Сообщество «Эрмитаж»",
            comments=(
                "Быстрая галерея изображений с masonry-сеткой. "
                "Виртуализированный список, асинхронные миниатюры, "
                "собственный GtkLayoutManager-free контейнер."
            ),
            license_type=Gtk.License.MIT_X11,
        )
        about.add_legal_section(
            "Технологии",
            "GTK 4 · libadwaita · PyGObject · GdkPixbuf",
            Gtk.License.MIT_X11,
            None,
        )
        about.present(self._window)

        # подписи «Details»/«Legal»/«Version» рисует libadwaita из собственных
        # каталогов; строки создаются при первом показе, поэтому переводим их
        # следующим проходом цикла событий
        def _apply_ru() -> bool:
            russify(about)
            return GLib.SOURCE_REMOVE

        GLib.timeout_add(50, _apply_ru)
