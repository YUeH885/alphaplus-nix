{ lib, pkgs, linux-src, ... }:

let
  configFile = ./alphaplus-kernel.config;
in
pkgs.linuxKernel.manualConfig {
  version = "7.3.3-alphaplus";
  modDirVersion = "7.3.3";
  src = linux-src;
  configfile = configFile;
  allowImportFromDerivation = true;

  extraMeta = {
    description = "Linux kernel for LG G8 ThinQ (LM-G820N, sm8150-lg-alphaplus)";
    platforms = [ "aarch64-linux" ];
  };
}
