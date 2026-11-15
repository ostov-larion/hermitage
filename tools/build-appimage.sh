#!/usr/bin/env bash
# Сборка самодостаточного AppImage «Эрмитажа».
#
# Внутрь упаковывается весь стек: CPython + PyGObject, GTK4, libadwaita,
# gdk-pixbuf с загрузчиками, typelib'ы, GSettings-схемы и тема иконок Adwaita.
# На машине пользователя не требуется ничего, кроме glibc, видеодрайвера
# и шрифтов.
#
#   ./tools/build-appimage.sh              # соберёт dist/Hermitage-x86_64.AppImage
#   APPIMAGE_COMPRESSION=xz ./tools/build-appimage.sh
#
set -euo pipefail

ROOT=$(cd "$(dirname "$0")/.." && pwd)
BUILD="$ROOT/build"
APPDIR="$BUILD/AppDir"
DIST="$ROOT/dist"
ARCH=${ARCH:-x86_64}
LIBDIR="/usr/lib/$ARCH-linux-gnu"

COMPRESSION=${APPIMAGE_COMPRESSION:-zstd}
APP_ID="io.github.hermitage.Hermitage"
OUT="$DIST/Hermitage-$ARCH.AppImage"

# --- интерпретатор с PyGObject ------------------------------------------------
PYTHON=""
for candidate in /usr/bin/python3 python3; do
  if command -v "$candidate" >/dev/null 2>&1 &&
     "$candidate" -c "import gi; gi.require_version('Gtk','4.0'); gi.require_version('Adw','1')" 2>/dev/null; then
    PYTHON=$(readlink -f "$(command -v "$candidate")")
    break
  fi
done
[ -n "$PYTHON" ] || { echo "не найден Python с PyGObject" >&2; exit 1; }

PYVER=$("$PYTHON" -c 'import sys; print("%d.%d" % sys.version_info[:2])')
STDLIB="/usr/lib/python$PYVER"
DIST_PACKAGES="/usr/lib/python3/dist-packages"

echo "==> Python $PYVER ($PYTHON)"
echo "==> AppDir: $APPDIR"

rm -rf "$APPDIR"
mkdir -p "$APPDIR/usr/bin" "$APPDIR/usr/lib" "$APPDIR/usr/share"
mkdir -p "$DIST"

# --- 1. CPython + стандартная библиотека -------------------------------------
cp "$PYTHON" "$APPDIR/usr/bin/python3"
ln -sf python3 "$APPDIR/usr/bin/python$PYVER"

PYVER_SHORT="${PYVER//./}"   # 3.13 → 313, как в суффиксе .so
echo "==> стандартная библиотека"
# Копируем stdlib без тестов, IDE и GUI-тулкитов: они занимают больше половины
# объёма и приложению не нужны.
STDLIB_EXCLUDES=(
  --exclude='*/__pycache__'
  --exclude="usr/lib/python$PYVER/test"
  --exclude="usr/lib/python$PYVER/idlelib"
  --exclude="usr/lib/python$PYVER/tkinter"
  --exclude="usr/lib/python$PYVER/lib2to3"
  --exclude="usr/lib/python$PYVER/ensurepip"
  --exclude="usr/lib/python$PYVER/turtledemo"
  --exclude="usr/lib/python$PYVER/pydoc_data"
  --exclude="usr/lib/python$PYVER/unittest"
  --exclude="usr/lib/python$PYVER/venv"
  --exclude="usr/lib/python$PYVER/distutils"
  --exclude="usr/lib/python$PYVER/sqlite3"
  --exclude="usr/lib/python$PYVER/curses"
  --exclude="usr/lib/python$PYVER/multiprocessing"
  --exclude="usr/lib/python$PYVER/lib-dynload/_tkinter"*
)
tar -C / -cf - "${STDLIB_EXCLUDES[@]}" "usr/lib/python$PYVER" | tar -C "$APPDIR" -xf -

# --- 2. PyGObject и само приложение ------------------------------------------
echo "==> PyGObject"
mkdir -p "$APPDIR/usr/lib/python3/dist-packages"
cp -a "$DIST_PACKAGES/gi" "$APPDIR/usr/lib/python3/dist-packages/"
find "$APPDIR/usr/lib/python3/dist-packages" -name "__pycache__" -type d -exec rm -rf {} + 2>/dev/null || true

echo "==> исходники приложения"
cp -a "$ROOT/src/masonry_gallery" "$APPDIR/usr/lib/python3/dist-packages/"
find "$APPDIR/usr/lib/python3/dist-packages/masonry_gallery" -name "__pycache__" -type d -exec rm -rf {} + 2>/dev/null || true

# --- 3. typelib'ы -------------------------------------------------------------
echo "==> typelib"
mkdir -p "$APPDIR/usr/lib/girepository-1.0"
cp -a "$LIBDIR/girepository-1.0/." "$APPDIR/usr/lib/girepository-1.0/"

# --- 4. gdk-pixbuf: загрузчики и утилита генерации кэша -----------------------
echo "==> gdk-pixbuf"
PIXBUF_LOADERS="$APPDIR/usr/lib/gdk-pixbuf-2.0/2.10.0/loaders"
mkdir -p "$PIXBUF_LOADERS"
cp -a "$LIBDIR/gdk-pixbuf-2.0/2.10.0/loaders/." "$PIXBUF_LOADERS/"
if [ -x /usr/bin/gdk-pixbuf-query-loaders ]; then
  cp /usr/bin/gdk-pixbuf-query-loaders "$APPDIR/usr/bin/"
else
  LOADER_TOOL=$(ls "$LIBDIR"/gdk-pixbuf-2.0/gdk-pixbuf-query-loaders 2>/dev/null | head -1 || true)
  [ -n "$LOADER_TOOL" ] && cp "$LOADER_TOOL" "$APPDIR/usr/bin/gdk-pixbuf-query-loaders"
fi

# --- 5. GSettings-схемы -------------------------------------------------------
echo "==> GSettings"
mkdir -p "$APPDIR/usr/share/glib-2.0/schemas"
# копируем все .xml: кроме *.gschema.xml там лежат определения enum'ов
# (org.gnome.desktop.enums.xml) и override'ы
cp -a /usr/share/glib-2.0/schemas/*.xml "$APPDIR/usr/share/glib-2.0/schemas/" 2>/dev/null || true
cp -a /usr/share/glib-2.0/schemas/*.override "$APPDIR/usr/share/glib-2.0/schemas/" 2>/dev/null || true
# схема самого приложения: без неё не сохраняются папка, сортировка и размер
cp "$ROOT/data/io.github.hermitage.Hermitage.gschema.xml" "$APPDIR/usr/share/glib-2.0/schemas/"
glib-compile-schemas "$APPDIR/usr/share/glib-2.0/schemas"

# --- 6. иконки ----------------------------------------------------------------
echo "==> иконки Adwaita"
mkdir -p "$APPDIR/usr/share/icons"
if [ -d /usr/share/icons/Adwaita ]; then
  # курсоры (12 МБ) не нужны: их берём у системы, а символические иконки — свои
  tar -C /usr/share/icons -cf - --exclude="Adwaita/cursors" --exclude="*.icon-theme.cache" Adwaita \
    | tar -C "$APPDIR/usr/share/icons" -xf -
fi
mkdir -p "$APPDIR/usr/share/icons/hicolor/scalable/apps"
cp "$ROOT/data/$APP_ID.svg" "$APPDIR/usr/share/icons/hicolor/scalable/apps/"

# --- 6b. переводы (без .mo строки libadwaita/GTK останутся английскими) -----
echo "==> переводы ru"
for mo in libadwaita gtk40 glib20 gdk-pixbuf; do
  src="/usr/share/locale/ru/LC_MESSAGES/$mo.mo"
  if [ -f "$src" ]; then
    mkdir -p "$APPDIR/usr/share/locale/ru/LC_MESSAGES"
    cp "$src" "$APPDIR/usr/share/locale/ru/LC_MESSAGES/"
  fi
done

# --- 7. замыкание библиотек ---------------------------------------------------
echo "==> библиотеки (рекурсивно по ldd)"
STAGE="$APPDIR/usr/lib"
# эти библиотеки обязаны быть системными: их нельзя тащить в AppImage
SKIP_RE='^(ld-linux[^ ]*|libc\.so[^ ]*|libc-[0-9][^ ]*|libm\.so[^ ]*|libm-[0-9][^ ]*|libmvec[^ ]*|libpthread[^ ]*|librt\.so[^ ]*|libdl\.so[^ ]*|libutil\.so[^ ]*|libanl\.so[^ ]*|libresolv[^ ]*|libnss_[^ ]*|libnsl[^ ]*|libgcc_s[^ ]*|libstdc\+\+[^ ]*|libGL[^ ]*|libEGL[^ ]*|libGLX[^ ]*|libGLdispatch[^ ]*|libOpenGL[^ ]*|libdrm[^ ]*|libgbm[^ ]*|libvulkan[^ ]*|libwayland-egl[^ ]*|libglapi[^ ]*|libLLVM[^ ]*)'

declare -A COPIED=()
QUEUE=(
  "$PYTHON"
  # суффикс расширений CPython пишется без точки: cpython-313, а не cpython-3.13
  "$APPDIR/usr/lib/python3/dist-packages/gi/_gi.cpython-${PYVER//./}-$ARCH-linux-gnu.so"
)
for loader in "$PIXBUF_LOADERS"/*.so; do QUEUE+=("$loader"); done
# модули расширений CPython тоже подгружаются по dlopen
for module in "$APPDIR/usr/lib/python$PYVER/lib-dynload/"*.so; do QUEUE+=("$module"); done
# GTK и libadwaita подгружаются по typelib-метаданным, а не через линковку,
# поэтому их нужно положить в AppDir явно (и уже от них идти по зависимостям)
for seed in libgtk-4.so.1 libadwaita-1.so.0 libgdk_pixbuf-2.0.so.0; do
  if [ -e "$LIBDIR/$seed" ]; then
    # -L: в Debian soname — это симлинк на файл с версией; без -L внутри
    # AppImage остался бы битый симлинк, и загрузчик взял бы библиотеку хоста
    cp -aL "$LIBDIR/$seed" "$STAGE/$seed"
    COPIED[$seed]=1
    QUEUE+=("$LIBDIR/$seed")
  fi
done

while [ ${#QUEUE[@]} -gt 0 ]; do
  current="${QUEUE[0]}"
  QUEUE=("${QUEUE[@]:1}")
  [ -e "$current" ] || continue
  while read -r resolved; do
    [ -n "$resolved" ] && [ -e "$resolved" ] || continue
    name=$(basename "$resolved")
    [[ $name =~ $SKIP_RE ]] && continue
    if [ -z "${COPIED[$name]:-}" ]; then
      COPIED[$name]=1
      cp -aL "$resolved" "$STAGE/$name"
      QUEUE+=("$resolved")
    fi
  done < <(ldd "$current" 2>/dev/null | awk '/=> \//{print $3}')
done
echo "    скопировано библиотек: ${#COPIED[@]}"
BROKEN=$(find "$STAGE" -maxdepth 1 -xtype l | wc -l)
if [ "$BROKEN" -ne 0 ]; then
  echo "ОШИБКА: в $STAGE остались битые симлинки ($BROKEN) — библиотеки уедут с хоста" >&2
  find "$STAGE" -maxdepth 1 -xtype l >&2
  exit 1
fi

# --- 8. AppRun и launcher -----------------------------------------------------
echo "==> AppRun"
cat > "$APPDIR/usr/bin/hermitage" <<'LAUNCHER'
#!/bin/sh
exec python3 -m masonry_gallery "$@"
LAUNCHER
chmod +x "$APPDIR/usr/bin/hermitage"

cat > "$APPDIR/AppRun" <<'APPRUN'
#!/bin/sh
# Точка входа AppImage: готовит окружение и запускает приложение.
set -e
HERE=$(dirname "$(readlink -f "$0")")

export PATH="$HERE/usr/bin:$PATH"
export PYTHONHOME="$HERE/usr"
export PYTHONPATH="$HERE/usr/lib/python3/dist-packages"
export LD_LIBRARY_PATH="$HERE/usr/lib${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}"
export GI_TYPELIB_PATH="$HERE/usr/lib/girepository-1.0"
export GSETTINGS_SCHEMA_DIR="$HERE/usr/share/glib-2.0/schemas"
export XDG_DATA_DIRS="$HERE/usr/share:${XDG_DATA_DIRS:-/usr/local/share:/usr/share}"
export PYTHONDONTWRITEBYTECODE=1
# Mesa/драйверы GL не входят в образ (их нельзя тащить за собой), поэтому по
# умолчанию рисуем через cairo — он не зависит от драйверов. Кто хочет GL:
# GSK_RENDERER=ngl ./Hermitage-x86_64.AppImage
export GSK_RENDERER="${GSK_RENDERER:-cairo}"
# галерея не проигрывает видео: без этого GTK тянет модуль GStreamer с хоста
export GTK_MEDIA="${GTK_MEDIA:-none}"
# dconf в образе нет (нет и демона шины настроек) — GSettings хранит ключи в
# обычном ini-файле, поэтому последняя папка и настройки вида сохраняются
export GSETTINGS_BACKEND="${GSETTINGS_BACKEND:-keyfile}"

# Кэш загрузчиков gdk-pixbuf содержит абсолютные пути, а точка монтирования
# AppImage каждый раз новая — поэтому кэш собирается при запуске.
export GDK_PIXBUF_MODULEDIR="$HERE/usr/lib/gdk-pixbuf-2.0/2.10.0/loaders"
if command -v gdk-pixbuf-query-loaders >/dev/null 2>&1; then
  CACHE=$(mktemp -t pixbuf-loaders.XXXXXX.cache) || CACHE=""
  if [ -n "$CACHE" ]; then
    gdk-pixbuf-query-loaders > "$CACHE" 2>/dev/null && export GDK_PIXBUF_MODULE_FILE="$CACHE"
  fi
fi

exec "$HERE/usr/bin/hermitage" "$@"
APPRUN
chmod +x "$APPDIR/AppRun"

# --- 9. метаданные AppImage ---------------------------------------------------
mkdir -p "$APPDIR/usr/share/metainfo"
cp "$ROOT/data/$APP_ID.metainfo.xml" "$APPDIR/usr/share/metainfo/"
cp "$ROOT/data/$APP_ID.desktop" "$APPDIR/$APP_ID.desktop"
cp "$ROOT/data/$APP_ID.svg" "$APPDIR/$APP_ID.svg"
# appimagetool ждёт растровую .DirIcon — рендерим 256×256 из svg
if command -v rsvg-convert >/dev/null 2>&1; then
  rsvg-convert -w 256 -h 256 "$ROOT/data/$APP_ID.svg" -o "$APPDIR/.DirIcon"
elif command -v convert >/dev/null 2>&1; then
  convert -background none -density 300 "$ROOT/data/$APP_ID.svg" -resize 256x256 "$APPDIR/.DirIcon"
else
  ln -sf "$APP_ID.svg" "$APPDIR/.DirIcon"
fi
mkdir -p "$APPDIR/usr/share/icons/hicolor/256x256/apps"
[ -f "$APPDIR/.DirIcon" ] && cp "$APPDIR/.DirIcon" "$APPDIR/usr/share/icons/hicolor/256x256/apps/$APP_ID.png" 2>/dev/null || true

# --- 10. сборка образа --------------------------------------------------------
APPIMAGETOOL="$BUILD/appimagetool"
if [ ! -x "$APPIMAGETOOL" ]; then
  echo "==> скачиваю appimagetool"
  curl -sL -o "$APPIMAGETOOL" \
    "https://github.com/AppImage/appimagetool/releases/download/continuous/appimagetool-$ARCH.AppImage"
  chmod +x "$APPIMAGETOOL"
fi

echo "==> appimagetool ($COMPRESSION)"
export ARCH
# Собираем во временный файл и подменяем готовый: если старая сборка сейчас
# запущена, писать прямо в неё нельзя («Text file busy»).
TMP_OUT="$OUT.new"
# APPIMAGE_EXTRACT_AND_RUN — чтобы сборщик работал и без FUSE
if ! APPIMAGE_EXTRACT_AND_RUN=1 "$APPIMAGETOOL" \
      --compression "$COMPRESSION" "$APPDIR" "$TMP_OUT" 2>"$BUILD/appimagetool.log"; then
  echo "    повтор без метаданных AppStream"
  APPIMAGE_EXTRACT_AND_RUN=1 "$APPIMAGETOOL" --no-appstream "$APPDIR" "$TMP_OUT"
fi
mv -f "$TMP_OUT" "$OUT"

echo
echo "готово: $OUT ($(du -h "$OUT" | cut -f1))"
echo "запуск: $OUT ~/Фото"
