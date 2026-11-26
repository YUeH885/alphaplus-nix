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

    ln -s /var/lib/alphaplus/sensors/registry $out/share/qcom/sm8150/LG/alphaplus/sensors/registry
  '';

  sensorInitScript = pkgs.writeShellScript "alphaplus-init-sensor-state" ''
    set -eu
    state_dir="/var/lib/alphaplus/sensors"
    reg_dir="$state_dir/registry"
    marker="$state_dir/.sns-populated"
    ${pkgs.coreutils}/bin/mkdir -p "$reg_dir/registry"

    if [ ! -f "$marker" ]; then
      sns_dev="/dev/disk/by-partlabel/sns"
      if [ -e "$sns_dev" ]; then
        mnt=$(${pkgs.coreutils}/bin/mktemp -d)
        cleanup() {
          ${pkgs.util-linux}/bin/umount "$mnt" 2>/dev/null || true
          ${pkgs.coreutils}/bin/rmdir "$mnt" 2>/dev/null || true
        }
        trap cleanup EXIT
        if ${pkgs.util-linux}/bin/mount -o ro,noatime "$sns_dev" "$mnt"; then
          if [ -d "$mnt/sensors/registry" ]; then
            ${pkgs.coreutils}/bin/cp -a "$mnt/sensors/registry/." "$reg_dir/"
          fi
          : > "$marker"
        else
          echo "warning: failed to mount $sns_dev; using SLPI default sensor registry" >&2
        fi
      fi
    fi

    ${pkgs.coreutils}/bin/chown -R fastrpc:fastrpc "$state_dir"
    ${pkgs.coreutils}/bin/chmod -R u+rwX,g=rX,o-rwx "$state_dir"
    ${pkgs.coreutils}/bin/chmod 0750 "$state_dir" "$reg_dir"
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
    wants = [ "dev-disk-by\\x2dpartlabel-sns.device" ];
    after = [
      "local-fs.target"
      "alphaplus-remoteproc-boot.service"
      "dev-fastrpc\\x2dsdsp.device"
      "dev-disk-by\\x2dpartlabel-sns.device"
    ];
    bindsTo = [ "dev-fastrpc\\x2dsdsp.device" ];
    wantedBy = [ "multi-user.target" ];
    serviceConfig = {
      Type = "notify";
      NotifyAccess = "main";
      User = "fastrpc";
      Group = "fastrpc";
      ExecStartPre = "+${sensorInitScript}";
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
    wantedBy = [ "hexagonrpcd.service" ];
  };
}
