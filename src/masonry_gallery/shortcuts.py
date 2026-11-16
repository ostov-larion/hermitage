"""Окно горячих клавиш (строится из XML через Gtk.Builder)."""
from __future__ import annotations

import gi

gi.require_version("Gtk", "4.0")
from gi.repository import Gtk  # noqa: E402

from .gtktext import russify  # noqa: E402

SHORTCUTS_XML = """<?xml version="1.0" encoding="UTF-8"?>
<interface>
  <object class="GtkShortcutsWindow" id="shortcuts-window">
    <property name="modal">True</property>
    <property name="title">Горячие клавиши</property>
    <child>
      <object class="GtkShortcutsSection">
        <property name="section-name">gallery</property>
        <property name="title">Эрмитаж</property>
        <child>
          <object class="GtkShortcutsGroup">
            <property name="title">Файлы</property>
            <child>
              <object class="GtkShortcutsShortcut">
                <property name="title">Открыть папку</property>
                <property name="accelerator">&lt;Control&gt;o</property>
              </object>
            </child>
            <child>
              <object class="GtkShortcutsShortcut">
                <property name="title">Обновить</property>
                <property name="accelerator">F5</property>
              </object>
            </child>
            <child>
              <object class="GtkShortcutsShortcut">
                <property name="title">Скопировать изображение</property>
                <property name="accelerator">&lt;Control&gt;c</property>
              </object>
            </child>
            <child>
              <object class="GtkShortcutsShortcut">
                <property name="title">Скопировать путь</property>
                <property name="accelerator">&lt;Control&gt;&lt;Shift&gt;c</property>
              </object>
            </child>
            <child>
              <object class="GtkShortcutsShortcut">
                <property name="title">Открыть с помощью…</property>
                <property name="accelerator">&lt;Control&gt;&lt;Shift&gt;o</property>
              </object>
            </child>
          </object>
        </child>
        <child>
          <object class="GtkShortcutsGroup">
            <property name="title">Навигация по сетке</property>
            <child>
              <object class="GtkShortcutsShortcut">
                <property name="title">Стрелки</property>
                <property name="subtitle">Перейти к соседней плитке</property>
                <property name="accelerator">Left Right Up Down</property>
              </object>
            </child>
            <child>
              <object class="GtkShortcutsShortcut">
                <property name="title">Открыть изображение</property>
                <property name="accelerator">Return space</property>
              </object>
            </child>
            <child>
              <object class="GtkShortcutsShortcut">
                <property name="title">Выбрать все</property>
                <property name="accelerator">&lt;Control&gt;a</property>
              </object>
            </child>
            <child>
              <object class="GtkShortcutsShortcut">
                <property name="title">Снять выбор / закрыть поиск</property>
                <property name="accelerator">Escape</property>
              </object>
            </child>
          </object>
        </child>
        <child>
          <object class="GtkShortcutsGroup">
            <property name="title">Вид</property>
            <child>
              <object class="GtkShortcutsShortcut">
                <property name="title">Поиск</property>
                <property name="accelerator">&lt;Control&gt;f</property>
              </object>
            </child>
            <child>
              <object class="GtkShortcutsShortcut">
                <property name="title">Размер плиток</property>
                <property name="subtitle">Ctrl + колесо мыши</property>
                <property name="accelerator">&lt;Control&gt;plus &lt;Control&gt;minus</property>
              </object>
            </child>
            <child>
              <object class="GtkShortcutsShortcut">
                <property name="title">Тёмное оформление</property>
                <property name="accelerator">&lt;Control&gt;d</property>
              </object>
            </child>
          </object>
        </child>
        <child>
          <object class="GtkShortcutsGroup">
            <property name="title">Просмотрщик</property>
            <child>
              <object class="GtkShortcutsShortcut">
                <property name="title">Следующее / предыдущее</property>
                <property name="accelerator">Right Left</property>
              </object>
            </child>
            <child>
              <object class="GtkShortcutsShortcut">
                <property name="title">Масштаб</property>
                <property name="subtitle">Вписать / 1:1</property>
                <property name="accelerator">0 1</property>
              </object>
            </child>
            <child>
              <object class="GtkShortcutsShortcut">
                <property name="title">Во весь экран</property>
                <property name="accelerator">F11</property>
              </object>
            </child>
            <child>
              <object class="GtkShortcutsShortcut">
                <property name="title">Закрыть</property>
                <property name="accelerator">Escape</property>
              </object>
            </child>
          </object>
        </child>
      </object>
    </child>
  </object>
</interface>
"""


def present_shortcuts(parent) -> None:
    builder = Gtk.Builder()
    try:
        builder.add_from_string(SHORTCUTS_XML)
    except Exception:  # noqa: BLE001 — окно подсказок не критично для работы
        return
    window = builder.get_object("shortcuts-window")
    if window is None:
        return
    # заголовок окна живёт во внутреннем Gtk.Stack внутри headerbar GTK,
    # поэтому его мало задать свойством — подписи правим уже после сборки
    window.set_title("Горячие клавиши")
    russify(window)
    window.set_transient_for(parent)
    # держим ссылку, иначе Gtk.Builder-объект может быть собран GC
    parent._shortcuts_window = window  # noqa: SLF001
    window.present()
