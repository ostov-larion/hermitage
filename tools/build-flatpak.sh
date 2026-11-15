#!/usr/bin/env bash
# Сборка и установка Flatpak-пакета «Эрмитаж» (локально, в профиль пользователя).
#
#   ./tools/build-flatpak.sh            # собрать и установить
#   ./tools/build-flatpak.sh --run      # собрать, установить и запустить
#
# Нужны flatpak и flatpak-builder:
#   Debian/Ubuntu: sudo apt install flatpak flatpak-builder
#   Fedora:        sudo dnf install flatpak flatpak-builder
# и рантайм GNOME (скачается при первой сборке, ~1 ГБ):
#   flatpak remote-add --if-not-exists --user flathub \
#     https://flathub.org/repo/flathub.flatpakrepo
set -euo pipefail
cd "$(dirname "$0")/.."

APP_ID="io.github.ermitage.Ermitage"
MANIFEST="flatpak/${APP_ID}.json"

if ! command -v flatpak-builder >/dev/null 2>&1; then
  echo "Не найден flatpak-builder. Debian/Ubuntu: sudo apt install flatpak flatpak-builder" >&2
  exit 1
fi

flatpak remote-add --if-not-exists --user flathub \
  https://flathub.org/repo/flathub.flatpakrepo

flatpak-builder --user --install --force-clean \
  --state-dir=.flatpak-builder-state \
  --repo=dist/flatpak-repo \
  build-flatpak "$MANIFEST"

echo "Установлено. Запуск: flatpak run $APP_ID"
if [ "${1:-}" = "--run" ]; then
  exec flatpak run "$APP_ID"
fi
