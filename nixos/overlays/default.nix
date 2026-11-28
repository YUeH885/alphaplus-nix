{ linux-src, uboot-src }:

final: prev: {
  alsa-utils = prev.alsa-utils.overrideAttrs (old: {
    postInstall = (old.postInstall or "") + ''
      install -Dm644 ${../files/alsa/init/00-alphaplus.conf} \
        "$out/share/alsa/init/postinit/00-alphaplus.conf"
    '';
  });
  # Keep eSpeak synthesis without bundling the MBROLA voice database.
  speechd = prev.speechd.override {
    espeak = final.espeak-ng.override { mbrolaSupport = false; };
  };
  iio-sensor-proxy = prev.iio-sensor-proxy.overrideAttrs (old: {
    patches = (old.patches or [ ]) ++ [
      ./patches/iio-sensor-proxy/0001-start-sensors-claimed-during-discovery.patch
      ./patches/iio-sensor-proxy/0002-discover-all-sensors-on-a-hotplugged-device.patch
    ];
  });
  geoclue2-alphaplus = prev.geoclue2.overrideAttrs (old: {
    patches = (old.patches or [ ]) ++ [
      ./patches/geoclue/0001-authorize-native-applications.patch
      ./patches/geoclue/0002-disable-ip-fallback-when-ip-disabled.patch
    ];
  });
  neard = prev.neard.overrideAttrs (old: {
    patches = (old.patches or [ ]) ++ [
      ./patches/neard/fix-tag-dbus-properties.patch
    ];
  });
  modemmanager-alphaplus = prev.modemmanager.overrideAttrs (old: {
    patches = (old.patches or [ ]) ++ [
      ./patches/modemmanager/allow-qrtr-without-data-port.patch
      ./patches/modemmanager/distinguish-sim-failure-from-absence.patch
      ./patches/modemmanager/fix-netlink-transaction-uaf.patch
      ./patches/modemmanager/preserve-ipa-mtu.patch
    ];
    mesonFlags = map (flag: if flag == "-Dqrtr=false" then "-Dqrtr=true" else flag)
      old.mesonFlags;
  });
  mobile-broadband-provider-info-alphaplus = prev.mobile-broadband-provider-info.overrideAttrs (old: {
    patches = (old.patches or [ ]) ++ [
      ./patches/mobile-broadband-provider-info/add-china-broadnet.patch
    ];
  });
  plasma-nm-alphaplus = (prev.kdePackages.plasma-nm.override {
    mobile-broadband-provider-info = final.mobile-broadband-provider-info-alphaplus;
  }).overrideAttrs (old: {
    patches = (old.patches or [ ]) ++ [
      ./patches/plasma-nm/hotspot-band-5ghz.patch
    ];
  });
  spacebar-alphaplus = prev.kdePackages.spacebar.overrideAttrs (old: {
    patches = (old.patches or [ ]) ++ [
      ./patches/spacebar/add-imsd-messaging-backend.patch
    ];
    postInstall = (old.postInstall or "") + ''
      install -Dm644 /dev/stdin "$out/share/dbus-1/services/org.kde.spacebar.Daemon.service" <<EOF
[D-BUS Service]
Name=org.kde.spacebar.Daemon
Exec=$out/libexec/spacebar-daemon
EOF
    '';
  });
  networkmanager-alphaplus = (prev.networkmanager.override {
    mobile-broadband-provider-info = final.mobile-broadband-provider-info-alphaplus;
  }).overrideAttrs (old: {
    patches = (old.patches or [ ]) ++ [
      ./patches/networkmanager/dnsmasq-bind-dynamic.patch
    ];
  });
  tqftpserv = prev.tqftpserv.overrideAttrs (old: {
    version = "1.1.1-unstable-2026-10-27";
    src = final.fetchFromGitHub {
      owner = "linux-msm";
      repo = "tqftpserv";
      rev = "eb4b9a04ff92d2753ededf32cc3b6ed287f6e29b";
      hash = "sha256-H33e1HRSedZZGZ8S00K8GCh3CNq7JZLQJuuaMHPacUc=";
    };
    buildInputs = (old.buildInputs or [ ]) ++ [ final.xz ];
    # Upstream nixpkgs patches for 1.1.1 are already merged in rev eb4b9a04.
    postPatch = "";
  });

  alphaplus-gnssd = final.callPackage ../pkgs/alphaplus-gnssd { };
  alphaplus-vss-init = final.callPackage ../pkgs/alphaplus-vss-init { };
  alphaplus-call-audio = final.callPackage ../pkgs/alphaplus-call-audio { };
  imsd-alphaplus = final.callPackage ../pkgs/imsd { };
  hexagonrpc = final.callPackage ../pkgs/hexagonrpc { };
  libssc = final.callPackage ../pkgs/libssc { };
  libcamera-alphaplus = final.callPackage ../pkgs/libcamera {
    libcamera = prev.libcamera.override { withQcam = true; };
  };
  ffmpeg-vpu = final.callPackage ../pkgs/ffmpeg-vpu { inherit (prev) ffmpeg_7; };
  qcom-va-driver = final.callPackage ../pkgs/qcom-va-driver { };
  pipewire-spa-alphaplus = final.callPackage ../pkgs/pipewire-bluetooth { };
  snapshot-alphaplus = final.callPackage ../pkgs/snapshot {
    snapshot = prev.snapshot.override { libcamera = final.libcamera-alphaplus; };
  };
  turnip-alphaplus = final.callPackage ../pkgs/turnip { };
  alphaplus-kernel = final.callPackage ../pkgs/kernel { inherit linux-src; };
  alphaplus-uboot = final.callPackage ../pkgs/boot/uboot.nix { inherit uboot-src; };
  alphaplus-desktop-helpers = final.callPackage ../pkgs/desktop-helpers { };
  alphaplus-kwin-keyboard = final.callPackage ../pkgs/kwin-keyboard { };
  alphaplus-flashlight = final.callPackage ../pkgs/flashlight { };
  alphaplus-bypass-charging = final.callPackage ../pkgs/bypass-charging { };
  alphaplus-nfc = final.callPackage ../pkgs/nfc { };
  alphaplus-usb-sleep-policy = final.callPackage ../pkgs/usb-sleep-policy { };
  fcitx5-osk-alphaplus = final.callPackage ../pkgs/fcitx5-osk { };
}
