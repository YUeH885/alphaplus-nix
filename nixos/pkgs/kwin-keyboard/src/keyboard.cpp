// SPDX-License-Identifier: GPL-2.0-or-later

#include <inputmethod.h>
#include <main.h>
#include <plugin.h>
#include <window.h>
#include <workspace.h>

#include <algorithm>
#include <cmath>
#include <utility>
#include <vector>

using namespace KWin;

namespace
{

bool isKeyboard(const Window *window)
{
    return window && window->resourceClass() == QStringLiteral("fcitx5-osk");
}

bool isFloating(const Window *window)
{
    return window && !window->isDeleted() && window->isMovable()
        && (window->isNormalWindow() || window->isDialog())
        && !window->isFullScreen() && window->maximizeMode() == MaximizeRestore && !window->tile()
        && !window->isInteractiveMove() && !window->isInteractiveResize();
}

class KeyboardAvoidance final : public Plugin
{
public:
    explicit KeyboardAvoidance(InputMethod *inputMethod)
        : m_inputMethod(inputMethod)
    {
        connect(inputMethod, &InputMethod::activeChanged, this, &KeyboardAvoidance::refreshCursor, Qt::QueuedConnection);
        connect(inputMethod, &InputMethod::activeWindowChanged, this, &KeyboardAvoidance::refreshCursor, Qt::QueuedConnection);
        connect(inputMethod, &InputMethod::cursorRectangleChanged, this, &KeyboardAvoidance::refreshCursor, Qt::QueuedConnection);

        auto *ws = workspace();
        connect(ws, &Workspace::windowActivated, this, &KeyboardAvoidance::refreshCursor, Qt::QueuedConnection);
        connect(ws, &Workspace::geometryChanged, this, &KeyboardAvoidance::resetOriginalAndAdjust);
        connect(ws, &Workspace::outputsChanged, this, &KeyboardAvoidance::resetOriginalAndAdjust);
        connect(ws, &Workspace::windowAdded, this, [this](Window *window) {
            if (isKeyboard(window)) {
                setKeyboard(window);
            }
        });
        connect(ws, &Workspace::windowRemoved, this, [this](Window *window) {
            if (m_keyboard == window) {
                setKeyboard(nullptr);
            }
        });

        for (auto *window : ws->windows()) {
            if (isKeyboard(window)) {
                setKeyboard(window);
                break;
            }
        }
        refreshCursor();
    }

    ~KeyboardAvoidance() override
    {
        restore();
    }

private:
    void setKeyboard(Window *keyboard)
    {
        disconnect(m_panelGeometryChanged);
        m_keyboard = keyboard;
        if (keyboard) {
            m_panelGeometryChanged = connect(keyboard, &Window::frameGeometryChanged, this, &KeyboardAvoidance::adjust);
        }
        adjust();
    }

    void refreshCursor()
    {
        auto *window = workspace()->activeWindow();
        if (m_window != window) {
            restore();
            for (const auto &connection : std::exchange(m_windowConnections, {})) {
                disconnect(connection);
            }
            m_window = window;
            m_cursor = RectF();
            if (window) {
                m_windowConnections = {
                    connect(window, &Window::interactiveMoveResizeStarted, this, [this, window] {
                        if (window->isInteractiveMove()) {
                            m_originalY.reset();
                        }
                    }),
                    connect(window, &Window::interactiveMoveResizeFinished, this, &KeyboardAvoidance::adjust),
                    connect(window, &Window::maximizedChanged, this, &KeyboardAvoidance::adjust),
                    connect(window, &Window::tileChanged, this, &KeyboardAvoidance::adjust),
                    connect(window, &Window::fullScreenChanged, this, &KeyboardAvoidance::adjust),
                };
            }
        }

        const bool hasInput = window && m_inputMethod->isActive()
            && m_inputMethod->activeWindow() == window;
        const auto cursor = hasInput ? m_inputMethod->cursorRectangle() : RectF();
        const auto relative = cursor.height() > 0 ? cursor.translated(-window->frameGeometry().topLeft()) : RectF();

        // KWin also emits cursorRectangleChanged for frame moves, including our own.
        if (m_cursor == relative) {
            return;
        }
        m_cursor = relative;
        adjust();
    }

    void resetOriginalAndAdjust()
    {
        // Saved global Y coordinates belong to the previous screen geometry/output layout.
        m_originalY.reset();
        adjust();
    }

    void adjust()
    {
        if (!m_keyboard) {
            restore();
            return;
        }
        if (m_window && (m_window->isInteractiveMove() || m_window->isInteractiveResize())) {
            return;
        }
        if (!isFloating(m_window)) {
            restore();
            return;
        }
        if (m_cursor.height() <= 0) {
            // Keep an already-shifted window raised while it stays active and the keyboard is open.
            return;
        }
        const auto frame = m_window->frameGeometry();
        const auto panel = m_keyboard->frameGeometry();
        const qreal originalY = m_originalY.value_or(frame.y());
        const auto cursor = m_cursor.translated(frame.x(), originalY);
        if (panel.isEmpty() || cursor.left() >= panel.right()
            || cursor.left() + std::max<qreal>(1, cursor.width()) <= panel.left()
            || cursor.top() >= panel.bottom() || cursor.bottom() <= panel.top()) {
            restore();
            return;
        }

        m_originalY = originalY;
        moveVertically(std::floor(panel.top() - m_cursor.bottom()));
    }

    void restore()
    {
        const auto originalY = std::exchange(m_originalY, std::nullopt);
        if (originalY && isFloating(m_window)) {
            moveVertically(*originalY);
        }
    }

    void moveVertically(qreal y)
    {
        const auto frame = m_window->frameGeometry();
        if (frame.y() != y) {
            m_window->move(QPointF(frame.x(), y));
        }
    }

    InputMethod *const m_inputMethod;
    QPointer<Window> m_keyboard;
    QPointer<Window> m_window;
    QMetaObject::Connection m_panelGeometryChanged;
    std::vector<QMetaObject::Connection> m_windowConnections;
    RectF m_cursor;
    std::optional<qreal> m_originalY;
};

} // namespace

class KeyboardAvoidanceFactory final : public PluginFactory
{
    Q_OBJECT
    Q_PLUGIN_METADATA(IID PluginFactory_iid FILE "keyboard.json")
    Q_INTERFACES(KWin::PluginFactory)

public:
    std::unique_ptr<Plugin> create() const override
    {
        auto *inputMethod = kwinApp()->inputMethod();
        return inputMethod ? std::make_unique<KeyboardAvoidance>(inputMethod) : nullptr;
    }
};

#include "keyboard.moc"
