{ config, pkgs, lib, ... }:

let
  authorizedKeys = [
    "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIBJHCdGROP8IHts65l8dnjuEXvoAPZCfrD2pyaFdEBul yuzu23234@gmail.com"
    "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIOhbYRUzZOlGpBzTlzrCLYGSmtGDnle6nLZ/xDXQ5ElN codex"
  ];
in
{
  users.mutableUsers = true;

  users.users.root = {
    openssh.authorizedKeys.keys = authorizedKeys;
  };

  users.users.alphaplus = {
    isNormalUser = true;
    uid = 1000;
    extraGroups = [
      "wheel"
      "audio"
      "video"
      "input"
      "render"
      "networkmanager"
    ];
    hashedPassword = "$6$nl54bN4eRgpCiW45$g/sUNMuzn2CdL.eJJJBuzR4y1SVnN4g8DpY.h4Cb2qjDG1mmpPA8QqN61K2R9Gb2BUtq5Z5LCJh01JQnNanCO0";
    openssh.authorizedKeys.keys = authorizedKeys;
  };

  security.sudo.wheelNeedsPassword = false;
}
