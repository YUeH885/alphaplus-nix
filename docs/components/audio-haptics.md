# 音频与触觉

## 硬件拓扑

| 部件 | 接口 / 拓扑 | 功能 |
| --- | --- | --- |
| WCD9340（Tavil） | SLIMbus NGD@171c0000 | 主编解码器、MBHC 耳机检测、AMIC1–3 模拟麦 |
| ES9218P Quad DAC | `TERT_MI2S_RX`（SD1） | 有线耳机 DAC，支持 HiFi1（<50 Ω）/ HiFi2（50–600 Ω）电源切换 |
| TFA9872 + SMA6101 | `SEC_MI2S_RX`（48 kHz）、I2C4 `0x34` / `0x1e` | 底部扬声器（TFADSP 保护）+ 顶部听筒/双扬声器；SMA6101 SDO 强制高阻 |
| DisplayPort 音频 | `MultiMedia3` / `DISPLAY_PORT_RX` | 依据外接显示器 ELD 协商格式并在关屏前排空音频 |
| WCN3998 BT SCO | SLIMbus NGD@17240000（`SLIMBUS_7`） | 8 kHz CVSD / 16 kHz mSBC 硬件 SCO 卸载（`pipewire-spa-alphaplus`） |
| DW7800 | I2C4 `0x59` | `FF_RUMBLE` 线性马达，配合 `feedbackd` 与 [lg,alphaplus.json](../../nixos/files/feedbackd/themes/lg,alphaplus.json) |

## 用户空间路由与采样率

- **ALSA 与 UCM**：`alsa-utils` overlay 注入 `00-alphaplus.conf` 初始化默认录音路由，[51-alphaplus.conf](../../nixos/files/alsa/51-alphaplus.conf) 定义 `Headphones`、`DisplayPort` 与 `Speakers` 互斥 Profile，以及 `AMIC3`（左）+ `AMIC1`（右）立体声麦与耳麦输入。
- **动态声道跟随旋转**：`alphaplus-audio-orientation` 监听 Wayland `DSI-1` 旋转方向，通过 WirePlumber 2×2 filter-graph 在 DSP 前交换左右声道或混为单声道（竖屏）。
- **高采样率直通与扬声器锁频**：[audio.nix](../../nixos/modules/hardware/audio.nix) 与内核共同对有线耳机（`TERT_MI2S_RX`）放行 `44.1 / 48 / 88.2 / 96 / 176.4 / 192 kHz`（`S32_LE`）；[51-alphaplus-audio.conf](../../nixos/files/wireplumber/wireplumber.conf.d/51-alphaplus-audio.conf) 将扬声器节点锁定在 `48 kHz` 以匹配固定 `SEC_MI2S_RX` / TFADSP 后端。

## 蜂窝通话音频

- `imsd-media` 通过 PipeWire 处理 VoLTE 的 RTP 音频。Plasma Dialer 通过 `callaudiod` 选择通话时的音频设备。
- `alphaplus-call-audio` 根据 ModemManager 的通话状态启停 QDSP6 `VoiceMMode1`。下行使用 `SEC_MI2S_RX`，上行使用 WCD9340 的 `SLIMBUS_0_TX`。

## 未实现

- **TFA9872 SaaM 扬声器拾音（DMIC0）**：内核已修正 WCD9340 `ADC MUX8`（`tx_adc_mux8_enum`）寄存器地址并在 `SLIM TX8` 上验证通路；原厂 `Tfa9872.cnt` 无 SaaM 配置，真机实测 `DMIC0` 在不同 TFA9872 / WCD9340 寄存器组合下均无有效拾音信号，暂未启用。
- **FM 收音接收**：主线缺少控制协议驱动与 AFE 后端路由。
- **AMIC4 麦克风**：实测仅为底噪（疑似韩版未贴片或偏置特殊），未开放为可用输入。
