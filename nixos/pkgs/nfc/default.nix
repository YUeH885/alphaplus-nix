{ lib, stdenv, buildPackages, pkg-config, qt6 }:

stdenv.mkDerivation {
  pname = "alphaplus-nfc";
  version = "1.0.0";
  src = ./.;
  strictDeps = true;
  dontWrapQtApps = true;

  nativeBuildInputs = [ pkg-config ];
  buildInputs = [ qt6.qtbase qt6.qtdeclarative ];

  buildPhase = ''
    runHook preBuild
    qt_cflags="$("$PKG_CONFIG" --cflags Qt6Qml Qt6DBus Qt6Gui)"
    qt_libs="$("$PKG_CONFIG" --libs Qt6Qml Qt6DBus Qt6Gui)"
    ${lib.getBin buildPackages.qt6.qtbase}/libexec/moc $qt_cflags \
      nfc.cpp -o nfc.moc
    "$CXX" -O2 -std=c++17 -fPIC -shared $qt_cflags \
      nfc.cpp $qt_libs -o libnfcplugin.so
    runHook postBuild
  '';

  installPhase = ''
    runHook preInstall
    applet="$out/share/plasma/plasmoids/org.alphaplus.nfc"
    mkdir -p "$applet"
    cp -r applet/. "$applet/"
    install -Dm755 libnfcplugin.so "$applet/contents/ui/backend/libnfcplugin.so"
    install -Dm644 icons/alphaplus-nfc-active-symbolic.svg \
      "$out/share/icons/hicolor/scalable/status/alphaplus-nfc-active-symbolic.svg"
    install -Dm644 icons/alphaplus-nfc-disabled-symbolic.svg \
      "$out/share/icons/hicolor/scalable/status/alphaplus-nfc-disabled-symbolic.svg"
    runHook postInstall
  '';

  meta = {
    description = "Plasma NFC control and NDEF reader/writer widget for the LG G8 ThinQ";
    license = lib.licenses.gpl2Plus;
    platforms = lib.platforms.linux;
  };
}
