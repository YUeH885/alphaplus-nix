#!/usr/bin/env python3
"""Generate ath10k wlan_mac.bin from LG factory FTM data."""

import os
from pathlib import Path
import sys
import tempfile

from ftm import FTM_DEVICE, FTM_ITEM_WLAN_MAC, read_ftm_mac


DEFAULT_OUTPUT_PATH = Path("/run/alphaplus/firmware/ath10k/WCN3990/hw1.0/wlan_mac.bin")


def format_wlan_mac(address):
    """Format a 6-byte big-endian MAC address into ath10k wlan_mac.bin text."""
    return f"Intf0MacAddress={address.hex()}\nEND\n".encode("ascii")


def write_wlan_mac(ftm_path=FTM_DEVICE, output_path=DEFAULT_OUTPUT_PATH):
    """Read the factory WLAN MAC from FTM and write wlan_mac.bin atomically."""
    address = read_ftm_mac(FTM_ITEM_WLAN_MAC, "WLAN MAC", ftm_path)
    payload = format_wlan_mac(address)

    output_path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        prefix=".wlan_mac.bin.",
        dir=output_path.parent,
    )
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(payload)
        os.chmod(tmp_path, 0o644)
        os.replace(tmp_path, output_path)
    except BaseException:
        tmp_path.unlink(missing_ok=True)
        raise
    return address


def main():
    write_wlan_mac()
    print("WLAN factory MAC file generated from LG FTM data")


if __name__ == "__main__":
    try:
        main()
    except (OSError, ValueError) as error:
        print(f"WLAN initialization failed: {error}", file=sys.stderr)
        sys.exit(1)
