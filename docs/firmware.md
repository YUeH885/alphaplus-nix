# 固件清单

设备固件由 [firmware.nix](../nixos/modules/hardware/firmware.nix) 从 `nixos/files/firmware/` 单独打包（不压缩：`hardware.firmwareCompression = "none"`），经 `/run/current-system/firmware` 与 `/lib/firmware` 提供给内核与用户态，避免引入体积庞大的完整 `linux-firmware`。

| 用途 | 路径 | 说明 |
| --- | --- | --- |
| Adreno 640 | `qcom/a630_sqe.fw`、`qcom/a640_gmu.bin`、`qcom/sm8150/a640_zap.*` | 原厂 vendor |
| ADSP | `adsp.mdt`、`adsp.bNN`、`adspr.jsn`、`adspua.jsn` | 原厂 modem |
| CDSP | `cdsp.mdt`、`cdsp.bNN`、`cdspr.jsn` | 原厂 modem |
| SLPI | `slpi.mdt`、`slpi.bNN`、`slpir.jsn`、`slpius.jsn` | 原厂 modem |
| Iris VPU | `qcom/sm8150/lmg820n/venus.mdt`、`venus.bNN` | 原厂 modem |
| MPSS | `modem.mdt`、`modem.bNN`、`modemr.jsn`、`modemuw.jsn`、`modem_pr/` | 原厂 modem |
| WLFW | `wlanmdsp.mbn` | 原厂 modem，由 `tqftpserv` 提供 |
| ath10k | `ath10k/WCN3990/hw1.0/{board.bin,firmware-5.bin}` | 原厂板级参数 + 上游 `linux-firmware` |
| Bluetooth | `qca/lmg820n-crbtfw21.tlv`、`qca/lmg820n-crnv21.bin` | 原厂 vendor |
| TFA9872 | `Tfa9872.cnt`、`Speaker_afe_custom_topology.bin` | 扬声器 DSP 参数与拓扑 |
| LC898124EP1 OIS | `onnn/lc898124ep1-lgit-{1,2}.{pm,dm}` | 提取自原厂相机驱动 |

## 启动时服务与限制

- **出厂 MAC 注入**：`alphaplus-wifi-address.service` 与 `alphaplus-bluetooth-address.service` 在驱动加载前读取本机 `ftm` 分区，分别生成 `/run/alphaplus/firmware/ath10k/WCN3990/hw1.0/wlan_mac.bin` 和写入蓝牙公共地址。
- **Remoteproc 启动**：`alphaplus-remoteproc-boot.service` 在本地文件系统就绪后拉起 ADSP、CDSP、SLPI 与 MPSS。
- **EFS 只读限制**：`rmtfs` 以 `-r -P -s` 运行；运行时对 Modem NV 的修改仅保留在内存副本中，不会写回物理 EFS 分区，重启后恢复原状。
