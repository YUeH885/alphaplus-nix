# alphaplus-nix

LG G8 ThinQ（`LM-G820N`，Qualcomm SM8150 / `alphaplus` 韩版）的 NixOS Flake 与板级适配仓库：基于主线 Linux `7.3.6`、U-Boot `v2026.10` 与 KDE Plasma 6（Wayland）。

## 硬件支持

| 子系统 | 状态 | 说明 | 专题 |
| --- | --- | --- | --- |
| 平台核心与存储 | 可用 | SM8150、UFS/F2FS、MicroSD、U-Boot extlinux、pstore；未做 UEFI 直启与 TDMB | [platform-core-storage.md](docs/components/platform-core-storage.md) |
| USB Type-C 与 DP | 可用 | USB 3.1 DRP/OTG、CDC 网卡/串口、DP Alt Mode 双屏输出 | [usb-typec-dp.md](docs/components/usb-typec-dp.md) |
| 显示、输入与图形 | 可用 | SW43410 1440×3120@60、Adreno 640（Turnip）、触摸板仿真、OSK；缺 AOD/HDR/局部刷新 | [display-input.md](docs/components/display-input.md) |
| 电源、充电与热管理 | 可用 | PM8150B + SMB1355 并联 PD 快充、VTS 温控、维护回睡、充电阈值与旁路供电 | [power-charging-thermal.md](docs/components/power-charging-thermal.md) |
| 无线与 Modem | 可用 | WCN3990 Wi-Fi（VHT160）、WCN3998 蓝牙（FTM MAC）、LTE 移动数据、`imsd` VoLTE 双向通话与 SMS over IP（Spacebar） | [wireless-modem.md](docs/components/wireless-modem.md) |
| GNSS | 可用 | MPSS QMI LOC、NMEA socket、XTRA / 粗位置注入与 ModemManager / BeaconDB 辅助 GeoClue | [gnss.md](docs/components/gnss.md) |
| 音频与触觉 | 可用 | WCD9340、ES9218P Quad DAC、双扬声器/听筒、BT SCO、Q6Voice（`VoiceMMode1`）、DW7800；缺 FM 收音 | [audio-haptics.md](docs/components/audio-haptics.md) |
| 相机 | 可用 | 4 颗 RGB（3A/PDAF/ToF-AF/OIS/FD501/FC4）+ 前置 ToF 深度相机 | [camera.md](docs/components/camera.md) |
| CDSP | 部分 | `remoteproc_cdsp` + `hexagonrpcd` 基础计算通路可用；缺 HVX/NN 运行时与安全计算域 | [cdsp.md](docs/components/cdsp.md) |
| VPU | 可用 | Iris 1.xx 4K 解码/编码、LLCC、动态 DDR 带宽缩放、8-bit UBWC 与 VA-API（`qcom-va-driver`，含 Vulkan VPP 与单上下文 DRC）；缺 CDSP VPP、10-bit UBWC EGL 导入与安全视频/8K | [vpu.md](docs/components/vpu.md) |
| 辅助外设 | 部分 | PM8941 RTC、PN547 NFC（`neard` + 托盘读写 UI）、PM8150L 闪光灯与托盘手电筒；缺 Goodix 指纹与 HCE | [auxiliary-peripherals.md](docs/components/auxiliary-peripherals.md) |
| 传感器 | 可用 | SLPI FastRPC + `hexagonrpc` + `libssc` + `iio-sensor-proxy` | [sensors.md](docs/components/sensors.md) |

## 快速开始

仓库默认启用 Cloudflare R2 二进制缓存（`https://alphaplus-nix.hwrng.dev`），设备端与外部构建机可直接拉取预编译的内核与定制包：

```bash
# 1. 构建可刷写镜像（dist/boot.img、dist/boot.ext4、dist/rootfs.f2fs）
tools/build-images.sh

# 2. 在 x86_64 工作站交叉构建并通过 SSH 部署到设备
tools/deploy-nixos.sh

# 3. 直接在设备端通过二进制缓存更新系统
sudo nixos-rebuild switch --flake github:YUeH885/alphaplus-nix#alphaplus
```

所有板级用户态源码与补丁均在 `nixos/` 内。如需修改内核或 U-Boot 源码，可额外检出对应仓库到本地 `source/`（脚本会自动附加 `--override-input`）：

```bash
git clone -b alphaplus-7.3.y https://github.com/YUeH885/linux.git source/linux
git clone -b alphaplus-v2026.10 https://github.com/YUeH885/u-boot.git source/u-boot
```

## 目录与文档

- [nixos/](nixos/)：NixOS 模块、交叉构建覆盖、板级软件包与固件/配置资产（内核配置见 [alphaplus-kernel.config](nixos/pkgs/kernel/alphaplus-kernel.config)）
- [tools/](tools/)：镜像构建、在线部署、boot 刷写及相机/VPU/SSC 验证工具
- [.github/](.github/)：GitHub Actions 构建与 R2 缓存发布工作流
- [source/](source/)（不纳入 Git）：本地可选的 Linux / U-Boot 源码工作区
- 文档索引：
  - [构建与缓存](docs/build.md)
  - [刷写与回滚](docs/flashing-and-recovery.md)
  - [固件清单](docs/firmware.md)
  - [硬件组件专题](docs/components/README.md)
