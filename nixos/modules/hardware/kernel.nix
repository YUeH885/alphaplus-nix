{ config, pkgs, lib, ... }:

{
  boot.kernelPackages = pkgs.linuxPackagesFor pkgs.alphaplus-kernel;

  boot.kernelModules = [
    "uinput"
  ];

  boot.kernelParams = [
    "console=tty0"
  ];

  boot.extraModprobeConfig = builtins.readFile ../../files/modprobe/alphaplus-pstore.conf;
  boot.blacklistedKernelModules = [ "msm" ];

  systemd.services.alphaplus-gpu-module = {
    description = "Load MSM DRM after the firmware is available";
    wantedBy = [ "graphical.target" ];
    after = [ "local-fs.target" ];
    before = [ "display-manager.service" ];
    serviceConfig = {
      Type = "oneshot";
      ExecStart = "${pkgs.kmod}/bin/modprobe msm";
      RemainAfterExit = true;
    };
  };

  systemd.services.alphaplus-pstore-blk = {
    description = "Load the persistent block log backend";
    wantedBy = [ "multi-user.target" ];
    requires = [ "dev-disk-by\\x2dpartlabel-pstore.device" ];
    after = [ "dev-disk-by\\x2dpartlabel-pstore.device" ];
    serviceConfig = {
      Type = "oneshot";
      ExecStart = "${pkgs.kmod}/bin/modprobe pstore_blk";
      RemainAfterExit = true;
    };
  };

  hardware.deviceTree = {
    enable = true;
    name = "qcom/sm8150-lg-alphaplus.dtb";
    filter = "sm8150-lg-alphaplus.dtb";
  };

  # Root storage and filesystem drivers are built into the kernel
  boot.initrd.enable = true;
  boot.initrd.systemd.emergencyAccess = true;
  boot.initrd.includeDefaultModules = false;
  boot.initrd.availableKernelModules = lib.mkForce [ ];
  boot.initrd.kernelModules = lib.mkForce [ ];

  boot.loader.grub.enable = false;
  boot.loader.timeout = 0;
  boot.loader.generic-extlinux-compatible = {
    enable = true;
    configurationLimit = 5;
  };
}
