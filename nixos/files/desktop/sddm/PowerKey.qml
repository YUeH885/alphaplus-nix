import QtQuick
import org.kde.plasma.plasma5support as Plasma5Support

Item {
    Plasma5Support.DataSource {
        id: screenPower
        engine: "executable"
        connectedSources: []

        onNewData: (sourceName, data) => {
            disconnectSource(sourceName);
            if (data["exit code"] !== 0) {
                console.error("Failed to turn off the greeter display:", data.stderr);
            }
        }
    }

    // Sleep preparation also sets DPMS off so KWin consumes the wake-up press.
    Shortcut {
        sequence: "Power Off"
        context: Qt.ApplicationShortcut
        autoRepeat: false
        onActivated: screenPower.connectSource("/usr/bin/kscreen-doctor --dpms off")
    }
}
