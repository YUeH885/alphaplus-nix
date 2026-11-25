{ config, pkgs, lib, ... }:

{
  imports = [
    ./plasma.nix
    ./input.nix
  ];
}
