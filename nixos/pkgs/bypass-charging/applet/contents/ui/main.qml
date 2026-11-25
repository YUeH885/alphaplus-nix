// SPDX-License-Identifier: GPL-2.0-or-later
import QtQuick
import QtQuick.Controls as Controls
import QtQuick.Layouts
import org.kde.kirigami as Kirigami
import org.kde.plasma.components as PlasmaComponents
import org.kde.plasma.core as PlasmaCore
import org.kde.plasma.plasmoid
import "backend" as Backend

PlasmoidItem {
    id: root

    readonly property string bypassIcon: bypass.active ? "battery-full-charged-symbolic" : "battery-full-charging-symbolic"
    readonly property string label: !bypass.available ? i18n("Bypass charging unavailable")
        : bypass.active ? i18n("Disable bypass charging") : i18n("Enable bypass charging")

    Plasmoid.icon: bypassIcon
    Plasmoid.status: bypass.active ? PlasmaCore.Types.ActiveStatus : PlasmaCore.Types.PassiveStatus
    Plasmoid.onActivated: bypass.toggle()
    activationTogglesExpanded: false
    toolTipMainText: label
    toolTipSubText: bypass.error
    preferredRepresentation: compactRepresentation
    compactRepresentation: toggleButton
    fullRepresentation: toggleButton

    Component {
        id: toggleButton

        PlasmaComponents.ToolButton {
            Layout.minimumWidth: Kirigami.Units.iconSizes.small
            Layout.minimumHeight: Kirigami.Units.iconSizes.small
            display: Controls.AbstractButton.IconOnly
            icon.name: bypass.error ? "dialog-warning" : root.bypassIcon
            text: root.label
            enabled: bypass.available
            Controls.ToolTip.visible: hovered
            Controls.ToolTip.text: bypass.error || root.label
            onClicked: bypass.toggle()
        }
    }

    Backend.BypassCharging {
        id: bypass
    }

    Timer {
        interval: 2000
        running: root.visible
        repeat: true
        triggeredOnStart: true
        onTriggered: bypass.refresh()
    }
}
