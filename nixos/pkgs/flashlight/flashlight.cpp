// SPDX-License-Identifier: GPL-2.0-or-later
#include <QFile>
#include <QFileInfo>
#include <QQmlExtensionPlugin>
#include <qqml.h>

namespace {
struct FlashChannel {
    QString brightnessPath;
    QString maximumPath;
};

/*
 * Both PM8150L flash channels drive the rear white LED package in parallel,
 * so toggling both together delivers the full 200 mA factory torch output.
 */
const FlashChannel channels[] = {
    {
        QStringLiteral("/sys/class/leds/white:flash/brightness"),
        QStringLiteral("/sys/class/leds/white:flash/max_brightness"),
    },
    {
        QStringLiteral("/sys/class/leds/yellow:flash/brightness"),
        QStringLiteral("/sys/class/leds/yellow:flash/max_brightness"),
    },
};

int readBrightness(const QString &path)
{
    QFile file(path);
    if (!file.open(QIODevice::ReadOnly))
        return -1;

    bool valid;
    const int value = file.readAll().trimmed().toInt(&valid);
    return valid ? value : -1;
}
}

class Flashlight : public QObject
{
    Q_OBJECT
    Q_PROPERTY(bool available READ available NOTIFY stateChanged)
    Q_PROPERTY(bool active READ active NOTIFY stateChanged)
    Q_PROPERTY(QString error READ error NOTIFY errorChanged)

public:
    explicit Flashlight(QObject *parent = nullptr) : QObject(parent)
    {
        refresh();
    }

    bool available() const { return m_available; }
    bool active() const { return m_active; }
    QString error() const { return m_error; }

    Q_INVOKABLE void refresh()
    {
        bool available = false;
        bool active = false;
        for (const auto &ch : channels) {
            const int brightness = readBrightness(ch.brightnessPath);
            if (brightness >= 0 && QFileInfo(ch.brightnessPath).isWritable()) {
                available = true;
                if (brightness > 0)
                    active = true;
            }
        }
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
            setError(tr("Flashlight is unavailable"));
            return;
        }

        const bool turnOn = !m_active;
        bool anyWritten = false;
        QString lastError;

        for (const auto &ch : channels) {
            if (!QFileInfo(ch.brightnessPath).isWritable())
                continue;

            const int brightness = turnOn ? readBrightness(ch.maximumPath) : 0;
            if (turnOn && brightness <= 0) {
                lastError = tr("Could not read flashlight brightness");
                continue;
            }

            const auto value = QByteArray::number(brightness) + '\n';
            QFile file(ch.brightnessPath);
            if (!file.open(QIODevice::WriteOnly | QIODevice::Unbuffered)
                || file.write(value) != value.size()) {
                lastError = tr("Could not set flashlight: %1").arg(file.errorString());
                continue;
            }
            anyWritten = true;
        }

        if (!anyWritten) {
            setError(lastError.isEmpty() ? tr("Flashlight is unavailable") : lastError);
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

class FlashlightPlugin : public QQmlExtensionPlugin
{
    Q_OBJECT
    Q_PLUGIN_METADATA(IID QQmlExtensionInterface_iid)

public:
    void registerTypes(const char *uri) override
    {
        qmlRegisterType<Flashlight>(uri, 1, 0, "Flashlight");
    }
};

#include "flashlight.moc"
