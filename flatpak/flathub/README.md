# Отправка в Flathub

Этот каталог — заготовка для [flathub/flathub](https://github.com/flathub/flathub):
манифест собирает приложение **из вашего git-репозитория по тегу**, а не из
локального каталога (локальный вариант лежит в `flatpak/` и нужен только для
повседневной сборки бандла).

## 1. Замените заглушки на свои

`io.github.ermitage.*` — это схема `<обратный домен>.<Имя>`, и для GitHub
Flathub требует, чтобы владелец из app-id совпадал с вашим аккаунтом.
Поэтому сначала:

```bash
./tools/setup-app-id.sh io.github.<ваш-логин>.Ermitage https://github.com/<ваш-логин>/ermitage
```

Скрипт переименует файлы метаданных, поправит app-id, id и путь схемы
GSettings, шапку манифеста и URL в метаинформации.

## 2. Опубликуйте исходники

```bash
git add -A && git commit -m "…"
git tag -a v1.0.0 -m "Эрмитаж 1.0.0"     # тег обязателен: манифест ссылается на него
git remote add origin https://github.com/<ваш-логин>/ermitage.git
git push -u origin main --tags
```

После пуша возьмите **полный** хеш тега и подставьте его в поле `commit`
в `flatpak/flathub/io.github.ermitage.Ermitage.json`:

```bash
git rev-parse v1.0.0^{commit}
```

## 3. Проверьте локально, как это соберёт Flathub

```bash
SRC_URL=file://$PWD ./tools/build-flatpak.sh --from-git
flatpak-builder-lint manifest flatpak/flathub/io.github.ermitage.Ermitage.json
flatpak-builder-lint appstream build-flatpak/files/share/metainfo/*.metainfo.xml
appstreamcli validate --explain data/io.github.<логин>.Ermitage.metainfo.xml
desktop-file-validate data/io.github.<логин>.Ermitage.desktop
```

## 4. Pull request

1. Форк `flathub/flathub`, ветка от `new-pr` (не от `master`).
2. Скопируйте туда `io.github.<логин>.Ermitage.json` в корень репозитория.
3. `git commit -m "New app: io.github.<логин>.Ermitage"`, пуш, PR в `flathub:new-pr`.
4. В PR-шаблоне отметьте, что приложение использует GNOME runtime, и
   дождитесь проверки бота `flathub-bot` (он соберёт x86_64 и aarch64).

## Требования, которые уже выполнены

* источник — `git` с тегом **и** зафиксированным `commit`;
* app-id в имени манифеста и внутри совпадает;
* `metadata_license` = MIT, `project_license` = MIT (как в LICENSE);
* AppStream: `<launchable>`, `<developer>`, `<content_rating>` OARS 1.1,
  `<releases>` с датой, скриншоты и `url` — по схеме; `appstreamcli validate`
  проходит без ошибок;
* `.desktop` проходит `desktop-file-validate`, `StartupWMClass` совпадает с
  app-id, иконка ставится как `share/icons/hicolor/scalable/apps/<app-id>.svg`;
* `finish-args` без `--filesystem=host` и без `--socket=session-bus`;
* манифест без `branch` (его задаёт Flathub), с `cleanup` от мусора сборки.

Что останется только вам: реальные URL репозитория и скриншотов (заглушки
`github.com/ermitage/ermitage` до публикации дают предупреждения
`url-not-reachable` — в CI Flathub это падение, локально это лишь warning).
