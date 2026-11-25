{ lib
, stdenv
, buildPackages
, pipewire
, meson
, ninja
, pkg-config
, dbus
, glib
, bluez
, sbc
, liblc3
, modemmanager
, libusb1
, libcamera-alphaplus
}:

stdenv.mkDerivation {
  pname = "pipewire-spa-alphaplus";
  inherit (pipewire) version src;

  patches = (pipewire.patches or [ ]) ++ [
    ./patches/0001-offload-transport-ready.patch
  ];

  strictDeps = true;

  depsBuildBuild = [ buildPackages.stdenv.cc ];

  nativeBuildInputs = [
    meson
    ninja
    pkg-config
    buildPackages.python3
    buildPackages.glib.dev
  ];

  buildInputs = [
    dbus
    glib
    bluez
    sbc
    liblc3
    modemmanager
    libusb1
    libcamera-alphaplus
  ];

  mesonAutoFeatures = "disabled";

  mesonFlags = [
    "-Dspa-plugins=enabled"
    "-Ddbus=enabled"
    "-Dbluez5=enabled"
    "-Dbluez5-backend-hsp-native=enabled"
    "-Dbluez5-backend-hfp-native=enabled"
    "-Dbluez5-backend-native-mm=enabled"
    "-Dbluez5-backend-ofono=enabled"
    "-Dbluez5-backend-hsphfpd=enabled"
    "-Dbluez5-codec-lc3=enabled"
    "-Dlibusb=enabled"
    "-Dlibcamera=enabled"
    "-Dsession-managers="
  ];

  ninjaFlags = [
    "spa/plugins/bluez5/libspa-bluez5.so"
    "spa/plugins/libcamera/libspa-libcamera.so"
  ];

  installPhase = ''
    runHook preInstall
    install -Dm755 spa/plugins/bluez5/libspa-bluez5.so \
      "$out/lib/spa-0.2/alphaplus/libspa-bluez5.so"
    install -Dm644 ../spa/plugins/bluez5/bluez-hardware.conf \
      "$out/share/spa-0.2/bluez5/bluez-hardware.conf"
    install -Dm755 spa/plugins/libcamera/libspa-libcamera.so \
      "$out/lib/spa-0.2/libcamera/libspa-libcamera.so"
    runHook postInstall
  '';

  meta = with lib; {
    description = "LM-G820N PipeWire SPA plugins for Bluetooth SCO offload and libcamera";
    license = licenses.mit;
    platforms = platforms.linux;
  };
}
