# VPU

## 已实现

- **Iris 1.xx 驱动与固件**：接入 `qcom,sm8150-iris`（`VIDEO.IR.1.2-00054-PROD-1`，`venus_mem` 5 MiB，`qcom/sm8150/lmg820n/venus.mdt`）、`VideoCC` 与 `iris_vpu1` 后端（保留 I/O 保护池），注册 V4L2 stateful M2M 设备 `/dev/video0`（解码）与 `/dev/video1`（编码）。
- **编解码规格（最高 4K）**：
  - 解码：H.264、HEVC（含 Main 10 10-bit）、VP9（含 Profile 2 10-bit），支持动态分辨率与 8/10-bit 切换，内部 TP10 UBWC DPB 与 `OUTPUT2` 线性 P010（256 字节对齐）双流输出。
  - 编码：H.264、HEVC（8/10-bit），支持 CBR/VBR、最多 1 帧连续 B 帧、NV12/P010 及 DMA-BUF 零拷贝输入、编码 drain 后续传。
- **用户空间集成（[`ffmpeg-vpu`](../../nixos/pkgs/ffmpeg-vpu/default.nix)）**：应用 V4L2 M2M 补丁导出线性 `NV12` / `P010` `AVDRMFrameDescriptor`（`DRM_FORMAT_MOD_LINEAR`）；内核关闭 `CONFIG_DMABUF_DEBUG` 供 `mpv`（`vo=gpu` + `hwdec=v4l2m2m`）通过 Wayland DRM PRIME 零拷贝播放。

## 未实现

- **LLCC 系统缓存资源管理**：未接入固件 HFI `VIDSC0/VIDSC1` LLCC 生命周期管理。
- **动态互连带宽缩放**：未对齐原厂动态 DDR 带宽缩放模型，目前按静态档位与宏块帧率估算。
- **UBWC GPU Modifier 零拷贝**：当前导出线性格式给 GPU/Wayland，尚未支持 TP10/NV12 UBWC 直接导入 Mesa。
- **安全视频与 8K**：未实现 Secure Video 会话及 8K 编解码。
