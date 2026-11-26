{ lib, stdenv }:

stdenv.mkDerivation {
  pname = "alphaplus-vss-init";
  version = "1.0.0";
  src = ./alphaplus-vss-init.c;
  dontUnpack = true;
  strictDeps = true;

  buildPhase = ''
    runHook preBuild
    "$CC" -O2 -Wall -Wextra -Werror "$src" -o alphaplus-vss-init
    runHook postBuild
  '';

  installPhase = ''
    runHook preInstall
    install -Dm755 alphaplus-vss-init "$out/bin/alphaplus-vss-init"
    runHook postInstall
  '';

  meta = with lib; {
    description = "LG vss_common QRTR handshake daemon for SM8150 modem SIM readiness";
    license = licenses.gpl2Plus;
    mainProgram = "alphaplus-vss-init";
    platforms = platforms.linux;
  };
}
