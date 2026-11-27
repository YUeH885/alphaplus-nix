{ lib, pkgs, linux-src, ... }:

let
  configFile = ./alphaplus-kernel.config;
in
pkgs.linuxKernel.manualConfig {
  version = "7.3.6-alphaplus";
  modDirVersion = "7.3.6";
  src = linux-src;
  configfile = configFile;
  allowImportFromDerivation = true;

  extraMeta = {
    description = "Linux kernel for LG G8 ThinQ (LM-G820N, sm8150-lg-alphaplus)";
    platforms = [ "aarch64-linux" ];
  };
}
