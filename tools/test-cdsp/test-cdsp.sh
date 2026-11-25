#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
CLIENT=${1:-"$SCRIPT_DIR/client"}
SHELL_PATH=${CDSP_SHELL:-"/usr/share/qcom/sm8150/LG/alphaplus/dsp/cdsp/fastrpc_shell_unsigned_3"}
DEVICE_ROOT=${CDSP_ROOT:-"/usr/share/qcom/sm8150/LG/alphaplus"}

if [[ ! -x "$CLIENT" ]]; then
    echo "Error: test client binary not found at $CLIENT. Run 'make' first." >&2
    exit 1
fi

HEXAGONRPCD_BIN=${HEXAGONRPCD:-$(command -v hexagonrpcd || echo "/usr/libexec/hexagonrpc/hexagonrpcd")}

exec "$HEXAGONRPCD_BIN" \
    -f /dev/fastrpc-cdsp -d cdsp -u \
    -c "$SHELL_PATH" \
    -R "$DEVICE_ROOT" \
    -p "$CLIENT"
