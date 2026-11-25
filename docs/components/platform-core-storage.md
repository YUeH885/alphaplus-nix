# 平台核心与存储

## 已实现

- **SoC 与总线**：SM8150 时钟、PM8150/PM8150B/PM8150L RPMh regulator、GDSC、NoC、LLCC、TLMM、PDC、QUP GENI I2C/SPI，以及 `qcom,sm8150-cpufreq-hw`（LMH/DCVSH 调频）与 CPU PMU（`CONFIG_PERF_EVENTS=y`）。
- **存储与根文件系统**：
  - UFS（`qcom,sm8150-ufshc`、QMP UFS PHY、`CONFIG_SCSI_UFS_BSG=y`）内建于内核；MicroSD（SDHCI，GPIO96 卡槽检测）可用。
  - `PARTLABEL=product_a`（2 GiB ext4）挂载为 `/boot`，`PARTLABEL=linux`（64 GiB F2FS，`zstd` 全文件压缩与 `atgc`/`gc_merge`/`lazytime`）挂载为 `/`，内存交换使用 `zramSwap`（`zstd`）。
- **引导链与 Slot 确认**：ABL 从 `boot_a` 加载 U-Boot（`lmg820n_defconfig`，保留 `simple-framebuffer`，支持图形菜单、USB Fastboot、CDC ACM、UMS 与 UEFI 入口），默认通过 `extlinux` 读取 `/boot/extlinux/extlinux.conf` 引导 NixOS（支持 `sysboot -p` 选旧条目）；启动后 `qbootctl.service` 执行 `qbootctl -m` 确认槽位健康。
- **看门狗与崩溃日志**：`qcom-wdt` 在系统挂起时暂停、唤醒后恢复；`alphaplus-pstore-blk.service` 加载 `pstore_blk` 将崩溃日志写入 `pstore` 分区。

## 限制与未实现

- **硬件加密引擎（故意禁用）**：`crypto@1dfa000` 因实测吞吐低于 ARMv8 NEON CE 指令且无法独占硬件而保持禁用。
- **TDMB 广播**：韩国版 `spi8`（`0xa80000`）`lge,tdmb` 无内核驱动。
- **UEFI 直接引导**：主引导链走 U-Boot `extlinux`，未在 NixOS 中配置独立 UEFI 启动入口。
