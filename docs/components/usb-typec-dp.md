# USB Type-C 与 DisplayPort

## 已实现

- **USB 3.1 与角色切换**：`qcom,sm8150-dwc3`、FEMTO HS PHY 与 QMP USB3/DP combo PHY 接入；PM8150B Type-C 通过 TCPM、USB role switch 与 VCONN boost（GPIO5）管理 DRP 握手与带电/无电扩展坞 `DR_SWAP`。
- **CDC Gadget**：`CONFIG_USB_CDC_COMPOSITE` 提供 `usb0`（CDC ECM 网卡）与 `ttyGS0`（CDC ACM 串口）；`78-alphaplus-modem-gadget.rules` 让 ModemManager 忽略 gadget 网卡；日常镜像使用 `console=tty0` 且不启用 `serial-getty@ttyGS0.service`，避免串口断开阻塞关机。
- **电源管理与 `s2idle` 连接保留**：`72-alphaplus-usb-autosuspend.rules` 默认开启 DWC3/PHY `power/control = auto`；已连接的 USB2/SuperSpeed 设备与外设在 `s2idle` 期间保持配置不断线，`vbus_draw_work` 限流通知延后至唤醒阶段应用。
- **DisplayPort Alt Mode**：内建 `CONFIG_TYPEC_DP_ALTMODE=y` 与 `CONFIG_TYPEC_MUX_GPIO_SBU=y`，启用 `mdss_dp`（HBR2 2-lane）与并发 USB+DP PHY 通道；板级集成 SBU 保护开关（GPIO87/35/119/38）及 `onnn,fsusb42` / `gpio-sbu-mux` AUX 开关（GPIO42 使能、GPIO148 正反插极性），支持冷启动 HPD 重放以及主副屏独立、双屏扩展与单外接屏独占显示。

## 未实现

- **按场景精细管控连接保留功耗**：`s2idle` 下保留 USB 连接会维持部分控制器/PHY 供电，与完全下电之间的按场景自动策略切换未做精细控制。
