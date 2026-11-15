#!/usr/bin/env bash
# Сборка «Эрмитажа» в один установочный файл Flatpak (.flatpak bundle).
#
#   ./tools/build-flatpak.sh
#   → dist/Ermitage-1.0.0-x86_64.flatpak
#
# Скрипт только СОБИРАЕТ: он ничего не устанавливает и не подключает
# пользователю репозитории — ни флатуб, ни локальный. Установку делает либо
# системный установщик (GNOME Software / KDE Discover / «Центр приложений»),
# либо `flatpak install`, и уже на стороне пользователя, а не сборки.
#
# Что нужно на машине сборки: flatpak, flatpak-builder и рантайм+SDK
# (org.gnome.Platform//51, org.gnome.Sdk//51). Рантайм ставится один раз:
#
#   flatpak remote-add --if-not-exists flathub https://flathub.org/repo/flathub.flatpakrepo
#   flatpak install org.gnome.Sdk//51 org.gnome.Platform//51
#
set -euo pipefail
cd "$(dirname "$0")/.."

APP_ID="io.github.ermitage.Ermitage"
BRANCH="${BRANCH:-stable}"      # ветку задаёт сборка: в манифесте она лишняя
ARCH="${ARCH:-x86_64}"
MANIFEST="flatpak/${APP_ID}.json"
REPO="dist/flatpak-repo"        # локальный ostree-репозиторий сборки (промежуточный)
BUILD_DIR="build-flatpak"
STATE_DIR=".flatpak-builder-state"

VERSION=$(sed -n "s/^__version__ = \"\(.*\)\"/\1/p" src/masonry_gallery/__init__.py)
: "${VERSION:=1.0.0}"
BUNDLE="dist/Ermitage-${VERSION}-${ARCH}.flatpak"

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

echo "==> сборка $APP_ID $VERSION (ветка $BRANCH, $ARCH)"
flatpak-builder \
  --force-clean \
  --default-branch="$BRANCH" \
  --disable-rofiles-fuse \
  --state-dir="$STATE_DIR" \
  --repo="$REPO" \
  --arch="$ARCH" \
  "$BUILD_DIR" "$MANIFEST"

rm -f "$BUNDLE"
echo "==> упаковка в $BUNDLE"

# --runtime-repo вшивает в бандл ссылку на flathub: если у пользователя ещё нет
# рантайма GNOME 51, установщик сам предложит его скачать. Это единственное
# место, где вообще упоминается репозиторий, — и оно работает на стороне
# установки, а не сборки.
flatpak build-bundle \
  --arch="$ARCH" \
  --runtime-repo=https://flathub.org/repo/flathub.flatpakrepo \
  "$REPO" "$BUNDLE" "$APP_ID" "$BRANCH"

echo
echo "готово: $BUNDLE ($(du -h "$BUNDLE" | cut -f1))"
echo "установка:"
echo "  двойной клик по файлу в файловом менеджере (GNOME Software / Discover), либо"
echo "  flatpak install $BUNDLE"
echo "запуск:"
echo "  flatpak run $APP_ID"
