#!/usr/bin/env bash
# Проверка того, что уйдёт в Flathub: манифест, AppStream, .desktop.
#
#   ./tools/lint-flatpak.sh            # после ./tools/build-flatpak.sh --from-git
#
set -uo pipefail
cd "$(dirname "$0")/.."

MANIFEST="flatpak/flathub/io.github.ermitage.Ermitage.json"
METAINFO="build-flatpak/files/share/metainfo/io.github.ermitage.Ermitage.metainfo.xml"
DESKTOP="build-flatpak/files/share/applications/io.github.ermitage.Ermitage.desktop"

# flatpak-builder-lint нужен gi (AppStream, OSTree), поэтому запускаем его
# системным python; если он поставлен в site-packages другого интерпретатора —
# подсказываем путь через PYTHONPATH.
lint() {
  if command -v flatpak-builder-lint >/dev/null 2>&1 &&
     flatpak-builder-lint --version >/dev/null 2>&1; then
    flatpak-builder-lint "$@"
  else
    PYTHONPATH="${LINT_SITE_PACKAGES:-/usr/local/lib/python3.13/site-packages}" \
      /usr/bin/python3 -m flatpak_builder_lint.cli "$@"
  fi
}

status=0
echo "== flatpak-builder-lint manifest"
lint manifest "$MANIFEST" || status=1

if [ -f "$METAINFO" ]; then
  echo "== flatpak-builder-lint appstream"
  lint appstream "$METAINFO" || status=1
  echo "== appstreamcli validate"
  appstreamcli validate --explain "$METAINFO" || status=1
else
  echo "== сборки нет: сначала ./tools/build-flatpak.sh --from-git"
fi

if [ -f "$DESKTOP" ]; then
  echo "== desktop-file-validate"
  desktop-file-validate "$DESKTOP" || status=1
fi

if [ "$status" -eq 0 ]; then
  echo "== замечаний нет (кроме заглушек github-URL, пока репозиторий не опубликован)"
fi
exit "$status"
