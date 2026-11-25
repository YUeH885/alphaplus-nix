{ config, pkgs, lib, ... }:

let
  rprocBootScript = pkgs.writeShellScript "alphaplus-rproc-boot" ''
    for r in /sys/class/remoteproc/remoteproc*; do
      [ -f "$r/state" ] || continue
      [ "$(cat "$r/state")" = offline ] || continue
      echo start > "$r/state" || true
    done
  '';
in
{
  environment.systemPackages = with pkgs; [
    rmtfs
    tqftpserv
    qbootctl
    qrtr
  ];

  systemd.services.alphaplus-remoteproc-boot = {
    description = "Boot offline Qualcomm remote processors from rootfs firmware";
    after = [ "local-fs.target" ];
    before = [ "rmtfs.service" ];
    wantedBy = [ "multi-user.target" ];
    serviceConfig = {
      Type = "oneshot";
      RemainAfterExit = true;
      ExecStart = "${rprocBootScript}";
    };
  };

  systemd.services.rmtfs = {
    description = "Qualcomm Remote Filesystem Service (RMTFS)";
    requires = [ "tqftpserv.service" ];
    after = [ "alphaplus-remoteproc-boot.service" "tqftpserv.service" ];
    wantedBy = [ "multi-user.target" ];
    serviceConfig = {
      ExecStart = "${pkgs.rmtfs}/bin/rmtfs -r -P -s";
      Restart = "always";
      RestartSec = "1s";
    };
  };

  systemd.services.tqftpserv = {
    description = "Qualcomm TFTP firmware server";
    after = [ "local-fs.target" ];
    wantedBy = [ "multi-user.target" ];
    serviceConfig = {
      ExecStart = "${pkgs.tqftpserv}/bin/tqftpserv";
      Restart = "always";
      RestartSec = "1s";
    };
  };

  systemd.services.qbootctl = {
    description = "Mark boot successful for Qualcomm ABL";
    requires = [ "dev-disk-by\\x2dpartlabel-boot_a.device" "dev-disk-by\\x2dpartlabel-boot_b.device" ];
    after = [ "dev-disk-by\\x2dpartlabel-boot_a.device" "dev-disk-by\\x2dpartlabel-boot_b.device" ];
    wantedBy = [ "multi-user.target" ];
    serviceConfig = {
      Type = "oneshot";
      RemainAfterExit = true;
      ExecStart = "${pkgs.qbootctl}/bin/qbootctl -m";
    };
  };
}
