# 辅助外设

## 已实现

- **RTC（PM8941 / `rtc-pm8xxx`）**：支持断电计时、`hctosys` 同步及 `s2idle` 唤醒源。
- **NFC（NXP PN547）**：挂载于 `i2c9`（`0x28`，`nxp-nci_i2c` + `RPMH_LN_BB_CLK2`），支持 ISO 14443-B 与 NFC Forum 标签读写（MIFARE Classic 受限于主线 `nci` 未映射 NXP 专有 Crypto1 RF 接口仅可识别 UID/协议）；[nfc.nix](../../nixos/modules/hardware/nfc.nix) 启用并修复了 `services.neard` 在 `dbus-broker` 下的标签 `Uid`/NDEF D-Bus 序列化缺陷（[fix-tag-dbus-properties.patch](../../nixos/overlays/patches/neard/fix-tag-dbus-properties.patch)，保持 `DefaultPowered = false`、`ConstantPoll = false` 按需上电），提供 `org.neard` D-Bus 与 `nfctool`。[plasma.nix](../../nixos/modules/desktop/plasma.nix) 集成系统托盘组件 [`alphaplus-nfc`](../../nixos/pkgs/nfc/default.nix)（`org.alphaplus.nfc`），支持托盘开关与按需启停 `Initiator` 轮询、`s2idle` 休眠前自动断电并在唤醒后按原状态恢复、标签 UID/类型展示、NDEF 记录解析（URI / Text / SmartPoster / MIME）与复制/打开，以及向可写标签写入 `URI` / `Text` 记录。
- **闪光灯与托盘手电筒（PM8150L Flash LED）**：`leds-qcom-flash` 注册 `white:flash` 与 `yellow:flash` 并联驱动后置单色温 LED（手电筒合计 `200 mA`、相机频闪合计 `1800 mA`）；桌面可通过系统托盘「状态和通知」的 `Flashlight`（[`alphaplus-flashlight`](../../nixos/pkgs/flashlight/default.nix)）经 `video` 组权限控制常亮。
- **传感器**：见 [sensors.md](sensors.md)。

## 未实现

- **指纹识别（Goodix）**：挂载于 `SPI7`（`0x89c000`），依赖专有 TrustZone TA，尚无开源驱动。
- **HCE 卡模拟**：仅支持读写标签与 P2P 底层接口，未实现基于主机卡模拟（HCE）的桌面移动支付/门禁卡模拟栈。
