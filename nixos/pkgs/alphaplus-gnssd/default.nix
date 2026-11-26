{ lib
, stdenv
, meson
, ninja
, pkg-config
, curl
, glib
, json-glib
, libqmi
, libqrtr-glib
, systemd
}:

stdenv.mkDerivation {
  pname = "alphaplus-gnssd";
  version = "0.1.12";

  src = ./gnss;

  postPatch = ''
    substituteInPlace src/gnss-xtra.c src/gnss-assist.c \
      --replace-fail '"/usr/bin/curl"' '"${lib.getExe curl}"'
  '';

  nativeBuildInputs = [
    meson
    ninja
    pkg-config
  ];

  mesonFlags = [
    "-Dsystemd_units=false"
  ];

  buildInputs = [
    glib
    json-glib
    libqmi
    libqrtr-glib
    systemd
    curl
  ];

  meta = with lib; {
    description = "SM8150 Qualcomm GNSS daemon for LM-G820N";
    license = licenses.gpl2Plus;
    platforms = platforms.linux;
  };
}
