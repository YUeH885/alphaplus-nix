{ lib
, stdenv
, python3
, makeWrapper
, kdePackages
, pipewire
}:

let
  pythonModules = with python3.pkgs; requiredPythonModules [
    dbus-python
    pygobject3
    evdev
    pywayland
  ];
  jsScript = ../../files/desktop/foreground-placement.js;
  touchpadKeyboardJs = ../../files/desktop/touchpad-keyboard.js;
in
stdenv.mkDerivation {
  pname = "alphaplus-desktop-helpers";
  version = "1.0.0";

  src = ./src;

  nativeBuildInputs = [ makeWrapper ];
  buildInputs = [ python3 ];

  installPhase = ''
    mkdir -p $out/bin $out/lib $out/share/alphaplus

    cp display_orientation.py output_configuration.py screen_lock.py $out/lib/
    cp ${jsScript} $out/share/alphaplus/foreground-placement.js
    cp ${touchpadKeyboardJs} $out/share/alphaplus/touchpad-keyboard.js

    for script in audio-orientation.py touchpad.py lockscreen.py foreground-placement.py; do
      base=$(basename "$script" .py)
      target="$out/bin/alphaplus-$base"
      cp "$script" "$target"
      chmod +x "$target"
      wrapProgram "$target" \
        --prefix PATH : "${lib.makeBinPath [ pipewire kdePackages.kconfig ]}" \
        --prefix PYTHONPATH : "$out/lib:${lib.makeSearchPath python3.sitePackages pythonModules}" \
        --set-default ALPHAPLUS_PLACEMENT_JS "$out/share/alphaplus/foreground-placement.js" \
        --set-default ALPHAPLUS_TOUCHPAD_KEYBOARD_JS "$out/share/alphaplus/touchpad-keyboard.js" \
        --set-default PLASMA_WAYLAND_PROTOCOLS_DIR "${kdePackages.plasma-wayland-protocols}/share/plasma-wayland-protocols"
    done
  '';

  meta = with lib; {
    description = "Hardware orientation, touchpad interception, and power helpers for LM-G820N";
    license = licenses.gpl2Plus;
    platforms = platforms.linux;
  };
}
