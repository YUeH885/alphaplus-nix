#!/usr/bin/env python3
"""Unit tests for usb_sleep_policy."""

from __future__ import annotations

import os
from pathlib import Path
import tempfile
import unittest

from usb_sleep_policy import (
    DWC3_CORE_REL,
    DWC3_GLUE_REL,
    NET_USB0_REL,
    PSY_USB_REL,
    ROLE_PATH_REL,
    TYPEC_MODE_REL,
    UDC_STATE_REL,
    collect_state,
    evaluate_policy,
    prepare_sleep,
    restore_sleep,
    write_policy_mode,
)


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


class UsbSleepPolicyTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.sysfs = self.root / "sys"
        self.proc = self.root / "proc"
        self.dev = self.root / "dev"
        self.policy = self.root / "var/lib/alphaplus/usb/sleep-policy"
        self.state = self.root / "run/usb-sleep-state.json"

        self.sysfs.mkdir(parents=True)
        self.proc.mkdir(parents=True)
        self.dev.mkdir(parents=True)

        _write(self.sysfs / DWC3_GLUE_REL / "power/wakeup", "enabled\n")
        _write(self.sysfs / DWC3_CORE_REL / "power/wakeup", "enabled\n")
        _write(self.sysfs / ROLE_PATH_REL, "none\n")
        _write(self.sysfs / UDC_STATE_REL, "not attached\n")
        _write(self.sysfs / PSY_USB_REL / "online", "0\n")
        _write(self.sysfs / PSY_USB_REL / "usb_type", "[Unknown] SDP DCP CDP C PD\n")
        _write(self.sysfs / TYPEC_MODE_REL, "default\n")
        _write(self.sysfs / NET_USB0_REL / "flags", "0x1002\n")
        _write(self.sysfs / NET_USB0_REL / "operstate", "down\n")

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _add_host_device(
        self,
        name: str,
        *,
        dev_class: str = "00",
        intf_classes: tuple[str, ...] = ("03",),
        wakeup: str = "disabled",
        mounted_block: bool = False,
    ) -> None:
        xhci_dev = self.sysfs / DWC3_CORE_REL / "xhci-hcd.1.auto"
        _write(xhci_dev / "power/wakeup", "enabled\n")
        real_dev = xhci_dev / "usb1" / name
        _write(real_dev / "bDeviceClass", f"{dev_class}\n")
        _write(real_dev / "power/wakeup", f"{wakeup}\n")

        bus_dir = self.sysfs / "bus/usb/devices"
        bus_dir.mkdir(parents=True, exist_ok=True)
        os.symlink(real_dev, bus_dir / name)

        for idx, cls in enumerate(intf_classes):
            intf_dir = real_dev / f"{name}:1.{idx}"
            _write(intf_dir / "bInterfaceClass", f"{cls}\n")
            os.symlink(intf_dir, bus_dir / f"{name}:1.{idx}")

        if mounted_block:
            part_dir = real_dev / "host0/target0:0:0/0:0:0:0/block/sda/sda1"
            _write(part_dir / "dev", "8:1\n")
            block_dir = self.sysfs / "class/block"
            block_dir.mkdir(parents=True, exist_ok=True)
            os.symlink(part_dir, block_dir / "sda1")
            _write(self.proc / "self/mountinfo", "100 1 8:1 / /mnt rw - ext4 /dev/sda1 rw\n")

    def _eval(self, profile: str = "balanced"):
        st = collect_state(
            sysfs_root=self.sysfs,
            proc_root=self.proc,
            dev_root=self.dev,
            policy_file=self.policy,
            profile_override=profile,
        )
        return evaluate_policy(st)

    def test_unplugged_powers_off(self) -> None:
        d = self._eval("performance")
        self.assertFalse(d.retain)
        self.assertEqual(d.reason, "role-none")

    def test_device_addressed_3a_usb0_down_powers_off(self) -> None:
        _write(self.sysfs / ROLE_PATH_REL, "device\n")
        _write(self.sysfs / UDC_STATE_REL, "addressed\n")
        _write(self.sysfs / PSY_USB_REL / "online", "1\n")
        _write(self.sysfs / TYPEC_MODE_REL, "3.0A\n")
        d = self._eval("balanced")
        self.assertFalse(d.retain)
        self.assertEqual(d.reason, "device-idle-power-off")

    def test_device_usb0_active_retains_in_balanced_but_off_in_power_saver(self) -> None:
        _write(self.sysfs / ROLE_PATH_REL, "device\n")
        _write(self.sysfs / UDC_STATE_REL, "configured\n")
        _write(self.sysfs / PSY_USB_REL / "online", "1\n")
        _write(self.sysfs / PSY_USB_REL / "usb_type", "Unknown SDP DCP CDP C [PD]\n")
        _write(self.sysfs / TYPEC_MODE_REL, "3.0A\n")
        _write(self.sysfs / NET_USB0_REL / "flags", "0x1003\n")
        _write(self.sysfs / NET_USB0_REL / "carrier", "1\n")
        _write(self.sysfs / NET_USB0_REL / "operstate", "up\n")

        self.assertTrue(self._eval("balanced").retain)
        d_saver = self._eval("power-saver")
        self.assertFalse(d_saver.retain)
        self.assertEqual(d_saver.reason, "device-active-power-saver")

    def test_device_ttygs0_open_retains(self) -> None:
        _write(self.sysfs / ROLE_PATH_REL, "device\n")
        _write(self.sysfs / UDC_STATE_REL, "configured\n")
        tty = self.dev / "ttyGS0"
        _write(tty, "")
        fd_dir = self.proc / "123/fd"
        fd_dir.mkdir(parents=True)
        os.symlink(tty, fd_dir / "3")

        d = self._eval("balanced")
        self.assertTrue(d.retain)
        self.assertEqual(d.reason, "device-gadget-active")

    def test_device_sdp_default_charging_protected_in_power_saver(self) -> None:
        _write(self.sysfs / ROLE_PATH_REL, "device\n")
        _write(self.sysfs / UDC_STATE_REL, "configured\n")
        _write(self.sysfs / PSY_USB_REL / "online", "1\n")
        _write(self.sysfs / PSY_USB_REL / "usb_type", "Unknown [SDP] DCP CDP C PD\n")
        _write(self.sysfs / TYPEC_MODE_REL, "default\n")

        d = self._eval("power-saver")
        self.assertTrue(d.retain)
        self.assertEqual(d.reason, "device-sdp-charging")

    def test_host_billboard_and_empty_hub_power_off(self) -> None:
        _write(self.sysfs / ROLE_PATH_REL, "host\n")
        self._add_host_device("1-1", dev_class="09", intf_classes=("09",))
        self._add_host_device("1-1.1", dev_class="11", intf_classes=("11",))

        d = self._eval("performance")
        self.assertFalse(d.retain)
        self.assertEqual(d.reason, "host-idle-power-off")

    def test_host_non_wakeup_hid_depends_on_profile(self) -> None:
        _write(self.sysfs / ROLE_PATH_REL, "host\n")
        self._add_host_device("1-1", dev_class="00", intf_classes=("03",), wakeup="disabled")

        self.assertFalse(self._eval("balanced").retain)
        d_perf = self._eval("performance")
        self.assertTrue(d_perf.retain)
        self.assertEqual(d_perf.reason, "host-functional-performance")

    def test_host_wakeup_enabled_device_retains_in_balanced(self) -> None:
        _write(self.sysfs / ROLE_PATH_REL, "host\n")
        self._add_host_device("1-1", dev_class="00", intf_classes=("03",), wakeup="enabled")

        d_bal = self._eval("balanced")
        self.assertTrue(d_bal.retain)
        self.assertEqual(d_bal.reason, "host-device-wakeup")

        d_saver = self._eval("power-saver")
        self.assertFalse(d_saver.retain)
        self.assertEqual(d_saver.reason, "host-wakeup-power-saver")

    def test_host_mounted_storage_retains_even_in_power_saver(self) -> None:
        _write(self.sysfs / ROLE_PATH_REL, "host\n")
        self._add_host_device(
            "2-1", dev_class="00", intf_classes=("08",), wakeup="disabled", mounted_block=True
        )
        d = self._eval("power-saver")
        self.assertTrue(d.retain)
        self.assertEqual(d.reason, "host-mounted-storage")

    def test_manual_override_and_prepare_restore(self) -> None:
        _write(self.sysfs / ROLE_PATH_REL, "host\n")
        self._add_host_device("2-1", dev_class="00", intf_classes=("08",), mounted_block=True)
        write_policy_mode("power-off", self.policy)

        d = prepare_sleep(
            sysfs_root=self.sysfs,
            proc_root=self.proc,
            dev_root=self.dev,
            policy_file=self.policy,
            state_file=self.state,
            profile_override="balanced",
        )
        self.assertFalse(d.retain)
        self.assertEqual(d.reason, "manual-power-off")
        self.assertEqual((self.sysfs / DWC3_GLUE_REL / "power/wakeup").read_text().strip(), "disabled")
        self.assertEqual((self.sysfs / DWC3_CORE_REL / "power/wakeup").read_text().strip(), "disabled")
        self.assertEqual(
            (self.sysfs / DWC3_CORE_REL / "xhci-hcd.1.auto/power/wakeup").read_text().strip(),
            "disabled",
        )

        # Second prepare (e.g. maintenance loop) keeps original saved state.
        write_policy_mode("retain", self.policy)
        d2 = prepare_sleep(
            sysfs_root=self.sysfs,
            proc_root=self.proc,
            dev_root=self.dev,
            policy_file=self.policy,
            state_file=self.state,
            profile_override="balanced",
        )
        self.assertTrue(d2.retain)
        self.assertEqual((self.sysfs / DWC3_GLUE_REL / "power/wakeup").read_text().strip(), "enabled")

        # Hot-unplugged child xhci wakeup node does not break restore for remaining nodes.
        (self.sysfs / DWC3_CORE_REL / "xhci-hcd.1.auto/power/wakeup").unlink()
        self.assertTrue(restore_sleep(self.state))
        self.assertEqual((self.sysfs / DWC3_GLUE_REL / "power/wakeup").read_text().strip(), "enabled")
        self.assertEqual((self.sysfs / DWC3_CORE_REL / "power/wakeup").read_text().strip(), "enabled")

    def test_empty_hub_with_wakeup_still_powers_off(self) -> None:
        _write(self.sysfs / ROLE_PATH_REL, "host\n")
        self._add_host_device("1-1", dev_class="09", intf_classes=("09",), wakeup="enabled")
        d = self._eval("balanced")
        self.assertFalse(d.retain)
        self.assertEqual(d.reason, "host-idle-power-off")


if __name__ == "__main__":
    unittest.main()
