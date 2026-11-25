# 传感器

## 已实现

- **SLPI 与 FastRPC**：DTS 启用 `remoteproc_slpi`（`slpi.mdt`、20 MiB 保留内存），`qcom_pd_mapper` 注册 `msm/slpi/root_pd` 与 `msm/slpi/sensor_pd`；FastRPC 支持 34 位 IOVA 高位 SID 别名映射及被信号中断后的上下文恢复。
- **校准与文件系统服务（[`hexagonrpc`](../../nixos/pkgs/hexagonrpc/default.nix)）**：[sensors.nix](../../nixos/modules/hardware/sensors.nix) 将 `nixos/files/sensors/` 打包至 `/usr/share/qcom/sm8150/LG/alphaplus/` 并在 `/var/lib/alphaplus/sensors/registry` 初始化可写状态目录；`60-hexagonrpcd.rules` 为 `fastrpc` 用户授权并按 udev `SYSTEMD_WANTS` 拉起 `hexagonrpcd.service`。
- **SSC 与 SensorProxy**：`libssc` 提供多级接近、陀螺仪（`rad/s`）及在线校准支持；`iio-sensor-proxy` 通过 overlay 补丁修复热插拔发现竞态；`81-alphaplus-sensors.rules` 配置 `ACCEL_MOUNT_MATRIX="-1,0,0;0,-1,0;0,0,1"` 对齐屏幕自动旋转。

## 未实现

- **ATMF04 SAR 接近传感器**：尚未接入韩国版硬件对应的内核驱动与用户空间处理。
- **跨设备出厂校准解耦**：`nixos/files/sensors/sensors/registry/` 当前内置特定样机的 `fac_cal` 校准种子，其余设备需替换为各自 `persist`/`mnt` 提取的校准文件。
