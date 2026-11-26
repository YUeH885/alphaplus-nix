{ lib
, stdenv
, buildPackages
, fetchzip
, meson
, ninja
, pkg-config
, glib
, libqmi
, protobufc
}:

stdenv.mkDerivation {
  pname = "libssc";
  version = "0.4.4";

  src = fetchzip {
    url = "https://salsa.debian.org/DebianOnMobile-team/libssc/-/archive/debian/0.4.4-2/libssc-debian-0.4.4-2.tar.gz";
    hash = "sha256-Y3PwO76/ARirBRgMbtSfLfQ1XuQmyXMwQgMOIA4JaY4=";
  };

  patches = [
    # Debian base patches
    "debian/patches/sensor-Remove-trailing-whitespace.patch"
    "debian/patches/sensor-Use-g_clear_signal_handler.patch"
    "debian/patches/sensor-Give-up-when-server-never-becomes-available.patch"

    # LM-G820N device & sensor enhancements
    ./patches/0001-handle-multilevel-proximity-events.patch
    ./patches/0002-correct-gyroscope-cli-output.patch
    ./patches/0003-use-sensor-main-context-and-keep-alive.patch
    ./patches/0004-log-request-errors-before-transferring-ownership.patch
    ./patches/0005-discard-unreliable-measurements.patch
    ./patches/0006-apply-online-vector-calibration.patch
    ./patches/0007-find-multiarch-vala-gir.patch
    ./patches/0008-support-cpp-sensor-clients.patch
    ./patches/0009-retry-transient-sensor-discovery.patch
  ];

  nativeBuildInputs = [
    meson
    ninja
    pkg-config
    buildPackages.protobufc
    buildPackages.protobuf
  ];

  propagatedBuildInputs = [
    glib
    libqmi
    protobufc
  ];

  meta = with lib; {
    description = "Library to expose Qualcomm Snapdragon Sensor Core sensors";
    homepage = "https://codeberg.org/DylanVanAssche/libssc";
    license = licenses.gpl3Plus;
    platforms = platforms.linux;
  };
}
