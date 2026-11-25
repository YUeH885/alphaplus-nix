{ config, pkgs, lib, ... }:

let
  dspDir = ../../files/sensors;
  cdspDir = ../../files/cdsp;

  dspAssets = pkgs.runCommand "alphaplus-dsp-assets" { } ''
    mkdir -p $out/share/qcom/sm8150/LG/alphaplus/dsp/cdsp
    mkdir -p $out/share/qcom/sm8150/LG/alphaplus/dsp/sdsp
    cp -rf ${dspDir}/* $out/share/qcom/sm8150/LG/alphaplus/
    cp -rf ${cdspDir}/* $out/share/qcom/sm8150/LG/alphaplus/dsp/cdsp/
    chmod -R u+w $out
    rm -f $out/share/qcom/sm8150/LG/alphaplus/manifest.json \
          $out/share/qcom/sm8150/LG/alphaplus/dsp/cdsp/manifest.json

    # Separate initial sensor registry seed from read-only package and link to writable state
    if [ -d $out/share/qcom/sm8150/LG/alphaplus/sensors/registry ]; then
      mkdir -p $out/share/alphaplus/sensors-seed
      mv $out/share/qcom/sm8150/LG/alphaplus/sensors/registry $out/share/alphaplus/sensors-seed/
      ln -s /var/lib/alphaplus/sensors/registry $out/share/qcom/sm8150/LG/alphaplus/sensors/registry
    fi
  '';

  sensorInitScript = pkgs.writeShellScript "alphaplus-init-sensor-state" ''
    mkdir -p /var/lib/alphaplus/sensors/registry
    # Ensure existing seeded directories/files from read-only Nix store paths are writable by fastrpc.
    chmod -R u+rwX,g=rX,o-rwx /var/lib/alphaplus/sensors
    if [ -d ${dspAssets}/share/alphaplus/sensors-seed/registry ]; then
      cp -r --update=none --no-preserve=mode ${dspAssets}/share/alphaplus/sensors-seed/registry/* \
        /var/lib/alphaplus/sensors/registry/
      chmod -R u+rwX,g=rX,o-rwx /var/lib/alphaplus/sensors/registry
    fi
    chmod 0750 /var/lib/alphaplus/sensors /var/lib/alphaplus/sensors/registry
  '';
in
{
  hardware.sensor.iio.enable = true;

  environment.systemPackages = with pkgs; [
    hexagonrpc
    libssc
    dspAssets
  ];

  services.udev.packages = [
    (pkgs.writeTextDir "lib/udev/rules.d/60-hexagonrpcd.rules" ''
      ACTION=="add", SUBSYSTEM=="misc", KERNEL=="fastrpc-*", OWNER="fastrpc", GROUP="fastrpc", MODE="0660"
    '')
  ];

  users.users.fastrpc = {
    isSystemUser = true;
    group = "fastrpc";
    home = "/run/fastrpc";
    description = "Hexagonrpcd user";
  };
  users.groups.fastrpc = { };

  # FastRPC userspace libraries expect canonical /usr/share/qcom path
  systemd.tmpfiles.rules = [
    "L+ /usr/share/qcom - - - - /run/current-system/sw/share/qcom"
  ];

  systemd.services.hexagonrpcd = {
    description = "Hexagon DSP sensors daemon (LM-G820N)";
    after = [
      "local-fs.target"
      "alphaplus-remoteproc-boot.service"
      "dev-fastrpc\\x2dsdsp.device"
    ];
    bindsTo = [ "dev-fastrpc\\x2dsdsp.device" ];
    wantedBy = [ "multi-user.target" ];
    serviceConfig = {
      User = "fastrpc";
      Group = "fastrpc";
      ExecStartPre = "${sensorInitScript}";
      ExecStart = "${pkgs.hexagonrpc}/bin/hexagonrpcd -f /dev/fastrpc-sdsp -d sdsp -s -R ${dspAssets}/share/qcom/sm8150/LG/alphaplus";
      StateDirectory = "alphaplus/sensors";
      StateDirectoryMode = "0750";
      ReadWritePaths = [ "/var/lib/alphaplus/sensors" ];
      Restart = "on-failure";
      RestartSec = "2s";
    };
  };

  systemd.services.iio-sensor-proxy = {
    requires = [ "hexagonrpcd.service" ];
    after = [
      "alphaplus-remoteproc-boot.service"
      "hexagonrpcd.service"
    ];
  };
}
