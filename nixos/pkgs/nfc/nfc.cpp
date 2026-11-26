// SPDX-License-Identifier: GPL-2.0-or-later
#include <QClipboard>
#include <QDBusConnection>
#include <QDBusError>
#include <QDBusMessage>
#include <QDBusMetaType>
#include <QDBusObjectPath>
#include <QDBusPendingCallWatcher>
#include <QDBusPendingReply>
#include <QDBusUnixFileDescriptor>
#include <QDBusVariant>
#include <QDesktopServices>
#include <QGuiApplication>
#include <QMap>
#include <QQmlExtensionPlugin>
#include <QStringList>
#include <QUrl>
#include <QVariantList>
#include <QVariantMap>
#include <qqml.h>

using InterfaceMap = QMap<QString, QVariantMap>;
using ManagedObjectMap = QMap<QDBusObjectPath, InterfaceMap>;

Q_DECLARE_METATYPE(InterfaceMap)
Q_DECLARE_METATYPE(ManagedObjectMap)

namespace {
constexpr auto NEARD_SERVICE = "org.neard";
constexpr auto ADAPTER_IFACE = "org.neard.Adapter";
constexpr auto TAG_IFACE = "org.neard.Tag";
constexpr auto RECORD_IFACE = "org.neard.Record";
constexpr auto PROPS_IFACE = "org.freedesktop.DBus.Properties";
constexpr auto OBJECT_MANAGER_IFACE = "org.freedesktop.DBus.ObjectManager";
constexpr auto LOGIN1_SERVICE = "org.freedesktop.login1";
constexpr auto LOGIN1_PATH = "/org/freedesktop/login1";
constexpr auto LOGIN1_MANAGER_IFACE = "org.freedesktop.login1.Manager";

bool isPrintableUtf8(const QByteArray &bytes, QString *decodedOut)
{
    if (bytes.isEmpty())
        return false;

    const QString text = QString::fromUtf8(bytes);
    if (text.toUtf8() != bytes)
        return false;

    for (const QChar ch : text) {
        if (ch.isNull() || (ch.category() == QChar::Other_Control && !ch.isSpace()))
            return false;
    }

    *decodedOut = text;
    return true;
}

QString formatHexPreview(const QByteArray &bytes)
{
    constexpr int maxPreviewBytes = 32;
    const QByteArray slice = bytes.left(maxPreviewBytes);
    QString hex = QString::fromLatin1(slice.toHex(' ')).toUpper();
    if (bytes.size() > maxPreviewBytes)
        hex += QStringLiteral(" ... (%1 bytes)").arg(bytes.size());
    return hex;
}

QString formatDetailLines(const QVariantMap &props, const QStringList &skipKeys)
{
    QStringList lines;
    for (auto it = props.cbegin(); it != props.cend(); ++it) {
        if (skipKeys.contains(it.key()))
            continue;
        if (it.value().typeId() == QMetaType::QByteArray) {
            const QByteArray payload = it.value().toByteArray();
            lines.append(QStringLiteral("%1: %2 bytes").arg(it.key()).arg(payload.size()));
            continue;
        }
        const QString val = it.value().toString();
        if (!val.isEmpty())
            lines.append(QStringLiteral("%1: %2").arg(it.key(), val));
    }
    return lines.join(QStringLiteral(" · "));
}

QVariantMap buildRecordEntry(const QString &path, const QVariantMap &props)
{
    const QString type = props.value(QStringLiteral("Type")).toString();
    QString title = type.isEmpty() ? QStringLiteral("Record") : type;
    QString summary;
    QString detail;
    QString uri;

    if (type == QLatin1String("URI")) {
        uri = props.value(QStringLiteral("URI")).toString();
        summary = uri;
        detail = formatDetailLines(props, { QStringLiteral("Type"), QStringLiteral("URI") });
    } else if (type == QLatin1String("Text")) {
        const QString lang = props.value(QStringLiteral("Language")).toString();
        const QString enc = props.value(QStringLiteral("Encoding")).toString();
        if (!lang.isEmpty())
            title = QStringLiteral("Text (%1)").arg(lang);
        summary = props.value(QStringLiteral("Representation")).toString();
        QStringList meta;
        if (!enc.isEmpty())
            meta.append(enc);
        if (!lang.isEmpty())
            meta.append(lang);
        detail = meta.join(QStringLiteral(" · "));
    } else if (type == QLatin1String("SmartPoster")) {
        uri = props.value(QStringLiteral("URI")).toString();
        const QString rep = props.value(QStringLiteral("Representation")).toString();
        summary = rep.isEmpty() ? uri : rep;
        QStringList meta;
        if (!rep.isEmpty() && !uri.isEmpty())
            meta.append(uri);
        const QString extra = formatDetailLines(props, {
            QStringLiteral("Type"),
            QStringLiteral("URI"),
            QStringLiteral("Representation"),
        });
        if (!extra.isEmpty())
            meta.append(extra);
        detail = meta.join(QStringLiteral(" · "));
    } else if (type == QLatin1String("MIME")) {
        const QString mime = props.value(QStringLiteral("MIME")).toString();
        title = mime.isEmpty() ? QStringLiteral("MIME") : mime;
        const QString ssid = props.value(QStringLiteral("SSID")).toString();
        const QByteArray payload = props.value(QStringLiteral("MIMEPayload")).toByteArray();
        QString decoded;
        if (!ssid.isEmpty()) {
            const QString pass = props.value(QStringLiteral("Passphrase")).toString();
            summary = pass.isEmpty() ? QStringLiteral("Wi-Fi: %1").arg(ssid)
                                     : QStringLiteral("Wi-Fi: %1 / %2").arg(ssid, pass);
        } else if (isPrintableUtf8(payload, &decoded)) {
            summary = decoded;
        } else if (!payload.isEmpty()) {
            summary = formatHexPreview(payload);
        } else {
            summary = mime;
        }
        detail = formatDetailLines(props, { QStringLiteral("Type"), QStringLiteral("MIME") });
    } else {
        summary = props.value(QStringLiteral("Representation")).toString();
        if (summary.isEmpty())
            summary = props.value(QStringLiteral("URI")).toString();
        detail = formatDetailLines(props, { QStringLiteral("Type") });
    }

    if (summary.isEmpty())
        summary = detail.isEmpty() ? path : detail;

    QVariantMap entry;
    entry.insert(QStringLiteral("path"), path);
    entry.insert(QStringLiteral("type"), type.isEmpty() ? QStringLiteral("Unknown") : type);
    entry.insert(QStringLiteral("title"), title);
    entry.insert(QStringLiteral("summary"), summary);
    entry.insert(QStringLiteral("detail"), detail);
    entry.insert(QStringLiteral("uri"), uri);
    entry.insert(QStringLiteral("copyText"), uri.isEmpty() ? summary : uri);
    return entry;
}

bool isBenignPowerReply(const QDBusError &err, bool targetState)
{
    if (err.type() == QDBusError::NoError)
        return true;
    const QString msg = err.message();
    if (targetState && msg.contains(QLatin1String("Already enabled"), Qt::CaseInsensitive))
        return true;
    if (!targetState && msg.contains(QLatin1String("Already disabled"), Qt::CaseInsensitive))
        return true;
    return false;
}
}

class NfcController : public QObject
{
    Q_OBJECT
    Q_PROPERTY(bool available READ available NOTIFY stateChanged)
    Q_PROPERTY(bool powered READ powered NOTIFY stateChanged)
    Q_PROPERTY(bool polling READ polling NOTIFY stateChanged)
    Q_PROPERTY(QString mode READ mode NOTIFY stateChanged)
    Q_PROPERTY(QStringList protocols READ protocols NOTIFY stateChanged)
    Q_PROPERTY(bool tagPresent READ tagPresent NOTIFY tagChanged)
    Q_PROPERTY(bool hasTagData READ hasTagData NOTIFY tagChanged)
    Q_PROPERTY(QString tagType READ tagType NOTIFY tagChanged)
    Q_PROPERTY(QString tagProtocol READ tagProtocol NOTIFY tagChanged)
    Q_PROPERTY(QString tagUid READ tagUid NOTIFY tagChanged)
    Q_PROPERTY(bool tagReadOnly READ tagReadOnly NOTIFY tagChanged)
    Q_PROPERTY(QVariantList records READ records NOTIFY tagChanged)
    Q_PROPERTY(bool writing READ writing NOTIFY writingChanged)
    Q_PROPERTY(QString statusMessage READ statusMessage NOTIFY statusMessageChanged)
    Q_PROPERTY(QString error READ error NOTIFY errorChanged)

public:
    explicit NfcController(QObject *parent = nullptr)
        : QObject(parent)
        , m_bus(QDBusConnection::systemBus())
    {
        qDBusRegisterMetaType<InterfaceMap>();
        qDBusRegisterMetaType<ManagedObjectMap>();

        m_bus.connect(QString::fromLatin1(NEARD_SERVICE), QStringLiteral("/"),
                      QString::fromLatin1(OBJECT_MANAGER_IFACE), QStringLiteral("InterfacesAdded"),
                      this, SLOT(onInterfacesAdded(QDBusObjectPath, InterfaceMap)));
        m_bus.connect(QString::fromLatin1(NEARD_SERVICE), QStringLiteral("/"),
                      QString::fromLatin1(OBJECT_MANAGER_IFACE), QStringLiteral("InterfacesRemoved"),
                      this, SLOT(onInterfacesRemoved(QDBusObjectPath, QStringList)));
        m_bus.connect(QString::fromLatin1(NEARD_SERVICE), QString(),
                      QString::fromLatin1(PROPS_IFACE), QStringLiteral("PropertiesChanged"),
                      this, SLOT(onPropertiesChanged(QString, QVariantMap, QStringList, QDBusMessage)));
        m_bus.connect(QStringLiteral("org.freedesktop.DBus"), QStringLiteral("/org/freedesktop/DBus"),
                      QStringLiteral("org.freedesktop.DBus"), QStringLiteral("NameOwnerChanged"),
                      this, SLOT(onNameOwnerChanged(QString, QString, QString)));
        m_bus.connect(QString::fromLatin1(LOGIN1_SERVICE), QString::fromLatin1(LOGIN1_PATH),
                      QString::fromLatin1(LOGIN1_MANAGER_IFACE), QStringLiteral("PrepareForSleep"),
                      this, SLOT(onPrepareForSleep(bool)));

        refresh();
    }

    bool available() const { return m_available; }
    bool powered() const { return m_powered; }
    bool polling() const { return m_polling; }
    QString mode() const { return m_mode; }
    QStringList protocols() const { return m_protocols; }
    bool tagPresent() const { return m_tagPresent; }
    bool hasTagData() const { return m_hasTagData; }
    QString tagType() const { return m_tagType; }
    QString tagProtocol() const { return m_tagProtocol; }
    QString tagUid() const { return m_tagUid; }
    bool tagReadOnly() const { return m_tagReadOnly; }
    QVariantList records() const { return m_records; }
    bool writing() const { return m_writing; }
    QString statusMessage() const { return m_statusMessage; }
    QString error() const { return m_error; }

    Q_INVOKABLE void refresh()
    {
        QDBusMessage msg = QDBusMessage::createMethodCall(
            QString::fromLatin1(NEARD_SERVICE), QStringLiteral("/"),
            QString::fromLatin1(OBJECT_MANAGER_IFACE), QStringLiteral("GetManagedObjects"));
        auto *watcher = new QDBusPendingCallWatcher(m_bus.asyncCall(msg), this);
        connect(watcher, &QDBusPendingCallWatcher::finished, this, [this](QDBusPendingCallWatcher *w) {
            w->deleteLater();
            QDBusPendingReply<ManagedObjectMap> reply = *w;
            if (reply.isError()) {
                resetAdapterState();
                setDaemonError(tr("NFC daemon unavailable: %1").arg(reply.error().message()));
                return;
            }
            applyManagedObjects(reply.value());
        });
    }

    Q_INVOKABLE void toggle()
    {
        setPowered(!m_powered);
    }

    Q_INVOKABLE void setPowered(bool on)
    {
        if (!m_available || m_adapterPath.isEmpty()) {
            setError(tr("No NFC adapter available"));
            return;
        }

        setError({});
        setStatusMessage({});
        if (!on)
            m_restoreOnWake = false;
        m_desiredPowered = on;
        m_desiredPolling = on;

        if (!on && m_polling) {
            stopScanAndPowerOff();
            return;
        }

        sendSetPowered(on, on);
    }

    Q_INVOKABLE void startScan(const QString &scanMode = QStringLiteral("Initiator"))
    {
        if (!m_available || m_adapterPath.isEmpty()) {
            setError(tr("No NFC adapter available"));
            return;
        }
        if (!m_powered) {
            setPowered(true);
            return;
        }

        setError({});
        QDBusMessage msg = QDBusMessage::createMethodCall(
            QString::fromLatin1(NEARD_SERVICE), m_adapterPath,
            QString::fromLatin1(ADAPTER_IFACE), QStringLiteral("StartPollLoop"));
        msg << (scanMode.isEmpty() ? QStringLiteral("Initiator") : scanMode);

        auto *watcher = new QDBusPendingCallWatcher(m_bus.asyncCall(msg), this);
        connect(watcher, &QDBusPendingCallWatcher::finished, this, [this](QDBusPendingCallWatcher *w) {
            w->deleteLater();
            QDBusPendingReply<> reply = *w;
            if (reply.isError()
                && !reply.error().message().contains(QLatin1String("In progress"), Qt::CaseInsensitive)
                && !reply.error().message().contains(QLatin1String("Already"), Qt::CaseInsensitive)) {
                setError(tr("Could not start NFC polling: %1").arg(reply.error().message()));
                return;
            }
            refresh();
        });
    }

    Q_INVOKABLE void stopScan()
    {
        if (!m_available || m_adapterPath.isEmpty())
            return;

        setError({});
        m_desiredPolling = false;
        QDBusMessage msg = QDBusMessage::createMethodCall(
            QString::fromLatin1(NEARD_SERVICE), m_adapterPath,
            QString::fromLatin1(ADAPTER_IFACE), QStringLiteral("StopPollLoop"));
        auto *watcher = new QDBusPendingCallWatcher(m_bus.asyncCall(msg), this);
        connect(watcher, &QDBusPendingCallWatcher::finished, this, [this](QDBusPendingCallWatcher *w) {
            w->deleteLater();
            QDBusPendingReply<> reply = *w;
            if (reply.isError() && !reply.error().message().contains(QLatin1String("Not polling"), Qt::CaseInsensitive))
                setError(tr("Could not stop NFC polling: %1").arg(reply.error().message()));
            refresh();
        });
    }

    Q_INVOKABLE void writeUri(const QString &uri)
    {
        const QString trimmed = uri.trimmed();
        if (trimmed.isEmpty()) {
            setError(tr("Enter a URI before writing to the tag"));
            return;
        }

        QVariantMap attrs;
        attrs.insert(QStringLiteral("Type"), QStringLiteral("URI"));
        attrs.insert(QStringLiteral("URI"), trimmed);
        writeRecord(attrs, tr("URI written to tag"));
    }

    Q_INVOKABLE void writeText(const QString &text, const QString &language = QStringLiteral("en-US"))
    {
        if (text.isEmpty()) {
            setError(tr("Enter text before writing to the tag"));
            return;
        }

        QVariantMap attrs;
        attrs.insert(QStringLiteral("Type"), QStringLiteral("Text"));
        attrs.insert(QStringLiteral("Encoding"), QStringLiteral("UTF-8"));
        attrs.insert(QStringLiteral("Language"), language.trimmed().isEmpty() ? QStringLiteral("en-US") : language.trimmed());
        attrs.insert(QStringLiteral("Representation"), text);
        writeRecord(attrs, tr("Text written to tag"));
    }

    Q_INVOKABLE void copyToClipboard(const QString &text)
    {
        if (text.isEmpty())
            return;
        if (auto *clipboard = QGuiApplication::clipboard()) {
            clipboard->setText(text);
            setStatusMessage(tr("Copied to clipboard"));
        }
    }

    Q_INVOKABLE void openUri(const QString &uri)
    {
        const QUrl url(uri.trimmed());
        const QString scheme = url.scheme().toLower();
        static const QStringList allowedSchemes = {
            QStringLiteral("http"),
            QStringLiteral("https"),
            QStringLiteral("mailto"),
            QStringLiteral("tel"),
            QStringLiteral("sms"),
            QStringLiteral("geo"),
        };
        if (!url.isValid() || scheme.isEmpty() || !allowedSchemes.contains(scheme)) {
            setError(tr("Unsupported or invalid URI: %1").arg(uri));
            return;
        }
        if (!QDesktopServices::openUrl(url))
            setError(tr("Could not open URI: %1").arg(uri));
    }

    Q_INVOKABLE void clearTagHistory()
    {
        if (m_tagPresent)
            return;
        m_hasTagData = false;
        m_tagType.clear();
        m_tagProtocol.clear();
        m_tagUid.clear();
        m_tagReadOnly = false;
        m_records.clear();
        setStatusMessage({});
        Q_EMIT tagChanged();
    }

    Q_INVOKABLE void clearError()
    {
        setError({});
    }

Q_SIGNALS:
    void stateChanged();
    void tagChanged();
    void writingChanged();
    void statusMessageChanged();
    void errorChanged();

private Q_SLOTS:
    void onInterfacesAdded(const QDBusObjectPath &, const InterfaceMap &)
    {
        refresh();
    }

    void onInterfacesRemoved(const QDBusObjectPath &path, const QStringList &interfaces)
    {
        if (path.path() == m_tagPath && interfaces.contains(QString::fromLatin1(TAG_IFACE))) {
            m_tagPresent = false;
            m_tagPath.clear();
            Q_EMIT tagChanged();
        }
        refresh();
    }

    void onPropertiesChanged(const QString &interface, const QVariantMap &changed,
                             const QStringList &, const QDBusMessage &msg)
    {
        if (interface == QLatin1String(ADAPTER_IFACE) && !m_adapterPath.isEmpty()
            && msg.path() == m_adapterPath) {
            bool changedAny = false;
            if (changed.contains(QStringLiteral("Powered"))) {
                m_powered = changed.value(QStringLiteral("Powered")).toBool();
                syncSleepInhibitor();
                changedAny = true;
            }
            if (changed.contains(QStringLiteral("Polling"))) {
                m_polling = changed.value(QStringLiteral("Polling")).toBool();
                changedAny = true;
            }
            if (changed.contains(QStringLiteral("Mode"))) {
                m_mode = changed.value(QStringLiteral("Mode")).toString();
                changedAny = true;
            }
            if (changedAny)
                Q_EMIT stateChanged();
            return;
        }

        if (!m_tagPath.isEmpty()
            && ((interface == QLatin1String(TAG_IFACE) && msg.path() == m_tagPath)
                || (interface == QLatin1String(RECORD_IFACE) && msg.path().startsWith(m_tagPath + QLatin1Char('/'))))) {
            refresh();
        }
    }

    void onNameOwnerChanged(const QString &name, const QString &, const QString &newOwner)
    {
        if (name != QLatin1String(NEARD_SERVICE))
            return;
        if (newOwner.isEmpty()) {
            m_sleepPowerOffInFlight = false;
            releaseSleepInhibitor();
            resetAdapterState();
            setDaemonError(tr("NFC daemon stopped"));
            return;
        }
        if (m_daemonError)
            setError({});
        refresh();
    }

    void onPrepareForSleep(bool goingToSleep)
    {
        m_sleeping = goingToSleep;
        if (goingToSleep) {
            // Turning the adapter off on sleep prevents holding PN547 VEN and RPMH_LN_BB_CLK2 active in s2idle.
            if (m_available && m_powered) {
                m_restoreOnWake = true;
                m_restorePollOnWake = m_polling;
                m_desiredPowered = false;
                m_desiredPolling = false;
                m_sleepPowerOffInFlight = true;
                if (m_polling)
                    stopScanAndPowerOff();
                else
                    sendSetPowered(false, false);
            } else {
                releaseSleepInhibitor();
            }
            return;
        }

        if (m_restoreOnWake) {
            const bool resumePoll = m_restorePollOnWake;
            m_restoreOnWake = false;
            m_restorePollOnWake = false;
            m_desiredPowered = true;
            m_desiredPolling = resumePoll;
            sendSetPowered(true, resumePoll);
        } else if (m_powered) {
            acquireSleepInhibitor();
        }
    }

private:
    void resetAdapterState()
    {
        m_available = false;
        m_powered = false;
        m_polling = false;
        m_mode = QStringLiteral("Idle");
        m_protocols.clear();
        m_adapterPath.clear();
        m_tagPresent = false;
        m_tagPath.clear();
        Q_EMIT stateChanged();
        Q_EMIT tagChanged();
    }

    void applyManagedObjects(const ManagedObjectMap &objects)
    {
        QString adapterPath;
        QVariantMap adapterProps;
        QString tagPath;
        QVariantMap tagProps;

        for (auto it = objects.cbegin(); it != objects.cend(); ++it) {
            const QString path = it.key().path();
            const InterfaceMap &ifaces = it.value();
            if (adapterPath.isEmpty() && ifaces.contains(QString::fromLatin1(ADAPTER_IFACE))) {
                adapterPath = path;
                adapterProps = ifaces.value(QString::fromLatin1(ADAPTER_IFACE));
            }
        }

        const bool available = !adapterPath.isEmpty();
        const bool powered = adapterProps.value(QStringLiteral("Powered")).toBool();
        const bool polling = adapterProps.value(QStringLiteral("Polling")).toBool();
        const QString mode = adapterProps.value(QStringLiteral("Mode"), QStringLiteral("Idle")).toString();
        const QStringList protocols = adapterProps.value(QStringLiteral("Protocols")).toStringList();

        if (available) {
            const QString prefix = adapterPath + QLatin1Char('/');
            for (auto it = objects.cbegin(); it != objects.cend(); ++it) {
                const QString path = it.key().path();
                if (path.startsWith(prefix) && it.value().contains(QString::fromLatin1(TAG_IFACE))) {
                    tagPath = path;
                    tagProps = it.value().value(QString::fromLatin1(TAG_IFACE));
                    break;
                }
            }
        }

        const bool adapterChanged = (m_available != available || m_adapterPath != adapterPath
            || m_powered != powered || m_polling != polling || m_mode != mode
            || m_protocols != protocols);

        m_available = available;
        m_adapterPath = adapterPath;
        m_powered = powered;
        m_polling = polling;
        m_mode = mode;
        m_protocols = protocols;
        syncSleepInhibitor();
        if (m_available && m_daemonError)
            setError({});

        bool tagUpdated = false;
        if (!tagPath.isEmpty()) {
            QVariantList newRecords;
            const QString recordPrefix = tagPath + QLatin1Char('/');
            for (auto it = objects.cbegin(); it != objects.cend(); ++it) {
                const QString path = it.key().path();
                if (path.startsWith(recordPrefix) && it.value().contains(QString::fromLatin1(RECORD_IFACE)))
                    newRecords.append(buildRecordEntry(path, it.value().value(QString::fromLatin1(RECORD_IFACE))));
            }

            const QString newType = tagProps.value(QStringLiteral("Type")).toString();
            const QString newProtocol = tagProps.value(QStringLiteral("Protocol")).toString();
            const QString newUid = QString::fromLatin1(
                tagProps.value(QStringLiteral("Uid")).toByteArray().toHex(':')).toUpper();
            const bool newReadOnly = tagProps.value(QStringLiteral("ReadOnly")).toBool();

            tagUpdated = (!m_tagPresent || m_tagPath != tagPath || !m_hasTagData
                || m_tagType != newType || m_tagProtocol != newProtocol
                || m_tagUid != newUid || m_tagReadOnly != newReadOnly
                || m_records != newRecords);

            m_tagPresent = true;
            m_tagPath = tagPath;
            m_hasTagData = true;
            m_tagType = newType;
            m_tagProtocol = newProtocol;
            m_tagUid = newUid;
            m_tagReadOnly = newReadOnly;
            m_records = newRecords;
        } else if (m_tagPresent) {
            // Retain lastTag fields after removal so the user can still copy or open read NDEF records.
            m_tagPresent = false;
            m_tagPath.clear();
            tagUpdated = true;
        }

        if (adapterChanged)
            Q_EMIT stateChanged();
        if (tagUpdated)
            Q_EMIT tagChanged();
    }

    void stopScanAndPowerOff()
    {
        QDBusMessage stopMsg = QDBusMessage::createMethodCall(
            QString::fromLatin1(NEARD_SERVICE), m_adapterPath,
            QString::fromLatin1(ADAPTER_IFACE), QStringLiteral("StopPollLoop"));
        auto *watcher = new QDBusPendingCallWatcher(m_bus.asyncCall(stopMsg), this);
        connect(watcher, &QDBusPendingCallWatcher::finished, this, [this](QDBusPendingCallWatcher *w) {
            w->deleteLater();
            if (m_desiredPowered) {
                if (m_sleepPowerOffInFlight) {
                    m_sleepPowerOffInFlight = false;
                    syncSleepInhibitor();
                }
                return;
            }
            sendSetPowered(false, false);
        });
    }

    void sendSetPowered(bool on, bool startPollingOnSuccess)
    {
        if (m_adapterPath.isEmpty()) {
            if (!on && m_sleepPowerOffInFlight) {
                m_sleepPowerOffInFlight = false;
                releaseSleepInhibitor();
            }
            return;
        }

        QDBusMessage msg = QDBusMessage::createMethodCall(
            QString::fromLatin1(NEARD_SERVICE), m_adapterPath,
            QString::fromLatin1(PROPS_IFACE), QStringLiteral("Set"));
        msg << QString::fromLatin1(ADAPTER_IFACE)
            << QStringLiteral("Powered")
            << QVariant::fromValue(QDBusVariant(on));

        auto *watcher = new QDBusPendingCallWatcher(m_bus.asyncCall(msg), this);
        connect(watcher, &QDBusPendingCallWatcher::finished, this,
                [this, on, startPollingOnSuccess](QDBusPendingCallWatcher *w) {
            w->deleteLater();
            QDBusPendingReply<> reply = *w;
            if (!on && m_sleepPowerOffInFlight) {
                m_sleepPowerOffInFlight = false;
                releaseSleepInhibitor();
            }
            if (!isBenignPowerReply(reply.error(), on)) {
                setError(tr("Could not change NFC power: %1").arg(reply.error().message()));
                refresh();
                return;
            }
            if (on && startPollingOnSuccess && m_desiredPolling)
                startScan(QStringLiteral("Initiator"));
            else
                refresh();
        });
    }

    void writeRecord(const QVariantMap &attrs, const QString &successText)
    {
        if (!m_tagPresent || m_tagPath.isEmpty()) {
            setError(tr("Hold a writable tag against the phone to write"));
            return;
        }
        if (m_tagReadOnly) {
            setError(tr("This tag is read-only"));
            return;
        }
        if (m_writing)
            return;

        setError({});
        setStatusMessage({});
        m_writing = true;
        Q_EMIT writingChanged();

        QDBusMessage msg = QDBusMessage::createMethodCall(
            QString::fromLatin1(NEARD_SERVICE), m_tagPath,
            QString::fromLatin1(TAG_IFACE), QStringLiteral("Write"));
        msg << attrs;

        auto *watcher = new QDBusPendingCallWatcher(m_bus.asyncCall(msg), this);
        connect(watcher, &QDBusPendingCallWatcher::finished, this,
                [this, successText](QDBusPendingCallWatcher *w) {
            w->deleteLater();
            m_writing = false;
            Q_EMIT writingChanged();

            QDBusPendingReply<> reply = *w;
            if (reply.isError()) {
                setError(tr("Failed to write tag: %1").arg(reply.error().message()));
                return;
            }
            setStatusMessage(successText);
            refresh();
        });
    }

    void acquireSleepInhibitor()
    {
        if (!m_powered || m_sleeping || m_sleepPowerOffInFlight
            || m_sleepInhibitor.isValid() || m_sleepInhibiting)
            return;

        m_sleepInhibiting = true;
        QDBusMessage msg = QDBusMessage::createMethodCall(
            QString::fromLatin1(LOGIN1_SERVICE),
            QString::fromLatin1(LOGIN1_PATH),
            QString::fromLatin1(LOGIN1_MANAGER_IFACE),
            QStringLiteral("Inhibit"));
        msg << QStringLiteral("sleep")
            << QStringLiteral("alphaplus-nfc")
            << QStringLiteral("Power off NFC before sleep")
            << QStringLiteral("delay");

        auto *watcher = new QDBusPendingCallWatcher(m_bus.asyncCall(msg), this);
        connect(watcher, &QDBusPendingCallWatcher::finished, this, [this](QDBusPendingCallWatcher *w) {
            w->deleteLater();
            m_sleepInhibiting = false;
            QDBusPendingReply<QDBusUnixFileDescriptor> reply = *w;
            if (reply.isError())
                return;
            if (m_powered && !m_sleeping && !m_sleepPowerOffInFlight)
                m_sleepInhibitor = reply.value();
        });
    }

    void releaseSleepInhibitor()
    {
        m_sleepInhibitor = QDBusUnixFileDescriptor();
    }

    void syncSleepInhibitor()
    {
        if (m_powered && !m_sleeping && !m_sleepPowerOffInFlight)
            acquireSleepInhibitor();
        else if (!m_sleepPowerOffInFlight)
            releaseSleepInhibitor();
    }

    void setStatusMessage(const QString &message)
    {
        if (m_statusMessage == message)
            return;
        m_statusMessage = message;
        Q_EMIT statusMessageChanged();
    }

    void setDaemonError(const QString &error)
    {
        m_daemonError = !error.isEmpty();
        if (m_error == error)
            return;
        m_error = error;
        Q_EMIT errorChanged();
    }

    void setError(const QString &error)
    {
        m_daemonError = false;
        if (m_error == error)
            return;
        m_error = error;
        Q_EMIT errorChanged();
    }

    QDBusConnection m_bus;
    QDBusUnixFileDescriptor m_sleepInhibitor;
    bool m_available = false;
    bool m_powered = false;
    bool m_polling = false;
    bool m_desiredPowered = false;
    bool m_desiredPolling = false;
    bool m_sleeping = false;
    bool m_sleepInhibiting = false;
    bool m_sleepPowerOffInFlight = false;
    bool m_restoreOnWake = false;
    bool m_restorePollOnWake = false;
    bool m_tagPresent = false;
    bool m_hasTagData = false;
    bool m_tagReadOnly = false;
    bool m_writing = false;
    bool m_daemonError = false;
    QString m_adapterPath;
    QString m_mode = QStringLiteral("Idle");
    QStringList m_protocols;
    QString m_tagPath;
    QString m_tagType;
    QString m_tagProtocol;
    QString m_tagUid;
    QVariantList m_records;
    QString m_statusMessage;
    QString m_error;
};

class NfcPlugin : public QQmlExtensionPlugin
{
    Q_OBJECT
    Q_PLUGIN_METADATA(IID QQmlExtensionInterface_iid)

public:
    void registerTypes(const char *uri) override
    {
        qDBusRegisterMetaType<InterfaceMap>();
        qDBusRegisterMetaType<ManagedObjectMap>();
        qmlRegisterType<NfcController>(uri, 1, 0, "NfcController");
    }
};

#include "nfc.moc"
