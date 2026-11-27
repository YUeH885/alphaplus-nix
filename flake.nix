{
  description = "NixOS configuration for LG G8 ThinQ (LM-G820N, sm8150-lg-alphaplus)";

  nixConfig = {
    extra-substituters = [ "https://alphaplus-nix.hwrng.dev" ];
    extra-trusted-public-keys = [
      "alphaplus-cache-1:vEKvOaAhSomjA2SE/+8LgiNcai6j+fl8YvKeOUwIB8M="
    ];
  };

  inputs = {
    nixpkgs.url = "github:nixos/nixpkgs/nixos-unstable";
    linux-src = {
      url = "github:YUeH885/linux/alphaplus-7.3.y";
      flake = false;
    };
    uboot-src = {
      url = "github:YUeH885/u-boot/alphaplus-v2026.10";
      flake = false;
    };
  };

  outputs = { self, nixpkgs, linux-src, uboot-src, ... }:
    let
      supportedSystems = [ "aarch64-linux" "x86_64-linux" ];
      forAllSystems = nixpkgs.lib.genAttrs supportedSystems;
      mkSystem = buildPlatform: nixpkgs.lib.nixosSystem {
        modules = [
          ({ config, lib, ... }: {
            nixpkgs = {
              inherit buildPlatform;
              hostPlatform = "aarch64-linux";
              overlays = [ self.overlays.default ];
            };
            _module.args.pkgs = lib.mkIf (buildPlatform != "aarch64-linux") (lib.mkForce (
              import ./nixos/cross-packages.nix {
                native = self.nixosConfigurations.alphaplus.pkgs;
                cross = import nixpkgs {
                  localSystem = config.nixpkgs.buildPlatform;
                  crossSystem = config.nixpkgs.hostPlatform;
                  inherit (config.nixpkgs) config overlays;
                };
              }
            ));
          })
          ./nixos/modules/hardware
          ./nixos/modules/system
          ./nixos/modules/desktop
        ];
      };

      mkRootfsImage = { pkgs, nixosConfig, name ? "alphaplus-images" }:
        let
          toplevel = nixosConfig.config.system.build.toplevel;
          closureInfo = pkgs.closureInfo { rootPaths = [ toplevel ]; };
          populateCmd = nixosConfig.config.boot.loader.generic-extlinux-compatible.populateCmd;
        in
        pkgs.runCommand name {
          nativeBuildInputs = [
            pkgs.android-tools
            pkgs.e2fsprogs
            pkgs.f2fs-tools
            pkgs.fakeroot
            pkgs.nix
            pkgs.coreutils
            pkgs.findutils
          ];
        } ''
          mkdir -p root/nix/store root/boot root/run root/bin root/nix/var/nix/profiles root/nix/var/nix/db root-boot

          while IFS= read -r path; do
            cp -a "$path" root/nix/store/
          done < ${closureInfo}/store-paths

          NIX_STATE_DIR=$(pwd)/root/nix/var/nix nix-store --load-db < ${closureInfo}/registration

          ln -s ${toplevel} root/nix/var/nix/profiles/system-1-link
          ln -s system-1-link root/nix/var/nix/profiles/system
          ln -s ${toplevel} root/run/current-system
          ln -s ${toplevel}/sw/bin/sh root/bin/sh

          # Populate the dedicated ext4 /boot partition (PARTLABEL=product_a, 2 GiB).
          ${populateCmd} -c ${toplevel} -d root-boot
          truncate -s 2G boot.ext4
          fakeroot -- sh -ec '
            chown -R 0:0 root-boot
            mke2fs -q -t ext4 -F -b 4096 -m 0 -L NIXOS_BOOT -d root-boot boot.ext4
          '
          e2fsck -fn boot.ext4

          # Populate the F2FS root partition (PARTLABEL=linux, 64 GiB) in 4096-byte blocks.
          truncate -s 64G rootfs.f2fs
          mkfs.f2fs -q -f -t 0 -l NixOS -R 0:0 -O extra_attr,inode_checksum,sb_checksum,compression rootfs.f2fs
          fakeroot -- sh -ec '
            chown -R 0:0 root
            sload.f2fs -P -c -f root rootfs.f2fs
          '
          fsck.f2fs -f rootfs.f2fs

          mkdir -p "$out"
          img2simg -s boot.ext4 "$out/boot.ext4" 4096
          img2simg -s rootfs.f2fs "$out/rootfs.f2fs" 4096
          ln -s ${pkgs.android-tools}/bin "$out/android-tools"
          ln -s ${pkgs.f2fs-tools}/bin "$out/f2fs-tools"
        '';
    in
    {
      overlays.default = import ./nixos/overlays { inherit linux-src uboot-src; };

      packages = forAllSystems (system:
        let
          pkgs = import nixpkgs {
            inherit system;
            config.allowUnfree = true;
            overlays = [ self.overlays.default ];
          };
          nixosConfig = if system == "aarch64-linux" then self.nixosConfigurations.alphaplus
            else self.nixosConfigurations.alphaplus-cross;
          pkgsAarch64 = nixosConfig.pkgs;
        in
        {
          kernel = pkgsAarch64.alphaplus-kernel;
          uboot = pkgsAarch64.alphaplus-uboot;
          boot = pkgs.callPackage ./nixos/pkgs/boot { uboot = pkgsAarch64.alphaplus-uboot; };

          alphaplus-gnssd = pkgsAarch64.alphaplus-gnssd;
          hexagonrpc = pkgsAarch64.hexagonrpc;
          libssc = pkgsAarch64.libssc;
          libcamera = pkgsAarch64.libcamera-alphaplus;
          ffmpeg-vpu = pkgsAarch64.ffmpeg-vpu;
          qcom-va-driver = pkgsAarch64.qcom-va-driver;
          desktop-helpers = pkgsAarch64.alphaplus-desktop-helpers;

          default = self.packages.${system}.rootfs;
          rootfs = mkRootfsImage {
            inherit pkgs nixosConfig;
            name = "alphaplus-images";
          };
        }
      );

      nixosConfigurations = {
        alphaplus = mkSystem "aarch64-linux";
        alphaplus-cross = mkSystem "x86_64-linux";
      };
    };
}
