# 构建与缓存

本文命令均在仓库根目录执行。

## 输入与产出

| 项目 | 位置 | 说明 |
| --- | --- | --- |
| 内核源码 | `flake.lock`（`linux-src`） / `source/linux` | `alphaplus-7.3.y` 分支；存在 `source/linux/.git` 时脚本自动优先覆盖 |
| 内核配置 | [alphaplus-kernel.config](../nixos/pkgs/kernel/alphaplus-kernel.config) | `manualConfig` 构建，内建 UFS、initrd、F2FS、ext4、`PREEMPT_LAZY`、`DMABUF_HEAPS` |
| U-Boot 源码 | `flake.lock`（`uboot-src`） / `source/u-boot` | `alphaplus-v2026.10` 分支，使用 `lmg820n_defconfig` |
| 系统与板级包 | [flake.nix](../flake.nix)、[nixos/](../nixos/) | 提供 `alphaplus`（aarch64 原生）与 `alphaplus-cross`（x86_64 混合交叉） |
| 镜像产物 | `dist/` | `boot.img`、`boot.ext4`、`rootfs.f2fs` |

## 本地构建

### 刷写镜像

使用 [build-images.sh](../tools/build-images.sh) 构建并导出稀疏镜像到 `dist/`（可通过 `OUT_DIR` 改路径）：

```bash
tools/build-images.sh          # 构建全部 3 个镜像
tools/build-images.sh boot     # 仅构建 boot.img（.#boot，包含 u-boot.bin、空 ramdisk 与 base.dtb）
tools/build-images.sh rootfs   # 构建 boot.ext4（2 GiB /boot）与 rootfs.f2fs（64 GiB F2FS /）
```

系统默认创建 `root` 与普通用户 `alphaplus`（初始密码同为 `alphaplus`，见 [users.nix](../nixos/modules/system/users.nix)）。

### 单项构建与检查

```bash
# 验证 flake 求值（存在本地源码时可加 --override-input）
nix flake check --no-build --option extra-platforms "" \
  --override-input linux-src "git+file://$PWD/source/linux?shallow=1" \
  --override-input uboot-src "git+file://$PWD/source/u-boot?shallow=1"

# 单独验证系统配置或内核（x86_64 主机）
nix build --no-link --option extra-platforms "" --option builders "" \
  .#nixosConfigurations.alphaplus-cross.config.system.build.toplevel
nix build --no-link --option extra-platforms "" --option builders "" \
  --override-input linux-src "git+file://$PWD/source/linux?shallow=1" .#kernel
```

## 更新部署

- **从工作站推送更新**：`tools/deploy-nixos.sh` 自动按主机架构选用 `alphaplus-cross` 或 `alphaplus`，通过 `nixos-rebuild switch` 构建并切换到目标设备（默认主机名 `alphaplus`）。
- **直接在设备更新**：设备上已预配置 R2 缓存公钥，可直接运行：

```bash
sudo nixos-rebuild switch --flake github:YUeH885/alphaplus-nix#alphaplus
```

## CI 与 R2 二进制缓存

[.github/workflows/ci.yml](../.github/workflows/ci.yml) 在 `push` 到 `main`、PR 或手动触发时于 `ubuntu-24.04-arm` 原生构建 `#alphaplus` 完整系统、`uboot` 与 `boot`。

非 PR 构建完成后，[r2-cache.py](../tools/ci/r2-cache.py) 仅签名并上传闭包中无 `cache.nixos.org-1:` 签名的路径（`zstd` 压缩，`nix-cache-info` 设为 `Priority: 41`），随后清理 R2 中不在本次活动清单内的旧对象，不保留历史版本。

[system/default.nix](../nixos/modules/system/default.nix) 与 [flake.nix](../flake.nix)（`nixConfig`）已默认启用公共缓存：

```nix
extra-substituters = [ "https://alphaplus-nix.hwrng.dev" ];
extra-trusted-public-keys = [
  "alphaplus-cache-1:vEKvOaAhSomjA2SE/+8LgiNcai6j+fl8YvKeOUwIB8M="
];
```

非本系统的构建机拉取 flake 时可附加 `--accept-flake-config`（或将两行写入 `/etc/nix/nix.conf`）以直接复用缓存。

<details>
<summary>Fork 或自托管时的 GitHub Actions Secrets / Variables 配置清单</summary>

生成签名密钥对：

```bash
nix key generate-secret --key-name alphaplus-cache-1 > alphaplus-cache.sec
nix key convert-secret-to-public < alphaplus-cache.sec
```

在仓库 `Settings -> Secrets and variables -> Actions` 配置：

- **Secrets**：`R2_ACCESS_KEY_ID`、`R2_SECRET_ACCESS_KEY`（R2 bucket 读写删权限）、`NIX_SIGNING_KEY`（私钥内容）
- **Variables**：`R2_ENDPOINT`（`https://<account-id>.r2.cloudflarestorage.com`）、`R2_BUCKET`、`R2_PUBLIC_URL`、`NIX_PUBLIC_KEY`

</details>
