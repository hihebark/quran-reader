datadir    = get_option('datadir')
app_id     = 'io.github.hihebark.QuranReader'
app_data   = datadir / app_id

install_data(
  app_id + '.desktop',
  install_dir: datadir / 'applications',
)

install_data(
  app_id + '.metainfo.xml',
  install_dir: datadir / 'metainfo',
)

install_subdir(
  'icons',
  install_dir: datadir,
)

install_data(
  'mushaf-qatar-layout.db',
  'quran-text.db',
  install_dir: app_data,
)

install_subdir(
  'quran-pages',
  install_dir: app_data,
)

install_data(
  app_id + '.service',
  install_dir: datadir / 'dbus-1' / 'services',
)
