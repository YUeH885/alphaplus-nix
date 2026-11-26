{ native, cross }:

let
  build = cross.buildPackages;
  packages = native // {
    inherit (cross)
      stdenv stdenvNoCC buildPackages pkgsStatic
      runCommand runCommandLocal symlinkJoin formats
      writeText writeTextFile writeTextDir writeScript writeScriptBin
      writeShellScript writeShellScriptBin writeShellApplication
      runtimeShell bashNonInteractive
      buildEnv closureInfo linkFarm concatText
      replaceVars replaceVarsWith substitute
      linuxPackagesFor aggregateModules makeModulesClosure makeInitrdNG
      glibcLocales;
    # Executed on the build host by NixOS's ensure-wrapper-integrity check.
    inherit (build) libcap-text-verifier;
    srcOnly = package: cross.srcOnly {
      inherit (package) name src;
      inherit (package.drvAttrs) patches postPatch;
      stdenv = cross.stdenvNoCC;
    };
  };
  libssc = native.libssc.override {
    inherit (cross) stdenv buildPackages meson pkg-config;
    inherit (build) ninja fetchzip;
  };
  libcamera = (native.libcamera.override {
    withQcam = true;
    inherit (cross) stdenv meson pkg-config;
    inherit (build) ninja makeFontsConf python3;
    python3Packages = build.python3Packages // {
      inherit (native.python3Packages) pybind11;
    };
    qt6 = native.qt6 // {
      wrapQtAppsHook = cross.qt6.wrapQtAppsHook.override {
        inherit (native.qt6) qtbase;
        # Generate the launcher without running the target compiler.
        makeBinaryWrapper = build.makeWrapper;
      };
    };
  }).overrideAttrs (old: {
    nativeBuildInputs = map (dep: if dep == native.openssl then build.openssl else dep)
      old.nativeBuildInputs;
    buildInputs = (old.buildInputs or [ ]) ++ [ native.bashNonInteractive ];
    # Point meson's Qt6 tool discovery to build-host moc/uic/rcc while compiling against ARM64 Qt6 headers.
    preConfigure = (old.preConfigure or "") + ''
      mkdir -p "$TMPDIR/qt6-pc"
      sed -e "s|^bindir=.*|bindir=${build.qt6.qtbase}/bin|" \
          -e "s|^libexecdir=.*|libexecdir=${build.qt6.qtbase}/libexec|" \
          "${native.qt6.qtbase}/lib/pkgconfig/Qt6Core.pc" > "$TMPDIR/qt6-pc/Qt6Core.pc"
      export PKG_CONFIG_PATH="$TMPDIR/qt6-pc''${PKG_CONFIG_PATH:+:$PKG_CONFIG_PATH}"
    '';
    postFixup = (old.postFixup or "") + ''
      patchShebangs --host --update "$out/bin"
    '';
  });
  libcamera-alphaplus = import ./pkgs/libcamera {
    inherit libcamera libssc;
    inherit (native) onnxruntime;
  };
  ffmpeg = native.ffmpeg_7.override {
    inherit (cross) stdenv buildPackages pkg-config;
    inherit (build) removeReferencesTo addDriverRunpath perl texinfo;
    withAss = true;
  };
  perlBuildEnv = native.perl.buildEnv.override {
    inherit (cross) buildEnv makeBinaryWrapper stdenv;
  };
  nixos-enter = native.nixos-enter.override {
    inherit (cross) replaceVarsWith;
    inherit (build) installShellFiles;
  };
  mobile-broadband-provider-info-alphaplus = native.mobile-broadband-provider-info-alphaplus.override {
    inherit (cross) stdenv;
    inherit (build) libxslt libxml2 meson ninja;
  };
  modemmanager-alphaplus = (native.modemmanager-alphaplus.override {
    inherit (cross) stdenv meson pkg-config udevCheckHook;
    inherit (build) gettext libxslt ninja python3;
    withIntrospection = false;
  }).overrideAttrs (old: {
    nativeBuildInputs = map (dep: if dep == native.glib then build.glib else dep)
      old.nativeBuildInputs;
  });
  # Build KDE derivations natively on x86_64 tools while linking against native ARM64 Qt6/KF6 libs.
  mkKdeDerivationCross = native.kdePackages.mkKdeDerivation.override {
    inherit (cross) stdenv makeSetupHook;
    inherit (build) cmake ninja jq python3 python3Packages;
    qt6 = native.qt6 // {
      wrapQtAppsHook = cross.qt6.wrapQtAppsHook.override {
        inherit (native.qt6) qtbase;
        makeBinaryWrapper = build.makeWrapper;
      };
      qtdeclarative = build.qt6.qtdeclarative;
    };
  };
  # Rebuild callaudiod with build-host meson/ninja for plasma-dialer's dependency graph.
  callaudiod = (native.callaudiod.override {
    inherit (cross) stdenv meson pkg-config;
    inherit (build) ninja;
  }).overrideAttrs (old: {
    nativeBuildInputs = old.nativeBuildInputs ++ [ build.glib.dev ];
  });
in
# Reuse binary-cache paths for ARM64 packages while running system generators
# and the board's source builds on the build platform.
packages // {
  callPackage = native.lib.callPackageWith packages;
  makeDBusConf = cross.makeDBusConf.override { dbus = native.dbus; };
  # Both configurations use fontconfig's little-endian 64-bit cache format.
  makeFontsCache = { fontDirectories, ... }: build.makeFontsCache { inherit fontDirectories; };
  qt6Packages = native.qt6Packages // {
    fcitx5-with-addons = native.qt6Packages.fcitx5-with-addons.override {
      inherit (cross) symlinkJoin makeBinaryWrapper;
    };
  };
  perl = native.perl // {
    buildEnv = perlBuildEnv;
    withPackages = f: perlBuildEnv.override { extraLibs = f native.perl.pkgs; };
  };
  inherit nixos-enter;
  nixos-install = native.nixos-install.override {
    inherit nixos-enter;
    inherit (cross) replaceVarsWith;
    inherit (build) installShellFiles;
  };
  nixos-build-vms = native.nixos-build-vms.override {
    inherit (cross) replaceVarsWith;
    inherit (build) installShellFiles;
  };

  ffmpeg-vpu = import ./pkgs/ffmpeg-vpu { ffmpeg_7 = ffmpeg; };
  inherit libcamera-alphaplus libssc;
  iio-sensor-proxy = native.iio-sensor-proxy.override {
    inherit libssc;
    inherit (cross) stdenv meson pkg-config udevCheckHook;
    inherit (build) cmake glib libxml2 ninja;
  };
  neard = (native.neard.override {
    inherit (cross) stdenv autoreconfHook pkg-config;
    inherit (build) autoconf-archive;
    gobject-introspection = null;
    wrapGAppsHook3 = null;
    python3Packages = native.python3Packages // {
      wrapPython = cross.python3Packages.wrapPython.override {
        python = native.python3Packages.python // {
          pythonOnBuildForHost = build.python3Packages.python;
        };
      };
    };
  }).overrideAttrs (_: {
    doCheck = false;
  });
  hexagonrpc = cross.hexagonrpc;
  geoclue2-alphaplus = (native.geoclue2-alphaplus.override {
    inherit (cross) stdenv meson pkg-config;
    inherit (build) intltool ninja python3;
    wrapGAppsHook3 = cross.wrapGAppsNoGuiHook.override { inherit (native) dconf; };
    vala = null;
    gobject-introspection = null;
    gtk-doc = null;
    docbook-xsl-nons = null;
    docbook_xml_dtd_412 = null;
    mesonEmulatorHook = null;
  }).overrideAttrs (old: {
    nativeBuildInputs = (old.nativeBuildInputs or [ ]) ++ [ build.glib.dev ];
    mesonFlags = old.mesonFlags ++ [ "-Dintrospection=false" "-Dgtk-doc=false" ];
    NIX_LDFLAGS = (old.NIX_LDFLAGS or "") + " -rpath-link ${native.stdenv.cc.cc.lib}/lib";
    postInstall = (old.postInstall or "") + ''
      mkdir -p "$devdoc"
    '';
  });
  speechd = native.speechd.override {
    inherit (cross) stdenv pkg-config autoreconfHook replaceVars;
    inherit (build) gettext libtool itstool texinfo;
    python3Packages = native.python3Packages // {
      inherit (build.python3Packages) python;
      wrapPython = cross.python3Packages.wrapPython.override {
        python = native.python3Packages.python // {
          pythonOnBuildForHost = build.python3Packages.python;
        };
      };
    };
    espeak = cross.espeak-ng.override {
      inherit (native) alsa-plugins pcaudiolib sonic;
      buildPackages = build // {
        espeak-ng = build.espeak-ng.override { mbrolaSupport = false; };
      };
      mbrolaSupport = false;
    };
  };
  alsa-utils = native.alsa-utils.override {
    inherit (cross) stdenv makeWrapper pkg-config symlinkJoin;
    inherit (build) gettext;
  };
  alphaplus-gnssd = native.alphaplus-gnssd.override {
    inherit (cross) stdenv meson pkg-config;
    inherit (build) ninja;
  };
  alphaplus-vss-init = native.alphaplus-vss-init.override {
    inherit (cross) stdenv;
  };
  alphaplus-call-audio = native.alphaplus-call-audio.override {
    inherit (cross) stdenv pkg-config;
    inherit modemmanager-alphaplus;
  };
  imsd-alphaplus = native.imsd-alphaplus.override {
    inherit (cross) stdenv pkg-config makeWrapper;
    inherit (build) fetchgit;
  };
  inherit modemmanager-alphaplus mobile-broadband-provider-info-alphaplus callaudiod;
  kdePackages = native.kdePackages // {
    # Override plasma-dialer so its build tools run natively on x86_64 with the cross callaudiod.
    plasma-dialer = (native.kdePackages.plasma-dialer.override {
      inherit callaudiod;
      inherit (cross) pkg-config;
      inherit (build.qt6) qtbase;
      mkKdeDerivation = mkKdeDerivationCross;
    }).overrideAttrs (old: {
      buildInputs = map (dep: if dep == build.qt6.qtbase then native.qt6.qtbase else dep)
        old.buildInputs;
    });
  };
  spacebar-alphaplus = native.spacebar-alphaplus.override {
    inherit (cross) pkg-config;
    mkKdeDerivation = mkKdeDerivationCross;
  };
  plasma-nm-alphaplus = native.plasma-nm-alphaplus.override {
    mkKdeDerivation = mkKdeDerivationCross;
    inherit (cross) replaceVars pkg-config;
    mobile-broadband-provider-info = mobile-broadband-provider-info-alphaplus;
  };
  networkmanager-alphaplus = (native.networkmanager-alphaplus.override {
    inherit (cross) stdenv meson pkg-config replaceVars udevCheckHook;
    inherit (build) bpftools clang elfutils gettext gnused ninja perl python3
      mesonEmulatorHook;
    docbook_xml_dtd_412 = null;
    docbook_xml_dtd_42 = null;
    docbook_xml_dtd_43 = null;
    docbook_xsl = null;
    gtk-doc = null;
    libxslt = null;
    vala = null;
    gobject-introspection = null;
    mobile-broadband-provider-info = mobile-broadband-provider-info-alphaplus;
  }).overrideAttrs (old: {
    mesonFlags = old.mesonFlags ++ [ "-Dintrospection=false" "-Dvapi=false" "-Ddocs=false" ];
    nativeBuildInputs = builtins.filter (dep: dep != null) old.nativeBuildInputs ++ [ build.glib.dev ];
    NIX_LDFLAGS = (old.NIX_LDFLAGS or "") + " -rpath-link ${native.stdenv.cc.cc.lib}/lib";
  });
  tqftpserv = native.tqftpserv.override {
    inherit (cross) stdenv meson pkg-config;
    inherit (build) ninja fetchFromGitHub;
  };
  alphaplus-kernel = cross.alphaplus-kernel;
  alphaplus-uboot = cross.alphaplus-uboot;
  alphaplus-desktop-helpers = native.alphaplus-desktop-helpers.override {
    inherit (cross) stdenv makeWrapper;
  };
  alphaplus-usb-sleep-policy = native.alphaplus-usb-sleep-policy.override {
    inherit (cross) stdenv buildPackages;
  };
  alphaplus-flashlight = import ./pkgs/flashlight {
    inherit (native) lib qt6;
    inherit (cross) stdenv buildPackages pkg-config;
  };
  alphaplus-bypass-charging = import ./pkgs/bypass-charging {
    inherit (native) lib qt6;
    inherit (cross) stdenv buildPackages pkg-config;
  };
  alphaplus-nfc = import ./pkgs/nfc {
    inherit (native) lib qt6;
    inherit (cross) stdenv buildPackages pkg-config;
  };
  fcitx5-osk-alphaplus = native.fcitx5-osk-alphaplus.override {
    inherit (cross) rustPlatform stdenv makeWrapper;
    inherit (build) fetchurl;
  };
  pipewire-spa-alphaplus = native.pipewire-spa-alphaplus.override {
    inherit libcamera-alphaplus;
    inherit (cross) stdenv buildPackages meson pkg-config;
    inherit (build) ninja;
  };
  turnip-alphaplus = native.turnip-alphaplus.override {
    inherit (cross) stdenv buildPackages pkg-config;
  };
  snapshot-alphaplus = import ./pkgs/snapshot {
    snapshot = (native.snapshot.override {
      inherit (cross) stdenv meson pkg-config rustPlatform;
      inherit (cross.pkgsBuildTarget) rustc;
      inherit (build) cargo desktop-file-utils ninja;
      libcamera = libcamera-alphaplus;
      wrapGAppsHook4 = cross.wrapGAppsNoGuiHook.override {
        inherit (native) dconf;
        inherit (build) makeWrapper;
      };
    }).overrideAttrs (old: {
      strictDeps = true;
      nativeBuildInputs = old.nativeBuildInputs ++ [ build.glib.dev build.gettext ];
      buildInputs = old.buildInputs ++ [ native.bashNonInteractive ];
      NIX_LDFLAGS = (old.NIX_LDFLAGS or "") + " -rpath-link ${native.stdenv.cc.cc.lib}/lib";
      postPatch = (old.postPatch or "") + ''
        substituteInPlace meson.build --replace-fail 'gtk_update_icon_cache: true' 'gtk_update_icon_cache: false'
      '';
      preConfigure = (old.preConfigure or "") + ''
        export PATH="${cross.pkgsBuildTarget.rustc}/bin:$PATH"
      '';
      postFixup = (old.postFixup or "") + ''
        patchShebangs --host --update "$out/bin"
      '';
    });
  };
}
