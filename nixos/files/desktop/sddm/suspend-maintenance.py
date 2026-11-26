#!/usr/bin/python3
"""Continue a systemd suspend request after charger maintenance wakeups."""

from contextlib import ExitStack
from dataclasses import dataclass
import errno
import os
from pathlib import Path
import struct
import subprocess
import sys


MAINTENANCE_ACTIONS = frozenset({
    "wdog-bark", "bat-temp", "batt-temp-delta", "temp-change",
    "usbin-icl-change", "chg-state-change", "temperature-change",
})
EXTERNAL_ACTIONS = frozenset({
    "pm8xxx_rtc_alarm", "dwc3", "dp_hs_phy_irq", "dm_hs_phy_irq", "ss_phy_irq",
})
INPUT_EVENT = struct.Struct("llHHi")
EV_KEY, EV_REL, EV_ABS, EV_SW = 1, 2, 3, 5
ABS_MT_TRACKING_ID = 0x39
USB_SLEEP_POLICY = "@usb_sleep_policy@"


@dataclass(frozen=True)
class Irq:
    count: int
    actions: frozenset[str]
    chip: str
    wakeup: bool


def read_irqs():
    lines = Path("/proc/interrupts").read_text().splitlines()
    cpu_count = len(lines[0].split())
    result = {}
    for line in lines[1:]:
        number, _, rest = line.partition(":")
        if not number.strip().isdecimal():
            continue
        number = int(number)
        path = Path("/sys/kernel/irq") / str(number)
        try:
            actions = frozenset(filter(None, path.joinpath("actions").read_text().strip().split(",")))
            chip = path.joinpath("chip_name").read_text().strip()
            wakeup = path.joinpath("wakeup").read_text().strip() == "enabled"
        except FileNotFoundError:
            # An IRQ removed during hot-unplug cannot authorize another sleep.
            continue
        result[number] = Irq(sum(map(int, rest.split()[:cpu_count])), actions, chip, wakeup)
    return result


def read_wakeup_irq():
    try:
        return int(Path("/sys/power/pm_wakeup_irq").read_text())
    except OSError as error:
        if error.errno == errno.ENODATA:
            return None
        raise


def maintenance_wakeup(before, after, wakeup_irq, summary_action):
    changed = {
        number: irq for number, irq in after.items()
        if number not in before or irq.count != before[number].count
    }
    children = [irq for irq in changed.values() if irq.chip == "qcom-i2c-pmic"]

    def maintenance(irq):
        if not irq.actions:
            return False
        if irq.actions <= MAINTENANCE_ACTIONS:
            return True
        # The GPIO summary wakes the system before nested IRQs are dispatched.
        return (irq.actions == {summary_action} and bool(children)
                and all(child.actions and child.actions <= MAINTENANCE_ACTIONS
                        for child in children))

    source = changed.get(wakeup_irq)
    if source is None or not maintenance(source):
        return False

    # pm_wakeup_irq records only the first IRQ; preserve concurrent RTC/USB wakes.
    for number, irq in changed.items():
        if irq.actions & EXTERNAL_ACTIONS:
            return False
        was_wakeup = number in before and before[number].wakeup
        if (irq.wakeup or was_wakeup) and not maintenance(irq):
            return False
    return True


def interactive_event(event_type, code, value):
    return ((event_type == EV_KEY and value > 0)
            or (event_type == EV_REL and value != 0)
            or event_type == EV_SW
            or (event_type == EV_ABS and code == ABS_MT_TRACKING_ID and value >= 0))


class InputMonitor:
    def __init__(self):
        self.stack = ExitStack()
        self.fds = []

    def __enter__(self):
        try:
            for path in sorted(Path("/dev/input").glob("event*")):
                fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK | os.O_CLOEXEC)
                self.stack.callback(os.close, fd)
                self.fds.append(fd)
        except BaseException:
            self.stack.close()
            raise
        return self

    def __exit__(self, *args):
        self.stack.close()

    def active(self):
        active = False
        for fd in tuple(self.fds):
            while True:
                try:
                    data = os.read(fd, INPUT_EVENT.size * 64)
                except BlockingIOError:
                    break
                except OSError as error:
                    if error.errno != errno.ENODEV:
                        raise
                    data = b""
                if not data:
                    # AVRCP teardown during suspend is not user input.
                    # ExitStack still owns the descriptor until this monitor exits.
                    self.fds.remove(fd)
                    break
                for _, _, event_type, code, value in INPUT_EVENT.iter_unpack(data):
                    active |= interactive_event(event_type, code, value)
        return active


def apply_usb_sleep_policy(action):
    result = subprocess.run([USB_SLEEP_POLICY, action], check=False)
    if result.returncode != 0:
        print(f"USB sleep policy {action} exited with {result.returncode}", file=sys.stderr, flush=True)


def main():
    summary_action = Path("/sys/class/power_supply/parallel/device").resolve(strict=True).parent.name
    apply_usb_sleep_policy("prepare")
    try:
        with InputMonitor() as inputs:
            before = read_irqs()
            while True:
                subprocess.run(["/usr/lib/systemd/systemd-sleep", "suspend"], check=True)
                wakeup_irq = read_wakeup_irq()
                after = read_irqs()
                if inputs.active() or not maintenance_wakeup(before, after, wakeup_irq, summary_action):
                    print(f"Suspend completed on wakeup IRQ {wakeup_irq}", flush=True)
                    return 0

                apply_usb_sleep_policy("prepare")
                # Wait for resumed drivers' wakeup sources before arming the next sleep.
                count = Path("/sys/power/wakeup_count").read_text()
                current = read_irqs()
                if inputs.active() or not maintenance_wakeup(before, current, wakeup_irq, summary_action):
                    return 0
                try:
                    Path("/sys/power/wakeup_count").write_text(count)
                except OSError as error:
                    if error.errno != errno.EINVAL:
                        raise
                    print("A new wakeup event arrived before suspend", flush=True)
                    return 0

                actions = ",".join(sorted(after[wakeup_irq].actions))
                print(f"Charger maintenance on IRQ {wakeup_irq} ({actions}); suspending again", flush=True)
                before = current
    finally:
        apply_usb_sleep_policy("restore")


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (OSError, ValueError, subprocess.CalledProcessError) as error:
        print(f"Suspend maintenance failed: {error}", file=sys.stderr)
        sys.exit(1)
