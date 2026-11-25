#!/usr/bin/env python3
"""Create the device's Breeze variant without changing packaged files."""

from pathlib import Path
import shutil
import sys


root = Path(sys.argv[1])
source = root / "usr/share/sddm/themes/breeze"
target = root / "usr/share/sddm/themes/breeze-alphaplus"
main = (source / "Main.qml").read_text()
for capability in ("canHibernate", "canSuspend", "canReboot", "canPowerOff"):
    line = f"enabled: sddm.{capability}"
    if line not in main:
        raise SystemExit(f"Unsupported Breeze theme: missing {line}")
    main = main.replace(line, f"visible: sddm.{capability}\n                        {line}")
anchor = "    id: root\n"
if main.count(anchor) != 1:
    raise SystemExit("Unsupported Breeze theme: missing unique root item")
main = main.replace(anchor, anchor + "\n    PowerKey {}\n")
shutil.copytree(source, target, dirs_exist_ok=True)
power_key = target / "PowerKey.qml"
shutil.copyfile(Path(__file__).parent / "PowerKey.qml", power_key)
power_key.chmod(0o644)
(target / "Main.qml").write_text(main)
