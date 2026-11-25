#!/usr/bin/env bash
set -euo pipefail
export LC_ALL=C

ROOT=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
OUT_DIR=${OUT_DIR:-$ROOT/dist}
TARGET=${1:-all}

usage() {
	cat <<'EOF'
Usage: tools/build-images.sh [all|boot|rootfs]

Build NixOS flashable images and export materialized files to dist/ (or $OUT_DIR):
  boot    Export dist/boot.img (.#boot)
  rootfs  Export dist/boot.ext4 and dist/rootfs.f2fs (.#rootfs)
  all     Export all three images (default)
EOF
}

case "$TARGET" in
	all|boot|rootfs) ;;
	-h|--help) usage; exit 0 ;;
	*) usage >&2; exit 2 ;;
esac

OVERRIDE_ARGS=()
if [[ -e "$ROOT/source/linux/.git" ]]; then
	OVERRIDE_ARGS+=(--override-input linux-src "git+file://$ROOT/source/linux?shallow=1")
fi
if [[ -e "$ROOT/source/u-boot/.git" ]]; then
	OVERRIDE_ARGS+=(--override-input uboot-src "git+file://$ROOT/source/u-boot?shallow=1")
fi

build_and_export() {
	local attr=$1
	shift
	local out_path
	out_path=$(nix build --no-link --print-out-paths --option extra-platforms "" --option builders "${NIX_BUILDERS:-}" \
		"${OVERRIDE_ARGS[@]}" "$ROOT#$attr")
	for file in "$@"; do
		cp -L --sparse=always -f "$out_path/$file" "$OUT_DIR/$file"
		chmod 0644 "$OUT_DIR/$file"
	done
}

mkdir -p "$OUT_DIR"

if [[ "$TARGET" == all || "$TARGET" == boot ]]; then
	echo "=== Building and exporting boot image to $OUT_DIR ==="
	build_and_export boot boot.img
fi

if [[ "$TARGET" == all || "$TARGET" == rootfs ]]; then
	echo "=== Building and exporting rootfs images to $OUT_DIR ==="
	build_and_export rootfs boot.ext4 rootfs.f2fs
fi

ls -lh "$OUT_DIR"
