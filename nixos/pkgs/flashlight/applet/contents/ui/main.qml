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

    readonly property string flashlightIcon: flashlight.active ? "flashlight-on" : "flashlight-off"
    readonly property string label: !flashlight.available ? i18n("Flashlight unavailable")
        : flashlight.active ? i18n("Turn off flashlight") : i18n("Turn on flashlight")

    Plasmoid.icon: flashlightIcon
    Plasmoid.status: flashlight.active ? PlasmaCore.Types.ActiveStatus : PlasmaCore.Types.PassiveStatus
    Plasmoid.onActivated: flashlight.toggle()
    activationTogglesExpanded: false
    toolTipMainText: label
    toolTipSubText: flashlight.error
    preferredRepresentation: compactRepresentation
    compactRepresentation: toggleButton
    fullRepresentation: toggleButton

    Component {
        id: toggleButton

        PlasmaComponents.ToolButton {
            Layout.minimumWidth: Kirigami.Units.iconSizes.small
            Layout.minimumHeight: Kirigami.Units.iconSizes.small
            display: Controls.AbstractButton.IconOnly
            icon.name: flashlight.error ? "dialog-warning" : root.flashlightIcon
            text: root.label
            enabled: flashlight.available
            Controls.ToolTip.visible: hovered
            Controls.ToolTip.text: flashlight.error || root.label
            onClicked: flashlight.toggle()
        }
    }

    Backend.Flashlight {
        id: flashlight
    }

    Timer {
        interval: 2000
        running: root.visible
        repeat: true
        triggeredOnStart: true
        onTriggered: flashlight.refresh()
    }
}
