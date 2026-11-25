"""Read and validate LG factory identifiers from the FTM partition."""

from pathlib import Path


FTM_DEVICE = Path("/dev/disk/by-partlabel/ftm")
FTM_BLOCK_SIZE = 4096
FTM_MAGIC = b"BSP Forever\0"
FTM_ITEM_WLAN_MAC = 6
FTM_ITEM_BT_ADDR = 8


def read_ftm_mac(item, label, path=FTM_DEVICE):
    """Read a 6-byte factory MAC address from an LG FTM item in display order."""
    with path.open("rb", buffering=0) as factory:
        factory.seek(FTM_BLOCK_SIZE)
        if factory.read(len(FTM_MAGIC)) != FTM_MAGIC:
            raise ValueError("Invalid LG FTM signature")
        factory.seek(item * FTM_BLOCK_SIZE)
        address = factory.read(6)
    # Reject all-zero, multicast (bit 0), or locally administered (bit 1) values.
    if len(address) != 6 or address == bytes(6) or address[0] & 3:
        raise ValueError(f"Invalid factory {label} address")
    return address
