import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from PySide6.QtCore import QCoreApplication, QEvent
from PySide6.QtWidgets import QSystemTrayIcon
from PySide6.QtTest import QTest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app import App
from tray import TrayIcon
from storage import Store, new_account
from test_gui import APPLICATION, pump


class FakeTray:
    def __init__(self, emit, parent=None):
        self.emit = emit
        self.visible = False
        self.closed = False
        self.shows = 0
        self.notifications = []

    def show(self):
        self.visible = True
        self.shows += 1

    def notify(self, title, message):
        self.notifications.append((title, message))

    def hide(self):
        self.visible = False

    def close(self):
        self.closed = True
        self.visible = False


class TrayTests(unittest.TestCase):
    def test_exit_waits_for_import_and_cancels_unapproved_preview(self):
        path = Path(self.folder.name) / "import.txt"
        path.write_text("mock-import", encoding="utf-8")
        self.window.start_import(path)
        self.window.request_exit()
        self.assertTrue(self.window.closing)
        pump(lambda: self.window._destroyed)
        self.assertFalse(
            any(
                a["openid"] == "mock-import"
                for a in Store(self.folder.name).data["accounts"]
            )
        )

    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.factory = patch("qt_ui.TrayIcon", FakeTray)
        self.factory.start()
        self.store = Store(self.folder.name)
        self.store.data["accounts"] = [new_account("mock-tray", "模拟账号")]
        self.window = App(self.store, demo=True)
        self.window.timer.stop()
        self.window.show()
        QTest.qWait(30)
        self.network = patch(
            "requests.Session.request",
            side_effect=AssertionError("Real network forbidden"),
        )
        self.network.start()

    def tearDown(self):
        if self.window.busy:
            self.window.cancel_run()
            pump(lambda: not self.window.busy)
        if self.window.import_worker:
            pump(lambda: self.window.import_worker is None)
        self.window.shutdown()
        pump(lambda: self.window._destroyed)
        self.window.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        APPLICATION.processEvents()
        self.factory.stop()
        self.network.stop()
        self.folder.cleanup()

    def test_x_hides_restores_and_reuses_icon_without_cancelling(self):
        self.window.close()
        tray = self.window.tray
        self.assertFalse(self.window.isVisible())
        self.assertTrue(tray.visible)
        self.assertFalse(self.window.closing)
        self.assertFalse(self.window.cancel.is_set())
        tray.emit("tray_action", "restore")
        self.assertTrue(self.window.isVisible())
        self.assertFalse(tray.visible)
        self.window.close()
        self.assertIs(self.window.tray, tray)
        self.assertEqual(tray.shows, 2)
        self.assertEqual(len(tray.notifications), 0)
        self.assertTrue(Store(self.folder.name).data["appearance"]["tray_notice_shown"])
        self.window.restore_window()
        self.window.request_exit()
        pump(lambda: self.window._destroyed)
        self.assertTrue(tray.closed)

    def test_hidden_manual_preview_restores_and_task_persists(self):
        self.window.start_run()
        self.window.on_close()
        self.assertFalse(self.window.isVisible())
        pump(lambda: self.window.preview_dialog is not None)
        self.assertFalse(self.window.isVisible())
        self.assertFalse(self.window.preview_dialog.isVisible())
        self.window.restore_window()
        self.assertTrue(self.window.preview_dialog.isVisible())
        self.window.preview_dialog.accept()
        pump(lambda: not self.window.busy)
        self.assertEqual(self.store.data["results"][0]["status"], "success")

    def test_hidden_timer_remains_hidden_and_saves_results(self):
        self.window.on_close()
        self.store.data["schedule"].update(
            enabled=True, time="00:00", last_trigger=None
        )
        self.window.tick()
        pump(lambda: not self.window.busy)
        self.assertFalse(self.window.isVisible())
        self.assertTrue(self.window.tray.visible)
        self.assertEqual(
            Store(self.folder.name).data["results"][0]["status"], "success"
        )

    def test_exit_waits_for_cancel_and_saves_before_destroy(self):
        self.window.start_run()
        self.window.preview_dialog.accept()
        self.window.on_close()
        tray = self.window.tray
        with patch.object(self.window, "confirm", return_value=True):
            self.window.request_exit()
        self.assertTrue(self.window.closing)
        self.assertFalse(self.window._destroyed)
        pump(lambda: self.window._destroyed)
        self.assertEqual(
            Store(self.folder.name).data["results"][0]["status"], "cancelled"
        )
        self.assertTrue(tray.closed)
        self.assertTrue(list(Path(self.folder.name).rglob("*.log")))

    def test_exit_declined_preserves_running_task(self):
        self.window.start_run()
        self.window.preview_dialog.accept()
        with patch.object(self.window, "confirm", return_value=False):
            self.window.request_exit()
        self.assertFalse(self.window.closing)
        self.assertFalse(self.window.cancel.is_set())
        self.window.cancel_run()
        pump(lambda: not self.window.busy)

    def test_tray_failure_keeps_window_actionable(self):
        with patch.object(
            FakeTray, "show", side_effect=RuntimeError("托盘不可用，请保留窗口。")
        ):
            self.window.on_close()
        self.assertTrue(self.window.isVisible())
        self.assertIsNone(self.window.tray)
        self.assertIn("保留窗口", self.window.notice.text())

    def test_qt_tray_menu_and_activation_callbacks_and_cleanup(self):
        events = []
        tray = TrayIcon(lambda *event: events.append(event), self.window)
        with patch.object(QSystemTrayIcon, "isSystemTrayAvailable", return_value=True):
            tray.show()
        self.assertTrue(tray.icon.isVisible())
        tray.restore_action.trigger()
        tray.exit_action.trigger()
        tray.icon.activated.emit(QSystemTrayIcon.ActivationReason.Trigger)
        tray.icon.activated.emit(QSystemTrayIcon.ActivationReason.DoubleClick)
        self.assertEqual(
            events,
            [
                ("tray_action", "restore"),
                ("tray_action", "exit"),
                ("tray_action", "restore"),
                ("tray_action", "restore"),
            ],
        )
        tray.hide()
        self.assertFalse(tray.icon.isVisible())
        tray.close()
        tray.close()
        self.assertTrue(tray.closed)

    def test_unavailable_native_tray_reports_reason(self):
        tray = TrayIcon(lambda *args: None, self.window)
        with patch.object(QSystemTrayIcon, "isSystemTrayAvailable", return_value=False):
            with self.assertRaisesRegex(RuntimeError, "系统托盘不可用"):
                tray.show()
        tray.close()

    def test_automatic_does_not_wait_for_hidden_manual_preview(self):
        self.window.start_run()
        self.window.on_close()
        self.store.data["schedule"].update(
            enabled=True, time="00:00", last_trigger=None
        )
        self.window.tick()
        self.assertIsNone(self.window.pending_request)
        pump(lambda: not self.window.busy)
        self.assertFalse(self.window.isVisible())
        self.assertEqual(self.store.data["results"][0]["status"], "success")

    def test_close_exit_preference_persists_and_cleans_up(self):
        self.window.close_behavior.setCurrentIndex(
            self.window.close_behavior.findData("exit")
        )
        self.assertEqual(
            Store(self.folder.name).data["appearance"]["close_behavior"], "exit"
        )
        self.window.on_close()
        pump(lambda: self.window._destroyed)
        self.assertIsNone(self.window.tray)


if __name__ == "__main__":
    unittest.main()
