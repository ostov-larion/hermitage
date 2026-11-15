"""Перевод строк, которые GTK и libadwaita берут из своих собственных каталогов.

Приложение целиком русское, но часть подписей приходит не из наших файлов:
заголовок окна горячих клавиш, пункты «Details»/«Legal» в окне «О программе»,
кнопки «Show All», «Website», «Report an Issue» и т. п. При наличии русской
локали в системе они переводятся через .mo-файлы (они кладутся в AppImage), но
если локаль не русская (или минимальный контейнер без locale-данных), остаются
английскими. Здесь мы просто подменяем известные строки.
"""
from __future__ import annotations

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gtk  # noqa: E402

STRINGS_RU = {
    # окно горячих клавиш
    "Shortcuts": "Горячие клавиши",
    "Search Results": "Результаты поиска",
    "Search Shortcuts": "Поиск по клавишам",
    "Show All": "Показать все",
    "No Results Found": "Ничего не найдено",
    "Try a different search": "Попробуйте другой запрос",
    # окно «О программе»
    "Details": "Подробности",
    "Legal": "Правовая информация",
    "Website": "Сайт",
    "Report an Issue": "Сообщить о проблеме",
    "Credits and thanks": "Благодарности",
    "Acknowledgements": "Благодарности",
    "License": "Лицензия",
    "Translate": "Перевод",
    "Copy Version": "Скопировать версию",
    "Version": "Версия",
    "Search": "Поиск",
    "Close": "Закрыть",
    "Open Menu": "Меню",
    "Main Menu": "Меню",
}

_ROW_TYPES = (Adw.ActionRow, Adw.ButtonRow, Adw.PreferencesRow)


def russify(widget) -> int:
    """Рекурсивно заменяет известные английские подписи на русские.

    Возвращает число заменённых строк (пригодится в отладке).
    """
    replaced = 0
    stack = [widget]
    while stack:
        node = stack.pop()
        try:
            if isinstance(node, Gtk.Label):
                text = node.get_text()
                if text in STRINGS_RU:
                    node.set_text(STRINGS_RU[text])
                    replaced += 1
            elif isinstance(node, (Adw.ActionRow, Adw.ButtonRow, Adw.PreferencesRow)):
                # у строк заголовок хранится в свойстве, а не в Gtk.Label
                try:
                    title = node.get_title()
                except Exception:  # noqa: BLE001
                    title = None
                if title in STRINGS_RU:
                    node.set_title(STRINGS_RU[title])
                    replaced += 1
            elif isinstance(node, Gtk.SearchEntry):
                placeholder = node.get_placeholder_text()
                if placeholder in STRINGS_RU:
                    node.set_placeholder_text(STRINGS_RU[placeholder])
                    replaced += 1
                inner = node.get_child()
                if inner is not None:
                    stack.append(inner)
                continue
        except Exception:  # noqa: BLE001 — косметика, не повод падать
            pass
        child = node.get_first_child()
        while child is not None:
            stack.append(child)
            child = child.get_next_sibling()
    return replaced
