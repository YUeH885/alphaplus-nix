# VPU

## 已实现

- **Iris 1.xx 驱动与固件**：接入 `qcom,sm8150-iris`（`VIDEO.IR.1.2-00054-PROD-1`，`venus_mem` 5 MiB，`qcom/sm8150/lmg820n/venus.mdt`）、`VideoCC` 与 `iris_vpu1` 后端（保留 I/O 保护池），注册 V4L2 stateful M2M 设备 `/dev/video0`（解码）与 `/dev/video1`（编码）。
- **LLCC 与动态互连带宽缩放**：
  - 系统缓存：在 `iris_vpu1` 接入 `LLCC_VIDSC0` / `LLCC_VIDSC1` 切片生命周期管理，于核心初始化与 PM 恢复时通过 HFI `HFI_CMD_SYS_SET_RESOURCE`（`HFI_RESOURCE_SYSCACHE`）注册给固件，并在下电时停用。
  - 动态带宽：`iris_vpu1` `calc_bw` 对齐 SM8150 编解码动态 DDR 带宽缩放模型（覆盖分辨率、帧率、`operating_rate`、8/10-bit、H.264/HEVC/VP9、UBWC 与线性 OPB 输出差异、LLCC 节省、B 帧、旋转与 QSMMU 开销），并按活跃帧字节数与完整状态生命周期触发时钟与互连重投票。
- **编解码规格（最高 4K）**：
  - 解码：H.264、HEVC（含 Main 10 10-bit）、VP9（含 Profile 2 10-bit），支持动态分辨率与 8/10-bit 切换，内部 UBWC DPB 与 `OUTPUT2` 双流输出（8-bit 线性 NV12 / UBWC `QC08C`，10-bit 256 字节对齐线性 P010）。
  - 编码：H.264、HEVC（8/10-bit），支持 CBR/VBR、最多 1 帧连续 B 帧、NV12/P010 及 DMA-BUF 零拷贝输入、编码 drain 后续传。
- **用户空间集成（[`ffmpeg-vpu`](../../nixos/pkgs/ffmpeg-vpu/default.nix)）**：应用 V4L2 M2M 补丁，在 `AV_PIX_FMT_DRM_PRIME` 零拷贝路径下为 8-bit 解码协商 `V4L2_PIX_FMT_QC08C` 并导出 `DRM_FORMAT_NV12` + `DRM_FORMAT_MOD_QCOM_COMPRESSED` 供 Mesa（Adreno 640）/ Wayland 导入，软件输出保持线性 `NV12`，10-bit 流保持线性 `P010`（`DRM_FORMAT_MOD_LINEAR`）；内核关闭 `CONFIG_DMABUF_DEBUG` 供 `mpv`（`vo=gpu` + `hwdec=v4l2m2m`）通过 Wayland DRM PRIME 零拷贝播放。

## 未实现

- **10-bit UBWC 零拷贝导入**：Mesa（Adreno 640）暂不支持 `DRM_FORMAT_P010` + `DRM_FORMAT_MOD_QCOM_COMPRESSED` EGL 导入，10-bit 解码保持线性 `P010` 导出。
- **安全视频与 8K**：未实现 Secure Video 会话及 8K 编解码。
