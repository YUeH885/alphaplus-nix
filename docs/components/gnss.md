# GNSS

## 已实现

- **传输链路**：MPSS 通过 GLINK SMEM、QRTR 与 `libqmi` / `libqrtr-glib` 提供 QMI LOC 服务。
- **`alphaplus-gnssd`**（源码位于 [nixos/pkgs/alphaplus-gnssd/gnss/](../../nixos/pkgs/alphaplus-gnssd/gnss/)）：
  - 以 `_gnss` 用户运行，按连接客户端数动态启停 1 Hz standalone 定位（支持 `--always-track`），经 `/run/alphaplus-gnss/nmea.sock`（`0660`）广播 NMEA-0183 语句。
  - 集成 UTC 时间注入、HTTPS XTRA 轨道数据（`xtra3grcej.bin`，24 小时本地缓存）下载注入，以及 `QMI_LOC_INJECT_POSITION` 粗位置辅助注入：在 `/var/lib/alphaplus-gnss/last-position.conf` 持久化上次卫星定位（按时间衰减缩放不确定度，优先用于 1 小时内热启动），并在缺少近期缓存时经系统 D-Bus 读取 `ModemManager` `3gpp-lac-ci` 小区信息向 BeaconDB 查询粗略参考坐标（禁用 IP 回退）。
  - 监听 logind `PrepareForSleep`，挂起前停用定位会话并关闭 QRTR 端点，唤醒后恢复。
- **桌面集成**：[gnss.nix](../../nixos/modules/hardware/gnss.nix) 启用 `geoclue2` 订阅 `nmea.sock`，开启 `[3g]`（ModemManager `3gpp-lac-ci`）与 `[wifi]` BeaconDB 室内粗定位（并打补丁在 `[ip] enable=false` 时禁止服务端 GeoIP 回退及空 GeoIP 请求），授权 `_gnss` 访问 `org.freedesktop.ModemManager1.Location`，并保留桌面定位权限提示补丁。

## 限制与未实现

- **基站辅助覆盖受限于公共数据库**：`3gpp-lac-ci` 与 Wi-Fi 粗定位及辅助位置查询依赖 BeaconDB 众包覆盖，未收录基站或网络不可达时回退为缓存卫星坐标或纯卫星冷搜索；未接入专有 SUPL / XTRA-T 服务器。
