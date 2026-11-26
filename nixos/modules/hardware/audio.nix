{ config, pkgs, lib, ... }:

let
  wireplumberConf = ../../files/wireplumber/wireplumber.conf.d;
  filesShare = ../../files;

  wireplumberConfigs = pkgs.runCommand "alphaplus-wireplumber-configs" { } ''
    mkdir -p $out/share/wireplumber/wireplumber.conf.d
    mkdir -p $out/share/wireplumber/scripts
    cp ${wireplumberConf}/*.conf $out/share/wireplumber/wireplumber.conf.d/
    cp -r ${filesShare}/wireplumber/scripts/* $out/share/wireplumber/scripts/
  '';

  alsaCardProfiles = pkgs.runCommand "alphaplus-alsa-card-profiles" { } ''
    mkdir -p $out/share/alsa-card-profile/mixer/profile-sets
    mkdir -p $out/share/alsa-card-profile/mixer/paths
    cp ${filesShare}/alsa-card-profile/mixer/profile-sets/* $out/share/alsa-card-profile/mixer/profile-sets/
    cp ${filesShare}/alsa-card-profile/mixer/paths/* $out/share/alsa-card-profile/mixer/paths/
  '';
in
{
  security.rtkit.enable = true;
  # PipeWire/WirePlumber ACP owns desktop routing and software volume state;
  # run alsactl init on card arrival so 00-alphaplus.conf configures capture
  # without alsactl store failing on the volatile TFA9872 DSP Resistance control.
  services.udev.extraRules = ''
    ACTION=="add", SUBSYSTEM=="sound", KERNEL=="controlC*", KERNELS!="card*", RUN+="${pkgs.alsa-utils}/bin/alsactl init $attr{device/number}"
  '';
  services.pipewire = {
    enable = true;
    alsa.enable = true;
    alsa.support32Bit = false;
    pulse.enable = true;
    wireplumber = {
      enable = true;
      configPackages = [ wireplumberConfigs ];
    };

    extraConfig.pipewire."10-alphaplus-hires" = {
      "context.properties" = {
        "default.clock.rate" = 48000;
        "default.clock.allowed-rates" = [ 44100 48000 88200 96000 176400 192000 ];
      };
    };
  };

  environment.etc."alsa-card-profile/mixer/profile-sets".source =
    "${alsaCardProfiles}/share/alsa-card-profile/mixer/profile-sets";
  environment.etc."alsa-card-profile/mixer/paths".source =
    "${alsaCardProfiles}/share/alsa-card-profile/mixer/paths";

  # WirePlumber and PipeWire require ACP and SPA search directories in runtime environment
  environment.sessionVariables = {
    ACP_PROFILES_DIR = "/etc/alsa-card-profile/mixer/profile-sets";
    ACP_PATHS_DIR = "/etc/alsa-card-profile/mixer/paths";
    SPA_PLUGIN_DIR = "${pkgs.pipewire-spa-alphaplus}/lib/spa-0.2:${pkgs.pipewire}/lib/spa-0.2";
  };

  systemd.user.services.pipewire.environment = {
    ACP_PROFILES_DIR = "/etc/alsa-card-profile/mixer/profile-sets";
    ACP_PATHS_DIR = "/etc/alsa-card-profile/mixer/paths";
    SPA_PLUGIN_DIR = "${pkgs.pipewire-spa-alphaplus}/lib/spa-0.2:${pkgs.pipewire}/lib/spa-0.2";
  };

  systemd.user.services.wireplumber.environment = {
    LC_ALL = "C.UTF-8";
    LANGUAGE = "en";
    ACP_PROFILES_DIR = "/etc/alsa-card-profile/mixer/profile-sets";
    ACP_PATHS_DIR = "/etc/alsa-card-profile/mixer/paths";
    SPA_PLUGIN_DIR = "${pkgs.pipewire-spa-alphaplus}/lib/spa-0.2:${pkgs.pipewire}/lib/spa-0.2";
  };
  systemd.user.services.pipewire-pulse.environment = {
    LC_ALL = "C.UTF-8";
    LANGUAGE = "en";
  };

  systemd.tmpfiles.rules = [
    "L+ /usr/share/alsa-card-profile - - - - ${alsaCardProfiles}/share/alsa-card-profile"
  ];

  environment.systemPackages = [ alsaCardProfiles pkgs.alsa-utils ];

  systemd.services.alphaplus-call-audio = {
    description = "LG alphaplus modem call audio";
    after = [ "ModemManager.service" ];
    wantedBy = [ "multi-user.target" ];
    serviceConfig = {
      ExecStart = lib.getExe pkgs.alphaplus-call-audio;
      Restart = "on-failure";
      RestartSec = "1s";
    };
  };

  environment.etc."alsa/conf.d/51-alphaplus.conf".source =
    ../../files/alsa/51-alphaplus.conf;
}
