# 相机

## 传感器配置

| 位置 | 型号 | 接口 / 物理链路 | 辅助器件 | 典型模式 |
| --- | --- | --- | --- | --- |
| 后置主摄 | Sony IMX363 | `cci0` M0（`0x10`），`csiphy0` 4-lane | M24C64M EEPROM、`LC898219XI` VCM、`LC898124EP1` OIS | `4032×3024@30`、`4032×2272@60`（4K60）、`2016×1512@120`、`2016×1136@60`（RAW10 / NV12） |
| 后置超广角 | Sony IMX351 | `cci0` M1（`0x1a`），`csiphy1` 4-lane | M24C32 EEPROM，定焦 | `4656×3492@30`（RAW10 / Dual-IFE NV12）、`3840×2160@60`、`2328×1744@30`、`2328×1312@60/120` |
| 后置长焦 | Samsung S5K3M5 | `cci1` M1（`0x2d`），`csiphy2` 4-lane（mux） | M24C32 EEPROM、`LC898219XI` VCM | `4032×3024@30`、`4032×2272@30`、`2016×1512@30`、`2016×1136@60`、`1008×568@120` |
| 前置主摄 | Sony IMX520 | `cci1` M0（`0x10`），`csiphy2` 2-lane（mux） | M24C64M EEPROM、`LC898219XI` VCM | 4 档 `30 fps` 模式（`3280×2460@30`、`1640×924@30` 等） |
| 前置 ToF | Infineon IRS2381C | `qupv3_se10_i2c`（`0x3d`），`csiphy3` 2-lane | M2453_B11 `nvmem` 校准、VFE Lite `vfe3` | 9-phase `224×1903@5–30`、5-phase `224×1211@5–30`（270° 安装角） |

S5K3M5 与 IMX520 通过 GPIO29/49 `video-mux` 共用 `csiphy2` 与 MCLK2，支持动态切换。

## 内核与 IFE 通路

- **Titan 175 CAMSS**：接入 4× CSIPHY、4× CSID Gen2、4× VFE/IFE 17x、`CAMCC`、`apps_smmu` 与 CAMNOC；支持 RDI RAW 与 VFE0/1 PIX 管线（Demux/BLS/WB/BPC/BCC/ABF/LSC/Demosaic/CCM/Gamma/GTM/CST/Scaler NV12），输入宽度 `> 4096` 时自动切换 Dual-CSID + Dual-IFE 左右分片拼接。
- **统计、PDAF 与电源**：64×48 AWB BG、1024-bin BHIST 与 5×5 BF 使用 coherent DMA 槽轮转；侧带 `VC=1` PDAF 走 `CSID RDI1 -> VFE WM1`（IMX363 Dual-PD `DT=0x2B`，S5K3M5 稀疏尾包 `DT=0x30`）；提升 VFE 时钟前动态为 `MMCX`/`MX` 电源域投票升档。
- **帧级请求与时间戳**：经 `V4L2_CID_QCOM_CAMSS_IFE_CONFIG` 绑定请求并在内核按 SOF 调度曝光/增益/`VBLANK`；libcamera 以 `CLOCK_BOOTTIME` 将 PIX 与 RAW10 的 `SensorTimestamp` 统一对齐到首行物理曝光起始时刻。
- **故障恢复与休眠**：单 VFE 总线溢出或违规时独立复位恢复且不影响其余 VFE；系统挂起前暂停活动流并下电空闲器件，唤醒后自动恢复队列、VCM 位置与 OIS 状态。

## 3A、对焦与防抖（libcamera）

[libcamera/default.nix](../../nixos/pkgs/libcamera/default.nix) 构建 `0.7.2`（`qcom` + `uvcvideo`、`qcam` 与 `libcamerasrc`），按需打开 VFE PIX 与推理引擎：

- **AE 与闪光**：FastConv AE、手动分离控制 `ExposureTimeMode`/`AnalogueGainMode`、64×48 AWB BG 频谱自动抗频闪（50/60 Hz），以及 PM8150L 双路闪光灯（预闪 SmartFlash、手电筒 `200 mA`、SOF 事件精确同步单次主闪 `1800 mA`）。
- **AWB 与 FC4**：前置使用 QTI AWB + 人脸辅助；后置使用 LG AWB 与异步 ONNX Runtime 推理（4 个模型位于 `/etc/camera/iqm`，由 [camera.nix](../../nixos/modules/hardware/camera.nix) 安装；`FC4Engine` 基于 AE 收敛与场景稳定性在活跃推理与静止节流间切换）。
- **AF 与 OIS**：IMX363 与 S5K3M5 使用基于 EEPROM Gain Map/DCC 校准的混合 PDAF + CDAF 闭环对焦；IMX520 在自动/连续对焦期间按需唤醒 IRS2381C ToF 做小孔模型投影深度粗对焦并配合紧致 CDAF 微调（无有效深度回退 CDAF）。IMX363 接入 `libssc` 重力补偿，并随角色切换 OIS `Still` / `Movie Pan/Tilt` 模式。

## 硬件人脸检测与 ToF

- **Titan 175 FD501（`qcom-fd`）**：导出 `/dev/qcom-fd`（单帧最多 35 张人脸、全角度、无需固件）；`HardwareFdEngine` 在开启 `FaceDetectMode` 时异步处理 NV12 亮度平面，输出元数据并联动 AWB/AF。
- **IRS2381C ToF**：作为第 5 路独立相机注册（不占用 VFE0/1），`TofDemodulator` 读取板载 `nvmem`（`PMDJGF` 校准与 NTC 温补），输出 `224×172` `NV12` 伪彩预览、`R16` 毫米深度图及 `SBGGR12_CSI2P` 原始相位流；离线工具见 [process-camera-tof.py](../../tools/process-camera-tof.py)。

## 桌面应用

- **GNOME Snapshot（`snapshot-alphaplus`）**：Turnip Vulkan 渲染，GStreamer `v4l2h264enc` 硬件编码 MP4，关闭前摄预览镜像并在显示/拍照/录像前校正 `Front ToF Camera` 竖直翻转。
- **qcam / qrca / PipeWire**：`qcam` 支持 `ScalerCrop` 变焦与裁切域点击对焦；`qrca` 走 Qt Multimedia + `libcameraprovider`；[pipewire-bluetooth/default.nix](../../nixos/pkgs/pipewire-bluetooth/default.nix) 编译 `libspa-libcamera.so` 供浏览器和 Portal 调用。

## 未实现

- **IQM Source 11 实拍精校**：`LgAwbDnn` 已打通，户外复杂混光与 24 色卡下的触发门槛与权重尚待校准。
- **Titan 175 其余成像单元**：原厂 DTS 中的 `JPEG Enc/DMA`、`LRME`、VFE `STATS_RS/CS`、VFE UBWC 输出及 `ICP/BPS/IPE`（固件包缺少 `CAMERA_ICP.elf`）尚未接入。
