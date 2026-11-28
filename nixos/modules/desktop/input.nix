{ config, pkgs, lib, ... }:

let
  keyboard = pkgs.fcitx5-osk-alphaplus;
  userConfig = ../../files/desktop/user-config;
  gtkCache = "/run/alphaplus/gtk3-immodules.cache";
  kwinrc = pkgs.writeText "alphaplus-kwinrc" ''
    [Wayland]
    InputMethod=/run/current-system/sw/share/applications/fyi.fortime.Fcitx5Osk.KwinLauncher.desktop

    [Xwayland]
    Scale=2
  '';
in
{
  i18n.inputMethod = {
    enable = true;
    type = "fcitx5";
    enableGtk3 = false;
    fcitx5 = {
      waylandFrontend = true;
      addons = [ pkgs.qt6Packages.fcitx5-chinese-addons ];
    };
  };
  environment.sessionVariables.GTK_IM_MODULE_FILE = gtkCache;
  system.activationScripts.alphaplus-gtk-immodules = ''
    install -d -m 0755 /run/alphaplus
    GTK_PATH=${config.i18n.inputMethod.package}/lib/gtk-3.0 \
      ${lib.getExe' (lib.getDev pkgs.gtk3) "gtk-query-immodules-3.0"} > "${gtkCache}"
  '';
  fonts.packages = [ keyboard ];
  environment.systemPackages = [ keyboard pkgs.alphaplus-kwin-keyboard ];
  services.dbus.packages = [ keyboard ];
  systemd.packages = [ keyboard ];
  systemd.services.fcitx5-osk-key-helper.wantedBy = [ "multi-user.target" ];

  systemd.tmpfiles.rules = [
    "d /home/alphaplus/.config 0755 alphaplus users -"
    "d /home/alphaplus/.config/fcitx5 0755 alphaplus users -"
    "d /home/alphaplus/.config/fcitx5-osk 0755 alphaplus users -"
    "C /home/alphaplus/.config/kwinrc 0644 alphaplus users - ${kwinrc}"
    "C /home/alphaplus/.config/kcminputrc 0644 alphaplus users - ${userConfig + "/kcminputrc"}"
    "C /home/alphaplus/.config/fcitx5/profile 0644 alphaplus users - ${userConfig + "/fcitx5/profile"}"
    "C /home/alphaplus/.config/fcitx5-osk/config.toml 0644 alphaplus users - ${userConfig + "/fcitx5-osk/config.toml"}"
    "C /home/alphaplus/.config/plasmakeyboardrc 0644 alphaplus users - ${userConfig + "/plasmakeyboardrc"}"
  ];
  environment.etc."xdg/kwinrc".source = kwinrc;
  environment.etc."xdg/kcminputrc".source = userConfig + "/kcminputrc";
  environment.etc."xdg/plasmakeyboardrc".source = userConfig + "/plasmakeyboardrc";
}
