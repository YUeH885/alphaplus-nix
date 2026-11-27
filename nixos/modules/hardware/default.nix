{ config, pkgs, lib, ... }:

let
  udevRules = pkgs.runCommand "alphaplus-udev-rules" { } ''
    mkdir -p "$out/lib/udev/rules.d"
    cp ${../../files/udev/rules.d}/*.rules "$out/lib/udev/rules.d/"
    substituteInPlace "$out/lib/udev/rules.d/70-alphaplus-camera.rules" \
      --replace-fail '/bin/chmod' '${pkgs.coreutils}/bin/chmod'
  '';

  feedbackdTheme = pkgs.runCommand "alphaplus-feedbackd-theme" { } ''
    install -Dm644 "${../../files/feedbackd/themes}/lg,alphaplus.json" \
      "$out/share/feedbackd/themes/lg,alphaplus.json"
  '';
in
{
  imports = [
    ./kernel.nix
    ./firmware.nix
    ./modem.nix
    ./audio.nix
    ./sensors.nix
    ./wifi.nix
    ./bluetooth.nix
    ./gnss.nix
    ./nfc.nix
    ./camera.nix
    ./usb.nix
  ];

  environment.systemPackages = [ pkgs.feedbackd feedbackdTheme pkgs.libqmi pkgs.iw ];
  environment.variables.GST_VA_ALL_DRIVERS = "1";
  hardware.graphics.extraPackages = [
    (lib.hiPrio pkgs.turnip-alphaplus)
    pkgs.qcom-va-driver
  ];
  services.dbus.packages = [ pkgs.feedbackd ];
  systemd.packages = [ pkgs.feedbackd ];
  # Preserve numeric 70-/72- rule ordering ahead of systemd 73-seat-late.rules for uaccess ACLs.
  services.udev.packages = [ pkgs.feedbackd udevRules ];
  services.udev.extraRules = ''
    ACTION=="add|change", SUBSYSTEM=="power_supply", KERNEL=="battery", ENV{CHARGE_LIMIT}="75,80", RUN+="${lib.getExe' pkgs.coreutils "chgrp"} wheel $sys$devpath/charge_behaviour", RUN+="${lib.getExe' pkgs.coreutils "chmod"} 0664 $sys$devpath/charge_behaviour"
  '';

  users.groups.feedbackd = { };

  systemd.tmpfiles.rules = [
    "L+ /usr/share/feedbackd - - - - /run/current-system/sw/share/feedbackd"
  ];
}
