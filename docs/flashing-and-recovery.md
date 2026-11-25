# 刷写与回滚

所有部署操作限定于 `boot_a`、`product_a` 与 `linux` 分区，**不要改动原厂 `_b` 槽位**。

## 分区布局

| 分区标签 | 文件系统 / 大小 | 用途 |
| --- | --- | --- |
| `boot_a` | Android boot image | 存放 U-Boot 二级引导镜像（含图形菜单、Fastboot 与 extlinux） |
| `dtbo_a` | 原厂 DTBO | 保留原厂不动；`boot.img` 内置对应 [base.dtb](../nixos/pkgs/boot/base.dtb) |
| `product_a` | ext4 / 2 GiB | 挂载为 `/boot`，保存内核、initrd、DTB 与 `/extlinux/extlinux.conf` |
| `linux` | F2FS / 64 GiB | 挂载为 `/`，启用 `zstd` 全文件压缩、atgc、gc_merge 与 lazytime |

系统启动后由 `qbootctl.service` 调用 `qbootctl -m` 标记当前槽位启动成功。

## 刷写镜像

### 1. U-Boot（`boot_a`）

仅在 U-Boot 或启动镜像变动时更新：

```bash
# 通过 SSH 上传并刷写（自带上传与回读 SHA-256 校验）
tools/flash-boot.sh
```

处于 Fastboot 模式时也可本地构建后直接刷写：

```bash
tools/build-images.sh boot
fastboot flash boot_a dist/boot.img
fastboot reboot
```

### 2. `/boot` 与根文件系统

首次安装或全量重刷时，通过 Fastboot 刷写 `product_a` 与 `linux`（先进入桌面后再通过 NetworkManager 连接 Wi-Fi）：

```bash
tools/build-images.sh rootfs
fastboot flash product_a dist/boot.ext4
fastboot flash linux dist/rootfs.f2fs
fastboot reboot
```

## 在线更新

- 工作站部署：`tools/deploy-nixos.sh`
- 设备本机更新（复用 R2 缓存）：`sudo nixos-rebuild switch --flake github:YUeH885/alphaplus-nix#alphaplus`

## 排障与回滚

镜像使用 `console=tty0`（崩溃时由 `pstore_blk` 将日志写入 `pstore` 分区）：

```bash
ssh root@alphaplus 'journalctl -b -1 -k --no-pager'
ssh root@alphaplus 'ls -l /sys/fs/pstore'
```

- **回滚系统/内核**：重启时在 U-Boot 菜单进入 `Boot Linux from UFS (NixOS)`（`sysboot -p`）选择上一条 generation，或在系统内运行 `nixos-rebuild switch --rollback`。
- **回滚 U-Boot**：用保留的可用 `boot.img` 重新执行 `fastboot flash boot_a`。
