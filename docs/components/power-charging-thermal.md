# 电源、充电与热管理

## 已实现

- **PMIC 与温度监测**：接入 PM8150/PM8150B/PM8150L SPMI、regmap、GPIO、RPMh regulator 与 PON；启用 `qcom,spmi-temp-alarm` 及 `qcom,spmi-adc-tm5`（`wp_therm`、`xo_therm`、`skin_therm`、`pa_therm0`、`pa_therm1` 五路温度通道，125°C passive trip）。
- **双路并联充电**：内建 `qcom_pm8150b`（主充 / Gen4 FG，BL-T41 3.4 Ah / 3 A / 4.43 V 参数）、`qcom-i2c-pmic` 16-bit I2C 解复用父驱动与 `qcom_smb1355`（SE4 I2C `0x0c`）并联充电；由 TCPM / BC1.2 驱动 5/9 V PD、70% 并联 FCC 分配与 +50 mV FV 偏置，FG 基于 `mem-attn` 与 `msoc-delta` 上报标准电量与电流。
- **VTS 温控与维护回睡**：`lg,alphaplus-charger` 结合 `xo`/`skin` 热敏电阻做 VTS 估算（DCP/CDP/PD 亮屏 +4°C 补偿）与五档限流（10 / 38.5 / 39 / 40 / 43°C）。主/并充 watchdog（64s / 16s）在挂起时支持 bark 唤醒喂狗，配合 `systemd-suspend.service` 的 `alphaplus-suspend` 包装器通过 `/sys/power/pm_wakeup_irq` 识别维护唤醒，确认无用户输入与外部唤醒后自动回睡。
- **充电阈值与旁路供电**：`qcom_pm8150b` 暴露标准 `charge_control_{start,end}_threshold` 与 `charge_behaviour`（[hardware/default.nix](../../nixos/modules/hardware/default.nix) 注入 `CHARGE_LIMIT=75,80` 对接 UPower / PowerDevil 阈值开关）；[bypass-charging](../../nixos/pkgs/bypass-charging/default.nix) 提供托盘「Bypass Charging」开关，直接切换 `inhibit-charge` 实现插电直供系统而不充电池。

## 未实现

- **外部计量校准**：USB 电流与 MID 电压直接读取 ADC5 原始转换，无出厂外接仪表校准补偿表。
