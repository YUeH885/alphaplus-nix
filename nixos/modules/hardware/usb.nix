{ pkgs, ... }:

{
  environment.systemPackages = [ pkgs.alphaplus-usb-sleep-policy ];

  systemd.tmpfiles.rules = [
    "d /var/lib/alphaplus/usb 0755 root root -"
  ];
}
