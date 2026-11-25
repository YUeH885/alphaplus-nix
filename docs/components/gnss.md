# GNSS

## 已实现

- **传输链路**：MPSS 通过 GLINK SMEM、QRTR 与 `libqmi` / `libqrtr-glib` 提供 QMI LOC 服务。
- **`alphaplus-gnssd`**（源码位于 [nixos/pkgs/alphaplus-gnssd/gnss/](../../nixos/pkgs/alphaplus-gnssd/gnss/)）：
  - 以 `_gnss` 用户运行，按连接客户端数动态启停 1 Hz standalone 定位（支持 `--always-track`），经 `/run/alphaplus-gnss/nmea.sock`（`0660`）广播 NMEA-0183 语句。
  - 集成 UTC 时间注入与 HTTPS XTRA 轨道数据（`xtra3grcej.bin`，24 小时本地缓存）下载注入。
  - 监听 logind `PrepareForSleep`，挂起前停用定位会话并关闭 QRTR 端点，唤醒后恢复。
- **桌面集成**：[gnss.nix](../../nixos/modules/hardware/gnss.nix) 启用 `geoclue2` 订阅 `nmea.sock`，并补充桌面定位权限提示补丁。

## 未实现

- **基站辅助定位**：尚未接入 ModemManager Location 蜂窝辅助定位，当前仅依赖 QMI LOC 卫星独立定位。
