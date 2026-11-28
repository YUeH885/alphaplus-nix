# 无线与 Modem

## WLAN（WCN3990）

- 使用 `ath10k_snoc` 与 mac80211/cfg80211（含 VHT160 副 80 MHz DFS 与 1x1 NSS/VHT MCS 修复），由 [firmware.nix](../../nixos/modules/hardware/firmware.nix) 提供 `wlanmdsp.mbn`、`board.bin` 与 `firmware-5.bin`。
- `ath10k_snoc` 跟踪 CE 中断的实际启用状态，支持基带在 Wi-Fi 初始化期间重启后的自动恢复。
- [wifi.nix](../../nixos/modules/hardware/wifi.nix) 通过 `alphaplus-wifi-address.service` 在加载驱动前读取本机 `ftm` 分区生成 `/run/alphaplus/firmware/ath10k/WCN3990/hw1.0/wlan_mac.bin`。
- 由 NetworkManager（`wpa_supplicant` 后端）管理连接并设置 `CN` 监管域，不在镜像内预置 Wi-Fi 凭据。
- 开启 `ipv4.method=shared` 热点时，[networkmanager-alphaplus](../../nixos/overlays/default.nix)（`dnsmasq-bind-dynamic.patch`）将热点 `dnsmasq` 改为 `--bind-dynamic --interface=<iface>` 并经 `/run/NetworkManager/no-stub-resolv.conf` 直连上游 DNS，[hotspot-ipv6.py](../../nixos/files/network/hotspot-ipv6.py) 针对蜂窝 `RAWIP` 上游按 RFC 7278 通过 Proxy NDP 与 `radvd` 分发原生 `/64` 前缀及链路本地 RDNSS（非 `RAWIP` 上游回落至 ULA NAT66）。
- 桌面热点由 [plasma-nm-alphaplus](../../nixos/overlays/default.nix)（`hotspot-band-5ghz.patch`）提供频段配置（默认 `HotspotBand=a`，在支持 5 GHz 的网卡上选用信道 36 与 80 MHz 频宽以启用 802.11ac / Wi-Fi 5，可在 Plasma 设置或连接配置对话框切换回 2.4 GHz）。

## Bluetooth（WCN3998）

- 挂载于 UART13（3.2 Mbaud，GPIO46 唤醒中断），加载 `qca/lmg820n-crbtfw21.tlv` 与 `qca/lmg820n-crnv21.bin`；启用 `AF_ALG`、`UHID` 与硬件 SCO 卸载。
- [bluetooth.nix](../../nixos/modules/hardware/bluetooth.nix) 屏蔽 `hci_uart` 自动加载，由 `alphaplus-bluetooth-address.service` 从 `ftm` 读取出厂地址并通过 `Set Public Address` 写入后再启动 BlueZ。

## Modem 基础

- `rmtfs`、`tqftpserv` 与 `alphaplus-remoteproc-boot.service` 负责拉起 MPSS（`modem.mdt`）并提供 EFS/TFTP 服务（`tqftpserv` 将 `persist` 分区 `rfs/msm/mpss` 同步至可写状态目录 `/var/lib/tqftpserv`）。GNSS 使用 QMI LOC（见 [gnss.md](gnss.md)）。
- `alphaplus-vss-init.service` 监听 QRTR `vss_common`（service `800`）上线与 SSR 重启事件，发送 `QMI_VSS_SERVER_CONNECT_CB_REQ_MSG_V02`（`0x0602`）握手以解除 MPSS `simlock`/USIM 初始化阻塞；之后 ModemManager 通过 QRTR/QMI 枚举基带、切换为 `online`、激活 SIM 并注册 LTE 网络（`mmcli -L`、`mmcli -m any` 查看）。
- `modemmanager-alphaplus` 已修复 `mm-netlink.c` 中 `transaction_complete()` 的释放后使用（UAF）缺陷，并通过 `preserve-ipa-mtu.patch` 保留 `ipa` 驱动设置的 `1508` MTU；配合内核 `IPA_MTU` 为 QMAP 与 UL Checksum v4 预留 8 字节头部空间，`qmapmux0.0` 可协商至标准 `1500` MTU，并通过 NetworkManager / Plasma NM 自动配置 IPv4/IPv6、路由与 DNS。
- `mobile-broadband-provider-info-alphaplus` 补充了中国广电（`46015`、APN `cbnet`）条目，并单独注入 `plasma-nm-alphaplus` 与 `networkmanager-alphaplus`，桌面向导与 `gsm.auto-config` 均可直接识别。

## 蜂窝通话与短信

- LTE 通话由 `imsd` 通过 ModemManager 请求独立的 `ims` 承载。基带缺少 IPv6 IMS 配置时，`imsd` 创建一条启用 P-CSCF PCO 请求的配置，再读取运营商返回的地址。IMS 注册使用 USIM AKA 和内核 XFRM/ESP（已适配无 `qop` AKA 鉴权、AUTS 重同步、基于服务小区的 FDD/TDD `P-Access-Network-Info` 以及接听后的 delayed-offer `re-INVITE`/`ACK` 媒体重协商）。`imsd-radio-gate.service` 监听 ModemManager 的 modem 状态：射频启用（含 `locked`）时启动 `imsd.service`，状态变为 `disabled`（关闭 WWAN）时停止，`imsd` 随 `ModemManager.service` 重启而重启；Plasma Dialer 随桌面会话常驻 `imsd-dialerd` 以接听来电并呼叫。
- 基带直接处理的通话仍可通过 ModemManager Voice 接口控制，音频使用 QDSP6 的 `VoiceMMode1`。详见 [音频与触觉](audio-haptics.md)。
- 短信默认通过 `imsd` 的 SMS over IP 收发（[modem.nix](../../nixos/modules/hardware/modem.nix) 中设置 `SMS_OVER_IMS=1`，由 SIM 卡 `EF_SMSP` 读取短信中心地址）。桌面端采用 `spacebar-alphaplus`（[add-imsd-messaging-backend.patch](../../nixos/overlays/patches/spacebar/add-imsd-messaging-backend.patch)），在 `spacebar-daemon` 中通过 `net.catcrafts.IMS1.Messaging` 导入暂存短信、接收实时来信信号、提交发送并跟踪 `sent` / `failed` 状态；未启用 SMS over IP 时仍保留原有 ModemManager 通路。
