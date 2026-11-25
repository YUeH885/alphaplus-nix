test_nmea = executable(
  'test-nmea',
  'test-nmea.c',
  '../src/gnss-nmea.c',
  include_directories: inc_src,
  dependencies: [glib_dep, gio_dep],
)
test('test-nmea', test_nmea)

test_state = executable(
  'test-state',
  'test-state.c',
  '../src/gnss-state.c',
  include_directories: inc_src,
  dependencies: [glib_dep],
)
test('test-state', test_state)

test_server = executable(
  'test-server',
  'test-server.c',
  include_directories: inc_src,
  dependencies: [glib_dep, gio_dep, gio_unix_dep, qmi_glib_dep, qrtr_glib_dep],
)
test('test-server', test_server)

test_xtra = executable(
  'test-xtra',
  'test-xtra.c',
  '../src/gnss-xtra.c',
  '../src/gnss-request.c',
  include_directories: inc_src,
  dependencies: [glib_dep, gio_dep, gio_unix_dep, qmi_glib_dep, qrtr_glib_dep],
)
test('test-xtra', test_xtra)


test_request = executable(
  'test-request',
  'test-request.c',
  '../src/gnss-request.c',
  include_directories: inc_src,
  dependencies: [glib_dep, gio_dep, qmi_glib_dep, qrtr_glib_dep],
)
test('test-request', test_request)

test_daemon = executable(
  'test-daemon',
  'test-daemon.c',
  '../src/gnss-state.c',
  '../src/gnss-nmea.c',
  '../src/gnss-server.c',
  include_directories: inc_src,
  dependencies: [glib_dep, gio_dep, gio_unix_dep, qmi_glib_dep, qrtr_glib_dep, systemd_dep],
)
test('test-daemon', test_daemon)

test_loc = executable(
  'test-loc',
  'test-loc.c',
  '../src/gnss-request.c',
  '../src/gnss-xtra.c',
  include_directories: inc_src,
  dependencies: [glib_dep, gio_dep, qmi_glib_dep, qrtr_glib_dep],
)
test('test-loc', test_loc)
