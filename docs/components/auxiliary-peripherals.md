# 辅助外设

## 已实现

- **RTC（PM8941 / `rtc-pm8xxx`）**：支持断电计时、`hctosys` 同步及 `s2idle` 唤醒源。
- **NFC（NXP PN547）**：挂载于 `i2c9`（`0x28`，`nxp-nci_i2c` + `RPMH_LN_BB_CLK2`），支持 ISO 14443-B 读卡；[nfc.nix](../../nixos/modules/hardware/nfc.nix) 启用 `services.neard`（`DefaultPowered = false`、`ConstantPoll = false` 按需上电），提供 `org.neard` D-Bus 与 `nfctool`。
- **闪光灯与托盘手电筒（PM8150L Flash LED）**：`leds-qcom-flash` 注册 `white:flash` 与 `yellow:flash` 并联驱动后置单色温 LED（手电筒合计 `200 mA`、相机频闪合计 `1800 mA`）；桌面可通过系统托盘「状态和通知」的 `Flashlight`（[`alphaplus-flashlight`](../../nixos/pkgs/flashlight/default.nix)）经 `video` 组权限控制常亮。
- **传感器**：见 [sensors.md](sensors.md)。

## 未实现

- **指纹识别（Goodix）**：挂载于 `SPI7`（`0x89c000`），依赖专有 TrustZone TA，尚无开源驱动。
- **NFC 桌面 UI 与 HCE 卡模拟**：仅提供底层 `neard` D-Bus 与 `nfctool`，尚无桌面标签管理 UI 与 HCE 栈。
