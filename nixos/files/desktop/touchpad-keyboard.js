function reportKeyboard() {
    var shown = false;
    for (const w of workspace.windowList()) {
        if (w.resourceClass === "fcitx5-osk") {
            shown = true;
            break;
        }
    }
    callDBus("org.alphaplus.Touchpad", "/org/alphaplus/Touchpad",
             "org.alphaplus.Touchpad", "SetKeyboardShown", shown);
}

workspace.windowAdded.connect(w => {
    if (w.resourceClass === "fcitx5-osk") {
        reportKeyboard();
    }
});
workspace.windowRemoved.connect(w => {
    if (w.resourceClass === "fcitx5-osk") {
        reportKeyboard();
    }
});
reportKeyboard();
