"""Apply the panel's manual transform and auto-rotation policy through Wayland."""

import os
from pathlib import Path
import select
import sys
import time

from display_orientation import destroy_proxy


TRANSFORMS = {
    "Normal": 0, "Rotated90": 1, "Rotated180": 2, "Rotated270": 3,
    "Flipped": 4, "Flipped90": 5, "Flipped180": 6, "Flipped270": 7,
}
AUTO_ROTATE_POLICIES = {"Never": 0, "InTabletMode": 1, "Always": 2}


class OutputUnavailableError(RuntimeError):
    """Raised when the compositor or target output cannot be reached."""


class OutputConfiguration:
    def __init__(self, runtime, connector="DSI-1"):
        from pywayland.client import Display
        from pywayland.scanner import Protocol

        directory = Path(runtime) / "alphaplus_output_protocols"
        if not (directory / "kde_output_management_v2").exists():
            directory.mkdir(mode=0o700, exist_ok=True)
            search_dirs = [
                os.environ.get("PLASMA_WAYLAND_PROTOCOLS_DIR", ""),
                "/run/current-system/sw/share/plasma-wayland-protocols",
                "/usr/share/plasma-wayland-protocols",
            ]
            proto_dir = next((d for d in search_dirs if d and os.path.isdir(d)), "/usr/share/plasma-wayland-protocols")
            protocols = [Protocol.parse_file(f"{proto_dir}/{name}.xml")
                         for name in ("kde-output-device-v2", "kde-output-management-v2")]
            imports = {interface.name: protocol.name for protocol in protocols
                       for interface in protocol.interface}
            for protocol in protocols:
                protocol.output(str(directory), imports)
        if str(runtime) not in sys.path:
            sys.path.insert(0, str(runtime))
        from alphaplus_output_protocols.kde_output_device_v2 import KdeOutputDeviceRegistryV2
        from alphaplus_output_protocols.kde_output_management_v2 import KdeOutputManagementV2

        self.connector = connector
        self.output_registry_interface = KdeOutputDeviceRegistryV2
        self.management_interface = KdeOutputManagementV2
        self.registry = None
        self.output_registry = None
        self.management = None
        self.outputs = {}
        self.output = None
        self.display = Display()
        try:
            self.display.connect()
        except (ValueError, RuntimeError) as error:
            self.close()
            raise OutputUnavailableError(f"Wayland display is unavailable: {error}") from error
        try:
            self.registry = self.display.get_registry()
            self.registry.dispatcher["global"] = self._global
            self.display.roundtrip()
            self.display.roundtrip()
            if self.management is None:
                raise OutputUnavailableError("KDE output management version 5 is required")
            self.output = next((output for output, name in self.outputs.items() if name == connector), None)
            if self.output is None:
                raise OutputUnavailableError(f"{connector} output is unavailable")
        except Exception:
            self.close()
            raise

    def _global(self, registry, name, interface, version):
        if interface == self.management_interface.name and version >= 5:
            self.management = registry.bind(name, self.management_interface, 5)
        elif interface == self.output_registry_interface.name and version >= 21:
            self.output_registry = registry.bind(name, self.output_registry_interface, 21)
            self.output_registry.dispatcher["output"] = self._output

    def _output(self, registry, output):
        self.outputs[output] = None
        output.dispatcher["name"] = lambda _, value: self.outputs.__setitem__(output, value)

    def apply(self, policy, transform):
        result = None

        def finished(success):
            nonlocal result
            result = success

        configuration = self.management.create_configuration()
        configuration.dispatcher["applied"] = lambda _: finished(True)
        configuration.dispatcher["failed"] = lambda _: finished(False)
        try:
            # Always send the manual transform, even when the visible transform matches.
            configuration.transform(self.output, TRANSFORMS[transform])
            configuration.set_auto_rotate_policy(self.output, AUTO_ROTATE_POLICIES[policy])
            configuration.apply()
            self.display.flush()
            deadline = time.monotonic() + 10
            while result is None:
                remaining = deadline - time.monotonic()
                if remaining <= 0 or not select.select([self.display.get_fd()], [], [], remaining)[0]:
                    raise TimeoutError("KWin output configuration timed out")
                self.display.read()
                self.display.dispatch()
            if not result:
                raise RuntimeError("KWin rejected the panel configuration")
        finally:
            ptr = getattr(configuration, "_ptr", None)
            try:
                configuration.destroy()
            finally:
                if ptr is not None:
                    configuration.interface.registry.pop(ptr, None)
            if self.display is not None:
                self.display.flush()

    def close(self):
        for output in list(self.outputs):
            destroy_proxy(output)
        self.outputs.clear()
        self.output = None
        destroy_proxy(self.output_registry)
        self.output_registry = None
        destroy_proxy(self.management)
        self.management = None
        destroy_proxy(self.registry)
        self.registry = None
        if self.display is not None:
            self.display.disconnect()
            self.display = None
