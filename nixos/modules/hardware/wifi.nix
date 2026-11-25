{ config, pkgs, lib, ... }:

let
  wifiAddressScript = pkgs.runCommand "alphaplus-wifi-address" { } ''
    install -Dm644 ${../../files/ftm/ftm.py} "$out/bin/ftm.py"
    install -Dm755 ${../../files/ftm/wifi-address.py} "$out/bin/wifi-address.py"
    substituteInPlace "$out/bin/wifi-address.py" \
      --replace-fail "#!/usr/bin/env python3" "#!${lib.getExe pkgs.python3}"
  '';
in
{
  # Generate wlan_mac.bin before remoteproc_mpss triggers ath10k_snoc FW_READY firmware load
  systemd.services.alphaplus-remoteproc-boot = {
    wants = [ "alphaplus-wifi-address.service" ];
    after = [ "alphaplus-wifi-address.service" ];
  };

  systemd.services.NetworkManager = {
    wants = [ "alphaplus-wifi-address.service" ];
    after = [ "alphaplus-wifi-address.service" ];
  };

  systemd.services.alphaplus-wifi-address = {
    description = "Generate LM-G820N Wi-Fi MAC file from factory FTM data";
    requires = [ "dev-disk-by\\x2dpartlabel-ftm.device" ];
    after = [ "local-fs.target" "dev-disk-by\\x2dpartlabel-ftm.device" ];
    before = [ "alphaplus-remoteproc-boot.service" "NetworkManager.service" ];
    wantedBy = [ "multi-user.target" ];
    serviceConfig = {
      Type = "oneshot";
      ExecStart = "${wifiAddressScript}/bin/wifi-address.py";
      TimeoutStartSec = "30s";
      RemainAfterExit = true;
    };
  };
}
