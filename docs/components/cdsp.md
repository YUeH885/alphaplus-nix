# CDSP 计算

## 已实现

- **内核与进程间通信**：DTS 启用 `remoteproc_cdsp`（`0x08300000`、`cdsp_mem` 55 MiB、`cdsp.mdt`）、GLINK 通道、`qcom,non-secure-domain` 与 coherent context bank CB1–CB8；`qcom_pd_mapper` 注册 `msm/cdsp/root_pd`（instance `76`），由 `alphaplus-remoteproc-boot.service` 随 `cdsp.mdt` 拉起；FastRPC 支持并发发送。
- **用户空间共享库**：[sensors.nix](../../nixos/modules/hardware/sensors.nix) 将 `nixos/files/cdsp/` 中的原厂 CDSP shell 与基础库打包至 `/usr/share/qcom/sm8150/LG/alphaplus/dsp/cdsp/`，配合 `hexagonrpcd`（`-u` 无签名模式）运行；测试向量位于 [tools/test-cdsp](../../tools/test-cdsp)。

## FC4 推理部署取舍

相机的 `fc4_awb.onnx`（19.87 MB）与场景分类器 `wb_graph_*.onnx`（35 KB）保持运行在 CPU 4 线程 ONNX Runtime 上，暂不迁移至 CDSP/HTA：

- 原厂 `libhta_dsp_skel.so` 与 `wb_graph_*.dlc` 绑定专有 Bionic/SNPE 接口，且缺少 `fc4_awb` 的标准 ONNX->HTA 编译链；
- 实机上 CPU 4 线程活跃推理约 `41–63 ms` / `10 Hz`（不阻塞 30/60/120 fps 帧流），静止节流态 `< 1 ms/s`，收益远小于维护独立常驻 CDSP 守护进程与跨进程 IPC 的复杂度。

## 限制与未实现

- **动态 User PD 并发上限**：CDSP 固件内部动态 User PD 上限为 6 个会话，多任务场景需要常驻守护进程复用句柄。
- **HVX 与神经网络运行时**：尚无 HVX 向量计算负载或通用的 CDSP 神经网络推理栈集成。
- **安全计算域**：原厂 `cdsp_sec_mem` 当前归入 System RAM，安全计算保护域未接入。
