"""Observe screen lock state as KWin registers and releases its D-Bus service."""


SERVICE = "org.freedesktop.ScreenSaver"
OBJECT_PATH = "/ScreenSaver"


class ScreenLockMonitor:
    def __init__(self, bus, callback, on_error):
        self.bus = bus
        self.callback = callback
        self.on_error = on_error
        self.owner = ""
        self.signals = [
            bus.add_signal_receiver(self._about_to_lock, signal_name="AboutToLock",
                                    dbus_interface="org.kde.screensaver", bus_name=SERVICE,
                                    path=OBJECT_PATH, sender_keyword="sender"),
            bus.add_signal_receiver(self._active_changed, signal_name="ActiveChanged",
                                    dbus_interface=SERVICE, bus_name=SERVICE,
                                    path=OBJECT_PATH, sender_keyword="sender"),
        ]
        self.watch = bus.watch_name_owner(SERVICE, self._owner_changed)

    def _emit(self, value):
        try:
            self.callback(value)
        except Exception as error:
            self.on_error(error)

    def _owner_changed(self, owner):
        self.owner = str(owner)
        if not owner:
            self._emit(None)
            return
        try:
            screen = self.bus.get_object(owner, OBJECT_PATH, introspect=False)
            active = bool(screen.GetActive(dbus_interface=SERVICE))
        except Exception as error:
            self.on_error(error)
            return
        self._emit(active)

    def _about_to_lock(self, sender):
        if sender == self.owner:
            self._emit(True)

    def _active_changed(self, active, sender):
        if sender == self.owner:
            self._emit(bool(active))

    def close(self):
        self.watch.cancel()
        for signal in self.signals:
            signal.remove()
