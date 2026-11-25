{ config, pkgs, lib, ... }:

{
  networking.hostName = "alphaplus";
  networking.firewall.enable = false;
  networking.networkmanager = {
    enable = true;
    dns = "systemd-resolved";
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
