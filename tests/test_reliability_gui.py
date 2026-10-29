import copy
import json
import sys
import tempfile
import threading
from pathlib import Path
from unittest.mock import patch, Mock
import unittest
from PySide6.QtCore import QCoreApplication, QEvent
from PySide6.QtWidgets import QApplication, QDialog
from PySide6.QtTest import QTest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app import App, create_application
from storage import Store, new_account
from runner import run_accounts
from data_management import restore_backup, create_backup, read_backup
from test_gui import pump
from test_reliability import MultiClient
from test_tray import FakeTray

APPLICATION = create_application()


class ReliabilityGuiTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.folder.name) / "data")
        self.store.data["accounts"] = [new_account("mock-a"), new_account("mock-b")]
        self.window = App(self.store, demo=True)
        self.window.timer.stop()
        self.window.show()
        self.network = patch(
            "requests.Session.request", side_effect=AssertionError("offline only")
        )
        self.network.start()
        MultiClient.task_ids = ["alpha", "beta"]
        MultiClient.days = {}
        MultiClient.windows = {}
        MultiClient.posted = []
        MultiClient.signed = set()
        MultiClient.on_status = None
        QTest.qWait(20)

    def tearDown(self):
        if self.window.busy:
            self.window.cancel_run()
            pump(lambda: not self.window.busy)
        if self.window.data_job:
            pump(lambda: self.window.data_job is None)
        if self.window.import_worker:
            pump(lambda: self.window.import_worker is None)
        self.window.pending_results = None
        self.window.journal_error = None
        self.window.shutdown()
        pump(lambda: self.window._destroyed)
        self.window.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        QApplication.processEvents()
        self.network.stop()
        self.folder.cleanup()

    def test_preferences_defaults_and_persistence(self):
        self.assertFalse(self.store.data["reliability"]["autostart"])
        self.assertFalse(self.window.start_hidden.isChecked())
        self.assertFalse(self.window.prevent_sleep.isChecked())
        self.assertTrue(self.window.notification_enabled.isChecked())
        self.assertFalse(self.window.notification_success.isChecked())
        self.window.notification_enabled.setChecked(False)
        self.window.start_hidden.setChecked(True)
        self.window.prevent_sleep.setChecked(True)
        loaded = Store(self.store.directory)
        self.assertFalse(loaded.data["notifications"]["enabled"])
        self.assertTrue(loaded.data["reliability"]["start_hidden"])
        self.assertTrue(loaded.data["reliability"]["prevent_sleep"])

    def test_new_user_guide_skips_without_enabling_schedule_existing_user_not_bothered(
        self,
    ):
        self.assertFalse(self.store.data["onboarding_done"])
        self.window.dismiss_onboarding()
        self.assertTrue(self.store.data["onboarding_done"])
        self.assertFalse(self.store.data["schedule"]["enabled"])
        loaded = Store(self.store.directory)
        self.assertTrue(loaded.data["onboarding_done"])

    def test_readiness_background_check_shows_saved_plan_and_missing_location(self):
        self.store.data["accounts"][0]["location"] = {
            "mode": "specified",
            "location_id": "missing",
        }
        self.window.inspect_readiness()
        pump(lambda: self.window.data_job is None)
        self.assertIn("计划未启用", self.window.readiness_label.text())
        self.assertIn("指定位置不存在", self.window.readiness_label.text())

    def test_manual_task_selector_is_nonblocking_and_cancellable(self):
        self.window.confirm_preview.setChecked(False)
        with patch("qt_ui.DemoClient", MultiClient):
            self.window.start_run(
                scope="single", identities=[self.store.data["accounts"][0]["id"]]
            )
            pump(lambda: self.window.task_dialog is not None)
            self.assertTrue(self.window.busy)
            self.window.cancel_run()
            pump(lambda: not self.window.busy)
        self.assertEqual(MultiClient.posted, [])
        self.assertEqual(self.store.data["results"][0]["status"], "cancelled")

    def test_manual_task_selector_uses_explicit_task_id(self):
        self.window.confirm_preview.setChecked(False)
        with patch("qt_ui.DemoClient", MultiClient):
            self.window.start_run(
                scope="single", identities=[self.store.data["accounts"][0]["id"]]
            )
            pump(lambda: self.window.task_dialog is not None)
            from PySide6.QtWidgets import QComboBox

            choices = self.window.task_dialog.findChild(QComboBox)
            choices.setCurrentIndex(1)
            self.window.task_dialog.accept()
            pump(lambda: not self.window.busy)
        self.assertEqual(MultiClient.posted, [("mock-a", "beta")])
        self.assertEqual(self.store.data["results"][0]["task_id"], "beta")

    def test_automatic_ambiguity_finishes_hidden_without_dialog_and_locates_failure(
        self,
    ):
        self.store.data["schedule"].update(enabled=True, time="00:00")
        self.window.hide()
        with patch("qt_ui.DemoClient", MultiClient):
            self.window.start_run(scheduled=True)
            pump(lambda: not self.window.busy)
        self.assertIsNone(self.window.task_dialog)
        self.assertFalse(self.window.isVisible())
        self.assertEqual(MultiClient.posted, [])
        self.assertEqual(self.window.notification_target["title"], "需要人工处理")
        self.window.open_notification_target()
        self.assertTrue(self.window.isVisible())
        self.assertEqual(self.window.stack.currentIndex(), 5)
        pump(
            lambda: self.window.log_page.worker is None
            and not self.window.log_page.pending
        )
        self.assertTrue(
            self.window.log_page.batch_target or self.window.log_page.record
        )

    def test_check_accounts_uses_background_worker_and_blocks_sign_execution(self):
        with patch("reliability_ui.DemoClient", MultiClient), patch.object(
            self.window, "show_text"
        ) as report:
            self.window.check_account_dialog()
            self.assertIsNotNone(self.window.data_job)
            self.window.start_run()
            self.assertFalse(self.window.busy)
            pump(lambda: self.window.data_job is None)
        self.assertEqual(MultiClient.posted, [])
        self.assertIn("不代表实际打卡成功", report.call_args.args[0])
        self.assertIn("任务 alpha", report.call_args.args[1])

    def test_import_preview_cancel_and_accept_deduplicate(self):
        path = Path(self.folder.name) / "result.json"
        path.write_text('{"openid":"mock-import"}')
        with patch.object(self.window, "confirm", return_value=False):
            self.window.start_import(path)
            pump(lambda: self.window.import_worker is None)
        self.assertEqual(len(self.store.data["accounts"]), 2)
        with patch.object(self.window, "confirm", return_value=True):
            self.window.start_import(path)
            pump(lambda: self.window.import_worker is None)
        self.assertEqual(len(self.store.data["accounts"]), 3)
        self.assertIn("下一步", self.window.notice.text())

    def test_data_actions_wait_for_run_and_unsaved_results(self):
        self.window.busy = True
        self.assertFalse(
            self.window.start_job(lambda: self.fail("must not run"), lambda _: None)
        )
        self.window.busy = False
        self.window.pending_results = []
        self.assertFalse(self.window.data_available())
        self.window.pending_results = None
        self.window.journal_error = "mock unsaved journal"
        self.assertFalse(self.window.data_available())

    def test_background_restore_updates_settings_without_task_or_registry(self):
        self.store.save()
        data = self.store.snapshot()
        data["accounts"][0]["label"] = "恢复模拟账号"
        data["schedule"]["enabled"] = True
        data["notifications"]["success"] = True
        with patch(
            "windows_integration.Autostart.set_enabled",
            side_effect=AssertionError("no registry"),
        ):
            self.window.start_job(
                lambda: restore_backup(
                    self.store, {"data": data, "logs_included": False}
                ),
                self.window.restored,
            )
            pump(lambda: self.window.data_job is None)
        self.assertFalse(self.store.data["schedule"]["enabled"])
        self.assertTrue(self.window.notification_success.isChecked())
        self.assertEqual(self.store.data["accounts"][0]["label"], "恢复模拟账号")
        self.assertFalse(self.window.busy)

    def test_notification_failure_does_not_interrupt_tasks_and_toggle_prevents_delivery(
        self,
    ):
        self.window.demo = False
        notice = {
            "title": "批次失败",
            "message": "请查看软件",
            "batch_id": "mock-batch",
            "account_id": None,
        }
        with patch("tray.TrayIcon") as factory:
            factory.return_value.show.side_effect = RuntimeError("mock unavailable")
            self.window.deliver_system_notice(notice)
        self.assertEqual(self.window.notification_target["batch_id"], "mock-batch")
        self.store.data["notifications"]["enabled"] = False
        with patch("tray.TrayIcon") as factory:
            self.window.deliver_system_notice(notice)
            factory.assert_not_called()

    def test_storage_notification_is_merged_and_opens_unsaved_result(self):
        self.window.pending_results = []
        with patch.object(self.window, "deliver_system_notice") as deliver:
            self.window.storage_system_notice()
            self.window.storage_system_notice()
        deliver.assert_called_once()
        self.window.open_notification_target()
        self.assertEqual(self.window.stack.currentIndex(), 0)

    def test_expired_pending_retry_emits_final_failure_notice(self):
        from datetime import timedelta
        from storage import now_shanghai
        from scheduling import begin_attempt, finish_attempt
        from execution import make_request

        moment = now_shanghai().replace(hour=12, minute=0)
        self.store.data["schedule"].update(enabled=True, time="11:00")
        request = make_request(self.store.snapshot(), "automatic", moment=moment)
        begin_attempt(self.store.data, request, moment)
        result = {
            "account_id": request["accounts"][0]["id"],
            "status": "failure",
            "retryable": True,
            "task_id": "expired",
            "task_window": {"start": "00:00:00", "end": "12:10:00"},
        }
        finish_attempt(self.store.data, request, [result], moment)
        self.assertTrue(self.store.data["automation"]["next_retry"])
        with patch(
            "qt_ui.now_shanghai", return_value=moment + timedelta(minutes=20)
        ), patch.object(self.window, "start_run") as start:
            self.window.tick()
        start.assert_not_called()
        self.assertEqual(self.window.notification_target["title"], "批次失败")
        self.assertIsNone(self.store.data["automation"]["next_retry"])

    def test_demo_cannot_modify_startup_or_modes(self):
        with patch(
            "windows_integration.Autostart.set_enabled",
            side_effect=AssertionError("no registry"),
        ):
            self.window.change_autostart(True)
            self.window.migrate_dialog()
        self.assertFalse(self.store.data["reliability"]["autostart"])

    def test_start_hidden_is_independent_of_close_exit_preference(self):
        self.store.data["appearance"]["close_behavior"] = "exit"
        with patch("qt_ui.TrayIcon", FakeTray):
            self.window.on_close(force_tray=True)
            self.assertFalse(self.window.isVisible())
            self.assertFalse(self.window._destroyed)
            self.assertEqual(
                Store(self.store.directory).data["appearance"]["close_behavior"], "exit"
            )
            self.window.restore_window()

    def test_task_selection_rule_is_saved_and_dirty_changes_are_explicit(self):
        self.window.task_rule_mode.setCurrentIndex(1)
        self.window.task_rule_name.setText("模拟精确名称")
        self.assertIn("未保存", self.window.schedule_badge.text())
        self.window.save_schedule()
        self.assertEqual(
            Store(self.store.directory).data["schedule"]["task_rule"]["name"],
            "模拟精确名称",
        )


if __name__ == "__main__":
    unittest.main()
