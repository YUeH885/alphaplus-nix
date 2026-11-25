#!/usr/bin/env python3
"""Read LG factory Bluetooth identity before allowing BlueZ to start."""

from collections import deque
from pathlib import Path
import shutil
import socket
import struct
import subprocess
import sys
import time

from ftm import FTM_DEVICE, FTM_ITEM_BT_ADDR, read_ftm_mac


HCI_DEV_NONE = 0xFFFF
HCI_CHANNEL_CONTROL = 3
READ_INDEX_LIST = 0x0003
READ_INFO = 0x0004
READ_UNCONF_INDEX_LIST = 0x0036
SET_PUBLIC_ADDRESS = 0x0039
CMD_COMPLETE = 0x0001
CMD_STATUS = 0x0002
INDEX_ADDED = 0x0004
UNCONF_INDEX_ADDED = 0x001D


def read_factory_address(path=FTM_DEVICE):
    # FTM stores display order; Bluetooth management uses little endian.
    return read_ftm_mac(FTM_ITEM_BT_ADDR, "Bluetooth public", path)[::-1]


class Management:
    def __init__(self, channel, timeout=30):
        self.channel = channel
        self.deadline = time.monotonic() + timeout
        self.events = deque()

    def receive(self):
        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("Bluetooth initialization timed out")
        self.channel.settimeout(remaining)
        packet = self.channel.recv(65535)
        event, index, length = struct.unpack_from("<HHH", packet)
        payload = packet[6:]
        if len(payload) != length:
            raise ValueError("Invalid Bluetooth management packet length")
        return event, index, payload

    def command(self, opcode, index=HCI_DEV_NONE, payload=b""):
        self.channel.sendall(struct.pack("<HHH", opcode, index, len(payload)) + payload)
        while True:
            event, response_index, data = self.receive()
            if event in (CMD_COMPLETE, CMD_STATUS):
                response_opcode, status = struct.unpack_from("<HB", data)
                if (response_opcode, response_index) != (opcode, index):
                    continue
                if status:
                    raise RuntimeError(f"Bluetooth command 0x{opcode:04x} failed: 0x{status:02x}")
                if event == CMD_COMPLETE:
                    return data[3:]
            else:
                self.events.append((event, response_index, data))

    def next_event(self):
        return self.events.popleft() if self.events else self.receive()


def is_board_controller(index):
    node = Path(f"/sys/class/bluetooth/hci{index}/device/of_node/compatible")
    return node.exists() and b"qcom,wcn3998-bt" in node.read_bytes().split(b"\0")


def find_controller(management):
    # These commands also subscribe the socket to both index event streams.
    for opcode, configured in ((READ_INDEX_LIST, True), (READ_UNCONF_INDEX_LIST, False)):
        data = management.command(opcode)
        count, = struct.unpack_from("<H", data)
        for index in struct.unpack_from(f"<{count}H", data, 2):
            if is_board_controller(index):
                return index, configured
    while True:
        event, index, _ = management.next_event()
        if event in (INDEX_ADDED, UNCONF_INDEX_ADDED) and is_board_controller(index):
            return index, event == INDEX_ADDED


def configure(management, address):
    index, configured = find_controller(management)
    if configured:
        info = management.command(READ_INFO, index)
        if info[:6] == address:
            return
        settings, = struct.unpack_from("<I", info, 13)
        if settings & 1:
            raise RuntimeError("Power off Bluetooth before changing its public address")

    # Discard discovery events; the next INDEX_ADDED must confirm this change.
    management.events.clear()
    management.command(SET_PUBLIC_ADDRESS, index, address)
    while True:
        event, event_index, _ = management.next_event()
        if (event, event_index) == (INDEX_ADDED, index):
            break
    if management.command(READ_INFO, index)[:6] != address:
        raise RuntimeError("Controller address does not match LG factory data")


def main():
    address = read_factory_address()
    modprobe_bin = shutil.which("modprobe") or "/usr/sbin/modprobe"
    subprocess.run([modprobe_bin, "hci_uart"], check=True)
    with socket.socket(socket.AF_BLUETOOTH, socket.SOCK_RAW, socket.BTPROTO_HCI) as channel:
        channel.bind((HCI_DEV_NONE, HCI_CHANNEL_CONTROL))
        configure(Management(channel), address)
    print("Bluetooth initialized with its LG factory address")


if __name__ == "__main__":
    try:
        main()
    except (OSError, ValueError, RuntimeError, struct.error, subprocess.CalledProcessError) as error:
        print(f"Bluetooth initialization failed: {error}", file=sys.stderr)
        sys.exit(1)
