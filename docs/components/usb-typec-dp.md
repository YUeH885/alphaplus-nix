# USB Type-C 与 DisplayPort

## 已实现

- **USB 3.1 与角色切换**：`qcom,sm8150-dwc3`、FEMTO HS PHY 与 QMP USB3/DP combo PHY 接入；PM8150B Type-C 通过 TCPM、USB role switch 与 VCONN boost（GPIO5）管理 DRP 握手与带电/无电扩展坞 `DR_SWAP`。
- **CDC Gadget**：`alphaplus-usb-gadget.service` 通过 `configfs` 配置 `usb0`（CDC NCM 网卡）和 `ttyGS0`（CDC ACM 串口）。内核在控制器启动时清除待连接状态，支持插线冷启动时直接枚举。`MaxPower` 按原厂设为 900 mA，描述符在 USB 2.0 和 SuperSpeed 下分别声明 500 mA 和 896 mA。普通数据口（SDP）使用默认 Type-C 电流档时，PM8150B 按 gadget 上报的 500 mA 或 900 mA 档位限流。`78-alphaplus-modem-gadget.rules` 让 ModemManager 忽略 gadget 网卡。日常镜像使用 `console=tty0`，串口登录由启动参数 `console=ttyGS0,115200` 启用。
- **电源管理与 `s2idle` 按场景连接保留**：`72-alphaplus-usb-autosuspend.rules` 默认开启 DWC3/PHY `power/control = auto`；内核在 `s2idle` 下支持保持已连接的 gadget/host 配置并延后应用 `vbus_draw_work` 限流。[usb.nix](../../nixos/modules/hardware/usb.nix) 与 [`alphaplus-usb-sleep-policy`](../../nixos/pkgs/usb-sleep-policy/default.nix) 通过 [suspend-maintenance.py](../../nixos/files/desktop/sddm/suspend-maintenance.py) 在进入 `s2idle` 前评估 USB 角色、UDC 状态、充电限流依赖（`SDP` + Type-C `default`）、`usb0` / `ttyGS0` 活跃度、下游唤醒外设与已挂载 USB 存储以及 `power-profiles-daemon` 配置档，按场景锁存 `a600000.usb` / `a6f8800.usb` / `xhci` 的 `power/wakeup`（在无活跃会话/非必须保留时彻底下电释放控制器、时钟与 PHY，并在最终唤醒后恢复），并支持 `alphaplus-usb-sleep-policy set auto|retain|power-off` 手动覆写。
- **DisplayPort Alt Mode**：内建 `CONFIG_TYPEC_DP_ALTMODE=y` 与 `CONFIG_TYPEC_MUX_GPIO_SBU=y`，启用 `mdss_dp`（HBR2 2-lane）与并发 USB+DP PHY 通道；板级集成 SBU 保护开关（GPIO87/35/119/38）及 `onnn,fsusb42` / `gpio-sbu-mux` AUX 开关（GPIO42 使能、GPIO148 正反插极性），支持冷启动 HPD 重放以及主副屏独立、双屏扩展与单外接屏独占显示。
