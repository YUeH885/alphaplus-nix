function reportForeground() {
    const window = workspace.activeWindow;
    const pid = window && window.normalWindow ? window.pid : 0;
    callDBus("org.alphaplus.ForegroundPlacement", "/org/alphaplus/ForegroundPlacement",
             "org.alphaplus.ForegroundPlacement", "SetForeground", pid);
}

workspace.windowActivated.connect(reportForeground);
reportForeground();
