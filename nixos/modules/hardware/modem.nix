{ config, pkgs, lib, ... }:

let
  imsd = pkgs.imsd-alphaplus;
  imsdStartScript = pkgs.writeShellScript "alphaplus-imsd-start" ''
    set -eu
    ${imsd}/libexec/ims-pdn-up
    load_env_file() {
      local file="$1"
      [ -r "$file" ] || return 0
      while IFS= read -r line || [ -n "$line" ]; do
        case "$line" in
          ""|\#*) continue ;;
          *=*)
            key="''${line%%=*}"
            val="''${line#*=}"
            case "$key" in
              [A-Za-z_][A-Za-z0-9_]*) export "$key=$val" ;;
            esac
            ;;
        esac
      done < "$file"
    }
    load_env_file /run/imsd.env
    load_env_file /etc/imsd.env
    exec ${lib.getExe imsd}
  '';
  rprocBootScript = pkgs.writeShellScript "alphaplus-rproc-boot" ''
    for r in /sys/class/remoteproc/remoteproc*; do
      [ -f "$r/state" ] || continue
      [ "$(cat "$r/state")" = offline ] || continue
      echo start > "$r/state" || true
    done
  '';
  populateTqftpReadwrite = pkgs.writeShellScript "alphaplus-tqftp-populate" ''
    set -eu
    rw_dir="/var/lib/tqftpserv"
    marker="$rw_dir/.populated"
    [ -f "$marker" ] && exit 0
    persist_dev="/dev/disk/by-partlabel/persist"
    [ -e "$persist_dev" ] || exit 0
    mnt=$(${pkgs.coreutils}/bin/mktemp -d)
    cleanup() {
      ${pkgs.util-linux}/bin/umount "$mnt" 2>/dev/null || true
      ${pkgs.coreutils}/bin/rmdir "$mnt" 2>/dev/null || true
    }
    trap cleanup EXIT
    ${pkgs.util-linux}/bin/mount -o ro,noatime "$persist_dev" "$mnt"
    if [ -d "$mnt/rfs/msm/mpss" ]; then
      ${pkgs.coreutils}/bin/cp -a "$mnt/rfs/msm/mpss/." "$rw_dir/"
      ${pkgs.coreutils}/bin/chown -R 0:0 "$rw_dir"
      ${pkgs.coreutils}/bin/chmod -R u+rwX "$rw_dir"
    fi
    : > "$marker"
  '';
in
{
  networking.modemmanager = {
    enable = true;
    package = pkgs.modemmanager-alphaplus;
  };

  systemd.services.ModemManager = {
    wantedBy = [ "multi-user.target" ];
    after = [ "rmtfs.service" "alphaplus-remoteproc-boot.service" "alphaplus-vss-init.service" ];
  };

  services.dbus.packages = [ imsd ];

  systemd.services.imsd = {
    description = "Userspace IMS and VoLTE";
    requires = [ "ModemManager.service" ];
    partOf = [ "ModemManager.service" ];
    after = [ "ModemManager.service" "network.target" ];
    wantedBy = [ "multi-user.target" "ModemManager.service" ];
    path = with pkgs; [
      coreutils gawk gnugrep gnused iproute2 libqmi
      modemmanager-alphaplus nftables pipewire sudo util-linux
    ];
    environment = {
      IMSD_PCSCF = lib.getExe' imsd "imsd-pcscf";
      IMSD_MEDIA = lib.getExe' imsd "imsd-media";
      SMS_OVER_IMS = "1";
      LC_ALL = "C";
    };
    serviceConfig = {
      Type = "simple";
      ExecStart = "${imsdStartScript}";
      StateDirectory = "imsd";
      StateDirectoryMode = "0700";
      Restart = "on-failure";
      RestartSec = 120;
      KillMode = "mixed";
      TimeoutStopSec = 10;
    };
  };

  systemd.services.alphaplus-vss-init = {
    description = "LG vss_common QRTR handshake for modem SIM readiness";
    after = [ "local-fs.target" ];
    before = [ "alphaplus-remoteproc-boot.service" "ModemManager.service" ];
    wantedBy = [ "multi-user.target" ];
    serviceConfig = {
      ExecStart = lib.getExe pkgs.alphaplus-vss-init;
      Restart = "always";
      RestartSec = "1s";
    };
  };

  environment.systemPackages = with pkgs; [
    imsd
    rmtfs
    tqftpserv
    qbootctl
    qrtr
  ];

  systemd.services.alphaplus-remoteproc-boot = {
    description = "Boot offline Qualcomm remote processors from rootfs firmware";
    requires = [ "tqftpserv.service" "rmtfs.service" ];
    after = [ "local-fs.target" "tqftpserv.service" "rmtfs.service" ];
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
    after = [ "tqftpserv.service" ];
    wantedBy = [ "multi-user.target" ];
    serviceConfig = {
      ExecStart = "${pkgs.rmtfs}/bin/rmtfs -r -P -s";
      Restart = "always";
      RestartSec = "1s";
    };
  };

  systemd.services.tqftpserv = {
    description = "Qualcomm TFTP firmware server";
    requires = [ "dev-disk-by\\x2dpartlabel-persist.device" ];
    after = [ "local-fs.target" "dev-disk-by\\x2dpartlabel-persist.device" ];
    wantedBy = [ "multi-user.target" ];
    serviceConfig = {
      StateDirectory = "tqftpserv";
      StateDirectoryMode = "0700";
      ExecStartPre = "${populateTqftpReadwrite}";
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
