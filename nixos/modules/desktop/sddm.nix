{ config, lib, pkgs, ... }:

let
  inherit (pkgs.kdePackages) kwin libkscreen plasma-desktop plasma-keyboard;
  kscreenDoctor = lib.getExe' libkscreen "kscreen-doctor";
  theme = pkgs.runCommand "alphaplus-sddm-theme" {
    nativeBuildInputs = [ pkgs.buildPackages.python3 ];
  } ''
    mkdir -p root/usr/share/sddm/themes $out/share/sddm/themes
    cp -r --no-preserve=mode ${plasma-desktop}/share/sddm/themes/breeze root/usr/share/sddm/themes/
    python3 ${../../files/desktop/sddm}/configure-sddm-theme.py root
    substituteInPlace root/usr/share/sddm/themes/breeze-alphaplus/PowerKey.qml \
      --replace-fail "/usr/bin/kscreen-doctor" "${kscreenDoctor}"
    mv root/usr/share/sddm/themes/breeze-alphaplus $out/share/sddm/themes/
  '';
  idleScript = pkgs.runCommand "alphaplus-sddm-idle" { } ''
    install -Dm755 ${../../files/desktop/sddm/sddm-idle.sh} "$out"
    substituteInPlace "$out" \
      --replace-fail "#!/bin/sh" "#!${pkgs.runtimeShell}" \
      --replace-fail "/usr/bin/swayidle" "${lib.getExe pkgs.swayidle}" \
      --replace-fail "/usr/bin/kscreen-doctor" "${kscreenDoctor}"
  '';
  greeterConfig = ../../files/desktop/kwinoutputconfig.json;
  greeterState = pkgs.writeText "alphaplus-sddm-state.conf" ''
    [Last]
    User=alphaplus
  '';
  prepareGreeter = pkgs.runCommand "alphaplus-prepare-greeter-sleep" { } ''
    install -Dm755 ${../../files/desktop/sddm/prepare-greeter-sleep.py} "$out"
    substituteInPlace "$out" \
      --replace-fail "#!/usr/bin/python3" "#!${lib.getExe pkgs.python3}" \
      --replace-fail "/usr/bin/kscreen-doctor" "${kscreenDoctor}"
  '';
  suspend = pkgs.runCommand "alphaplus-suspend" { } ''
    install -Dm755 ${../../files/desktop/sddm/suspend-maintenance.py} "$out"
    substituteInPlace "$out" \
      --replace-fail "#!/usr/bin/python3" "#!${lib.getExe pkgs.python3}" \
      --replace-fail "/usr/lib/systemd/systemd-sleep" "${config.systemd.package}/lib/systemd/systemd-sleep" \
      --replace-fail "@usb_sleep_policy@" "${lib.getExe pkgs.alphaplus-usb-sleep-policy}"
  '';
in
{
  environment.systemPackages = [ theme ];

  services.displayManager.sddm = {
    enable = true;
    theme = "breeze-alphaplus";
    settings.Theme = {
      CursorTheme = "breeze_cursors";
      CursorSize = 24;
    };
    wayland = {
      enable = true;
      compositor = "kwin";
      compositorCommand = lib.concatStringsSep " " [
        (lib.getExe' kwin "kwin_wayland")
        "--no-global-shortcuts --no-kactivities --no-lockscreen --locale1"
        "--inputmethod ${lib.getExe' plasma-keyboard "plasma-keyboard"}"
        "${idleScript}"
      ];
    };
  };

  systemd.tmpfiles.rules = [
    "d /var/lib/sddm/.config 0755 sddm sddm -"
    "C /var/lib/sddm/.config/kwinoutputconfig.json 0644 sddm sddm - ${greeterConfig}"
    "C /var/lib/sddm/state.conf 0644 sddm sddm - ${greeterState}"
  ];

  services.logind.settings.Login = {
    HandlePowerKey = "ignore";
    HandlePowerKeyLongPress = "ignore";
  };

  systemd.services.systemd-suspend.serviceConfig = {
    ExecStartPre = [ "${prepareGreeter}" ];
    ExecStart = [ "" "${suspend}" ];
    TimeoutStartSec = "infinity";
  };
}
