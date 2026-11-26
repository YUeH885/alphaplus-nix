#!/usr/bin/env python3
"""Scene-based USB s2idle retention and wakeup policy for LM-G820N."""

from __future__ import annotations

import argparse
from dataclasses import asdict, dataclass
import json
import os
from pathlib import Path
import re
import subprocess
import sys
from typing import Iterable


DEFAULT_SYSFS_ROOT = Path("/sys")
DEFAULT_PROC_ROOT = Path("/proc")
DEFAULT_DEV_ROOT = Path("/dev")
DEFAULT_POLICY_FILE = Path("/var/lib/alphaplus/usb/sleep-policy")
DEFAULT_STATE_FILE = Path("/run/alphaplus-usb-sleep-state.json")
BUSCTL_BIN = "@busctl@"

VALID_MODES = ("auto", "retain", "power-off")
VALID_PROFILES = ("power-saver", "balanced", "performance")
USB_DEV_NAME_RE = re.compile(r"^\d+-\d+(?:\.\d+)*$")

DWC3_GLUE_REL = Path("bus/platform/devices/a6f8800.usb")
DWC3_CORE_REL = Path("bus/platform/devices/a6f8800.usb/a600000.usb")
ROLE_PATH_REL = Path("class/usb_role/a600000.usb-role-switch/role")
UDC_STATE_REL = Path("class/udc/a600000.usb/state")
NET_USB0_REL = Path("class/net/usb0")
PSY_USB_REL = Path("class/power_supply/usb")
TYPEC_MODE_REL = Path("class/typec/port0/power_operation_mode")


@dataclass(frozen=True)
class HostUsbDevice:
    name: str
    device_class: str
    interface_classes: tuple[str, ...]
    wakeup_enabled: bool
    has_mounted_block: bool

    @property
    def is_hub(self) -> bool:
        return self.device_class == "09"

    @property
    def is_billboard_only(self) -> bool:
        if self.device_class == "11":
            return True
        return bool(self.interface_classes) and all(c == "11" for c in self.interface_classes)

    @property
    def is_functional(self) -> bool:
        return not self.is_hub and not self.is_billboard_only


@dataclass(frozen=True)
class UsbSceneState:
    mode: str
    profile: str
    role: str
    udc_state: str
    usb_online: bool
    usb_type: str
    typec_op_mode: str
    usb0_active: bool
    ttygs0_open: bool
    host_devices: tuple[HostUsbDevice, ...]

    @property
    def sdp_charging_dependent(self) -> bool:
        # qcom_pm8150b only enforces gadget_current_limit on SDP with Type-C default Rp.
        return self.usb_online and self.usb_type == "SDP" and self.typec_op_mode == "default"


@dataclass(frozen=True)
class PolicyDecision:
    retain: bool
    reason: str
    mode: str
    profile: str
    role: str
    udc_state: str


def _read_text(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8").strip()
    except OSError:
        return None


def _parse_active_bracket(value: str | None) -> str:
    if not value:
        return "Unknown"
    match = re.search(r"\[([^\]]+)\]", value)
    return match.group(1) if match else "Unknown"


def read_policy_mode(policy_file: Path = DEFAULT_POLICY_FILE) -> str:
    value = _read_text(policy_file)
    if value in VALID_MODES:
        return value
    return "auto"


def write_policy_mode(mode: str, policy_file: Path = DEFAULT_POLICY_FILE) -> None:
    if mode not in VALID_MODES:
        raise ValueError(f"Unsupported policy mode {mode!r}; expected one of {', '.join(VALID_MODES)}")
    policy_file.parent.mkdir(parents=True, exist_ok=True)
    policy_file.write_text(f"{mode}\n", encoding="utf-8")


def query_power_profile(busctl_bin: str = BUSCTL_BIN) -> str:
    cmd = busctl_bin if not busctl_bin.startswith("@") else "busctl"
    try:
        result = subprocess.run(
            [
                cmd,
                "get-property",
                "org.freedesktop.UPower.PowerProfiles",
                "/org/freedesktop/UPower/PowerProfiles",
                "org.freedesktop.UPower.PowerProfiles",
                "ActiveProfile",
            ],
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=2,
        )
    except (OSError, subprocess.SubprocessError):
        return "balanced"

    parts = result.stdout.strip().split(None, 1)
    if len(parts) == 2 and parts[0] == "s":
        profile = parts[1].strip('"')
        if profile in VALID_PROFILES:
            return profile
    return "balanced"


def _is_usb0_active(sysfs_root: Path) -> bool:
    net_dir = sysfs_root / NET_USB0_REL
    flags_text = _read_text(net_dir / "flags")
    if not flags_text:
        return False
    try:
        iff_up = bool(int(flags_text, 16) & 0x1)
    except ValueError:
        return False
    if not iff_up:
        return False
    carrier = _read_text(net_dir / "carrier")
    operstate = _read_text(net_dir / "operstate")
    return carrier == "1" or operstate == "up"


def _is_ttygs0_open(proc_root: Path, dev_root: Path) -> bool:
    target = os.path.realpath(dev_root / "ttyGS0")
    try:
        entries = list(proc_root.iterdir())
    except OSError:
        return False
    for entry in entries:
        if not entry.name.isdigit():
            continue
        fd_dir = entry / "fd"
        try:
            fds = list(fd_dir.iterdir())
        except OSError:
            continue
        for fd in fds:
            try:
                if os.path.realpath(fd) == target:
                    return True
            except OSError:
                continue
    return False


def _mounted_major_minors(proc_root: Path) -> set[str]:
    text = _read_text(proc_root / "self/mountinfo")
    if not text:
        return set()
    mounted: set[str] = set()
    for line in text.splitlines():
        fields = line.split()
        if len(fields) >= 3:
            mounted.add(fields[2])
    return mounted


def _mounted_usb_block_prefixes(sysfs_root: Path, proc_root: Path, controller_prefix: str) -> tuple[str, ...]:
    mounted_devs = _mounted_major_minors(proc_root)
    if not mounted_devs:
        return ()
    block_root = sysfs_root / "class/block"
    if not block_root.exists():
        return ()
    prefixes: list[str] = []
    for entry in block_root.iterdir():
        try:
            resolved = os.path.realpath(entry)
        except OSError:
            continue
        if not resolved.startswith(controller_prefix):
            continue
        dev_num = _read_text(entry / "dev")
        if dev_num and dev_num in mounted_devs:
            prefixes.append(resolved)
    return tuple(prefixes)


def _collect_host_devices(sysfs_root: Path, proc_root: Path) -> tuple[HostUsbDevice, ...]:
    core_dir = sysfs_root / DWC3_CORE_REL
    usb_bus = sysfs_root / "bus/usb/devices"
    if not core_dir.exists() or not usb_bus.exists():
        return ()

    core_real = os.path.realpath(core_dir).rstrip("/") + "/"
    mounted_blocks = _mounted_usb_block_prefixes(sysfs_root, proc_root, core_real)

    bus_entries = sorted(usb_bus.iterdir(), key=lambda p: p.name)
    devices: list[HostUsbDevice] = []
    for entry in bus_entries:
        if not USB_DEV_NAME_RE.match(entry.name):
            continue
        try:
            dev_real = os.path.realpath(entry)
        except OSError:
            continue
        if not dev_real.startswith(core_real):
            continue

        dev_prefix = dev_real.rstrip("/") + "/"
        has_mounted = any(b.startswith(dev_prefix) for b in mounted_blocks)
        dev_class = (_read_text(entry / "bDeviceClass") or "").lower()
        wakeup_enabled = _read_text(entry / "power/wakeup") == "enabled"

        intf_classes: list[str] = []
        intf_prefix = f"{entry.name}:"
        for intf in bus_entries:
            if intf.name.startswith(intf_prefix):
                cls = (_read_text(intf / "bInterfaceClass") or "").lower()
                if cls:
                    intf_classes.append(cls)

        devices.append(
            HostUsbDevice(
                name=entry.name,
                device_class=dev_class,
                interface_classes=tuple(intf_classes),
                wakeup_enabled=wakeup_enabled,
                has_mounted_block=has_mounted,
            )
        )
    return tuple(devices)


def collect_state(
    *,
    sysfs_root: Path = DEFAULT_SYSFS_ROOT,
    proc_root: Path = DEFAULT_PROC_ROOT,
    dev_root: Path = DEFAULT_DEV_ROOT,
    policy_file: Path = DEFAULT_POLICY_FILE,
    profile_override: str | None = None,
) -> UsbSceneState:
    mode = read_policy_mode(policy_file)
    profile = profile_override if profile_override in VALID_PROFILES else query_power_profile()
    role = _read_text(sysfs_root / ROLE_PATH_REL) or "none"
    udc_state = _read_text(sysfs_root / UDC_STATE_REL) or "not attached"
    usb_online = _read_text(sysfs_root / PSY_USB_REL / "online") == "1"
    usb_type = _parse_active_bracket(_read_text(sysfs_root / PSY_USB_REL / "usb_type"))
    typec_op_mode = _read_text(sysfs_root / TYPEC_MODE_REL) or "default"
    usb0_active = _is_usb0_active(sysfs_root)
    ttygs0_open = _is_ttygs0_open(proc_root, dev_root)
    host_devices = _collect_host_devices(sysfs_root, proc_root) if role == "host" else ()

    return UsbSceneState(
        mode=mode,
        profile=profile,
        role=role,
        udc_state=udc_state,
        usb_online=usb_online,
        usb_type=usb_type,
        typec_op_mode=typec_op_mode,
        usb0_active=usb0_active,
        ttygs0_open=ttygs0_open,
        host_devices=host_devices,
    )


def evaluate_policy(state: UsbSceneState) -> PolicyDecision:
    def decide(retain: bool, reason: str) -> PolicyDecision:
        return PolicyDecision(
            retain=retain,
            reason=reason,
            mode=state.mode,
            profile=state.profile,
            role=state.role,
            udc_state=state.udc_state,
        )

    if state.mode == "power-off":
        return decide(False, "manual-power-off")

    if state.mode == "retain":
        if state.role == "host":
            return decide(True, "manual-retain-host")
        if state.role == "device" and state.udc_state not in {"not attached", "attached", "powered"}:
            return decide(True, "manual-retain-device")
        return decide(False, "unattached")

    # Auto mode: decide per active role and workload/charging scene.
    if state.role == "none":
        return decide(False, "role-none")

    if state.role == "device":
        if state.udc_state in {"not attached", "attached", "powered"}:
            return decide(False, f"device-{state.udc_state.replace(' ', '-')}")

        if state.sdp_charging_dependent:
            return decide(True, "device-sdp-charging")

        if state.usb0_active or state.ttygs0_open:
            if state.profile == "power-saver":
                return decide(False, "device-active-power-saver")
            return decide(True, "device-gadget-active")

        if state.profile == "performance" and state.udc_state in {"configured", "suspended"}:
            return decide(True, "device-configured-performance")

        return decide(False, "device-idle-power-off")

    if state.role == "host":
        if any(d.has_mounted_block for d in state.host_devices):
            return decide(True, "host-mounted-storage")

        if any(d.is_functional and d.wakeup_enabled for d in state.host_devices):
            if state.profile == "power-saver":
                return decide(False, "host-wakeup-power-saver")
            return decide(True, "host-device-wakeup")

        if state.profile == "performance" and any(d.is_functional for d in state.host_devices):
            return decide(True, "host-functional-performance")

        return decide(False, "host-idle-power-off")

    return decide(False, f"unknown-role-{state.role}")


def wakeup_paths(sysfs_root: Path = DEFAULT_SYSFS_ROOT) -> list[Path]:
    paths: list[Path] = []
    for rel in (DWC3_CORE_REL, DWC3_GLUE_REL):
        wakeup = sysfs_root / rel / "power/wakeup"
        if wakeup.exists():
            paths.append(wakeup)
    core_dir = sysfs_root / DWC3_CORE_REL
    if core_dir.exists():
        for child in sorted(core_dir.glob("xhci-hcd.*"), key=lambda p: p.name):
            wakeup = child / "power/wakeup"
            if wakeup.exists():
                paths.append(wakeup)
    return paths


def _write_wakeup(path: Path, value: str) -> bool:
    try:
        path.write_text(f"{value}\n", encoding="utf-8")
        return True
    except OSError as error:
        print(f"warning: failed to write {path}: {error}", file=sys.stderr, flush=True)
        return False


def prepare_sleep(
    *,
    sysfs_root: Path = DEFAULT_SYSFS_ROOT,
    proc_root: Path = DEFAULT_PROC_ROOT,
    dev_root: Path = DEFAULT_DEV_ROOT,
    policy_file: Path = DEFAULT_POLICY_FILE,
    state_file: Path = DEFAULT_STATE_FILE,
    profile_override: str | None = None,
) -> PolicyDecision:
    state = collect_state(
        sysfs_root=sysfs_root,
        proc_root=proc_root,
        dev_root=dev_root,
        policy_file=policy_file,
        profile_override=profile_override,
    )
    decision = evaluate_policy(state)
    targets = wakeup_paths(sysfs_root)

    saved: dict[str, str] = {}
    if not state_file.exists():
        for path in targets:
            current = _read_text(path)
            if current in {"enabled", "disabled"}:
                saved[str(path)] = current
        state_file.parent.mkdir(parents=True, exist_ok=True)
        state_file.write_text(json.dumps(saved, sort_keys=True), encoding="utf-8")

    desired = "enabled" if decision.retain else "disabled"
    for path in targets:
        _write_wakeup(path, desired)

    return decision


def restore_sleep(state_file: Path = DEFAULT_STATE_FILE) -> bool:
    if not state_file.exists():
        return False
    try:
        saved = json.loads(state_file.read_text(encoding="utf-8"))
    finally:
        state_file.unlink(missing_ok=True)

    if isinstance(saved, dict):
        for raw_path, value in saved.items():
            if value not in {"enabled", "disabled"}:
                continue
            path = Path(raw_path)
            if path.exists():
                _write_wakeup(path, value)
    return True


def _format_status(state: UsbSceneState, decision: PolicyDecision, targets: Iterable[Path]) -> str:
    target_summary = ", ".join(
        f"{p.parent.parent.name}={_read_text(p) or 'missing'}" for p in targets
    ) or "none"
    host_summary = ", ".join(
        f"{d.name}(cls={d.device_class or '?'},intf={','.join(d.interface_classes) or '-'},"
        f"wakeup={'on' if d.wakeup_enabled else 'off'},mounted={'yes' if d.has_mounted_block else 'no'})"
        for d in state.host_devices
    ) or "none"
    payload = {
        "policy": asdict(decision),
        "scene": {
            "usb_online": state.usb_online,
            "usb_type": state.usb_type,
            "typec_power_operation_mode": state.typec_op_mode,
            "sdp_charging_dependent": state.sdp_charging_dependent,
            "usb0_active": state.usb0_active,
            "ttygs0_open": state.ttygs0_open,
            "host_devices": host_summary,
        },
        "wakeup_targets": target_summary,
    }
    return json.dumps(payload, indent=2, ensure_ascii=False)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Manage USB s2idle retention policy")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("status", help="Show current scene, wakeup state, and s2idle decision")
    sub.add_parser("evaluate", help="Print the computed s2idle decision")

    set_parser = sub.add_parser("set", help="Persist USB sleep policy override mode")
    set_parser.add_argument("mode", choices=VALID_MODES)

    sub.add_parser("prepare", help="Apply USB s2idle wakeup policy before suspend")
    sub.add_parser("restore", help="Restore pre-suspend USB wakeup state")

    args = parser.parse_args(argv)
    try:
        if args.command == "status":
            state = collect_state()
            decision = evaluate_policy(state)
            print(_format_status(state, decision, wakeup_paths()))
            return 0
        if args.command == "evaluate":
            decision = evaluate_policy(collect_state())
            print(json.dumps(asdict(decision), ensure_ascii=False))
            return 0
        if args.command == "set":
            write_policy_mode(args.mode)
            print(f"USB s2idle policy mode set to {args.mode}")
            return 0
        if args.command == "prepare":
            decision = prepare_sleep()
            action = "retain" if decision.retain else "power-off"
            print(
                f"USB s2idle policy: {action} "
                f"(reason={decision.reason}, mode={decision.mode}, "
                f"profile={decision.profile}, role={decision.role}, udc={decision.udc_state})",
                flush=True,
            )
            return 0
        if args.command == "restore":
            restore_sleep()
            return 0
    except OSError as error:
        print(f"alphaplus-usb-sleep-policy {args.command}: {error}", file=sys.stderr)
        return 1
    return 2


if __name__ == "__main__":
    sys.exit(main())
