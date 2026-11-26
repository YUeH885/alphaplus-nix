// SPDX-License-Identifier: GPL-2.0-or-later
import QtQuick
import QtQuick.Controls as Controls
import QtQuick.Layouts
import org.kde.kirigami as Kirigami
import org.kde.plasma.components as PlasmaComponents
import org.kde.plasma.core as PlasmaCore
import org.kde.plasma.extras as PlasmaExtras
import org.kde.plasma.plasmoid
import "backend" as Backend

PlasmoidItem {
    id: root

    readonly property string nfcIcon: nfc.powered
        ? "alphaplus-nfc-active-symbolic"
        : "alphaplus-nfc-disabled-symbolic"

    readonly property string statusText: {
        if (!nfc.available)
            return i18n("NFC unavailable");
        if (nfc.tagPresent)
            return nfc.tagProtocol ? i18n("Tag detected (%1)", nfc.tagProtocol) : i18n("Tag detected");
        if (!nfc.powered)
            return i18n("Off");
        if (nfc.polling)
            return i18n("Scanning for tags...");
        return i18n("Ready (Idle)");
    }

    Plasmoid.icon: nfc.error && !nfc.available ? "dialog-warning" : nfcIcon
    Plasmoid.status: (nfc.powered || nfc.tagPresent || nfc.error !== "")
        ? PlasmaCore.Types.ActiveStatus
        : PlasmaCore.Types.PassiveStatus
    toolTipMainText: i18n("NFC")
    toolTipSubText: nfc.error || statusText

    compactRepresentation: Item {
        Layout.minimumWidth: Kirigami.Units.iconSizes.small
        Layout.minimumHeight: Kirigami.Units.iconSizes.small

        MouseArea {
            anchors.fill: parent
            onClicked: root.expanded = !root.expanded
        }

        Kirigami.Icon {
            anchors.fill: parent
            source: Plasmoid.icon
        }
    }

    fullRepresentation: PlasmaExtras.Representation {
        collapseMarginsHint: true
        Layout.minimumWidth: Kirigami.Units.gridUnit * 18
        Layout.minimumHeight: Kirigami.Units.gridUnit * 20
        Layout.preferredWidth: Kirigami.Units.gridUnit * 22
        Layout.preferredHeight: Kirigami.Units.gridUnit * 24

        header: PlasmaExtras.PlasmoidHeading {
            contentItem: RowLayout {
                spacing: Kirigami.Units.smallSpacing

                Kirigami.Icon {
                    source: root.nfcIcon
                    Layout.preferredWidth: Kirigami.Units.iconSizes.medium
                    Layout.preferredHeight: Kirigami.Units.iconSizes.medium
                }

                ColumnLayout {
                    Layout.fillWidth: true
                    spacing: 0

                    PlasmaComponents.Label {
                        Layout.fillWidth: true
                        text: i18n("NFC")
                        font.weight: Font.DemiBold
                        elide: Text.ElideRight
                    }

                    PlasmaComponents.Label {
                        Layout.fillWidth: true
                        text: root.statusText
                        font: Kirigami.Theme.smallFont
                        opacity: 0.75
                        elide: Text.ElideRight
                    }
                }

                PlasmaComponents.Button {
                    visible: nfc.available && nfc.powered
                    enabled: nfc.polling || !nfc.tagPresent
                    icon.name: nfc.polling ? "process-stop" : "view-refresh"
                    text: nfc.polling ? i18n("Stop") : i18n("Scan")
                    onClicked: nfc.polling ? nfc.stopScan() : nfc.startScan("Initiator")
                }

                PlasmaComponents.Switch {
                    enabled: nfc.available
                    checked: nfc.powered
                    onToggled: {
                        nfc.setPowered(checked);
                        checked = Qt.binding(() => nfc.powered);
                    }
                }
            }
        }

        ColumnLayout {
            anchors.fill: parent
            anchors.margins: Kirigami.Units.smallSpacing
            spacing: Kirigami.Units.smallSpacing

            Kirigami.InlineMessage {
                Layout.fillWidth: true
                type: Kirigami.MessageType.Error
                visible: nfc.error !== ""
                text: nfc.error
                actions: Kirigami.Action {
                    icon.name: "dialog-close"
                    text: i18n("Dismiss")
                    onTriggered: nfc.clearError()
                }
            }

            Kirigami.InlineMessage {
                Layout.fillWidth: true
                type: Kirigami.MessageType.Positive
                visible: nfc.statusMessage !== ""
                text: nfc.statusMessage
            }

            PlasmaComponents.ScrollView {
                id: scrollView
                Layout.fillWidth: true
                Layout.fillHeight: true
                clip: true
                contentWidth: availableWidth
                Controls.ScrollBar.horizontal.policy: Controls.ScrollBar.AlwaysOff

                ColumnLayout {
                    width: scrollView.availableWidth
                    spacing: Kirigami.Units.mediumSpacing

                    Kirigami.PlaceholderMessage {
                        Layout.fillWidth: true
                        Layout.topMargin: Kirigami.Units.largeSpacing
                        visible: !nfc.hasTagData
                        icon.name: root.nfcIcon
                        text: !nfc.available
                            ? i18n("NFC daemon not available")
                            : !nfc.powered
                                ? i18n("NFC is turned off")
                                : nfc.polling
                                    ? i18n("Hold an NFC tag or card near the back of the phone")
                                    : i18n("NFC is idle. Tap Scan to read a tag")
                    }

                    Controls.Frame {
                        Layout.fillWidth: true
                        visible: nfc.hasTagData

                        contentItem: ColumnLayout {
                            spacing: Kirigami.Units.smallSpacing

                            RowLayout {
                                Layout.fillWidth: true
                                spacing: Kirigami.Units.smallSpacing

                                ColumnLayout {
                                    Layout.fillWidth: true
                                    spacing: 2

                                    PlasmaComponents.Label {
                                        Layout.fillWidth: true
                                        text: nfc.tagPresent ? i18n("Current Tag") : i18n("Last Scanned Tag")
                                        font.weight: Font.DemiBold
                                    }

                                    PlasmaComponents.Label {
                                        Layout.fillWidth: true
                                        font: Kirigami.Theme.smallFont
                                        opacity: 0.75
                                        text: {
                                            const parts = [];
                                            if (nfc.tagType)
                                                parts.push(nfc.tagType);
                                            if (nfc.tagProtocol)
                                                parts.push(nfc.tagProtocol);
                                            if (nfc.tagUid)
                                                parts.push(i18n("UID %1", nfc.tagUid));
                                            parts.push(nfc.tagReadOnly ? i18n("Read-Only") : i18n("Writable"));
                                            return parts.join(" · ");
                                        }
                                        elide: Text.ElideRight
                                    }
                                }

                                PlasmaComponents.ToolButton {
                                    visible: nfc.tagUid !== ""
                                    icon.name: "edit-copy"
                                    Controls.ToolTip.visible: hovered
                                    Controls.ToolTip.text: i18n("Copy UID")
                                    onClicked: nfc.copyToClipboard(nfc.tagUid)
                                }

                                PlasmaComponents.ToolButton {
                                    visible: !nfc.tagPresent
                                    icon.name: "edit-clear"
                                    Controls.ToolTip.visible: hovered
                                    Controls.ToolTip.text: i18n("Clear")
                                    onClicked: nfc.clearTagHistory()
                                }
                            }

                            PlasmaComponents.Label {
                                Layout.fillWidth: true
                                visible: nfc.records.length === 0
                                opacity: 0.75
                                wrapMode: Text.Wrap
                                text: i18n("Tag contains no NDEF records.")
                            }

                            Repeater {
                                model: nfc.records

                                delegate: Controls.Frame {
                                    required property var modelData
                                    Layout.fillWidth: true

                                    contentItem: ColumnLayout {
                                        spacing: 4

                                        RowLayout {
                                            Layout.fillWidth: true

                                            PlasmaComponents.Label {
                                                Layout.fillWidth: true
                                                text: modelData.title
                                                font: Kirigami.Theme.smallFont
                                                opacity: 0.8
                                                elide: Text.ElideRight
                                            }

                                            PlasmaComponents.ToolButton {
                                                visible: modelData.uri !== ""
                                                icon.name: "internet-web-browser"
                                                Controls.ToolTip.visible: hovered
                                                Controls.ToolTip.text: i18n("Open URI")
                                                onClicked: nfc.openUri(modelData.uri)
                                            }

                                            PlasmaComponents.ToolButton {
                                                icon.name: "edit-copy"
                                                Controls.ToolTip.visible: hovered
                                                Controls.ToolTip.text: i18n("Copy")
                                                onClicked: nfc.copyToClipboard(modelData.copyText)
                                            }
                                        }

                                        PlasmaComponents.Label {
                                            Layout.fillWidth: true
                                            text: modelData.summary
                                            wrapMode: Text.WrapAnywhere
                                            textFormat: Text.PlainText
                                        }

                                        PlasmaComponents.Label {
                                            Layout.fillWidth: true
                                            visible: modelData.detail !== ""
                                            text: modelData.detail
                                            font: Kirigami.Theme.smallFont
                                            opacity: 0.65
                                            wrapMode: Text.WrapAnywhere
                                            textFormat: Text.PlainText
                                        }
                                    }
                                }
                            }
                        }
                    }

                    Controls.Frame {
                        Layout.fillWidth: true
                        visible: nfc.available && nfc.powered

                        contentItem: ColumnLayout {
                            spacing: Kirigami.Units.smallSpacing

                            RowLayout {
                                Layout.fillWidth: true
                                spacing: Kirigami.Units.smallSpacing

                                property bool writeUriMode: true
                                id: writeHeader

                                PlasmaComponents.Label {
                                    Layout.fillWidth: true
                                    text: i18n("Write NDEF Tag")
                                    font.weight: Font.DemiBold
                                }

                                PlasmaComponents.Button {
                                    checkable: true
                                    checked: writeHeader.writeUriMode
                                    text: i18n("URI")
                                    onClicked: writeHeader.writeUriMode = true
                                }

                                PlasmaComponents.Button {
                                    checkable: true
                                    checked: !writeHeader.writeUriMode
                                    text: i18n("Text")
                                    onClicked: writeHeader.writeUriMode = false
                                }
                            }

                            PlasmaComponents.TextField {
                                id: writeInput
                                Layout.fillWidth: true
                                enabled: !nfc.writing
                                placeholderText: writeHeader.writeUriMode
                                    ? i18n("https://example.org")
                                    : i18n("Enter plain text")
                            }

                            RowLayout {
                                Layout.fillWidth: true

                                PlasmaComponents.Label {
                                    Layout.fillWidth: true
                                    font: Kirigami.Theme.smallFont
                                    opacity: 0.75
                                    wrapMode: Text.Wrap
                                    text: !nfc.tagPresent
                                        ? i18n("Tap a writable tag to enable writing")
                                        : nfc.tagReadOnly
                                            ? i18n("Current tag is read-only")
                                            : i18n("Ready to write to %1", nfc.tagType || i18n("tag"))
                                }

                                PlasmaComponents.Button {
                                    enabled: nfc.tagPresent && !nfc.tagReadOnly && !nfc.writing && writeInput.text.trim().length > 0
                                    icon.name: "document-save"
                                    text: nfc.writing ? i18n("Writing...") : i18n("Write")
                                    onClicked: {
                                        if (writeHeader.writeUriMode)
                                            nfc.writeUri(writeInput.text);
                                        else
                                            nfc.writeText(writeInput.text);
                                    }
                                }
                            }
                        }
                    }
                }
            }
        }
    }

    Backend.NfcController {
        id: nfc
    }
}
