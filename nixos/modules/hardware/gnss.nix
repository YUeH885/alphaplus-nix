{ config, pkgs, lib, ... }:

{
  environment.systemPackages = [ pkgs.alphaplus-gnssd ];
  services.geoclue2 = {
    enable = true;
    package = pkgs.geoclue2.overrideAttrs (old: {
      patches = (old.patches or [ ]) ++ [
        ./patches/geoclue/0001-authorize-native-applications.patch
      ];
    });
  };
  environment.etc."geoclue/conf.d/80-alphaplus-gnss.conf".source =
    ../../files/geoclue/80-alphaplus-gnss.conf;

  users.users._gnss = {
    isSystemUser = true;
    group = "_gnss";
    extraGroups = [ "geoclue" ];
  };
  users.groups._gnss = { };
  users.groups.geoclue = { };

  systemd.services.alphaplus-gnssd = {
    description = "LM-G820N GNSS Location Daemon";
    after = [
      "alphaplus-remoteproc-boot.service"
      "rmtfs.service"
      "tqftpserv.service"
    ];
    wants = [
      "alphaplus-remoteproc-boot.service"
      "rmtfs.service"
      "tqftpserv.service"
    ];
    before = [ "geoclue.service" ];
    wantedBy = [ "multi-user.target" ];
    serviceConfig = {
      Type = "notify";
      User = "_gnss";
      Group = "geoclue";
      SupplementaryGroups = [ "_gnss" ];
      RuntimeDirectory = "alphaplus-gnss";
      RuntimeDirectoryMode = "0750";
      StateDirectory = "alphaplus-gnss";
      StateDirectoryMode = "0750";
      ExecStart = "${pkgs.alphaplus-gnssd}/bin/alphaplus-gnssd";
      Restart = "on-failure";
      RestartSec = "2s";
      TimeoutStopSec = "15s";
      TimeoutStartSec = "15s";
    };
  };
}
