#!/usr/bin/env python3
"""Generate the LM-G820N Plasma shell with touch-friendly lock screen activation."""

from pathlib import Path
import shutil
import sys


root = Path(sys.argv[1])
source = root / "usr/share/plasma/shells/org.kde.plasma.desktop"
target = root / "org.kde.plasma.desktop"
ui = (source / "contents/lockscreen/LockScreenUi.qml").read_text()
keyboard = "Keyboards.KWinVirtualKeyboard.visible" if "Keyboards.KWinVirtualKeyboard.visible" in ui else "inputPanel.keyboardActive"
changes = {
    f"property bool blockUI: containsMouse && (mainStack.depth > 1 || mainBlock.mainPasswordBox.text.length > 0 || {keyboard})":
        f"property bool blockUI: {keyboard} || (containsMouse && (mainStack.depth > 1 || mainBlock.mainPasswordBox.text.length > 0))",
    "uiVisible = seenPositionChange;": "uiVisible = uiVisible || seenPositionChange;",
}
if keyboard == "inputPanel.keyboardActive":
    changes["        onExited: {\n            uiVisible = false;\n        }\n"] = ""
for before, after in changes.items():
    if ui.count(before) != 1:
        raise RuntimeError("Unsupported Plasma lock screen activation handler")
    ui = ui.replace(before, after)
if target.exists():
    shutil.rmtree(target)
shutil.copytree(source, target)
(target / "contents/lockscreen/LockScreenUi.qml").write_text(ui)
