project('alphaplus-gnssd', 'c',
  version: '0.1.12',
  default_options: [
    'c_std=gnu11',
    'warning_level=2',
  ],
)

glib_dep = dependency('glib-2.0', version: '>= 2.62')
gio_dep = dependency('gio-2.0', version: '>= 2.56')
gio_unix_dep = dependency('gio-unix-2.0', version: '>= 2.56')
json_glib_dep = dependency('json-glib-1.0')
qmi_glib_dep = dependency('qmi-glib', version: '>= 1.38.0')
qrtr_glib_dep = dependency('qrtr-glib', version: '>= 1.4.0')
systemd_dep = dependency('libsystemd')
m_dep = meson.get_compiler('c').find_library('m', required : false)
add_project_arguments('-DDAEMON_VERSION="' + meson.project_version() + '"', language: 'c')

inc_src = include_directories('src')

sources = files(
  'src/gnss-nmea.c',
  'src/gnss-state.c',
  'src/gnss-request.c',
  'src/gnss-server.c',
  'src/gnss-assist.c',
  'src/gnss-xtra.c',
  'src/gnss-loc.c',
  'src/main.c',
)

executable(
  'alphaplus-gnssd',
  sources,
  include_directories: inc_src,
  dependencies: [
    glib_dep,
    gio_dep,
    gio_unix_dep,
    json_glib_dep,
    qmi_glib_dep,
    qrtr_glib_dep,
    systemd_dep,
    m_dep,
  ],
  install: true,
  install_dir: get_option('bindir'),
)

if get_option('systemd_units')
  install_data(
    'sysusers.d/alphaplus-gnss.conf',
    install_dir: 'lib/sysusers.d',
  )

  install_data(
    'systemd/alphaplus-gnssd.service',
    install_dir: 'lib/systemd/system',
  )
endif

subdir('tests')
