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

  alphaplus-gnssd = final.callPackage ../pkgs/alphaplus-gnssd { };
  hexagonrpc = final.callPackage ../pkgs/hexagonrpc { };
  libssc = final.callPackage ../pkgs/libssc { };
  libcamera-alphaplus = final.callPackage ../pkgs/libcamera {
    libcamera = prev.libcamera.override { withQcam = true; };
  };
  ffmpeg-vpu = final.callPackage ../pkgs/ffmpeg-vpu { inherit (prev) ffmpeg_7; };
  pipewire-spa-alphaplus = final.callPackage ../pkgs/pipewire-bluetooth { };
  snapshot-alphaplus = final.callPackage ../pkgs/snapshot {
    snapshot = prev.snapshot.override { libcamera = final.libcamera-alphaplus; };
  };
  turnip-alphaplus = final.callPackage ../pkgs/turnip { };
  alphaplus-kernel = final.callPackage ../pkgs/kernel { inherit linux-src; };
  alphaplus-uboot = final.callPackage ../pkgs/boot/uboot.nix { inherit uboot-src; };
  alphaplus-desktop-helpers = final.callPackage ../pkgs/desktop-helpers { };
  alphaplus-flashlight = final.callPackage ../pkgs/flashlight { };
  alphaplus-bypass-charging = final.callPackage ../pkgs/bypass-charging { };
  fcitx5-osk-alphaplus = final.callPackage ../pkgs/fcitx5-osk { };
}
