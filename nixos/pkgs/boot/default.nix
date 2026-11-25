{ runCommand, android-tools, gzip, cpio, uboot }:

runCommand "alphaplus-boot-image" {
  nativeBuildInputs = [ android-tools gzip cpio ];
} ''
  gzip -n -c "${uboot}/u-boot.bin" > kernel.gz
  : | cpio -o -H newc > ramdisk.cpio 2>/dev/null

  mkdir -p "$out"
  mkbootimg \
    --header_version 2 \
    --pagesize 4096 \
    --base 0x00000000 \
    --kernel_offset 0x00008000 \
    --ramdisk_offset 0x01000000 \
    --second_offset 0x00000000 \
    --tags_offset 0x00000100 \
    --dtb_offset 0x01f00000 \
    --os_version 14.0.0 \
    --os_patch_level 2025-09 \
    --cmdline 'androidboot.hardware=qcom androidboot.memcg=1 lpm_levels.sleep_disabled=1 msm_rtb.filter=0x237 service_locator.enable=1 swiotlb=2048 firmware_class.path=/vendor/firmware_mnt/image loop.max_part=7 androidboot.usbcontroller=a600000.dwc3 kpti=off androidboot.init_fatal_reboot_target=recovery androidboot.boot_devices=soc/1d84000.ufshc' \
    --kernel kernel.gz \
    --ramdisk ramdisk.cpio \
    --dtb "${./base.dtb}" \
    -o "$out/boot.img"

  avbtool add_hash_footer \
    --image "$out/boot.img" \
    --partition_size $((0x6000000)) \
    --partition_name boot \
    --salt 0000000000000000000000000000000000000000000000000000000000000000
''
