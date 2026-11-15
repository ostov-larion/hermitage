#!/usr/bin/env bash
# Запуск «Эрмитажа» из исходников, без установки.
#
#   ./run.sh                  # последняя открытая папка или приветствие
#   ./run.sh ~/Фото           # открыть конкретный каталог
#   ERMITAGE_TIMING=1 ./run.sh ~/Фото      # с замерами индексации
#   ERMITAGE_CACHE_MB=96 ./run.sh ~/Фото   # меньше кэш текстур
set -euo pipefail
cd "$(dirname "$0")"
export PYTHONPATH="${PYTHONPATH:+$PYTHONPATH:}src"

# Схема GSettings нужна для сохранения последней папки, сортировки и размера
# плиток. Если она не установлена в систему — компилируем локальную копию и
# подсовываем её через GSETTINGS_SCHEMA_DIR (без этого настройки ушли бы в
# JSON-файл вместо dconf).
SCHEMA_SRC="data/io.github.ermitage.Ermitage.gschema.xml"
SCHEMA_DIR="${XDG_CACHE_HOME:-$HOME/.cache}/ermitage/schemas"
if [ -f "$SCHEMA_SRC" ] && command -v glib-compile-schemas >/dev/null 2>&1; then
  if [ ! -f "$SCHEMA_DIR/gschemas.compiled" ] || [ "$SCHEMA_SRC" -nt "$SCHEMA_DIR/gschemas.compiled" ]; then
    mkdir -p "$SCHEMA_DIR"
    cp "$SCHEMA_SRC" "$SCHEMA_DIR/"
    glib-compile-schemas "$SCHEMA_DIR" 2>/dev/null || true
  fi
  [ -f "$SCHEMA_DIR/gschemas.compiled" ] && export GSETTINGS_SCHEMA_DIR="$SCHEMA_DIR"
fi

# ищем интерпретатор, в котором есть PyGObject с GTK4 и libadwaita
for candidate in "${ERMITAGE_PYTHON:-python3}" /usr/bin/python3 python3.13 python3.12 python3.11 python3.10; do
  if command -v "$candidate" >/dev/null 2>&1 &&
     "$candidate" -c "import gi; gi.require_version('Gtk','4.0'); gi.require_version('Adw','1')" 2>/dev/null; then
    exec "$candidate" -m masonry_gallery "$@"
  fi
done

echo "Не найден Python с PyGObject (GTK4 + libadwaita)." >&2
echo "Debian/Ubuntu: sudo apt install python3-gi gir1.2-gtk-4.0 gir1.2-adw-1" >&2
echo "Fedora:        sudo dnf install python3-gobject gtk4 libadwaita" >&2
exit 1
