{ config, pkgs, lib, ... }:

{
  imports = [
    ./boot.nix
    ./users.nix
    ./network.nix
  ];

  time.timeZone = "Asia/Shanghai";
  i18n.defaultLocale = "zh_CN.UTF-8";

  documentation.nixos.enable = false;

  systemd.settings.Manager = {
    LogTarget = "journal";
    ShowStatus = false;
  };
  services.journald.settings.Journal = {
    SystemMaxUse = "200M";
  };
  systemd.services."user@".serviceConfig.Delegate = "cpu cpuset memory pids";
  boot.kernel.sysctl = {
    "vm.swappiness" = 180;
    "vm.watermark_boost_factor" = 0;
    "vm.watermark_scale_factor" = 125;
    "vm.page-cluster" = 0;
  };
  zramSwap = {
    enable = true;
    algorithm = "zstd";
    memoryPercent = 100;
    priority = 100;
  };

  nix.channel.enable = false;

  nix.settings = {
    experimental-features = [ "nix-command" "flakes" ];
    auto-optimise-store = true;
    nix-path = config.nix.nixPath;
    extra-substituters = [ "https://alphaplus-nix.hwrng.dev" ];
    extra-trusted-public-keys = [
      "alphaplus-cache-1:vEKvOaAhSomjA2SE/+8LgiNcai6j+fl8YvKeOUwIB8M="
    ];
  };

  nix.gc = {
    automatic = true;
    dates = "weekly";
    options = "--delete-older-than 14d";
  };

  system.stateVersion = "24.11";
}
