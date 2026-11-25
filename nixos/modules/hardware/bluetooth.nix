{ config, pkgs, lib, ... }:

let
  btAddressScript = pkgs.runCommand "alphaplus-bluetooth-address" { } ''
    install -Dm644 ${../../files/ftm/ftm.py} "$out/bin/ftm.py"
    install -Dm755 ${../../files/ftm/bluetooth-address.py} "$out/bin/bluetooth-address.py"
    substituteInPlace "$out/bin/bluetooth-address.py" \
      --replace-fail "#!/usr/bin/env python3" "#!${lib.getExe pkgs.python3}"
  '';
in
{
  hardware.bluetooth = {
    enable = true;
    powerOnBoot = true;
    settings.General.KernelExperimental = "a6695ace-ee7f-4fb9-881a-5fac66c629af";
  };

  # Prevent early autoloading of hci_uart so factory MAC can be written to the controller first
  boot.blacklistedKernelModules = [ "hci_uart" ];
  systemd.services.bluetooth = {
    requires = [ "alphaplus-bluetooth-address.service" ];
    after = [ "alphaplus-bluetooth-address.service" ];
  };

  systemd.services.alphaplus-bluetooth-address = {
    description = "Initialize LM-G820N Bluetooth with its factory address";
    requires = [ "dev-disk-by\\x2dpartlabel-ftm.device" ];
    after = [ "local-fs.target" "dev-disk-by\\x2dpartlabel-ftm.device" ];
    before = [ "bluetooth.service" ];
    wantedBy = [ "multi-user.target" ];
    path = [ pkgs.kmod ];
    serviceConfig = {
      Type = "oneshot";
      ExecStart = "${btAddressScript}/bin/bluetooth-address.py";
      TimeoutStartSec = "45s";
      RemainAfterExit = true;
    };
  };
}
