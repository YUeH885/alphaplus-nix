{ config, pkgs, lib, ... }:

let
  firmwareDir = ../../files/firmware;
  firmwarePkg = pkgs.runCommand "alphaplus-firmware" { } ''
    mkdir -p $out/lib/firmware
    cp -r --no-preserve=mode ${firmwareDir}/* $out/lib/firmware/
    rm -f $out/lib/firmware/manifest.json
    install -Dm644 ${pkgs.linux-firmware}/lib/firmware/ath10k/WCN3990/hw1.0/firmware-5.bin \
      $out/lib/firmware/ath10k/WCN3990/hw1.0/firmware-5.bin
    ln -sfn /run/alphaplus/firmware/ath10k/WCN3990/hw1.0/wlan_mac.bin \
      $out/lib/firmware/ath10k/WCN3990/hw1.0/wlan_mac.bin
  '';
in
{
  hardware.enableRedistributableFirmware = false;
  hardware.enableAllFirmware = false;
  nixpkgs.config.allowUnfree = true;

  # Userspace daemons and kernel loader require uncompressed raw firmware files
  hardware.firmwareCompression = "none";

  # Compatibility link for services expecting traditional firmware location
  systemd.tmpfiles.rules = [
    "L+ /lib/firmware - - - - /run/current-system/firmware"
  ];

  hardware.firmware = [ firmwarePkg pkgs.wireless-regdb ];
}
