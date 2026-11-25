#!/usr/bin/python3
"""Blank the active SDDM display before systemd freezes user sessions."""

import json
import pwd
import subprocess
import sys
import time


def loginctl(*args):
    return subprocess.run(
        ["loginctl", *args], check=True, stdout=subprocess.PIPE, text=True
    ).stdout


def blank_display(user, runtime):
    # Match the default KWin socket used by the SDDM compositor.
    options = {
        "user": user.pw_uid,
        "group": user.pw_gid,
        "extra_groups": [],
        "env": {
            "LC_ALL": "C.UTF-8",
            "XDG_RUNTIME_DIR": runtime,
            "DBUS_SESSION_BUS_ADDRESS": f"unix:path={runtime}/bus",
            "WAYLAND_DISPLAY": "wayland-0",
            "QT_QPA_PLATFORM": "wayland",
        },
    }
    deadline = time.monotonic() + 10
    subprocess.run(["/usr/bin/kscreen-doctor", "--dpms", "off"],
                   check=True, timeout=10, **options)
    # KScreen exits after sending the request; KWin may still be fading out.
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("Greeter displays did not turn off")
        result = subprocess.run(
            ["/usr/bin/kscreen-doctor", "--dpms", "show"], check=True,
            stdout=subprocess.PIPE, text=True, timeout=remaining, **options,
        )
        states = result.stdout.splitlines()
        if states and all(state.endswith(": off") for state in states):
            return
        time.sleep(0.05)


def main():
    sessions = json.loads(loginctl("list-sessions", "--json=short"))
    for session in sessions:
        if session["class"] != "greeter":
            continue
        properties = dict(line.split("=", 1) for line in loginctl(
            "show-session", session["session"], "-p", "Active", "-p", "Type"
        ).splitlines())
        if properties["Active"] != "yes" or properties["Type"] != "wayland":
            continue

        user = pwd.getpwuid(session["uid"])
        runtime = loginctl("show-user", str(user.pw_uid), "-p", "RuntimePath", "--value").strip()
        blank_display(user, runtime)
        print(f"Prepared greeter session {session['session']} for suspend", flush=True)


if __name__ == "__main__":
    try:
        main()
    except (OSError, ValueError, KeyError, subprocess.SubprocessError) as error:
        print(f"Greeter sleep preparation failed: {error}", file=sys.stderr)
        sys.exit(1)
