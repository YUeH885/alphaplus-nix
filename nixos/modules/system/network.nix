{ config, pkgs, lib, ... }:

let
  hotspotIpv6Script = pkgs.runCommand "alphaplus-hotspot-ipv6" { } ''
    install -Dm755 ${../../files/network/hotspot-ipv6.py} "$out"
    substituteInPlace "$out" \
      --replace-fail "#!/usr/bin/env python3" "#!${lib.getExe pkgs.python3}" \
      --replace-fail "@ip@" "${lib.getExe' pkgs.iproute2 "ip"}" \
      --replace-fail "@nft@" "${lib.getExe' pkgs.nftables "nft"}" \
      --replace-fail "@nmcli@" "${lib.getExe' pkgs.networkmanager-alphaplus "nmcli"}" \
      --replace-fail "@radvd@" "${lib.getExe' pkgs.radvd "radvd"}" \
      --replace-fail "@sysctl@" "${lib.getExe' pkgs.procps "sysctl"}"
  '';
in
{
  networking.hostName = "alphaplus";
  networking.firewall.enable = false;
  boot.kernel.sysctl = {
    "net.ipv6.conf.all.accept_ra" = 2;
    "net.ipv6.conf.default.accept_ra" = 2;
  };
  environment.etc."NetworkManager/dnsmasq-shared.d/hotspot.conf".text = ''
    resolv-file=/run/NetworkManager/no-stub-resolv.conf
  '';
  networking.networkmanager = {
    enable = true;
    package = pkgs.networkmanager-alphaplus;
    dns = "systemd-resolved";
    dispatcherScripts = [
      {
        source = hotspotIpv6Script;
        type = "basic";
      }
    ];
    wifi = {
      backend = "wpa_supplicant";
      macAddress = "permanent";
      scanRandMacAddress = false;
    };
  };
  services.resolved.enable = true;
  systemd.services.NetworkManager.preStart = ''
    ${pkgs.iw}/bin/iw reg reload
    ${pkgs.iw}/bin/iw reg set CN
  '';

  services.openssh = {
    enable = true;
    settings = {
      PermitRootLogin = "prohibit-password";
      PasswordAuthentication = false;
    };
  };

  systemd.services."serial-getty@ttyGS0" = {
    enable = true;
    wantedBy = [ "getty.target" ];
    unitConfig.ConditionKernelCommandLine = "console=ttyGS0,115200";
    serviceConfig = {
      ExecStart = [
        ""
        "-${lib.getExe' pkgs.util-linux "agetty"} --autologin root --noreset --noclear --keep-baud 115200,57600,38400,9600 - ${"$"}{TERM}"
      ];
      KillSignal = "SIGKILL";
      SuccessExitStatus = "SIGKILL";
    };
  };
}
