{ config, pkgs, lib, ... }:

{
  fileSystems."/" = {
    device = "/dev/disk/by-partlabel/linux";
    fsType = "f2fs";
    options = [
      "rw"
      "noatime"
      "compress_algorithm=zstd"
      "compress_chksum"
      "compress_extension=*"
      "atgc"
      "gc_merge"
      "lazytime"
    ];
  };

  fileSystems."/boot" = {
    device = "/dev/disk/by-partlabel/product_a";
    fsType = "ext4";
    options = [ "rw" "noatime" ];
  };

  fileSystems."/sys/fs/pstore" = {
    device = "pstore";
    fsType = "pstore";
    options = [ "nosuid" "nodev" "noexec" ];
  };
}
