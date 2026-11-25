{ lib, stdenv, buildPackages, pkg-config, qt6 }:

stdenv.mkDerivation {
  pname = "alphaplus-bypass-charging";
  version = "1.0.0";
  src = ./.;
  strictDeps = true;
  dontWrapQtApps = true;

  nativeBuildInputs = [ pkg-config ];
  buildInputs = [ qt6.qtbase qt6.qtdeclarative ];

  buildPhase = ''
    runHook preBuild
    ${lib.getBin buildPackages.qt6.qtbase}/libexec/moc $("$PKG_CONFIG" --cflags Qt6Qml) \
      bypass-charging.cpp -o bypass-charging.moc
    "$CXX" -O2 -std=c++17 -fPIC -shared $("$PKG_CONFIG" --cflags Qt6Qml) \
      bypass-charging.cpp $("$PKG_CONFIG" --libs Qt6Qml) -o libbypasschargingplugin.so
    runHook postBuild
  '';

  installPhase = ''
    runHook preInstall
    applet="$out/share/plasma/plasmoids/org.alphaplus.bypasscharging"
    mkdir -p "$applet"
    cp -r applet/. "$applet/"
    install -Dm755 libbypasschargingplugin.so "$applet/contents/ui/backend/libbypasschargingplugin.so"
    runHook postInstall
  '';

  meta = {
    description = "Plasma bypass charging toggle widget for the LG G8 ThinQ";
    license = lib.licenses.gpl2Plus;
    platforms = lib.platforms.linux;
  };
}
