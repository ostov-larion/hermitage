"""Настройки приложения.

Основной бэкенд — GSettings (схема `io.github.ermitage.Ermitage`): так делают
все приложения GNOME, и настройки попадают в dconf. Но приложение должно
работать и «из исходников», где схема ещё не установлена, поэтому если схемы
нет — те же ключи читаются и пишутся в
`$XDG_CONFIG_HOME/ermitage/settings.json`.
"""
from __future__ import annotations

import json
import os

import gi

gi.require_version("Gio", "2.0")
from gi.repository import Gio, GLib  # noqa: E402

SCHEMA_ID = "io.github.ermitage.Ermitage"

DEFAULTS = {
    "last-folder": "",
    "sort-mode": "name",
    "thumb-size": "m",
    "color-scheme": "system",
    "window-width": 1280,
    "window-height": 840,
}


class Settings:
    """Небольшая обёртка: одинаковый API поверх GSettings и JSON-файла."""

    def __init__(self) -> None:
        self._gs = None
        try:
            source = Gio.SettingsSchemaSource.get_default()
            schema = source.lookup(SCHEMA_ID, True) if source is not None else None
            if schema is not None:
                self._gs = Gio.Settings.new_full(schema, None, None)
        except Exception:  # noqa: BLE001 — работаем и без схемы
            self._gs = None

        self._path = os.path.join(GLib.get_user_config_dir(), "ermitage", "settings.json")
        self._data: dict = {} if self._gs is not None else self._read()

    # ------------------------------------------------------------------ служебное
    @property
    def backend(self) -> str:
        return "gsettings" if self._gs is not None else "json"

    def _read(self) -> dict:
        try:
            with open(self._path, "r", encoding="utf-8") as handle:
                data = json.load(handle)
            return data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            return {}

    def _write(self) -> None:
        if self._gs is not None:
            return
        try:
            os.makedirs(os.path.dirname(self._path), exist_ok=True)
            with open(self._path, "w", encoding="utf-8") as handle:
                json.dump(self._data, handle, ensure_ascii=False, indent=2)
        except OSError:
            pass

    # ------------------------------------------------------------------ строки
    def get_string(self, key: str) -> str:
        if self._gs is not None:
            return self._gs.get_string(key)
        value = self._data.get(key, DEFAULTS[key])
        return value if isinstance(value, str) else DEFAULTS[key]

    def set_string(self, key: str, value: str) -> None:
        if self._gs is not None:
            self._gs.set_string(key, value)
            return
        if self._data.get(key) != value:
            self._data[key] = value
            self._write()

    # --------------------------------------------------------------- булевы
    def get_boolean(self, key: str) -> bool:
        if self._gs is not None:
            return self._gs.get_boolean(key)
        value = self._data.get(key, DEFAULTS[key])
        return bool(value)

    def set_boolean(self, key: str, value: bool) -> None:
        if self._gs is not None:
            self._gs.set_boolean(key, bool(value))
            return
        if self._data.get(key) != bool(value):
            self._data[key] = bool(value)
            self._write()

    # --------------------------------------------------------------- целые
    def get_int(self, key: str) -> int:
        if self._gs is not None:
            return self._gs.get_int(key)
        value = self._data.get(key, DEFAULTS[key])
        return value if isinstance(value, int) else int(DEFAULTS[key])

    def set_int(self, key: str, value: int) -> None:
        if self._gs is not None:
            self._gs.set_int(key, int(value))
            return
        if self._data.get(key) != int(value):
            self._data[key] = int(value)
            self._write()
