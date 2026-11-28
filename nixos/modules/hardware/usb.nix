{ pkgs, ... }:

let
  gadgetRoot = "/sys/kernel/config/usb_gadget";
  gadgetDevice = "sys-subsystem-gadget-devices-alphaplus.device";
in
{
  environment.systemPackages = [ pkgs.alphaplus-usb-sleep-policy ];

  services.udev.extraRules = ''
    SUBSYSTEM=="gadget", KERNELS=="a600000.usb", TAG+="systemd", ENV{SYSTEMD_ALIAS}+="/sys/subsystem/gadget/devices/alphaplus", ENV{SYSTEMD_WANTS}+="alphaplus-usb-gadget.service"
  '';

  systemd.services.alphaplus-usb-gadget = {
    description = "Configure the AlphaPlus USB gadget";
    bindsTo = [ gadgetDevice ];
    requires = [ "sys-kernel-config.mount" ];
    after = [
      gadgetDevice
      "sys-kernel-config.mount"
    ];

    # Rebinding during a system switch can interrupt SSH over USB.
    restartIfChanged = false;
    unitConfig.ConditionPathIsDirectory = gadgetRoot;
    serviceConfig = {
      Type = "oneshot";
      RemainAfterExit = true;
      WorkingDirectory = gadgetRoot;
    };

    script = ''
      mkdir alphaplus
      cd alphaplus || exit 1

      echo 0x1d6b > idVendor
      echo 0x0104 > idProduct
      echo 0xef > bDeviceClass
      echo 0x02 > bDeviceSubClass
      echo 0x01 > bDeviceProtocol

      mkdir -p strings/0x409 configs/c.1 functions/ncm.usb0 functions/acm.GS0
      echo "AlphaPlus USB Gadget" > strings/0x409/product
      echo 900 > configs/c.1/MaxPower
      ln -s functions/ncm.usb0 configs/c.1/
      ln -s functions/acm.GS0 configs/c.1/

      echo a600000.usb > UDC
    '';

    postStop = ''
      if [[ ! -d alphaplus ]]; then
        exit 0
      fi
      if [[ -n $(<alphaplus/UDC) ]]; then
        echo "" > alphaplus/UDC
      fi

      rm -f alphaplus/configs/c.1/{ncm.usb0,acm.GS0}
      for entry in configs/c.1 functions/acm.GS0 functions/ncm.usb0 strings/0x409; do
        if [[ -d "alphaplus/$entry" ]]; then
          rmdir "alphaplus/$entry"
        fi
      done
      rmdir alphaplus
    '';
  };

  systemd.tmpfiles.rules = [
    "d /var/lib/alphaplus/usb 0755 root root -"
  ];
}
