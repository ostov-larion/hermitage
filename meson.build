project(
  'ermitage',
  version: '1.0.0',
  meson_version: '>= 0.63.0',
  default_options: ['prefix=/usr/local'],
)

py = import('python').find_installation('python3')
moduledir = py.get_install_dir()

# python-пакет
install_subdir(
  'src/masonry_gallery',
  install_dir: moduledir,
  exclude_files: ['*.pyc', '__pycache__/*', '**/__pycache__/*'],
)

# launcher
conf = configuration_data()
conf.set('PYTHON', py.full_path())
# куда именно ставится пакет — важно для Flatpak: там префикс /app, и
# site-packages не всегда попадает в sys.path системного python
conf.set('MODULEDIR', moduledir)
configure_file(
  input: 'build-aux/ermitage.in',
  output: 'ermitage',
  configuration: conf,
  install_dir: get_option('bindir'),
  install_mode: 'rwxr-xr-x',
)

# схема GSettings: последняя папка, сортировка, размер плиток, тёмная тема
gnome = import('gnome')
gnome.compile_schemas(depend_files: 'data/io.github.ermitage.Ermitage.gschema.xml')
install_data(
  'data/io.github.ermitage.Ermitage.gschema.xml',
  install_dir: get_option('datadir') / 'glib-2.0' / 'schemas',
)

# данные приложения
install_data(
  'data/io.github.ermitage.Ermitage.desktop',
  install_dir: get_option('datadir') / 'applications',
)
install_data(
  'data/io.github.ermitage.Ermitage.metainfo.xml',
  install_dir: get_option('datadir') / 'metainfo',
)
install_data(
  'data/io.github.ermitage.Ermitage.svg',
  install_dir: get_option('datadir') / 'icons' / 'hicolor' / 'scalable' / 'apps',
)

# инструменты разработчика
install_data('tools/make_samples.py', 'tools/bench.py', install_dir: get_option('datadir') / 'ermitage' / 'tools')

# обязательные шаги после установки: без них в Flatpak (префикс /app) не будет
# скомпилированной схемы GSettings, кэша иконок и базы .desktop
gnome.post_install(
  glib_compile_schemas: true,
  update_desktop_database: true,
  gtk_update_icon_cache: true,
)

message('Эрмитаж ' + meson.project_version() + ' → префикс ' + get_option('prefix'))
