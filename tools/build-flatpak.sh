#!/usr/bin/env bash
# Сборка «Эрмитажа» в один установочный файл Flatpak (.flatpak bundle).
#
#   ./tools/build-flatpak.sh                  # из рабочего каталога → dist/*.flatpak
#   ./tools/build-flatpak.sh --from-git       # как соберёт Flathub: из git по тегу
#   SRC_URL=file://$PWD ./tools/build-flatpak.sh --from-git   # то же, без сети
#
# Скрипт только СОБИРАЕТ: он ничего не устанавливает и не подключает
# пользователю репозитории — ни flathub для самого приложения, ни локальный.
# Установку делает либо системный установщик (GNOME Software / KDE Discover /
# «Центр приложений»), либо `flatpak install` — на стороне пользователя.
#
# На машине сборки нужны flatpak, flatpak-builder и (один раз) рантайм:
#
#   flatpak install flathub org.gnome.Platform//51 org.gnome.Sdk//51
#
set -euo pipefail
cd "$(dirname "$0")/.."

APP_ID="io.github.hermitage.Hermitage"
BRANCH="${BRANCH:-stable}"       # в манифесте ветки нет: её задаёт сборка
ARCH="${ARCH:-x86_64}"
REPO="dist/flatpak-repo"         # локальный ostree-репозиторий (промежуточный)
BUILD_DIR="build-flatpak"
STATE_DIR=".flatpak-builder-state"
MODE="dir"

for arg in "$@"; do
  case "$arg" in
    --from-git) MODE="git" ;;
    *) echo "неизвестный аргумент: $arg" >&2; exit 2 ;;
  esac
done

MANIFEST="flatpak/${APP_ID}.json"                    # источник: рабочий каталог
[ "$MODE" = git ] && MANIFEST="flatpak/flathub/${APP_ID}.json"   # как на Flathub

VERSION=$(sed -n "s/^__version__ = \"\(.*\)\"/\1/p" src/masonry_gallery/__init__.py)
: "${VERSION:=1.2.1}"
BUNDLE="dist/Hermitage-${VERSION}-${ARCH}.flatpak"

for tool in flatpak flatpak-builder; do
  command -v "$tool" >/dev/null 2>&1 || {
    echo "Не найден $tool. Debian/Ubuntu: sudo apt install flatpak flatpak-builder" >&2
    exit 1
  }
done

if ! flatpak info "runtime/org.gnome.Sdk//51" >/dev/null 2>&1; then
  echo "Нет рантайма org.gnome.Sdk//51. Поставьте его один раз:" >&2
  echo "  flatpak install flathub org.gnome.Sdk//51 org.gnome.Platform//51" >&2
  exit 1
fi

# SRC_URL позволяет проверить «флатубную» сборку без публикации репозитория:
# url в манифесте подменяется на локальный клон (file://…).
EFFECTIVE_MANIFEST="$MANIFEST"
if [ -n "${SRC_URL:-}" ]; then
  EFFECTIVE_MANIFEST="$(mktemp --suffix=.json)"
  python3 - "$MANIFEST" "$EFFECTIVE_MANIFEST" "$SRC_URL" <<'PY'
import json, sys
src, dst, url = sys.argv[1], sys.argv[2], sys.argv[3]
data = json.load(open(src))
for module in data.get("modules", []):
    for source in module.get("sources", []):
        if source.get("type") == "git":
            source["url"] = url
json.dump(data, open(dst, "w"), ensure_ascii=False, indent=2)
PY
  echo "==> источник подменён на $SRC_URL"
fi

echo "==> сборка $APP_ID $VERSION (ветка $BRANCH, $ARCH, режим $MODE)"
flatpak-builder \
  --force-clean \
  --disable-rofiles-fuse \
  --default-branch="$BRANCH" \
  --state-dir="$STATE_DIR" \
  --repo="$REPO" \
  --arch="$ARCH" \
  "$BUILD_DIR" "$EFFECTIVE_MANIFEST"

rm -f "$BUNDLE"
echo "==> упаковка в $BUNDLE"

# --runtime-repo вшивает в бандл ссылку на flathub: если у получателя нет
# рантайма GNOME, установщик сам предложит его скачать. Это единственное
# упоминание репозитория — и оно работает на стороне установки.
flatpak build-bundle \
  --arch="$ARCH" \
  --runtime-repo=https://flathub.org/repo/flathub.flatpakrepo \
  "$REPO" "$BUNDLE" "$APP_ID" "$BRANCH"

echo
echo "готово: $BUNDLE ($(du -h "$BUNDLE" | cut -f1))"
echo "установка: двойной клик по файлу (GNOME Software / Discover) либо"
echo "           flatpak install $BUNDLE"
echo "запуск:    flatpak run $APP_ID"
