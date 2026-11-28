# 显示、输入与图形

## 显示与 GPU

- **DPU + SW43410 面板**：从 bootloader 继承 `simple-framebuffer`（`0x9d200000`，18 MiB）及 7nm DSI PLL 状态后平滑接管；驱动 1440×3120@60 Hz 命令模式面板（4-lane D-PHY、DSC 1.1、920.1456 MHz），支持空闲 ULPS、44 µs DPU CPU latency QoS、首帧点亮背光（7–1023，BC DIM 平滑调光）及 DSPP IGC/PCC/GC 色彩管线。
- **Adreno 640 与 Turnip**：`CONFIG_DRM_MSM=m` 由 `alphaplus-gpu-module.service` 在 rootfs 挂载后加载。Adreno 640 CCU 可能预读渲染目标所在 64 KiB 区域起点，内核保留 64 KiB VA 对齐 quirk，用户态 [`turnip-alphaplus`](../../nixos/pkgs/turnip/default.nix) 对分配基址做 64 KiB 对齐并替换系统 Freedreno Vulkan ICD。

## 物理输入与桌面适配

- **触摸屏** `lg,sw42000`（I2C17 / PDC90）以 panel follower 提供 Type-B 多点触控。PM8150 GPIO6 对应音量加，PON RESIN 对应音量减，PON PWRKEY 对应电源键，PM8150 GPIO1 对应 Google Assistant 侧键（`KEY_TV`）。
- **SDDM Wayland**（[sddm.nix](../../nixos/modules/desktop/sddm.nix)）：KWin 竖屏登录器，集成 `plasma-keyboard`、`PowerKey.qml` 电源键灭屏、30 秒空闲关屏（`alphaplus-sddm-idle`）与睡眠前关屏（`alphaplus-prepare-greeter-sleep`）。
- **Plasma 6 Wayland**（[plasma.nix](../../nixos/modules/desktop/plasma.nix)）：默认横屏 `Rotated270`、关闭自动旋转（`Never`）、电源键统一设为关闭屏幕（[powerdevilrc](../../nixos/files/desktop/user-config/powerdevilrc)）、集成 KDE Connect 与 Chromium，并通过 [input.nix](../../nixos/modules/desktop/input.nix) 编译 [`fcitx5-osk-alphaplus`](../../nixos/pkgs/fcitx5-osk/default.nix)（`zwp_input_panel_v1` 虚拟键盘 + `fcitx5-chinese-addons`）。
- **桌面辅助用户服务**（[desktop-helpers](../../nixos/pkgs/desktop-helpers/default.nix)）：
  - `alphaplus-touchpad` 通过 uinput 将 SW42000 映射为虚拟触摸板 `LG G8 Touchpad`。解锁后，单按 Google Assistant 侧键显示或收起屏幕键盘，300 毫秒内连按两下切换触摸板和直接触摸。锁屏自动退回直接触摸，电源键关屏时有 1.2 秒防误触保护。
  - 屏幕键盘在桌面上贴着屏幕底边显示，覆盖与它重叠的任务栏区域。任务栏留在原位。
  - `alphaplus-kwin-keyboard` 插件使用 Wayland 应用上报的光标矩形。屏幕键盘遮住浮动窗口里的光标时，插件上移窗口至光标露出，保持宽高并允许顶部移出屏幕。同一窗口内输入焦点消失时保持上移位置；键盘收起、切换活动窗口或改为使用未被遮挡的输入框时，窗口复位。手动拖动窗口、窗口退出浮动状态或屏幕布局变化后，不再按旧位置复位。
  - `alphaplus-lockscreen`：锁屏时切回竖屏 `Normal` 并切换 KWin 输入法至 `plasma-keyboard`，解锁后恢复。
  - `alphaplus-foreground-placement`：监听 KWin 活动窗口与锁屏状态，通过 systemd `AllowedCPUs` 将前台应用及解锁态 KWin/Plasma Shell 调度至大核（CPU4–7）。

## 未实现

- **高级面板特性**：局部刷新（Partial Update）、息屏显示（AOD）与 HDR 尚未接入。
