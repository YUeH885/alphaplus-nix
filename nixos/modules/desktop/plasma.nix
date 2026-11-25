{ config, pkgs, lib, ... }:

let
  helpers = pkgs.alphaplus-desktop-helpers;
  powerdevilrc = ../../files/desktop/user-config/powerdevilrc;
  qrcaWithLibcamera = let
    pluginPath = lib.makeSearchPath "lib/gstreamer-1.0" (with pkgs; [
      libcamera-alphaplus
      pipewire
      gst_all_1.gstreamer.out
      gst_all_1.gst-plugins-base
      gst_all_1.gst-plugins-good
      gst_all_1.gst-plugins-bad
    ]);
  in pkgs.symlinkJoin {
    name = "qrca-alphaplus";
    paths = [ pkgs.kdePackages.qrca ];
    nativeBuildInputs = [ pkgs.buildPackages.makeWrapper ];
    postBuild = ''
      # The CAMSS capture nodes need media routing set up by libcameraprovider.
      # Feed Prison's CPU decoder packed frames so it can copy them without plane metadata.
      wrapProgram "$out/bin/qrca" \
        --set-default QT_MEDIA_BACKEND gstreamer \
        --set-default GST_PLUGIN_FEATURE_RANK libcameraprovider:300,v4l2deviceprovider:0,pipewiredeviceprovider:0 \
        --set-default QT_GSTREAMER_OVERRIDE_VIDEO_CONVERSION_ELEMENT \
          'videoscale add-borders=true ! videoconvert ! capsfilter caps="video/x-raw,format=BGRx,width=1280,height=960"' \
        --prefix GST_PLUGIN_SYSTEM_PATH_1_0 : "${pluginPath}"
    '';
  };
  desktopOutputs = pkgs.writeText "alphaplus-kwinoutputconfig.json" (
    lib.replaceStrings
      [ ''"transform": "Normal"'' ]
      [ ''"transform": "Rotated270"'' ]
      (builtins.readFile ../../files/desktop/kwinoutputconfig.json)
  );
  touchShell = pkgs.runCommand "alphaplus-plasma-shell" { nativeBuildInputs = [ pkgs.buildPackages.python3 ]; } ''
    mkdir -p root/usr/share/plasma "$out"
    cp -r --no-preserve=mode ${pkgs.kdePackages.plasma-desktop}/share/plasma/shells root/usr/share/plasma/
    python3 ${../../files/desktop/configure-plasma-shell.py} root
    mv root/org.kde.plasma.desktop "$out/"
  '';
in
{
  imports = [ ./sddm.nix ];
  services.desktopManager.plasma6 = {
    enable = true;
    enableQt5Integration = false;
  };
  programs.kde-pim.enable = false;
  programs.kdeconnect.enable = true;
  # Keep 70-alphaplus-uinput.rules (GROUP="input") instead of NixOS 99-local.rules (GROUP="uinput").
  hardware.uinput.enable = false;
  environment.plasma6.excludePackages = with pkgs.kdePackages; [
    plasma-workspace-wallpapers
    qrca
  ];

  fonts.packages = with pkgs; [
    noto-fonts
    noto-fonts-cjk-sans
    noto-fonts-color-emoji
  ];

  environment.systemPackages = with pkgs; [
    helpers
    alphaplus-flashlight
    alphaplus-bypass-charging
    chromium
    libcamera-alphaplus
    gst_all_1.gstreamer
    snapshot-alphaplus
    qrcaWithLibcamera
    ffmpeg-vpu
    kdePackages.plasma-browser-integration
    kdePackages.konsole
    kdePackages.dolphin
  ];
  systemd.tmpfiles.rules = [
    "C /home/alphaplus/.config/kwinoutputconfig.json 0644 alphaplus users - ${desktopOutputs}"
    "C /home/alphaplus/.config/powerdevilrc 0644 alphaplus users - ${powerdevilrc}"
    "d /home/alphaplus/.local 0755 alphaplus users -"
    "d /home/alphaplus/.local/share 0755 alphaplus users -"
    "d /home/alphaplus/.local/share/plasma 0755 alphaplus users -"
    "d /home/alphaplus/.local/share/plasma/shells 0755 alphaplus users -"
    "L+ /home/alphaplus/.local/share/plasma/shells/org.kde.plasma.desktop - - - - ${touchShell}/org.kde.plasma.desktop"
    "d /root/.config 0700 root root -"
    "d /root/.config/systemd 0755 root root -"
    "d /root/.config/systemd/user 0755 root root -"
    "L+ /root/.config/systemd/user/drkonqi-coredump-launcher.socket - - - - /dev/null"
  ];

  environment.etc."xdg/powerdevilrc".source = powerdevilrc;

  # Run xdg-user-dirs via systemd user unit with C.UTF-8 instead of the zh_CN XDG autostart entry.
  systemd.packages = [ pkgs.xdg-user-dirs ];

  environment.etc."xdg/autostart/xdg-user-dirs.desktop".text = ''
    [Desktop Entry]
    Type=Application
    Name=User folders update
    Hidden=true
  '';

  systemd.user.services.xdg-user-dirs = {
    overrideStrategy = "asDropin";
    wantedBy = [ "graphical-session-pre.target" ];
    environment = {
      LC_ALL = "C.UTF-8";
      LANG = "C.UTF-8";
    };
  };

  systemd.user.services.alphaplus-audio-orientation = {
    description = "Adapt LM-G820N stereo audio channels to screen rotation";
    after = [ "plasma-kwin_wayland.service" "wireplumber.service" ];
    partOf = [ "graphical-session.target" ];
    wantedBy = [ "plasma-workspace.target" ];
    serviceConfig = {
      Type = "simple";
      ExecStart = "${helpers}/bin/alphaplus-audio-orientation";
      Restart = "on-failure";
      RestartSec = "1s";
    };
  };

  systemd.user.services.alphaplus-touchpad = {
    description = "Use the touchscreen as a touchpad";
    after = [ "dbus.service" "plasma-kwin_wayland.service" ];
    partOf = [ "graphical-session.target" ];
    wantedBy = [ "plasma-workspace.target" ];
    serviceConfig = {
      Type = "simple";
      ExecStart = "${helpers}/bin/alphaplus-touchpad";
      Restart = "on-failure";
      RestartSec = "1s";
    };
  };

  systemd.user.services.alphaplus-lockscreen = {
    description = "Use portrait orientation and Plasma Keyboard on the lock screen";
    after = [ "dbus.service" "plasma-kwin_wayland.service" ];
    partOf = [ "graphical-session.target" ];
    wantedBy = [ "plasma-workspace.target" ];
    serviceConfig = {
      Type = "simple";
      ExecStart = "${helpers}/bin/alphaplus-lockscreen";
      ExecStopPost = "${helpers}/bin/alphaplus-lockscreen --restore";
      RuntimeDirectory = "alphaplus-lockscreen";
      RuntimeDirectoryMode = "0700";
      StateDirectory = "alphaplus-lockscreen";
      StateDirectoryMode = "0700";
      Restart = "on-failure";
      RestartSec = "1s";
    };
  };

  systemd.user.services.alphaplus-foreground-placement = {
    description = "Place foreground applications and Plasma rendering on faster CPUs";
    after = [ "dbus.service" "plasma-kwin_wayland.service" "plasma-plasmashell.service" ];
    partOf = [ "graphical-session.target" ];
    wantedBy = [ "plasma-workspace.target" ];
    serviceConfig = {
      Type = "dbus";
      BusName = "org.alphaplus.ForegroundPlacement";
      ExecStart = "${helpers}/bin/alphaplus-foreground-placement";
      ExecStopPost = "${helpers}/bin/alphaplus-foreground-placement --restore";
      RuntimeDirectory = "alphaplus-foreground-placement";
      RuntimeDirectoryMode = "0700";
      Restart = "on-failure";
      RestartSec = "1s";
    };
  };
}
