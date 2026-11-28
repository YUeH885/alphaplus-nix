{ lib
, stdenv
, cmake
, ninja
, pkg-config
, kdePackages
, qt6
, wayland
, libepoxy
, libdrm
, vulkan-headers
, vulkan-loader
, qtbaseHost ? null
}:

stdenv.mkDerivation {
  pname = "alphaplus-kwin-keyboard";
  inherit (kdePackages.kwin) version;

  src = ./src;
  strictDeps = true;
  dontWrapQtApps = true;
  nativeBuildInputs = [ cmake ninja pkg-config ];
  buildInputs = [
    kdePackages.kwin
    kdePackages.extra-cmake-modules
    kdePackages.kconfig
    kdePackages.kcoreaddons
    kdePackages.kwindowsystem
    qt6.qtbase
    qt6.qtdeclarative
    wayland
    libepoxy
    libdrm
    vulkan-headers
    vulkan-loader
  ];
  cmakeFlags = [
    "-DKWIN_PLUGIN_DIR=${qt6.qtbase.qtPluginPrefix}/kwin/plugins"
  ] ++ lib.optional (qtbaseHost != null)
    "-DCMAKE_AUTOMOC_EXECUTABLE=${qtbaseHost}/libexec/moc";

  meta = with lib; {
    description = "Move floating KWin windows to keep the input cursor above the on-screen keyboard";
    license = licenses.gpl2Plus;
    platforms = platforms.linux;
  };
}
