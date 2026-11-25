#!/usr/bin/env bash
set -Eeuo pipefail
export LC_ALL=C

ROOT=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
TARGET=${1:-${SSH_TARGET:-alphaplus}}
if [[ "$TARGET" != *@* ]]; then
	TARGET="root@$TARGET"
fi
DEFAULT_FLAKE_TARGET=alphaplus
if [[ $(uname -m) == x86_64 ]]; then
	DEFAULT_FLAKE_TARGET=alphaplus-cross
fi
FLAKE_TARGET=${2:-${FLAKE_TARGET:-$DEFAULT_FLAKE_TARGET}}
FLAKE_TARGET=${FLAKE_TARGET##*#}

LINUX_OVERRIDE_ARGS=()
if [[ -e "$ROOT/source/linux/.git" ]]; then
	LINUX_OVERRIDE_ARGS=(--override-input linux-src "git+file://$ROOT/source/linux?shallow=1")
fi

echo "=== Deploying NixOS configuration to $TARGET (#$FLAKE_TARGET) ==="
nixos-rebuild switch --option extra-platforms "" --option builders "${NIX_BUILDERS:-}" \
	"${LINUX_OVERRIDE_ARGS[@]}" --flake "$ROOT#$FLAKE_TARGET" --target-host "$TARGET" --build-host localhost
