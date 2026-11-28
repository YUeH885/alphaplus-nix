# VPU

## 已实现

- **Iris 1.xx 驱动与固件**：接入 `qcom,sm8150-iris`（`VIDEO.IR.1.2-00054-PROD-1`，`venus_mem` 5 MiB，`qcom/sm8150/lmg820n/venus.mdt`）、`VideoCC` 与 `iris_vpu1` 后端（保留 I/O 保护池），注册 V4L2 stateful M2M 设备 `qcom-iris-decoder` 和 `qcom-iris-encoder`，可通过 `/sys/class/video4linux/video*/name` 确认各自的节点；在 HFI Gen1 上补齐解码序输出（`V4L2_CID_MPEG_VIDEO_DEC_DISPLAY_DELAY*` 映射 `HFI_PROPERTY_PARAM_VDEC_OUTPUT_ORDER`）、64 缓冲 decoder CAPTURE 池上限、错误缓冲保留时间戳、非显示解码输出（`HFI_BUFFERFLAG_DROP_FRAME`）驱动内复用、`iris_close()` 并发关闭防 UAF 保护、非 DRC 流式解码中途 `STREAMOFF(CAPTURE)` 全端口冲刷、HFI 4XX 首次 `SOURCE_CHANGE` 前独立非 VCL 头 NAL 合并，以及编码控件 `PREPEND_SPSPPS_TO_IDR`、`BITRATE_PEAK`、`GOP_SIZE`（上限 `INT_MAX`）与帧级 QP（`HFI_PROPERTY_CONFIG_VENC_FRAME_QP`）。
- **LLCC 与动态互连带宽缩放**：
  - 系统缓存：在 `iris_vpu1` 接入 `LLCC_VIDSC0` / `LLCC_VIDSC1` 切片生命周期管理，于核心初始化与 PM 恢复时通过 HFI `HFI_CMD_SYS_SET_RESOURCE`（`HFI_RESOURCE_SYSCACHE`）注册给固件，并在下电时停用。
  - 动态时钟与带宽：`iris_vpu1` `calc_bw` 对齐 SM8150 编解码动态 DDR 带宽缩放模型（覆盖分辨率、帧率、`operating_rate`、8/10-bit、H.264/HEVC/VP9、UBWC 与线性 OPB 输出差异、LLCC 节省、B 帧、旋转与 QSMMU 开销），并按活跃帧字节数、解码序串行延迟预算（`DISPLAY_DELAY_ENABLE`）、解码器占空比 DCVS 升频及完整状态生命周期触发时钟与互连重投票。
- **编解码规格（最高 4K）**：
  - 解码：H.264、HEVC（含 Main 10 10-bit）、VP9（含 Profile 2 10-bit），支持动态分辨率与 8/10-bit 切换，内部 UBWC DPB 与 `OUTPUT2` 双流输出（8-bit 线性 NV12 / UBWC `QC08C`，10-bit 256 字节对齐线性 P010）。
  - 编码：H.264、HEVC（8/10-bit），支持 CBR/VBR、最多 1 帧连续 B 帧、NV12/P010 及 DMA-BUF 零拷贝输入、编码 drain 后续传。
- **系统 VA-API 后端（[`qcom-va-driver`](../../nixos/pkgs/qcom-va-driver/default.nix)）**：以 `qcom_drv_video.so`（含 `msm_drv_video.so` 与 `v4l2_drv_video.so` 符号链接）接入 `hardware.graphics.extraPackages` 并设置 `GST_VA_ALL_DRIVERS=1`，可在 MSM DRM 节点（`/dev/dri/renderD128`）上由 `libva` 自动发现。提供 H.264 / HEVC（8/10-bit）/ VP9（8/10-bit）硬件解码、H.264（8-bit）与 HEVC（8/10-bit）硬件编码（CQP/CBR/VBR）、DMA-BUF 零拷贝解码和转码，以及面向持久导出 surface 的 Adreno 640 EGL/GLES 3.2 GPU 拷贝。HEVC Main 10 编码在 V4L2 支持 Main 10 和 P010 输入时启用。
  - **Vulkan VPP** 使用 32 位 SSBO 在 Adreno 640 Turnip 上处理线性 NV12 和 P010，提供缩放、裁剪、旋转、格式转换、图层叠加和 HDR10 到 SDR 的色调映射。[vulkan-vpp-32bit-ssbo.patch](../../nixos/pkgs/qcom-va-driver/vulkan-vpp-32bit-ssbo.patch)
  - **单上下文动态分辨率切换** 在收到 `SOURCE_CHANGE` 并取出旧 `CAPTURE` 队列的 `LAST` 缓冲区后重建 `CAPTURE`，保留 `OUTPUT` 流和仍在使用的旧帧。应用需提供与各帧尺寸匹配的 VA surface，整个上下文使用同一种输出像素格式。[decoder-drc.patch](../../nixos/pkgs/qcom-va-driver/decoder-drc.patch)
- **V4L2 M2M 用户空间集成（[`ffmpeg-vpu`](../../nixos/pkgs/ffmpeg-vpu/default.nix)）**：应用 V4L2 M2M 补丁，在 `AV_PIX_FMT_DRM_PRIME` 零拷贝路径下为 8-bit 解码协商 `V4L2_PIX_FMT_QC08C` 并导出 `DRM_FORMAT_NV12` + `DRM_FORMAT_MOD_QCOM_COMPRESSED` 供 Mesa（Adreno 640）/ Wayland 导入，软件输出保持线性 `NV12`，10-bit 流保持线性 `P010`（`DRM_FORMAT_MOD_LINEAR`）；内核关闭 `CONFIG_DMABUF_DEBUG` 供 `mpv`（`vo=gpu` + `hwdec=v4l2m2m`）通过 Wayland DRM PRIME 零拷贝播放。

## 未实现

- **CDSP VPP**：当前构建仅启用 Turnip Vulkan VPP。
- **10-bit UBWC 零拷贝导入**：Mesa（Adreno 640）暂不支持 `DRM_FORMAT_P010` + `DRM_FORMAT_MOD_QCOM_COMPRESSED` EGL 导入，10-bit 解码保持线性 `P010` 导出。
- **安全视频与 8K**：未实现 Secure Video 会话及 8K 编解码。
