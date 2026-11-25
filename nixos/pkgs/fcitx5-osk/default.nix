{ lib
, stdenv
, rustPlatform
, fetchurl
, makeWrapper
, libglvnd
, vulkan-loader
, wayland
, libxkbcommon
}:

let
  rev = "76a33b65b1c72519788c09cfb9f94c1df0f61949";

  nerdFont = fetchurl {
    url = "https://raw.githubusercontent.com/ryanoasis/nerd-fonts/v3.4.0/patched-fonts/Noto/Serif/NotoSerifNerdFont-Regular.ttf";
    sha256 = "5b39ad296c59dac8caa4414f84dc014946847f6cd59c44c031adde287f2e6b22";
  };
  nerdFontLicense = fetchurl {
    url = "https://raw.githubusercontent.com/ryanoasis/nerd-fonts/v3.4.0/patched-fonts/Noto/Serif/LICENSE_OFL.txt";
    sha256 = "6a73f9541c2de74158c0e7cf6b0a58ef774f5a780bf191f2d7ec9cc53efe2bf2";
  };
  nerdFontOfl = fetchurl {
    url = "https://raw.githubusercontent.com/ryanoasis/nerd-fonts/v3.4.0/patched-fonts/Noto/Serif/OFL.txt";
    sha256 = "f980083077881cf69be49d860e64a9ada3f0e97407f52058fa8f6fe599ab6a23";
  };
in
rustPlatform.buildRustPackage {
  pname = "fcitx5-osk-alphaplus";
  version = "0.2.1";

  src = fetchurl {
    name = "fcitx5-osk-${rev}.tar.gz";
    url = "https://codeload.github.com/fortime/fcitx5-osk/tar.gz/${rev}";
    sha256 = "8501c5fdc1c3ff7e8197c53437351284bd85335b4f69aacaddf7181157298b91";
  };

  patches = [
    ./patches/0001-window-lifecycle.patch
    ./patches/0002-candidate-extbutton.patch
  ];

  cargoHash = "sha256-Zx3wPuIrvWbLsMWtCP0efq1rLolVMS94xlKaaFqgJeA=";

  # Build without custom-action-http-api to avoid pulling OpenSSL/reqwest into the OSK closure.
  buildNoDefaultFeatures = true;

  # Override upstream single-CU full LTO so release cross-builds stay fast and bounded in RAM.
  CARGO_PROFILE_RELEASE_LTO = "thin";
  CARGO_PROFILE_RELEASE_CODEGEN_UNITS = "16";

  nativeBuildInputs = [ makeWrapper ];

  postInstall = ''
    install -Dm644 assets/icons/fcitx5-osk.svg \
      "$out/share/icons/hicolor/scalable/apps/fyi.fortime.Fcitx5Osk.svg"
    install -Dm644 LICENSE "$out/share/licenses/fcitx5-osk/LICENSE"
    install -Dm644 LICENSE "$out/share/licenses/fcitx5-osk-kwin-launcher/LICENSE"
    install -Dm644 pkg/share/dbus-1/system.d/fyi.fortime.Fcitx5OskKeyHelper.conf \
      "$out/etc/dbus-1/system.d/fyi.fortime.Fcitx5OskKeyHelper.conf"

    mkdir -p "$out/share/fcitx5-osk/examples"
    cp -r pkg/share/fcitx5-osk/. "$out/share/fcitx5-osk/"
    rm -f "$out/share/fcitx5-osk/custom_actions/otp.toml" \
          "$out/share/fcitx5-osk/custom_actions/pw.toml"
    cp -r assets/layouts assets/key_sets "$out/share/fcitx5-osk/examples/"

    mkdir -p "$out/etc/xdg/fcitx5-osk/themes"
    ln -s "$out/share/fcitx5-osk/themes/breeze-light.toml" \
      "$out/etc/xdg/fcitx5-osk/themes/breeze-light.toml"

    install -Dm644 pkg/share/applications/fyi.fortime.Fcitx5Osk.desktop.in \
      "$out/share/applications/fyi.fortime.Fcitx5Osk.desktop"
    install -Dm644 pkg/share/applications/fyi.fortime.Fcitx5Osk.KwinLauncher.desktop.in \
      "$out/share/applications/fyi.fortime.Fcitx5Osk.KwinLauncher.desktop"
    install -Dm644 pkg/share/dbus-1/services/fyi.fortime.Fcitx5Osk.service.in \
      "$out/share/dbus-1/services/fyi.fortime.Fcitx5Osk.service"
    install -Dm644 pkg/lib/systemd/system/fcitx5-osk-key-helper.service.in \
      "$out/lib/systemd/system/fcitx5-osk-key-helper.service"

    # Omit fyi.fortime.Fcitx5Osk.KwinLauncher.service: alphaplus-lockscreen manages
    # KWin InputMethod switching, and D-Bus server activation races with it.
    for item in "$out"/share/applications/*.desktop "$out"/share/dbus-1/services/*.service "$out"/lib/systemd/system/*.service; do
      substituteInPlace "$item" --replace-fail '@INSTALL_BIN_DIR@' "$out/bin"
    done

    install -Dm644 "${nerdFont}" \
      "$out/share/fonts/truetype/fcitx5-osk/NotoSerifNerdFont-Regular.ttf"
    install -Dm644 "${nerdFontLicense}" \
      "$out/share/licenses/fcitx5-osk/NotoSerifNerdFont/LICENSE_OFL.txt"
    install -Dm644 "${nerdFontOfl}" \
      "$out/share/licenses/fcitx5-osk/NotoSerifNerdFont/OFL.txt"
  '';

  postFixup = ''
    wrapProgram "$out/bin/fcitx5-osk" \
      --prefix LD_LIBRARY_PATH : "${lib.makeLibraryPath [ libglvnd vulkan-loader wayland libxkbcommon ]}" \
      --prefix XDG_DATA_DIRS : "$out/share"
    wrapProgram "$out/bin/fcitx5-osk-kwin-launcher" \
      --set-default FCITX5_OSK_KWIN_LAUNCHER_HEALTH_CHECK_SECONDS 86400
  '';

  meta.platforms = [ "aarch64-linux" ];
}
