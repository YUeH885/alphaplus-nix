{ lib
, stdenv
, buildPackages
, python3
, systemd
}:

stdenv.mkDerivation {
  pname = "alphaplus-usb-sleep-policy";
  version = "1.0.0";

  src = ./.;

  strictDeps = true;
  nativeBuildInputs = [ buildPackages.python3 ];

  doCheck = true;
  checkPhase = ''
    runHook preCheck
    python3 -m unittest discover -s . -v
    runHook postCheck
  '';

  installPhase = ''
    runHook preInstall
    install -Dm755 usb_sleep_policy.py "$out/bin/alphaplus-usb-sleep-policy"
    substituteInPlace "$out/bin/alphaplus-usb-sleep-policy" \
      --replace-fail "#!/usr/bin/env python3" "#!${lib.getExe python3}" \
      --replace-fail "@busctl@" "${lib.getExe' systemd "busctl"}"
    runHook postInstall
  '';

  meta = with lib; {
    description = "Scene-based USB s2idle retention policy controller for LM-G820N";
    license = licenses.gpl2Plus;
    mainProgram = "alphaplus-usb-sleep-policy";
    platforms = platforms.linux;
  };
}
