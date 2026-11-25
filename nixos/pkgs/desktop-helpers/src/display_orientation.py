"""Observe the current panel transform through Wayland wl_output events."""

from dataclasses import dataclass
import logging


TRANSFORMS = ("Normal", "Rotated90", "Rotated180", "Rotated270")


def destroy_proxy(proxy):
    """Release a pywayland proxy and remove strong references before display disconnect."""
    if proxy is None:
        return
    ptr = getattr(proxy, "_ptr", None)
    if ptr is not None:
        proxy._destroy()
        proxy.interface.registry.pop(ptr, None)


@dataclass
class OutputState:
    proxy: object
    name: str | None = None
    transform: int | None = None


class DisplayOrientation:
    def __init__(self, connector="DSI-1"):
        from pywayland.client import Display
        from pywayland.protocol.wayland import WlOutput

        self.connector = connector
        self.output_interface = WlOutput
        self.outputs = {}
        self.transform = None
        self.callback = None
        self.source_id = None
        self.error = None
        self.registry = None
        self.display = Display()
        self.display.connect()
        try:
            self.registry = self.display.get_registry()
            self.registry.dispatcher["global"] = self._global
            self.registry.dispatcher["global_remove"] = self._remove
            # The second barrier includes the outputs bound during the first one.
            self.display.roundtrip()
            self.display.roundtrip()
        except Exception:
            self.close()
            raise

    def _global(self, registry, name, interface, version):
        if interface != "wl_output":
            return
        if version < 4:
            self.error = RuntimeError("wl_output version 4 is required")
            return
        output = registry.bind(name, self.output_interface, 4)
        state = OutputState(output)
        self.outputs[name] = state
        output.dispatcher["name"] = lambda _, value: setattr(state, "name", value)
        output.dispatcher["geometry"] = lambda _, *values: setattr(state, "transform", values[-1])
        output.dispatcher["done"] = lambda _: self._commit(state)

    def _commit(self, state):
        if state.name != self.connector or state.transform is None:
            return
        if state.transform not in range(len(TRANSFORMS)):
            self.error = RuntimeError(f"Unsupported panel transform: {state.transform}")
            return
        transform = TRANSFORMS[state.transform]
        if transform != self.transform:
            self.transform = transform
            if self.callback:
                try:
                    self.callback(transform)
                except Exception as error:
                    self.error = error

    def _remove(self, registry, name):
        state = self.outputs.pop(name, None)
        if state:
            ptr = getattr(state.proxy, "_ptr", None)
            state.proxy.release()
            if ptr is not None:
                state.proxy.interface.registry.pop(ptr, None)
            if state.name == self.connector:
                self.transform = None

    def start(self, callback, loop):
        from gi.repository import GLib, GLibUnix

        if self.error:
            raise self.error
        self.callback = callback
        self.loop = loop
        self.source_id = GLibUnix.fd_add_full(
            GLib.PRIORITY_DEFAULT, self.display.get_fd(),
            GLib.IO_IN | GLib.IO_HUP | GLib.IO_ERR, self._readable)
        if self.transform:
            callback(self.transform)

    def _readable(self, source, condition):
        from gi.repository import GLib

        try:
            if condition & (GLib.IO_HUP | GLib.IO_ERR):
                raise RuntimeError("Wayland display disconnected")
            self.display.read()
            self.display.dispatch()
            if self.error:
                raise self.error
            if self.display.flush() < 0:
                raise RuntimeError("Could not flush Wayland requests")
        except Exception as error:
            self.error = error
            logging.exception("Display orientation observer failed")
            self.source_id = None
            self.loop.quit()
            return GLib.SOURCE_REMOVE
        return GLib.SOURCE_CONTINUE

    def close(self):
        if self.source_id is not None:
            from gi.repository import GLib

            GLib.source_remove(self.source_id)
            self.source_id = None
        for state in list(self.outputs.values()):
            destroy_proxy(state.proxy)
        self.outputs.clear()
        destroy_proxy(self.registry)
        self.registry = None
        if self.display is not None:
            self.display.disconnect()
            self.display = None
