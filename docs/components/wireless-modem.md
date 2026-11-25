# 无线与 Modem

## WLAN（WCN3990）

- 使用 `ath10k_snoc` 与 mac80211/cfg80211（含 VHT160 副 80 MHz DFS 与 1x1 NSS/VHT MCS 修复），由 [firmware.nix](../../nixos/modules/hardware/firmware.nix) 提供 `wlanmdsp.mbn`、`board.bin` 与 `firmware-5.bin`。
- [wifi.nix](../../nixos/modules/hardware/wifi.nix) 通过 `alphaplus-wifi-address.service` 在加载驱动前读取本机 `ftm` 分区生成 `/run/alphaplus/firmware/ath10k/WCN3990/hw1.0/wlan_mac.bin`。
- 由 NetworkManager（`wpa_supplicant` 后端）管理连接并设置 `CN` 监管域，不在镜像内预置 Wi-Fi 凭据。

## Bluetooth（WCN3998）

- 挂载于 UART13（3.2 Mbaud，GPIO46 唤醒中断），加载 `qca/lmg820n-crbtfw21.tlv` 与 `qca/lmg820n-crnv21.bin`；启用 `AF_ALG`、`UHID` 与硬件 SCO 卸载。
- [bluetooth.nix](../../nixos/modules/hardware/bluetooth.nix) 屏蔽 `hci_uart` 自动加载，由 `alphaplus-bluetooth-address.service` 从 `ftm` 读取出厂地址并通过 `Set Public Address` 写入后再启动 BlueZ。

## Modem 基础

`rmtfs`、`tqftpserv` 与 `alphaplus-remoteproc-boot.service` 负责拉起 MPSS（`modem.mdt`）并提供 EFS/TFTP 服务，目前用于驱动 QMI LOC GNSS（见 [gnss.md](gnss.md)）。

## 未实现

- **蜂窝通话与移动数据**：Modem 语音通话（QDSP6 MVM/CVS/CVP）与移动数据拨号（ModemManager / IPA）尚未接入。
