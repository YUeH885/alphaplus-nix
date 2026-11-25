{ lib, buildUBoot, buildPackages, uboot-src }:

(buildUBoot {
  defconfig = "lmg820n_defconfig";
  version = "2026.10-alphaplus";
  src = uboot-src;

  filesToInstall = [ "u-boot.bin" "dts/dt.dtb" ];

  postInstall = ''
    ln -s dt.dtb "$out/u-boot.dtb"
  '';

  extraMeta = {
    description = "U-Boot bootloader for LG G8 ThinQ (LM-G820N)";
    platforms = [ "aarch64-linux" ];
  };
}).overrideAttrs (old: {
  nativeBuildInputs = (old.nativeBuildInputs or [ ]) ++ [ buildPackages.xxd ];
})
