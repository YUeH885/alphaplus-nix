{ lib, pkgs, ... }:

let
  cameraDir = ../../files/camera;

  cameraAssets = pkgs.runCommand "alphaplus-camera-assets" { } ''
    mkdir -p $out/share/camera/iqm
    cp ${cameraDir}/*.onnx $out/share/camera/iqm/
    chmod 0644 $out/share/camera/iqm/*
  '';
in
{
  environment.etc."camera/iqm".source = "${cameraAssets}/share/camera/iqm";

  # Run after 72-feedbackd.rules, which assigns *:flash LEDs to the feedbackd group.
  services.udev.extraRules = ''
    ACTION=="add|change", SUBSYSTEM=="leds", KERNEL=="white:flash|yellow:flash", RUN+="${lib.getExe' pkgs.coreutils "chgrp"} video $sys$devpath/brightness", RUN+="${lib.getExe' pkgs.coreutils "chmod"} 0664 $sys$devpath/brightness"
    SUBSYSTEM=="misc", KERNEL=="qcom-fd", GROUP="video", MODE="0660", TAG+="uaccess"
  '';

  system.activationScripts.alphaplus-camera-cleanup = ''
    if [ -L /vendor/etc/camera/iqm ] && [ "$(readlink /vendor/etc/camera/iqm)" = /etc/camera/iqm ]; then
      unlink /vendor/etc/camera/iqm
    fi
  '';
}
