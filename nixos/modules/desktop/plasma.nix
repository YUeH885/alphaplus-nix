{ config, pkgs, lib, ... }:

let
  helpers = pkgs.alphaplus-desktop-helpers;
  powerdevilrc = ../../files/desktop/user-config/powerdevilrc;
  plasmaDialer = pkgs.symlinkJoin {
    name = "plasma-dialer-alphaplus";
    paths = [ pkgs.kdePackages.plasma-dialer ];
    postBuild = ''
      rm "$out/share/dbus-1/services/org.kde.modemdaemon.service" \
         "$out/etc/xdg/autostart/org.kde.modem.daemon.desktop"
      for iface in CallUtils DeviceUtils UssdUtils; do
        name="org.kde.telephony.$iface"
        cat > "$out/share/dbus-1/services/$name.service" <<EOF
      [D-BUS Service]
      Name=$name
      Exec=${lib.getExe' pkgs.imsd-alphaplus "imsd-dialerd"}
      SystemdService=imsd-dialerd.service
      EOF
      done
    '';
  };
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
  # WrapQtAppsHook expands transitive dependencies into ~80 XDG_DATA_DIRS entries, which stalls
  # Plasma startup on ENOENT lookups; rebuild a thin shell wrapper that keeps Qt/QML search paths
  # from upstream makeCWrapper while leaving XDG_DATA_DIRS to the compact systemd environment.
  mkTrimmedExec = pkg: binRelPath:
    pkgs.runCommand "alphaplus-${baseNameOf binRelPath}-trimmed" {
      nativeBuildInputs = [ pkgs.buildPackages.python3 ];
    } ''
      python3 - "${pkg}/${binRelPath}" "${pkgs.runtimeShell}" "$out" << 'EOF'
      import os, shlex, sys

      src, shell, dst = sys.argv[1], sys.argv[2], sys.argv[3]
      real = os.path.realpath(src)
      data = open(real, "rb").read().decode("latin1")
      idx = data.find("makeCWrapper")
      if idx == -1:
          raise SystemExit(f"Expected makeCWrapper in {real}")
      block = data[idx:].split("\x00")[0]

      tokens = shlex.split(block.replace("\\\n", " "), comments=True)
      if len(tokens) < 2 or tokens[0] != "makeCWrapper":
          raise SystemExit(f"Invalid makeCWrapper header in {real}")
      target = tokens[1]

      lines = [f"#!{shell}"]
      prefixes = {}
      suffixes = {}
      sets = {}
      defaults = {}
      i = 2
      while i < len(tokens):
          flag = tokens[i]
          if flag == "--inherit-argv0":
              i += 1
          elif flag in ("--prefix", "--suffix"):
              if i + 3 >= len(tokens):
                  raise SystemExit(f"Incomplete {flag} arguments in {real}")
              var, sep, val = tokens[i + 1], tokens[i + 2], tokens[i + 3]
              i += 4
              if var == "XDG_DATA_DIRS":
                  continue
              target_map = prefixes if flag == "--prefix" else suffixes
              items = target_map.setdefault(var, [])
              for entry in val.split(sep or ":"):
                  if entry and entry not in items:
                      items.append(entry)
          elif flag in ("--set", "--set-default"):
              if i + 2 >= len(tokens):
                  raise SystemExit(f"Incomplete {flag} arguments in {real}")
              var, val = tokens[i + 1], tokens[i + 2]
              i += 3
              if var == "XDG_DATA_DIRS":
                  continue
              if flag == "--set":
                  sets[var] = val
              else:
                  defaults[var] = val
          else:
              raise SystemExit(f"Unsupported wrapper flag {flag} in {real}")

      for var, items in prefixes.items():
          if var == "QT_PLUGIN_PATH" and "/run/current-system/sw/lib/qt-6/plugins" not in items:
              items.append("/run/current-system/sw/lib/qt-6/plugins")
          elif var == "NIXPKGS_QT6_QML_IMPORT_PATH" and "/run/current-system/sw/lib/qt-6/qml" not in items:
              items.append("/run/current-system/sw/lib/qt-6/qml")
          joined = ":".join(items)
          lines.append("export " + var + "=" + shlex.quote(joined) + '"''${' + var + ':+:$' + var + '}"')
      for var, items in suffixes.items():
          joined = ":".join(items)
          lines.append("export " + var + '="''${' + var + ':+$' + var + ':}"' + shlex.quote(joined))
      for var, val in sets.items():
          lines.append("export " + var + "=" + shlex.quote(val))
      for var, val in defaults.items():
          lines.append('if [ -z "''${' + var + ':-}" ]; then export ' + var + "=" + shlex.quote(val) + "; fi")
      if "NIXPKGS_QT6_QML_IMPORT_PATH" in prefixes:
          lines.append('export QML2_IMPORT_PATH="$NIXPKGS_QT6_QML_IMPORT_PATH''${QML2_IMPORT_PATH:+:$QML2_IMPORT_PATH}"')
      lines.append(f"exec {shlex.quote(target)} \"$@\"")

      with open(dst, "w") as f:
          f.write("\n".join(lines) + "\n")
      os.chmod(dst, 0o755)
      EOF
    '';
  mkTrimmedPlasmaService = { pkg, bin, args ? "", after ? [ ], serviceConfig ? { } }: {
    overrideStrategy = "asDropin";
    enableDefaultPath = false;
    after = [ "dbus.service" ] ++ after;
    environment.XDG_DATA_DIRS = lib.concatStringsSep ":" [
      "%h/.local/share"
      "%h/.nix-profile/share"
      "%h/.local/state/nix/profile/share"
      "/etc/profiles/per-user/%u/share"
      "/nix/var/nix/profiles/default/share"
      "/run/current-system/sw/share"
    ];
    serviceConfig = {
      ExecStart = [ "" "${mkTrimmedExec pkg bin}${lib.optionalString (args != "") " ${args}"}" ];
    } // serviceConfig;
  };
in
{
  imports = [ ./sddm.nix ];
  services.desktopManager.plasma6 = {
    enable = true;
    enableQt5Integration = false;
  };
  programs.kde-pim.enable = false;
  programs.kdeconnect.enable = true;
  services.dbus.packages = [ plasmaDialer pkgs.spacebar-alphaplus pkgs.callaudiod ];
  systemd.user.services.imsd-dialerd = {
    description = "Plasma Dialer IMS bridge";
    after = [ "dbus.service" ];
    partOf = [ "graphical-session.target" ];
    wantedBy = [ "plasma-workspace.target" ];
    serviceConfig = {
      Type = "dbus";
      BusName = "org.kde.telephony.CallUtils";
      ExecStart = lib.getExe' pkgs.imsd-alphaplus "imsd-dialerd";
      Restart = "on-failure";
      RestartSec = "1s";
    };
  };
  # Keep 70-alphaplus-uinput.rules (GROUP="input") instead of NixOS 99-local.rules (GROUP="uinput").
  hardware.uinput.enable = false;
  environment.plasma6.excludePackages = with pkgs.kdePackages; [
    plasma-workspace-wallpapers
    plasma-nm
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
    alphaplus-nfc
    plasma-nm-alphaplus
    plasmaDialer
    spacebar-alphaplus
    callaudiod
    chromium
    libcamera-alphaplus
    gst_all_1.gstreamer
    snapshot-alphaplus
    qrcaWithLibcamera
    ffmpeg-vpu
    kdePackages.plasma-browser-integration
    kdePackages.plasma5support
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

  systemd.user.services = {
    xdg-user-dirs = {
      overrideStrategy = "asDropin";
      wantedBy = [ "graphical-session-pre.target" ];
      environment = {
        LC_ALL = "C.UTF-8";
        LANG = "C.UTF-8";
      };
    };

    plasma-kwin_wayland = {
      overrideStrategy = "asDropin";
      enableDefaultPath = false;
      after = [ "dbus.service" ];
    };

    plasma-ksplash = mkTrimmedPlasmaService {
      pkg = pkgs.kdePackages.plasma-workspace;
      bin = "bin/ksplashqml";
    };

    plasma-ksmserver = mkTrimmedPlasmaService {
      pkg = pkgs.kdePackages.plasma-workspace;
      bin = "bin/ksmserver";
    };

    plasma-kded6 = mkTrimmedPlasmaService {
      pkg = pkgs.kdePackages.kded;
      bin = "bin/kded6";
    };

    plasma-plasmashell = mkTrimmedPlasmaService {
      pkg = pkgs.kdePackages.plasma-workspace;
      bin = "bin/plasmashell";
      args = "--no-respawn";
      serviceConfig.TimeoutStopSec = "5s";
    };

    plasma-powerdevil = mkTrimmedPlasmaService {
      pkg = pkgs.kdePackages.powerdevil;
      bin = "libexec/org_kde_powerdevil";
    };

    plasma-polkit-agent = mkTrimmedPlasmaService {
      pkg = pkgs.kdePackages.polkit-kde-agent-1;
      bin = "libexec/polkit-kde-authentication-agent-1";
    };

    plasma-kactivitymanagerd = mkTrimmedPlasmaService {
      pkg = pkgs.kdePackages.kactivitymanagerd;
      bin = "libexec/kactivitymanagerd";
    };

    plasma-xdg-desktop-portal-kde = mkTrimmedPlasmaService {
      pkg = pkgs.kdePackages.xdg-desktop-portal-kde;
      bin = "libexec/xdg-desktop-portal-kde";
    };

    # Mask DrKonqi cleanup/sentry units that define After=default.target / After=timers.target,
    # creating shutdown ordering cycles that tear down dbus-broker before plasmashell stops.
    drkonqi-coredump-cleanup.enable = false;
  };

  systemd.user.paths.drkonqi-sentry-postman.enable = false;

  systemd.user.timers.drkonqi-coredump-cleanup.enable = false;

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
