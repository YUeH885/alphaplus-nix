# 传感器

## 已实现

- **SLPI 与 FastRPC**：DTS 启用 `remoteproc_slpi`（`slpi.mdt`、20 MiB 保留内存），`qcom_pd_mapper` 注册 `msm/slpi/root_pd` 与 `msm/slpi/sensor_pd`；FastRPC 支持 34 位 IOVA 高位 SID 别名映射及被信号中断后的上下文恢复。
- **校准与文件系统服务（[`hexagonrpc`](../../nixos/pkgs/hexagonrpc/default.nix)）**：[sensors.nix](../../nixos/modules/hardware/sensors.nix) 将 `nixos/files/sensors/` 打包至 `/usr/share/qcom/sm8150/LG/alphaplus/`，并在 `hexagonrpcd.service` 首次启动时以只读方式挂载本机 `PARTLABEL=sns` 分区（`/dev/disk/by-partlabel/sns`），将其 `sensors/registry/`（含本机出厂 `fac_cal` 与持久校准）导入可写状态目录 `/var/lib/alphaplus/sensors/registry`（分区不可用时回退由 SLPI 按 `sensors/config/*.json` 自动生成默认注册表）；`60-hexagonrpcd.rules` 为 `fastrpc` 用户授权设备节点，[81-alphaplus-sensors.rules](../../nixos/files/udev/rules.d/81-alphaplus-sensors.rules) 通过 udev `SYSTEMD_WANTS` 拉起 `hexagonrpcd.service`，后者在 FastRPC 反向监听初始化完成后通过 `Type=notify`（`READY=1`）再放行 `iio-sensor-proxy`。
- **SSC 与 SensorProxy**：`libssc` 提供多级接近、陀螺仪（`rad/s`）、在线校准与 DSP 初始化期空 SUID 响应短时异步重试支持；`iio-sensor-proxy` 通过 overlay 补丁修复热插拔发现竞态；`81-alphaplus-sensors.rules` 配置 `ACCEL_MOUNT_MATRIX="-1,0,0;0,-1,0;0,0,1"` 对齐屏幕自动旋转。

## 未实现

- **ATMF04 SAR 接近传感器**：尚未接入韩国版硬件对应的内核驱动与用户空间处理。
