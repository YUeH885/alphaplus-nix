// SPDX-License-Identifier: GPL-2.0-or-later
#include <QFile>
#include <QFileInfo>
#include <QQmlExtensionPlugin>
#include <QRegularExpression>
#include <QStringList>
#include <qqml.h>

namespace {
const auto chargeBehaviourPath = QStringLiteral("/sys/class/power_supply/battery/charge_behaviour");

QStringList readTokens(const QString &path, QString *rawOut = nullptr)
{
    QFile file(path);
    if (!file.open(QIODevice::ReadOnly))
        return {};

    const QString text = QString::fromUtf8(file.readAll()).trimmed();
    if (rawOut)
        *rawOut = text;
    if (text.isEmpty())
        return {};

    return text.split(QRegularExpression(QStringLiteral("\\s+")), Qt::SkipEmptyParts);
}
}

class BypassCharging : public QObject
{
    Q_OBJECT
    Q_PROPERTY(bool available READ available NOTIFY stateChanged)
    Q_PROPERTY(bool active READ active NOTIFY stateChanged)
    Q_PROPERTY(QString error READ error NOTIFY errorChanged)

public:
    explicit BypassCharging(QObject *parent = nullptr) : QObject(parent)
    {
        refresh();
    }

    bool available() const { return m_available; }
    bool active() const { return m_active; }
    QString error() const { return m_error; }

    Q_INVOKABLE void refresh()
    {
        const QStringList tokens = readTokens(chargeBehaviourPath);
        const bool supportsInhibit = tokens.contains(QStringLiteral("inhibit-charge"))
            || tokens.contains(QStringLiteral("[inhibit-charge]"));
        const bool available = supportsInhibit && QFileInfo(chargeBehaviourPath).isWritable();
        const bool active = tokens.contains(QStringLiteral("[inhibit-charge]"));

        if (m_available == available && m_active == active)
            return;

        m_available = available;
        m_active = active;
        Q_EMIT stateChanged();
    }

    Q_INVOKABLE void toggle()
    {
        refresh();
        if (!m_available) {
            setError(tr("Bypass charging is unavailable"));
            return;
        }

        const QByteArray target = m_active ? QByteArrayLiteral("auto\n")
                                           : QByteArrayLiteral("inhibit-charge\n");
        QFile file(chargeBehaviourPath);
        if (!file.open(QIODevice::WriteOnly | QIODevice::Unbuffered)
            || file.write(target) != target.size()) {
            setError(tr("Could not set bypass charging: %1").arg(file.errorString()));
            return;
        }

        setError({});
        refresh();
    }

Q_SIGNALS:
    void stateChanged();
    void errorChanged();

private:
    void setError(const QString &error)
    {
        if (m_error == error)
            return;

        m_error = error;
        Q_EMIT errorChanged();
    }

    bool m_available = false;
    bool m_active = false;
    QString m_error;
};

class BypassChargingPlugin : public QQmlExtensionPlugin
{
    Q_OBJECT
    Q_PLUGIN_METADATA(IID QQmlExtensionInterface_iid)

public:
    void registerTypes(const char *uri) override
    {
        qmlRegisterType<BypassCharging>(uri, 1, 0, "BypassCharging");
    }
};

#include "bypass-charging.moc"
