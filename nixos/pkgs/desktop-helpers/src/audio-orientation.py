#!/usr/bin/python3
"""Route speaker channels before the DSP while preserving physical codec slots."""

import codecs
import json
import logging
import os
import signal
import subprocess

from display_orientation import DisplayOrientation, TRANSFORMS

logger = logging.getLogger("alphaplus-audio-orientation")
SPEAKERS = "alsa_output.platform-sound.speakers"
CONTROLS = ("top:Gain 1", "top:Gain 2", "bottom:Gain 1", "bottom:Gain 2")


def channel_matrix(transform):
    if transform not in TRANSFORMS:
        raise ValueError(f"Unsupported audio transform: {transform}")
    if transform == "Rotated270":
        return (1.0, 0.0, 0.0, 1.0)
    if transform == "Rotated90":
        return (0.0, 1.0, 1.0, 0.0)
    return (0.5, 0.5, 0.5, 0.5)


class AudioOrientationManager:
    def __init__(self):
        self.transform = "Normal"
        self.nodes = {}
        self.applied = {}

    def update(self, transform):
        channel_matrix(transform)
        self.transform = transform
        for node_id, ready in self.nodes.items():
            if ready:
                self._apply(node_id)

    def pipewire_update(self, objects):
        for obj in objects:
            node_id = obj["id"]
            info = obj.get("info")
            if info is None:
                self.nodes.pop(node_id, None)
                self.applied.pop(node_id, None)
                continue
            if obj.get("type") != "PipeWire:Interface:Node":
                continue
            props = info.get("props", {})
            if props.get("node.name") == SPEAKERS:
                self.nodes.setdefault(node_id, False)
            if node_id not in self.nodes:
                continue
            params = info.get("params", {}).get("Props")
            if params is not None:
                names = set()
                for param in params:
                    names.update(param.get("params", [])[::2])
                self.nodes[node_id] = all(name in names for name in CONTROLS)
                if not self.nodes[node_id]:
                    self.applied.pop(node_id, None)
            if self.nodes[node_id]:
                self._apply(node_id)

    def _apply(self, node_id):
        if self.applied.get(node_id) == self.transform:
            return
        values = channel_matrix(self.transform)
        params = [value for pair in zip(CONTROLS, values) for value in pair]
        subprocess.run(
            ["pw-cli", "set-param", str(node_id), "Props", json.dumps({"params": params})],
            check=True, stdout=subprocess.DEVNULL, timeout=5,
        )
        self.applied[node_id] = self.transform
        logger.info("Speaker matrix: %s, node %d, gains %s", self.transform, node_id, values)


class PipeWireMonitor:
    def __init__(self, callback, loop):
        from gi.repository import GLib

        self.callback = callback
        self.loop = loop
        self.error = None
        self.buffer = ""
        self.decoder = codecs.getincrementaldecoder("utf-8")()
        self.json_decoder = json.JSONDecoder()
        self.process = subprocess.Popen(["pw-dump", "--monitor", "--no-colors"], stdout=subprocess.PIPE)
        os.set_blocking(self.process.stdout.fileno(), False)
        self.source = GLib.io_add_watch(
            self.process.stdout.fileno(), GLib.IO_IN | GLib.IO_HUP | GLib.IO_ERR, self._read,
        )

    def _read(self, fd, condition):
        try:
            data = os.read(fd, 65536)
            if not data:
                raise RuntimeError("PipeWire monitor disconnected")
            self.feed(self.decoder.decode(data))
            return True
        except Exception as error:
            self.error = error
            self.source = None
            self.loop.quit()
            return False

    def feed(self, data):
        self.buffer += data
        while True:
            self.buffer = self.buffer.lstrip()
            if not self.buffer:
                return
            try:
                objects, end = self.json_decoder.raw_decode(self.buffer)
            except json.JSONDecodeError:
                return
            self.buffer = self.buffer[end:]
            self.callback(objects)

    def close(self):
        from gi.repository import GLib

        if self.source is not None:
            GLib.source_remove(self.source)
            self.source = None
        if self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=5)
        else:
            self.process.wait(timeout=5)
        self.process.stdout.close()


def main():
    from gi.repository import GLib, GLibUnix

    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    loop = GLib.MainLoop()
    manager = AudioOrientationManager()
    orientation = DisplayOrientation()
    monitor = None
    for signum in (signal.SIGTERM, signal.SIGINT):
        GLibUnix.signal_add(GLib.PRIORITY_DEFAULT, signum, lambda: (loop.quit(), False)[1])
    try:
        orientation.start(manager.update, loop)
        monitor = PipeWireMonitor(manager.pipewire_update, loop)
        loop.run()
        if orientation.error:
            raise orientation.error
        if monitor.error:
            raise monitor.error
    finally:
        if monitor is not None:
            monitor.close()
        orientation.close()


if __name__ == "__main__":
    main()
