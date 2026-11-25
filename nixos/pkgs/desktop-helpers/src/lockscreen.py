#!/usr/bin/python3
"""Use portrait orientation and Plasma Keyboard while the Plasma session is locked."""

import json
import logging
import os
from pathlib import Path
import signal
import subprocess
import sys

from output_configuration import (
    AUTO_ROTATE_POLICIES,
    OutputConfiguration,
    OutputUnavailableError,
    TRANSFORMS,
)
from screen_lock import ScreenLockMonitor


LOCK_KEYBOARD = os.environ.get(
    "LOCK_KEYBOARD",
    "/run/current-system/sw/share/applications/org.kde.plasma.keyboard.desktop"
    if os.path.exists("/run/current-system/sw/share/applications/org.kde.plasma.keyboard.desktop")
    else "/usr/share/applications/org.kde.plasma.keyboard.desktop"
)


class LockPolicy:
    def __init__(self, settings, state_path):
        self.settings = settings
        self.state_path = state_path
        self.original = json.loads(state_path.read_text()) if state_path.exists() else None
        self.locked = False

    def update(self, locked, allow_output_unavailable=False):
        if locked:
            if self.locked:
                return
            if self.original is None:
                original = self.settings.snapshot()
                temporary = self.state_path.with_suffix(".tmp")
                temporary.write_text(json.dumps(original) + "\n")
                temporary.replace(self.state_path)
                self.original = original
            self.settings.input_method(LOCK_KEYBOARD)
            self.settings.output("Never", "Normal")
            self.locked = True
            logging.info("Applied portrait orientation and Plasma Keyboard for the lock screen")
        elif self.original is not None:
            self.settings.input_method(self.original["input_method"])
            try:
                self.settings.output(self.original["auto_rotation"], self.original["transform"])
            except OutputUnavailableError:
                if not allow_output_unavailable:
                    raise
                self.locked = False
                logging.warning(
                    "Skipping output restore during shutdown because display output is unavailable; "
                    "keeping saved state for next session"
                )
                return
            self.state_path.unlink()
            self.original = None
            self.locked = False
            logging.info("Restored desktop orientation policy and input method")


class Settings:
    def __init__(self, runtime):
        self.runtime = runtime

    def snapshot(self):
        config_home = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
        config_path = config_home / "kwinoutputconfig.json"
        auto_rotation, transform = "Never", "Rotated270"
        if config_path.exists():
            try:
                config = json.loads(config_path.read_text())
                outputs = next(item["data"] for item in config if item["name"] == "outputs")
                panel = next(output for output in outputs if output["connectorName"] == "DSI-1")
                candidate_rotation = panel.get("autoRotation")
                candidate_transform = panel.get("transform")
                if candidate_rotation in AUTO_ROTATE_POLICIES:
                    auto_rotation = candidate_rotation
                if candidate_transform in TRANSFORMS:
                    transform = candidate_transform
            except (OSError, UnicodeDecodeError, json.JSONDecodeError, KeyError, StopIteration, TypeError):
                logging.warning("Falling back to default DSI-1 output config for lockscreen snapshot")
        input_method = subprocess.check_output(
            ["kreadconfig6", "--file", "kwinrc", "--group", "Wayland", "--key", "InputMethod"],
            text=True).strip()
        return {"auto_rotation": auto_rotation, "transform": transform,
                "input_method": input_method}

    def output(self, policy, transform):
        output = OutputConfiguration(self.runtime)
        try:
            output.apply(policy, transform)
        finally:
            output.close()

    def input_method(self, path):
        command = ["kwriteconfig6", "--file", "kwinrc", "--group", "Wayland",
                   "--key", "InputMethod", "--notify"]
        command.extend([path] if path else ["--delete"])
        subprocess.run(command, check=True)


def main():
    if sys.argv[1:] not in ([], ["--restore"]):
        raise ValueError("Unexpected arguments")

    import dbus
    from dbus.mainloop.glib import DBusGMainLoop
    from gi.repository import GLib, GLibUnix

    runtime = Path(os.environ.get("RUNTIME_DIRECTORY") or (Path(os.environ["XDG_RUNTIME_DIR"]) / "alphaplus-lockscreen"))
    state = Path(os.environ["STATE_DIRECTORY"])
    policy = LockPolicy(Settings(runtime), state / "desktop.json")
    if sys.argv[1:] == ["--restore"]:
        policy.update(False, allow_output_unavailable=True)
        return 0
    DBusGMainLoop(set_as_default=True)
    bus = dbus.SessionBus()
    loop = GLib.MainLoop()
    failed = False

    def update(locked):
        if locked is None:
            policy.locked = False
        else:
            policy.update(bool(locked))

    def monitoring_failed(error):
        nonlocal failed
        logging.error("Lock screen policy failed", exc_info=(type(error), error, error.__traceback__))
        failed = True
        loop.quit()

    monitor = ScreenLockMonitor(bus, update, monitoring_failed)
    for signum in (signal.SIGTERM, signal.SIGINT):
        GLibUnix.signal_add(GLib.PRIORITY_DEFAULT, signum, lambda: (loop.quit(), False)[1])
    try:
        loop.run()
    finally:
        monitor.close()
        policy.update(False, allow_output_unavailable=True)
    return int(failed)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    sys.exit(main())
