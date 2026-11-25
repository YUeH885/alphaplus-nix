#!/usr/bin/env bash
set -euo pipefail
export LC_ALL=C

ROOT=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
IMAGE=${1:-}
TARGET=${SSH_TARGET:-alphaplus}
if [[ "$TARGET" != *@* ]]; then
	TARGET="root@$TARGET"
fi
REMOTE_TEMP=/tmp/boot_a_flash.img

if [[ -z "$IMAGE" ]]; then
	UBOOT_OVERRIDE_ARGS=()
	if [[ -e "$ROOT/source/u-boot/.git" ]]; then
		UBOOT_OVERRIDE_ARGS=(--override-input uboot-src "git+file://$ROOT/source/u-boot?shallow=1")
	fi
	IMAGE=$(nix build --no-link --print-out-paths --option extra-platforms "" --option builders "${NIX_BUILDERS:-}" \
		"${UBOOT_OVERRIDE_ARGS[@]}" "$ROOT#boot")/boot.img
fi

[[ -s "$IMAGE" ]] || { echo "Error: Missing image: $IMAGE" >&2; exit 1; }
LOCAL_SHA256=$(sha256sum "$IMAGE" | awk '{print $1}')

echo "Uploading $IMAGE to $TARGET ($LOCAL_SHA256)..."
scp "$IMAGE" "$TARGET:$REMOTE_TEMP"

echo "Writing boot image to boot_a on $TARGET..."
ssh "$TARGET" bash -s -- "$REMOTE_TEMP" "$LOCAL_SHA256" <<'REMOTE'
set -euo pipefail
image=$1
expected_sha=$2

device=$(readlink -f /dev/disk/by-partlabel/boot_a)
[[ -b "$device" ]] || { echo "Error: boot_a is not a block device." >&2; exit 1; }

trap 'rm -f "$image"' EXIT

# Verify uploaded image
printf '%s  %s\n' "$expected_sha" "$image" | sha256sum -c -

# Flash and sync
img_size=$(stat -c '%s' "$image")
dd if="$image" of="$device" bs=4096 status=progress conv=fsync
sync

# Verify readback
readback_sha=$(head -c "$img_size" "$device" | sha256sum | awk '{print $1}')
[[ "$readback_sha" == "$expected_sha" ]] || {
	echo "Error: Readback checksum mismatch ($readback_sha != $expected_sha)" >&2
	exit 1
}

echo "boot_a successfully flashed and verified."
REMOTE
