"""Native Qt tray. Callbacks execute on the GUI thread."""

from PySide6.QtWidgets import QMenu, QSystemTrayIcon
from ui_style import icon


class TrayIcon:
    def __init__(self, emit, parent=None):
        self.icon = QSystemTrayIcon(icon("check", "#BBD7CE", 32), parent)
        self.icon.setToolTip("平安签到 · 后台运行")
        self.menu = QMenu(parent)
        self.restore_action = self.menu.addAction(icon("overview"), "显示窗口")
        self.menu.addSeparator()
        self.exit_action = self.menu.addAction(icon("exit"), "退出软件")
        self.restore_action.triggered.connect(lambda: emit("tray_action", "restore"))
        self.exit_action.triggered.connect(lambda: emit("tray_action", "exit"))
        self.icon.activated.connect(
            lambda reason: (
                emit("tray_action", "restore")
                if reason
                in (
                    QSystemTrayIcon.ActivationReason.Trigger,
                    QSystemTrayIcon.ActivationReason.DoubleClick,
                )
                else None
            )
        )
        self.icon.setContextMenu(self.menu)
        self.icon.messageClicked.connect(lambda: emit("tray_action", "notification"))
        self.closed = False

    def show(self):
        if self.closed:
            raise RuntimeError("托盘已停止，请重新启动软件。")
        if not QSystemTrayIcon.isSystemTrayAvailable():
            raise RuntimeError("系统托盘不可用，请保留窗口或通过设置页面退出。")
        self.icon.show()

    def hide(self):
        self.icon.hide()

    def notify(self, title, message):
        self.icon.showMessage(
            title, message, QSystemTrayIcon.MessageIcon.Information, 6000
        )

    def close(self):
        if self.closed:
            return
        self.closed = True
        self.icon.hide()
        self.icon.setContextMenu(None)
        self.menu.close()
        self.icon.deleteLater()
        self.menu.deleteLater()
