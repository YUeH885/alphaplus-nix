{ lib, stdenv, pkg-config, alsa-lib, glib, modemmanager-alphaplus }:

stdenv.mkDerivation {
  pname = "alphaplus-call-audio";
  version = "1.0";
  src = ./alphaplus-call-audio.c;
  dontUnpack = true;

  nativeBuildInputs = [ pkg-config ];
  buildInputs = [ alsa-lib glib modemmanager-alphaplus ];
  strictDeps = true;

  buildPhase = ''
    runHook preBuild
    "$CC" -O2 -Wall -Wextra -Wno-unused-parameter -o alphaplus-call-audio "$src" \
      $("$PKG_CONFIG" --cflags --libs alsa mm-glib)
    runHook postBuild
  '';

  installPhase = ''
    runHook preInstall
    install -Dm755 alphaplus-call-audio "$out/bin/alphaplus-call-audio"
    runHook postInstall
  '';

  meta = {
    description = "ModemManager call audio service for LG LM-G820N";
    license = lib.licenses.gpl2Plus;
    mainProgram = "alphaplus-call-audio";
    platforms = lib.platforms.linux;
  };
}
