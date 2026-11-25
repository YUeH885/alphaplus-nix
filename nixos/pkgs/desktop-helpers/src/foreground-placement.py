#!/usr/bin/python3
"""Place the active Plasma application and desktop rendering on faster CPUs."""

import json
import logging
import os
from pathlib import Path
import signal
import sys

from screen_lock import ScreenLockMonitor


RENDER_UNITS = {"plasma-kwin_wayland.service", "plasma-plasmashell.service"}
BUS_NAME = "org.alphaplus.ForegroundPlacement"
OBJECT_PATH = "/org/alphaplus/ForegroundPlacement"
SCRIPT_NAME = "alphaplus-foreground-placement"
SCRIPT_PATH = os.environ.get("ALPHAPLUS_PLACEMENT_JS", "/usr/share/alphaplus/foreground-placement.js")


class UnitGone(Exception):
    pass


def cpu_mask(cpus):
    value = sum(1 << cpu for cpu in cpus)
    return value.to_bytes((value.bit_length() + 7) // 8, "little")


def faster_cpus(capacities):
    minimum = min(capacities.values())
    return {cpu for cpu, capacity in capacities.items() if capacity > minimum}


class Placement:
    def __init__(self, manager, cpus, originals, save):
        self.manager = manager
        self.cpus = cpus
        self.originals = originals
        self.save = save

    def restore(self, unit):
        try:
            self.manager.set_cpus(unit, self.originals[unit])
        except UnitGone:
            pass
        del self.originals[unit]
        self.save()
        logging.info("Restored %s", unit)

    def update(self, foreground, enabled):
        desired = set()
        if enabled:
            desired = RENDER_UNITS | ({foreground} if foreground else set())
        for unit in set(self.originals) - desired:
            self.restore(unit)
        for unit in desired - self.originals.keys():
            try:
                original = self.manager.get_cpus(unit)
                allowed = int.from_bytes(original, "little")
                cpus = {cpu for cpu in self.cpus if not original or allowed & (1 << cpu)}
                if not cpus:
                    continue
                # Persist before changing systemd: ExecStopPost also runs after a crash.
                self.originals[unit] = original
                self.save()
                self.manager.set_cpus(unit, cpu_mask(cpus))
                logging.info("Placed %s on CPUs %s", unit, sorted(cpus))
            except UnitGone:
                if unit in self.originals:
                    del self.originals[unit]
                    self.save()


def main():
    if sys.argv[1:] not in ([], ["--restore"]):
        raise ValueError("Unexpected arguments")

    state = Path(os.environ["RUNTIME_DIRECTORY"]) / "original-cpus.json"
    originals = {name: bytes.fromhex(mask) for name, mask in json.loads(
        state.read_text() if state.exists() else "{}").items()}

    def save():
        temporary = state.with_suffix(".tmp")
        temporary.write_text(json.dumps({name: mask.hex() for name, mask in originals.items()}))
        temporary.replace(state)

    if sys.argv[1:] == ["--restore"] and not originals:
        return 0

    import dbus
    import dbus.service
    from dbus.mainloop.glib import DBusGMainLoop
    from gi.repository import GLib, GLibUnix

    manager_name = "org.freedesktop.systemd1"
    gone_errors = {manager_name + ".NoSuchUnit", manager_name + ".NoUnitForPID",
                   "org.freedesktop.DBus.Error.UnknownObject"}

    def make_adapter(bus):
        # Follow systemd --user unique name changes across nixos-rebuild daemon-reexec.
        manager = dbus.Interface(
            bus.get_object(manager_name, "/org/freedesktop/systemd1",
                           introspect=False, follow_name_owner_changes=True),
            manager_name + ".Manager",
        )
        unrestricted = cpu_mask(os.sched_getaffinity(0))

        class Manager:
            def unit(self, name):
                return dbus.Interface(
                    bus.get_object(manager_name, manager.GetUnit(name, signature="s"),
                                   introspect=False, follow_name_owner_changes=True),
                    "org.freedesktop.DBus.Properties",
                )

            def get_cpus(self, name):
                try:
                    kind = "Scope" if name.endswith(".scope") else "Service"
                    return bytes(self.unit(name).Get(manager_name + "." + kind, "AllowedCPUs",
                                                     signature="ss"))
                except dbus.DBusException as error:
                    if error.get_dbus_name() in gone_errors:
                        raise UnitGone(name) from error
                    raise

            def set_cpus(self, name, mask):
                try:
                    # Clearing the last cpuset property can disable systemd's controller
                    # writes while the old kernel mask remains. Expand it before clearing.
                    if not mask:
                        manager.SetUnitProperties(
                            name, True,
                            [("AllowedCPUs", dbus.ByteArray(unrestricted, variant_level=1))],
                            signature="sba(sv)",
                        )
                    manager.SetUnitProperties(
                        name, True,
                        [("AllowedCPUs", dbus.ByteArray(mask, variant_level=1))],
                        signature="sba(sv)",
                    )
                except dbus.DBusException as error:
                    if error.get_dbus_name() in gone_errors:
                        raise UnitGone(name) from error
                    raise

            def foreground(self, pid):
                if pid <= 0:
                    return None
                try:
                    unit_path = manager.GetUnitByPID(dbus.UInt32(pid), signature="u")
                    properties = dbus.Interface(
                        bus.get_object(manager_name, unit_path,
                                       introspect=False, follow_name_owner_changes=True),
                        "org.freedesktop.DBus.Properties",
                    )
                    name = str(properties.Get(manager_name + ".Unit", "Id", signature="ss"))
                    # Plasma launches application trees in app-*.service or app-*.scope,
                    # and D-Bus activated GUI apps in dbus-*.service under app*.slice.
                    if name.startswith("app-") and name.endswith((".service", ".scope")):
                        return name
                    if name.startswith("dbus-") and name.endswith(".service"):
                        slice_name = str(properties.Get(manager_name + ".Unit", "Slice", signature="ss"))
                        if slice_name == "app.slice" or slice_name.startswith("app-"):
                            return name
                    return None
                except dbus.DBusException as error:
                    if error.get_dbus_name() in gone_errors:
                        return None
                    raise

        return Manager()

    DBusGMainLoop(set_as_default=True)
    if sys.argv[1:] == ["--restore"]:
        try:
            bus = dbus.SessionBus()
            Placement(make_adapter(bus), set(), originals, save).update(None, False)
        except dbus.DBusException as error:
            logging.warning("Skipping CPU affinity restore because session D-Bus is unavailable: %s", error)
        return 0

    bus = dbus.SessionBus()
    adapter = make_adapter(bus)
    capacities = {cpu: int(Path(f"/sys/devices/system/cpu/cpu{cpu}/cpu_capacity").read_text())
                  for cpu in os.sched_getaffinity(0)}
    placement = Placement(adapter, faster_cpus(capacities), originals, save)
    placement.update(None, False)
    loop = GLib.MainLoop()
    failed = False

    def guarded(function):
        def callback(*args, **kwargs):
            nonlocal failed
            try:
                return function(*args, **kwargs)
            except Exception:
                logging.exception("Foreground placement failed")
                failed = True
                loop.quit()
        return callback

    def monitoring_failed(error):
        nonlocal failed
        logging.error("Screen lock monitoring failed",
                      exc_info=(type(error), error, error.__traceback__))
        failed = True
        loop.quit()

    class Service(dbus.service.Object):
        def __init__(self):
            self.name = dbus.service.BusName(BUS_NAME, bus, do_not_queue=True)
            super().__init__(self.name, OBJECT_PATH)
            self.kwin_owner = ""
            self.locked = True
            self.foreground = None
            self.script = None

        def update(self):
            placement.update(self.foreground, bool(self.kwin_owner) and not self.locked)

        @dbus.service.method(BUS_NAME, in_signature="i", out_signature="", sender_keyword="sender")
        def SetForeground(self, pid, sender=None):
            if sender != self.kwin_owner:
                raise dbus.DBusException("Foreground events must come from KWin",
                                         name="org.freedesktop.DBus.Error.AccessDenied")
            self.set_foreground(pid)

        @guarded
        def set_foreground(self, pid):
            self.foreground = adapter.foreground(pid)
            self.update()

        @guarded
        def lock_changed(self, locked):
            self.locked = locked is None or bool(locked)
            self.update()

        @guarded
        def attach_kwin(self, owner):
            self.kwin_owner = str(owner)
            self.foreground = None
            self.script = None
            placement.update(None, False)
            if not self.kwin_owner:
                return
            scripting = dbus.Interface(
                bus.get_object(self.kwin_owner, "/Scripting", introspect=False),
                "org.kde.kwin.Scripting",
            )
            if scripting.isScriptLoaded(SCRIPT_NAME):
                scripting.unloadScript(SCRIPT_NAME)
            script_id = int(scripting.loadScript(SCRIPT_PATH, SCRIPT_NAME, signature="ss"))
            if script_id < 0:
                raise RuntimeError("KWin could not load the foreground placement script")
            self.script = scripting
            script = dbus.Interface(
                bus.get_object(self.kwin_owner, f"/Scripting/Script{script_id}", introspect=False),
                "org.kde.kwin.Script",
            )
            script.run()

    service = Service()
    monitor = ScreenLockMonitor(bus, service.lock_changed, monitoring_failed)
    kwin_watch = bus.watch_name_owner("org.kde.KWin", service.attach_kwin)
    for signum in (signal.SIGTERM, signal.SIGINT):
        GLibUnix.signal_add(GLib.PRIORITY_DEFAULT, signum, lambda: (loop.quit(), False)[1])
    try:
        loop.run()
    finally:
        kwin_watch.cancel()
        monitor.close()
        try:
            placement.update(None, False)
        except dbus.DBusException as error:
            logging.warning("Failed to restore CPU affinity on exit: %s", error)
        if service.script and bus.name_has_owner("org.kde.KWin"):
            try:
                service.script.unloadScript(SCRIPT_NAME)
            except dbus.DBusException:
                pass
    return int(failed)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    sys.exit(main())
