#!/usr/bin/python3
"""Use the touchscreen as a touchpad handled by libinput with dynamic screen orientation.

The touchscreen is grabbed and its contacts are forwarded to a virtual
touchpad, so libinput and KWin provide pointer motion, tap-to-click,
tap-and-drag, scrolling and touchpad gestures, all configurable in the
system settings. The Assistant key controls the on-screen keyboard and
switches between touchpad mode and direct touch. Display orientation
follows the compositor through Wayland output events.
"""

import glob
import logging
import os
import signal
import sys
import time

from display_orientation import DisplayOrientation, TRANSFORMS
from screen_lock import ScreenLockMonitor

DOUBLE_PRESS_TIMEOUT_MS = 300
GRAB_DELAY = 0.15
TOUCHPAD_RESOLUTION = 45
BLANK_SUPPRESSION_TIMEOUT = 1.2
VIRTUAL_NAME = "LG G8 Touchpad"
TOUCHPAD_BUS_NAME = "org.alphaplus.Touchpad"
TOUCHPAD_OBJECT_PATH = "/org/alphaplus/Touchpad"
TOUCHPAD_SCRIPT_NAME = "alphaplus-touchpad-keyboard"
TOUCHPAD_SCRIPT_PATH = os.environ.get(
    "ALPHAPLUS_TOUCHPAD_KEYBOARD_JS", "/usr/share/alphaplus/touchpad-keyboard.js"
)
OSK_BUS_NAME = "fyi.fortime.Fcitx5Osk"
OSK_OBJECT_PATH = "/fyi/fortime/Fcitx5Osk/Controller"
OSK_INTERFACE = "fyi.fortime.Fcitx5Osk.Controller1"


def is_display_on():
    for path in sorted(glob.glob("/sys/class/drm/card*-DSI-1/dpms")):
        with open(path, "r") as f:
            return f.read().strip().lower() == "on"
    raise RuntimeError("DSI-1 DPMS attribute is unavailable")


def is_virtual_input(path):
    # uinput devices (Sunshine, our own touchpad) live under /sys/devices/virtual.
    sysfs = os.path.realpath(os.path.join("/sys/class/input", os.path.basename(path)))
    return sysfs.startswith("/sys/devices/virtual/")


def find_touchscreen(paths, InputDevice, ecodes):
    candidates = []
    for path in paths:
        if is_virtual_input(path):
            continue
        try:
            device = InputDevice(path)
        except OSError:
            continue
        capabilities = device.capabilities(absinfo=False)
        axes = set(capabilities.get(ecodes.EV_ABS, []))
        if {ecodes.ABS_MT_POSITION_X, ecodes.ABS_MT_POSITION_Y,
                ecodes.ABS_MT_SLOT, ecodes.ABS_MT_TRACKING_ID} <= axes:
            candidates.append(device)
        else:
            device.close()
    if not candidates:
        raise RuntimeError("No touchscreen found")
    selected = next((d for d in candidates if "touchscreen" in d.name.lower()), candidates[0])
    for candidate in candidates:
        if candidate is not selected:
            candidate.close()
    return selected


def find_key_device(paths, InputDevice, ecodes, name, code):
    for path in paths:
        try:
            device = InputDevice(path)
        except OSError:
            continue
        if device.name == name and code in device.capabilities(absinfo=False).get(ecodes.EV_KEY, []):
            return device
        device.close()
    return None


class Touchpad:
    def __init__(self, device, ecodes, UInput, AbsInfo, raw_x_max, raw_y_max, initial_transform="Normal"):
        self.device = device
        self.ecodes = ecodes
        self.UInput = UInput
        self.AbsInfo = AbsInfo
        self.raw_x_max = raw_x_max
        self.raw_y_max = raw_y_max
        self.current_transform = initial_transform or "Normal"
        self.virtual = self._create_virtual()
        self.grabbed = False
        self.enabled = True
        self.locked = False
        self.grab_when_released = False
        self.slot = 0
        self.contacts = {}
        self.cancelled = False
        self.changed = set()
        self.removed = set()
        self.ids = {}
        self.next_id = 1
        self.pressed = False
        self.tool = None
        self.suppressed_until = 0.0
        self.suppress_wait_release = False
        self.grabbed_for_suppression = False
        self.keyboard_shown = False
        self.restore_touchpad = False
        self.on_virtual_replaced = None
        self.on_keyboard_toggle = None

    def _create_virtual(self):
        is_landscape = self.current_transform in ("Rotated270", "Rotated90")
        width = self.raw_y_max if is_landscape else self.raw_x_max
        height = self.raw_x_max if is_landscape else self.raw_y_max
        return self.UInput(
            {
                self.ecodes.EV_ABS: [
                    (self.ecodes.ABS_X, self.AbsInfo(0, 0, width, 0, 0, TOUCHPAD_RESOLUTION)),
                    (self.ecodes.ABS_Y, self.AbsInfo(0, 0, height, 0, 0, TOUCHPAD_RESOLUTION)),
                    (self.ecodes.ABS_MT_SLOT, self.AbsInfo(0, 0, 9, 0, 0, 0)),
                    (self.ecodes.ABS_MT_TRACKING_ID, self.AbsInfo(0, 0, 65535, 0, 0, 0)),
                    (self.ecodes.ABS_MT_POSITION_X, self.AbsInfo(0, 0, width, 0, 0, TOUCHPAD_RESOLUTION)),
                    (self.ecodes.ABS_MT_POSITION_Y, self.AbsInfo(0, 0, height, 0, 0, TOUCHPAD_RESOLUTION)),
                ],
                self.ecodes.EV_KEY: [
                    self.ecodes.BTN_LEFT, self.ecodes.BTN_RIGHT, self.ecodes.BTN_MIDDLE,
                    self.ecodes.BTN_TOUCH, self.ecodes.BTN_TOOL_FINGER,
                    self.ecodes.BTN_TOOL_DOUBLETAP, self.ecodes.BTN_TOOL_TRIPLETAP,
                    self.ecodes.BTN_TOOL_QUADTAP,
                ],
            },
            name=VIRTUAL_NAME,
            input_props=[self.ecodes.INPUT_PROP_POINTER],
        )

    def set_transform(self, transform):
        if not transform or transform == self.current_transform:
            return
        if transform not in TRANSFORMS:
            raise ValueError(f"Unsupported touchpad transform: {transform}")
        self.current_transform = transform
        # Removing the device cancels pending libinput gestures before remapping.
        self.cancel_gesture()
        logging.info("Touchpad orientation updated to %s", transform)

    def transform_coordinates(self, raw_x, raw_y):
        t = self.current_transform
        if t == "Rotated90":
            # Inverted landscape: notch on right, bottom on left
            return self.raw_y_max - raw_y, raw_x
        elif t == "Normal":
            # Normal portrait: notch on top, bottom on bottom
            return raw_x, raw_y
        elif t == "Rotated180":
            # Inverted portrait: notch on bottom, bottom on top
            return self.raw_x_max - raw_x, self.raw_y_max - raw_y
        else:
            # Default / Rotated270: standard landscape, notch on left, bottom on right
            return raw_y, self.raw_x_max - raw_x

    def grab(self):
        if self.enabled and not self.locked and not self.grabbed:
            self.device.grab()
            self.grabbed = True

    def ungrab(self):
        if self.grabbed:
            self.device.ungrab()
            self.grabbed = False

    def set_enabled(self, enabled):
        enabled = bool(enabled)
        if enabled == self.enabled:
            return
        self.enabled = enabled
        self.grab_when_released = False
        if enabled:
            # Give KWin time to consume the finger lifts before taking the device.
            self.schedule_grab()
            logging.info("Touchpad mode enabled")
        else:
            self.release()
            if not self.grabbed_for_suppression:
                self.ungrab()
            logging.info("Touchpad mode disabled")

    def schedule_grab(self):
        from gi.repository import GLib
        GLib.timeout_add(int(GRAB_DELAY * 1000), self.grab_if_enabled)

    def grab_if_enabled(self):
        if self.enabled and not self.locked:
            if self.contacts:
                self.grab_when_released = True
            else:
                self.grab_when_released = False
                self.grab()
        return False

    def set_locked(self, locked):
        locked = bool(locked)
        if locked == self.locked:
            return
        self.locked = locked
        self.grab_when_released = False
        self.cancel_gesture()
        if locked:
            if not self.grabbed_for_suppression:
                self.ungrab()
        elif self.enabled:
            self.schedule_grab()
        logging.info("Lock screen direct touch %s", "enabled" if locked else "disabled")

    def on_power_key(self, pressed):
        if not pressed:
            return
        if not is_display_on():
            # Power key requested display wake-up. Clear any leftover suppression.
            self.cancel_suppression()
            self.cancel_gesture()
            return
        now = time.monotonic()
        if now < self.suppressed_until:
            # Rapid consecutive power key press while blanking; cancel suppression.
            self.cancel_suppression()
            self.cancel_gesture()
            return
        # Power key requested display shutdown. Suppress touch until blanking completes
        # and all ongoing/new contacts lift.
        self.suppressed_until = now + BLANK_SUPPRESSION_TIMEOUT
        self.suppress_wait_release = bool(self.contacts)
        if not self.grabbed:
            try:
                self.device.grab()
                self.grabbed = True
                self.grabbed_for_suppression = True
            except OSError:
                pass
        self.cancel_gesture()
        from gi.repository import GLib
        GLib.timeout_add(int(BLANK_SUPPRESSION_TIMEOUT * 1000), self._on_suppression_timeout)

    def _on_suppression_timeout(self):
        if self.suppressed_until > 0 and time.monotonic() >= self.suppressed_until:
            if not self.contacts:
                self.cancel_suppression()
            else:
                self.suppress_wait_release = True
        return False

    def cancel_suppression(self):
        self.suppressed_until = 0.0
        self.suppress_wait_release = False
        if self.grabbed_for_suppression:
            self.grabbed_for_suppression = False
            if self.locked or not self.enabled:
                self.ungrab()

    def is_suppressed(self):
        if self.suppressed_until <= 0:
            return False
        now = time.monotonic()
        if now < self.suppressed_until:
            if self.contacts:
                self.suppress_wait_release = True
            else:
                self.suppress_wait_release = False
            return True
        if self.suppress_wait_release and self.contacts:
            return True
        self.cancel_suppression()
        return False

    def cancel_gesture(self):
        # Include contacts whose frames are still queued on the physical device.
        while (event := self.device.read_one()) is not None:
            if event.type != self.ecodes.EV_SYN:
                self.on_event(event)
        # Device removal cancels libinput's pending taps and drag timers.
        old_path = self.virtual.device.path
        self.virtual.close()
        self.virtual = self._create_virtual()
        if self.on_virtual_replaced:
            self.on_virtual_replaced(old_path, self.virtual.device.path)
        self.ids.clear()
        self.changed.clear()
        self.removed.clear()
        self.pressed = False
        self.tool = None
        self.cancelled = bool(self.contacts)

    def release(self):
        # Lift every forwarded contact so the virtual touchpad cannot stick.
        for slot in sorted(self.ids):
            self.virtual.write(self.ecodes.EV_ABS, self.ecodes.ABS_MT_SLOT, slot)
            self.virtual.write(self.ecodes.EV_ABS, self.ecodes.ABS_MT_TRACKING_ID, -1)
        self.ids.clear()
        if self.pressed:
            self.pressed = False
            self.virtual.write(self.ecodes.EV_KEY, self.ecodes.BTN_TOUCH, 0)
        if self.tool is not None:
            self.virtual.write(self.ecodes.EV_KEY, self.tool, 0)
            self.tool = None
        self.virtual.syn()

    def on_readable(self, fd, condition):
        while True:
            event = self.device.read_one()
            if event is None:
                break
            self.on_event(event)
        return True

    def on_event(self, event):
        ecodes = self.ecodes
        if event.type == ecodes.EV_ABS:
            if event.code == ecodes.ABS_MT_SLOT:
                self.slot = event.value
            elif event.code == ecodes.ABS_MT_TRACKING_ID:
                self.on_tracking_id(event.value)
            elif event.code in (ecodes.ABS_MT_POSITION_X, ecodes.ABS_MT_POSITION_Y):
                contact = self.contacts.get(self.slot)
                if contact is not None:
                    index = 0 if event.code == ecodes.ABS_MT_POSITION_X else 1
                    contact[index] = event.value
                    self.changed.add(self.slot)
        elif event.type == ecodes.EV_SYN and event.code == ecodes.SYN_REPORT:
            self.on_frame()

    def on_tracking_id(self, value):
        if value >= 0:
            self.contacts[self.slot] = [None, None]
            self.changed.add(self.slot)
        elif self.slot in self.contacts:
            del self.contacts[self.slot]
            self.changed.add(self.slot)
            self.removed.add(self.slot)

    def on_frame(self):
        if not self.contacts and self.grab_when_released:
            self.grab_if_enabled()
        if not self.is_suppressed() and not self.locked:
            if self.cancelled:
                self.cancelled = bool(self.contacts)
            elif self.enabled and self.grabbed:
                self.forward()
        self.changed.clear()
        self.removed.clear()

    def update_tool(self, count):
        tools = {1: self.ecodes.BTN_TOOL_FINGER,
                 2: self.ecodes.BTN_TOOL_DOUBLETAP,
                 3: self.ecodes.BTN_TOOL_TRIPLETAP}
        tool = tools.get(count, self.ecodes.BTN_TOOL_QUADTAP if count >= 4 else None)
        if tool == self.tool:
            return
        if self.tool is not None:
            self.virtual.write(self.ecodes.EV_KEY, self.tool, 0)
        self.tool = tool
        if tool is not None:
            self.virtual.write(self.ecodes.EV_KEY, tool, 1)

    def forward(self):
        if not self.changed and not self.removed:
            return
        touching = bool(self.contacts)
        if touching != self.pressed:
            self.pressed = touching
            self.virtual.write(self.ecodes.EV_KEY, self.ecodes.BTN_TOUCH, 1 if touching else 0)
        self.update_tool(len(self.contacts))
        for slot in sorted(self.changed | self.removed):
            self.virtual.write(self.ecodes.EV_ABS, self.ecodes.ABS_MT_SLOT, slot)
            if slot in self.removed:
                if slot in self.ids:
                    self.virtual.write(self.ecodes.EV_ABS, self.ecodes.ABS_MT_TRACKING_ID, -1)
                    del self.ids[slot]
                continue
            raw_x, raw_y = self.contacts[slot]
            if raw_x is None or raw_y is None:
                continue
            if slot not in self.ids:
                self.ids[slot] = self.next_id
                self.next_id += 1
                self.virtual.write(self.ecodes.EV_ABS, self.ecodes.ABS_MT_TRACKING_ID,
                                   self.ids[slot])
            x, y = self.transform_coordinates(raw_x, raw_y)
            self.virtual.write(self.ecodes.EV_ABS, self.ecodes.ABS_MT_POSITION_X, x)
            self.virtual.write(self.ecodes.EV_ABS, self.ecodes.ABS_MT_POSITION_Y, y)
            self.virtual.write(self.ecodes.EV_ABS, self.ecodes.ABS_X, x)
            self.virtual.write(self.ecodes.EV_ABS, self.ecodes.ABS_Y, y)
        self.virtual.syn()

    def toggle_mode(self):
        self.restore_touchpad = False
        self.set_enabled(not self.enabled)

    def set_keyboard_shown(self, shown):
        shown = bool(shown)
        if shown == self.keyboard_shown:
            return
        self.keyboard_shown = shown
        if shown:
            if self.enabled:
                self.restore_touchpad = True
                self.set_enabled(False)
        elif self.restore_touchpad:
            self.restore_touchpad = False
            self.set_enabled(True)

    def toggle_keyboard(self):
        if self.on_keyboard_toggle:
            self.on_keyboard_toggle(not self.keyboard_shown)

    def close(self):
        self.cancel_suppression()
        self.release()
        self.ungrab()
        try:
            self.virtual.close()
        except Exception:
            pass


class AssistantKey:
    def __init__(self, device, touchpad, ecodes, GLib):
        self.device = device
        self.touchpad = touchpad
        self.ecodes = ecodes
        self.GLib = GLib
        self.single_press_source = None

    def can_activate(self):
        return not self.touchpad.locked and not self.touchpad.is_suppressed()

    def cancel_single_press(self):
        if self.single_press_source is not None:
            self.GLib.source_remove(self.single_press_source)
            self.single_press_source = None

    def on_single_press(self):
        self.single_press_source = None
        if self.can_activate():
            self.touchpad.toggle_keyboard()
        return False

    def on_readable(self, fd, condition):
        while (event := self.device.read_one()) is not None:
            # The board reports the Assistant button as KEY_TV on gpio-keys alongside volume up.
            if event.type != self.ecodes.EV_KEY or event.code != self.ecodes.KEY_TV or event.value != 1:
                continue
            if not self.can_activate():
                self.cancel_single_press()
            elif self.single_press_source is not None:
                self.cancel_single_press()
                self.touchpad.toggle_mode()
            else:
                self.single_press_source = self.GLib.timeout_add(DOUBLE_PRESS_TIMEOUT_MS, self.on_single_press)
        return True

    def close(self):
        self.cancel_single_press()
        self.device.close()


class PowerKeyRelay:
    def __init__(self, device, touchpad, ecodes, UInput):
        self.device = device
        self.touchpad = touchpad
        self.ecodes = ecodes
        self.virtual = UInput({ecodes.EV_KEY: [ecodes.KEY_POWER]}, name="LG G8 Power Key")
        self.grabbed = False
        self.devices = set()
        self.waiting = set()
        self.pending = []
        touchpad.on_virtual_replaced = self.touchpad_replaced

    def touchpad_replaced(self, old_path, new_path):
        if not self.pending:
            return
        old_name = os.path.basename(old_path)
        self.waiting.discard(("added", old_name))
        if old_name in self.devices:
            self.waiting.add(("removed", old_name))
        self.waiting.add(("added", os.path.basename(new_path)))

    def grab_if_ready(self):
        names = {os.path.basename(self.virtual.device.path),
                 os.path.basename(self.touchpad.virtual.device.path)}
        if not self.grabbed and names <= self.devices:
            # These queued events have already reached the native power-key device.
            while self.device.read_one() is not None:
                pass
            self.device.grab()
            if self.ecodes.KEY_POWER in self.device.active_keys():
                # Let the native device finish a press that began before takeover.
                self.device.ungrab()
                return
            self.grabbed = True

    def device_added(self, name):
        self.devices.add(name)
        self.grab_if_ready()
        self.waiting.discard(("added", name))
        self.forward_pending()

    def device_removed(self, name):
        self.devices.discard(name)
        self.waiting.discard(("removed", name))
        self.forward_pending()

    def forward_pending(self):
        if self.waiting:
            return
        for value in self.pending:
            self.virtual.write(self.ecodes.EV_KEY, self.ecodes.KEY_POWER, value)
            self.virtual.syn()
        self.pending.clear()

    def on_readable(self, fd, condition):
        while (event := self.device.read_one()) is not None:
            if event.type != self.ecodes.EV_KEY or event.code != self.ecodes.KEY_POWER:
                continue
            if not self.grabbed:
                if event.value == 0:
                    self.grab_if_ready()
                continue
            self.pending.append(event.value)
            if event.value == 1 and not self.waiting:
                old_name = os.path.basename(self.touchpad.virtual.device.path)
                self.waiting.add(("removed", old_name))
                self.touchpad.on_power_key(True)
                new_name = os.path.basename(self.touchpad.virtual.device.path)
                self.waiting.add(("added", new_name))
            self.forward_pending()
        return True

    def close(self):
        if self.grabbed:
            self.device.ungrab()
        self.virtual.close()
        self.device.close()


def main():
    import dbus
    import dbus.service
    from dbus.mainloop.glib import DBusGMainLoop
    from evdev import AbsInfo, InputDevice, UInput, ecodes, list_devices
    from gi.repository import GLib, GLibUnix

    DBusGMainLoop(set_as_default=True)
    bus = dbus.SessionBus()
    input_paths = list_devices()
    device = find_touchscreen(input_paths, InputDevice, ecodes)
    device_x_max = device.absinfo(ecodes.ABS_MT_POSITION_X).max
    device_y_max = device.absinfo(ecodes.ABS_MT_POSITION_Y).max

    orientation = DisplayOrientation()
    if orientation.error:
        orientation.close()
        raise orientation.error
    initial_transform = orientation.transform or "Normal"

    touchpad = Touchpad(
        device, ecodes, UInput, AbsInfo, device_x_max, device_y_max, initial_transform
    )
    logging.info("Using %s as a touchpad (initial orientation: %s)", device.name, initial_transform)

    power_key = find_key_device(input_paths, InputDevice, ecodes, "pm8941_pwrkey", ecodes.KEY_POWER)
    if power_key is None:
        raise RuntimeError("No pm8941 power key found")

    power = PowerKeyRelay(power_key, touchpad, ecodes, UInput)
    manager_path = "/org/kde/KWin/InputDevice"
    manager_interface = "org.kde.KWin.InputDeviceManager"
    # KWin processes libinput's final button and scroll events before deviceRemoved.
    for member, callback in (("deviceAdded", power.device_added), ("deviceRemoved", power.device_removed)):
        bus.add_signal_receiver(callback, signal_name=member, dbus_interface=manager_interface,
                                bus_name="org.kde.KWin", path=manager_path)

    loop = GLib.MainLoop()
    lock_error = None
    kwin_error = None

    def monitoring_failed(error):
        nonlocal lock_error
        logging.error("Screen lock monitoring failed", exc_info=(type(error), error, error.__traceback__))
        lock_error = error
        loop.quit()

    touchpad.set_locked(True)
    monitor = ScreenLockMonitor(bus, lambda locked: touchpad.set_locked(locked is None or locked),
                                monitoring_failed)

    class KeyboardWatcher(dbus.service.Object):
        def __init__(self):
            self.name = dbus.service.BusName(TOUCHPAD_BUS_NAME, bus, do_not_queue=True)
            super().__init__(self.name, TOUCHPAD_OBJECT_PATH)
            self.kwin_owner = ""
            self.script = None

        @dbus.service.method(TOUCHPAD_BUS_NAME, in_signature="b", out_signature="", sender_keyword="sender")
        def SetKeyboardShown(self, shown, sender=None):
            if sender != self.kwin_owner:
                raise dbus.DBusException("Keyboard events must come from KWin",
                                         name="org.freedesktop.DBus.Error.AccessDenied")
            touchpad.set_keyboard_shown(bool(shown))

        def attach_kwin(self, owner):
            nonlocal kwin_error
            try:
                self.kwin_owner = str(owner)
                self.script = None
                touchpad.set_keyboard_shown(False)
                if not self.kwin_owner:
                    power.devices.clear()
                    return
                properties = dbus.Interface(
                    bus.get_object(self.kwin_owner, manager_path, introspect=False),
                    "org.freedesktop.DBus.Properties",
                )
                power.devices = set(properties.Get(manager_interface, "devicesSysNames"))
                power.grab_if_ready()
                scripting = dbus.Interface(
                    bus.get_object(self.kwin_owner, "/Scripting", introspect=False),
                    "org.kde.kwin.Scripting",
                )
                if scripting.isScriptLoaded(TOUCHPAD_SCRIPT_NAME):
                    scripting.unloadScript(TOUCHPAD_SCRIPT_NAME)
                script_id = int(scripting.loadScript(TOUCHPAD_SCRIPT_PATH, TOUCHPAD_SCRIPT_NAME, signature="ss"))
                if script_id < 0:
                    raise RuntimeError("KWin could not load the touchpad keyboard script")
                self.script = scripting
                script = dbus.Interface(
                    bus.get_object(self.kwin_owner, f"/Scripting/Script{script_id}", introspect=False),
                    "org.kde.kwin.Script",
                )
                script.run()
            except Exception as error:
                logging.error("Failed to attach to KWin", exc_info=(type(error), error, error.__traceback__))
                kwin_error = error
                loop.quit()

        def close(self):
            if self.script and bus.name_has_owner("org.kde.KWin"):
                try:
                    self.script.unloadScript(TOUCHPAD_SCRIPT_NAME)
                except dbus.DBusException:
                    pass

    def toggle_keyboard(show):
        try:
            controller = dbus.Interface(
                bus.get_object(OSK_BUS_NAME, OSK_OBJECT_PATH, introspect=False),
                OSK_INTERFACE,
            )
            if show:
                controller.ForceShow()
            else:
                controller.ForceHide()
        except dbus.DBusException as error:
            logging.warning("Failed to %s on-screen keyboard: %s", "show" if show else "hide", error)

    touchpad.on_keyboard_toggle = toggle_keyboard
    keyboard_watcher = KeyboardWatcher()
    kwin_watch = bus.watch_name_owner("org.kde.KWin", keyboard_watcher.attach_kwin)
    orientation.start(touchpad.set_transform, loop)
    GLibUnix.fd_add_full(GLib.PRIORITY_DEFAULT, device.fd, GLib.IO_IN, touchpad.on_readable)
    GLibUnix.fd_add_full(GLib.PRIORITY_HIGH, power_key.fd, GLib.IO_IN, power.on_readable)
    assistant_device = find_key_device(input_paths, InputDevice, ecodes, "gpio-keys", ecodes.KEY_TV)
    assistant_key = None
    if assistant_device is not None:
        assistant_key = AssistantKey(assistant_device, touchpad, ecodes, GLib)
        GLibUnix.fd_add_full(GLib.PRIORITY_DEFAULT, assistant_device.fd, GLib.IO_IN, assistant_key.on_readable)
    else:
        logging.warning("No Assistant key found")
    for signum in (signal.SIGTERM, signal.SIGINT):
        GLibUnix.signal_add(GLib.PRIORITY_DEFAULT, signum, lambda: (loop.quit(), False)[1])
    try:
        touchpad.grab_if_enabled()
        loop.run()
        if orientation.error:
            raise orientation.error
        if lock_error:
            raise lock_error
        if kwin_error:
            raise kwin_error
    finally:
        kwin_watch.cancel()
        keyboard_watcher.close()
        monitor.close()
        orientation.close()
        if assistant_key is not None:
            assistant_key.close()
        touchpad.close()
        device.close()
        power.close()
    return 0


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    sys.exit(main())
