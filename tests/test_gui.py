import json
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch
from PySide6.QtCore import Qt, QTime, QDate, QThread, QCoreApplication, QEvent
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QLineEdit, QDialog, QPushButton

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app import App, AccountDialog, create_application
from qt_ui import PAGES, PAGE_INDEX, LocationDialog
from runner import run_accounts
from api import ApiError
from datetime import timedelta
from storage import Store, new_account, now_shanghai
from locations import TASK_LOCATION, saved_location
from test_locations import MockClient

APPLICATION = create_application()


def pump(check, timeout=8, preview=None):
    end = time.monotonic() + timeout
    while not check() and time.monotonic() < end:
        QApplication.processEvents()
        if preview:
            preview()
        QTest.qWait(10)
    if not check():
        raise AssertionError("Qt task did not finish before timeout")


class GuiTests(unittest.TestCase):
    def test_log_failure_remains_visible_without_faking_result(self):
        self.accounts()
        self.window.confirm_preview.setChecked(False)
        with patch("qt_ui.run_accounts", side_effect=self.offline), patch.object(
            self.store, "write_log", side_effect=OSError("read only")
        ):
            self.window.start_run()
            pump(lambda: not self.window.busy)
        self.assertIn("日志保存失败", self.window.notice.text())
        self.assertIn("写入权限", self.window.notice.text())
        self.assertEqual(len(Store(self.folder.name).data["results"]), 2)
        self.assertTrue(self.window.log_text.toPlainText())

    def test_single_instance_rejects_same_data_directory(self):
        from desktop_common import SingleInstance
        import os

        if os.name != "nt":
            self.skipTest("Windows named mutex")
        first = SingleInstance(self.folder.name)
        try:
            with self.assertRaisesRegex(RuntimeError, "已经运行"):
                SingleInstance(self.folder.name)
            other = SingleInstance(Path(self.folder.name) / "other")
            other.close()
        finally:
            first.close()

    def test_existing_theme_and_account_data_survive_ui_migration(self):
        self.accounts()
        self.store.data["appearance"].update(theme="light", accent="legacy-value")
        self.store.save()
        before = self.store.path.read_bytes()
        loaded = Store(self.folder.name)
        window = App(loaded, demo=True)
        window.timer.stop()
        try:
            self.assertEqual(window.theme, "light")
            self.assertEqual(window.store.data["appearance"]["accent"], "legacy-value")
            self.assertEqual(window.store.data["accounts"], self.store.data["accounts"])
            self.assertEqual(self.store.path.read_bytes(), before)
        finally:
            window.shutdown()
            pump(lambda: window._destroyed)
            window.deleteLater()

    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.store = Store(self.folder.name)
        self.window = App(self.store, demo=True)
        self.window.timer.stop()
        self.window.show()
        QTest.qWait(30)
        self.network = patch(
            "requests.Session.request",
            side_effect=AssertionError("Real network forbidden during GUI validation"),
        )
        self.network.start()
        (
            MockClient.submitted,
            MockClient.on_login,
            MockClient.sample,
            MockClient.final_status,
        ) = ([], None, None, 0)

    def tearDown(self):
        if self.window.busy:
            self.window.cancel_run()
            pump(lambda: not self.window.busy)
        if self.window.import_worker:
            pump(lambda: self.window.import_worker is None)
        self.window.pending_results = None
        self.window.shutdown()
        pump(lambda: self.window._destroyed)
        self.window.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        QApplication.processEvents()
        self.network.stop()
        self.folder.cleanup()

    def accounts(self):
        self.store.data["accounts"] = [
            new_account("mock-private-a", "我的账号"),
            new_account("mock-private-b", "备用账号"),
        ]
        self.window.refresh()

    def offline(self, *args, **kwargs):
        kwargs.update(client_factory=MockClient, delay_scale=0)
        return run_accounts(*args, **kwargs)

    def accept_previews(self):
        if self.window.preview_dialog:
            self.window.preview_dialog.accept()

    def test_dark_default_light_persistence_and_empty_actions(self):
        self.assertEqual(self.window.theme, "dark")
        self.window.navigate(1)
        self.assertTrue(self.window.account_empty.isVisible())
        self.window.start_run()
        self.assertIn("账号", self.window.notice.text())
        self.assertFalse(self.window.busy)
        self.window.toggle_theme()
        self.assertEqual(Store(self.folder.name).data["appearance"]["theme"], "light")
        self.window.navigate("settings")
        self.assertEqual(self.window.theme_combo.currentData(), "light")
        self.assertTrue(all(not b.icon().isNull() for b in self.window.nav_buttons))

    def test_account_dialog_hidden_copy_validation_and_override(self):
        self.accounts()
        account = self.store.data["accounts"][0]
        dialog = AccountDialog(self.window, account)
        self.assertEqual(dialog.openid.echoMode(), QLineEdit.EchoMode.Password)
        dialog.reveal()
        self.assertEqual(dialog.openid.echoMode(), QLineEdit.EchoMode.Normal)
        dialog.copy_identifier()
        if QApplication.clipboard().text() == account["openid"]:
            self.assertIn("已复制", dialog.notice.text())
        else:
            self.assertIn("剪贴板暂不可用", dialog.notice.text())
        dialog.openid.setText(self.store.data["accounts"][1]["openid"])
        dialog.save()
        self.assertIn("已存在", dialog.notice.text())
        dialog.openid.setText("mock-new")
        dialog.location_choice.mode.setCurrentIndex(
            dialog.location_choice.mode.findData("task")
        )
        dialog.save()
        self.assertEqual(dialog.values["location"], TASK_LOCATION)
        dialog.deleteLater()

    def test_search_selection_enable_copy_and_delete_confirmation(self):
        self.accounts()
        self.window.navigate(1)
        self.window.account_search.setText("备用")
        self.assertEqual(self.window.account_table.rowCount(), 1)
        self.window.account_table.selectRow(0)
        self.window.copy_account()
        if QApplication.clipboard().text() == "mock-private-b":
            self.assertIn("已复制", self.window.notice.text())
        else:
            self.assertIn("剪贴板暂不可用", self.window.notice.text())
        self.assertNotIn("mock-private-b", self.window.notice.text())
        self.window.show_identifier.setChecked(True)
        self.assertEqual(self.window.identifier_text.text(), "mock-private-b")
        self.window.set_accounts_enabled(False)
        self.assertFalse(Store(self.folder.name).data["accounts"][1]["enabled"])
        with patch.object(self.window, "confirm", return_value=False):
            self.window.delete_accounts()
        self.assertEqual(len(self.store.data["accounts"]), 2)
        with patch.object(self.window, "confirm", return_value=True):
            self.window.delete_accounts()
        self.assertEqual(len(Store(self.folder.name).data["accounts"]), 1)

    def test_background_import_deduplicates_and_reports_failure(self):
        path = Path(self.folder.name) / "mock.txt"
        path.write_text("mock-1,mock-1 mock-2", encoding="utf-8")
        with patch.object(self.window, 'confirm', return_value=True):
            self.window.start_import(path)
            pump(lambda: self.window.import_worker is None)
        self.assertEqual(len(self.store.data["accounts"]), 2)
        self.assertIn("新增 2", self.window.notice.text())
        self.window.start_import(path)
        pump(lambda: self.window.import_worker is None)
        self.assertEqual(len(self.store.data["accounts"]), 2)
        self.window.start_import(Path(self.folder.name) / "missing.txt")
        pump(lambda: self.window.import_worker is None)
        self.assertIn("无法读取", self.window.notice.text())

    def test_location_crud_precision_reference_guard_and_invalid(self):
        self.accounts()
        dialog = self.window.new_location()
        dialog.name.setText("测试宿舍")
        dialog.latitude.setText("28.1310861234567")
        dialog.longitude.setText("112.994658987654")
        dialog.save()
        location = self.store.data["locations"][-1]
        self.assertEqual(location["latitude"], 28.1310861234567)
        dialog = self.window.edit_location(location["id"])
        dialog.name.setText("改名宿舍")
        dialog.save()
        self.window.apply_global_location(location["id"])
        self.assertEqual(
            Store(self.folder.name).data["global_location"]["location_id"],
            location["id"],
        )
        self.window.delete_location(location["id"])
        self.assertIn("全局", self.window.notice.text())
        self.window.global_choice.mode.setCurrentIndex(
            self.window.global_choice.mode.findData("task")
        )
        self.window.save_global_location()
        with patch.object(self.window, "confirm", return_value=True):
            self.window.delete_location(location["id"])
        self.assertEqual(len(self.store.data["locations"]), 1)
        dialog = self.window.new_location()
        dialog.name.setText("错误")
        dialog.latitude.setText("nan")
        dialog.longitude.setText("0")
        dialog.save()
        self.assertIn("纬度", dialog.errors["latitude"].text())
        dialog.reject()

    def test_schedule_saved_and_unsaved_values_do_not_trigger(self):
        self.window.schedule_time.setTime(QTime(12, 30))
        self.window.schedule_mode.setCurrentIndex(2)
        self.window.schedule_enabled.setChecked(True)
        self.assertIn("未保存", self.window.schedule_badge.text())
        self.assertFalse(self.store.data["schedule"]["enabled"])
        self.window.save_schedule()
        self.assertIn("日期", self.window.notice.text())
        self.assertFalse(self.store.data["schedule"]["enabled"])
        self.window.schedule_dates.setPlainText("2027-01-01\n2027-01-01")
        self.window.save_schedule()
        self.assertEqual(
            Store(self.folder.name).data["schedule"]["dates"], ["2027-01-01"]
        )
        self.assertIn("已保存", self.window.schedule_badge.text())
        self.window.schedule_time.setTime(QTime(11, 0))
        self.window.load_schedule()
        self.assertEqual(self.window.schedule_time.time(), QTime(12, 30))

    def test_manual_selected_batch_and_timer_use_same_snapshot_on_qthread(self):
        self.accounts()
        self.window.confirm_preview.setChecked(True)
        calls = []
        threads = []
        main_thread = threading.get_ident()

        def offline(*args, **kwargs):
            calls.append(kwargs.copy())
            threads.append(threading.get_ident())
            return self.offline(*args, **kwargs)

        with patch("qt_ui.run_accounts", side_effect=offline), patch(
            "runner.random.random",
            side_effect=AssertionError("Specified location must not drift"),
        ):
            self.window.account_table.selectRow(0)
            self.window.start_run(selected=True)
            self.accept_previews()
            pump(lambda: not self.window.busy)
            self.assertEqual(len(MockClient.submitted), 1)
            self.window.start_run()
            self.accept_previews()
            pump(lambda: not self.window.busy)
            self.assertEqual(len(MockClient.submitted), 3)
            self.store.data["success_dates"].clear()
            self.store.data["results"].clear()
            self.store.data["schedule"].update(
                enabled=True, time="00:00", last_trigger=None
            )
            self.window.tick()
            pump(lambda: not self.window.busy)
            self.assertEqual(len(MockClient.submitted), 5)
        self.assertTrue(all(t != main_thread for t in threads))
        self.assertTrue(calls[-1]["scheduled"])
        self.assertIsNone(calls[-1]["preview_handler"])
        self.assertTrue(self.store.data["schedule"]["last_trigger"])
        self.assertEqual(len(self.window.previews), 2)
        campus = self.store.data["locations"][0]
        self.assertTrue(
            all(
                payload["signLat"] == campus["latitude"]
                and payload["signLng"] == campus["longitude"]
                for _, payload in MockClient.submitted
            )
        )
        self.assertEqual(len(self.store.data["results"]), 2)
        self.assertTrue(list(Path(self.folder.name).rglob("*.log")))
        self.assertNotIn("mock-private", self.window.log_text.toPlainText())

    def test_batch_preview_cancel_starts_no_account_and_active_cancel_no_submit(self):
        self.accounts()
        with patch("qt_ui.run_accounts", side_effect=self.offline):
            self.window.start_run()
            self.assertFalse(self.window.busy)
            self.assertFalse(self.window.preview_dialog.isModal())
            self.window.preview_dialog.reject()
            self.assertIsNone(self.window.pending_request)
            self.assertEqual(MockClient.submitted, [])
            self.assertEqual(self.store.data["results"], [])
            self.window.start_run()
            self.accept_previews()
            self.window.cancel_run()
            pump(lambda: not self.window.busy)
            self.assertEqual(MockClient.submitted, [])
            self.assertTrue(
                all(r["status"] == "cancelled" for r in self.store.data["results"])
            )

    def test_failure_is_visible_server_result_and_history_filters(self):
        self.accounts()
        self.window.confirm_preview.setChecked(False)
        MockClient.final_status = 3
        with patch("qt_ui.run_accounts", side_effect=self.offline):
            self.window.start_run()
            pump(lambda: not self.window.busy)
        self.assertTrue(
            all(r["status"] == "failure" for r in self.store.data["results"])
        )
        self.assertIn("失败 2", self.window.batch_summary.text())
        self.assertNotIn("失败 2", self.window.footer_status.text())
        self.window.navigate(4)
        self.window.history_result.setCurrentIndex(
            self.window.history_result.findData("failure")
        )
        self.assertEqual(self.window.history_table.rowCount(), 2)
        self.window.history_account.setCurrentIndex(1)
        self.assertEqual(self.window.history_table.rowCount(), 1)
        self.window.history_date_mode.setCurrentIndex(2)
        self.window.history_date.setDate(QDate(2000, 1, 1))
        self.assertEqual(self.window.history_table.rowCount(), 0)
        self.window.reset_history_filters()
        self.assertEqual(self.window.history_table.rowCount(), 2)

    def test_save_failure_retains_result_and_safe_exit_until_retry(self):
        self.accounts()
        self.window.confirm_preview.setChecked(False)
        with patch("qt_ui.run_accounts", side_effect=self.offline), patch.object(
            self.store, "save", side_effect=OSError("read only")
        ):
            self.window.start_run()
            pump(lambda: not self.window.busy)
        self.assertEqual(len(self.window.pending_results), 2)
        self.assertEqual(self.store.data["results"], [])
        self.window.request_exit()
        self.assertFalse(self.window._destroyed)
        self.window.retry_results()
        self.window.timer.stop()
        self.assertIsNone(self.window.pending_results)
        self.assertEqual(len(Store(self.folder.name).data["results"]), 2)

    def test_resize_scroll_themes_collapse_and_keyboard(self):
        self.accounts()
        for theme in ("dark", "light"):
            self.window.theme = theme
            self.window.apply_theme()
            for width, height in ((740, 540), (960, 650), (1240, 840), (1440, 960)):
                self.window.resize(width, height)
                for page in range(len(PAGES)):
                    self.window.navigate(page)
                    QApplication.processEvents()
                    scroll = self.window.stack.currentWidget()
                    self.assertGreater(scroll.viewport().width(), 300)
                    self.assertGreaterEqual(
                        scroll.widget().height(), scroll.viewport().height()
                    )
                    self.assertTrue(self.window.primary_action.isVisible())
                    self.assertLessEqual(
                        self.window.primary_action.geometry().right(),
                        self.window.primary_action.parentWidget().width(),
                    )
        self.window.toggle_sidebar()
        self.assertEqual(self.window.sidebar.width(), 68)
        self.window.activateWindow()
        self.window.setFocus()
        QTest.qWait(40)
        QTest.keyClick(self.window, Qt.Key.Key_2, Qt.KeyboardModifier.ControlModifier)
        self.assertEqual(self.window.stack.currentIndex(), 1)
        self.window.log_toggle.click()
        self.assertTrue(self.window.log_panel.isVisible())
        self.window.log_toggle.click()
        self.assertFalse(self.window.log_panel.isVisible())

    def test_all_range_ignores_search_selection_and_main_button_is_global(self):
        self.accounts()
        self.window.account_search.setText("备用")
        self.window.account_table.selectRow(0)
        for page in range(len(PAGES)):
            self.window.navigate(page)
            self.assertEqual(self.window.primary_action.text(), "打卡全部启用账号（2）")
        self.assertIn("显示 1 / 共 2", self.window.account_counts.text())
        self.window.start_run()
        self.assertEqual(self.window.pending_request["count"], 2)
        self.window.preview_dialog.reject()
        self.window.account_table.clearSelection()
        self.window.start_run(selected=True)
        self.assertIsNone(self.window.pending_request)
        self.assertIn("选择", self.window.notice.text())

    def test_disabled_selected_and_sorted_row_actions_keep_identity(self):
        self.accounts()
        target = self.store.data["accounts"][1]
        target["enabled"] = False
        self.window.refresh()
        self.window.account_table.sortItems(1, Qt.SortOrder.DescendingOrder)
        for row in range(self.window.account_table.rowCount()):
            if (
                self.window.account_table.item(row, 1).data(Qt.ItemDataRole.UserRole)
                == target["id"]
            ):
                self.window.account_table.selectRow(row)
                self.window.start_run(selected=True)
                self.assertEqual(
                    self.window.pending_request["accounts"][0]["id"], target["id"]
                )
                self.window.preview_dialog.reject()
                operations = self.window.account_table.cellWidget(row, 5)
                buttons = operations.findChildren(QPushButton)
                buttons[0].click()
                self.assertEqual(
                    self.window.pending_request["accounts"][0]["id"], target["id"]
                )
                self.window.preview_dialog.reject()
                with patch.object(self.window, "account_dialog") as edit:
                    buttons[1].click()
                    self.assertEqual(edit.call_args.args[0]["id"], target["id"])
                break
        self.assertFalse(target["enabled"])

    def test_batch_confirm_once_and_snapshot_survives_edits(self):
        self.accounts()
        with patch("qt_ui.run_accounts", side_effect=self.offline):
            self.window.start_run()
            request = self.window.pending_request
            latitude = request["accounts"][0]["run_location"]["latitude"]
            self.store.data["locations"][0]["latitude"] = 0
            self.store.data["accounts"][0]["enabled"] = False
            self.accept_previews()
            pump(lambda: not self.window.busy)
            self.assertIsNone(self.window.preview_dialog)
            self.assertEqual(len(MockClient.submitted), 2)
            self.assertTrue(
                all(p["signLat"] == latitude for _, p in MockClient.submitted)
            )
            self.assertFalse(self.store.data["accounts"][0]["enabled"])

    def test_past_plan_save_cancel_next_and_explicit_now(self):
        self.accounts()
        self.window.schedule_time.setTime(QTime(0, 0))
        self.window.schedule_enabled.setChecked(True)
        with patch.object(self.window, "choose_past_plan", return_value=None):
            self.window.save_schedule()
        self.assertFalse(self.store.data["schedule"]["enabled"])
        with patch.object(self.window, "choose_past_plan", return_value="next"):
            self.window.save_schedule()
        self.assertTrue(self.store.data["schedule"]["last_trigger"])
        self.window.tick()
        self.assertFalse(self.window.busy)
        self.window.schedule_time.setTime(QTime(0, 1))
        with patch.object(self.window, "choose_past_plan", return_value="now"), patch(
            "qt_ui.run_accounts", side_effect=self.offline
        ):
            self.window.save_schedule()
            pump(lambda: not self.window.busy)
        self.assertIsNone(self.window.preview_dialog)
        self.assertEqual(len(MockClient.submitted), 2)
        self.assertIn("成功 2", self.window.batch_summary.text())

    def test_gui_automatic_retry_is_persisted_deduplicated_and_bounded(self):
        self.accounts()
        self.store.data["schedule"].update(
            enabled=True, time="00:00", last_trigger=None
        )
        attempts = []

        class TemporaryFailure(MockClient):
            def login(client):
                attempts.append(client.openid)
                if client.openid == "mock-private-b":
                    raise ApiError("模拟暂时超时", "network_timeout", True)
                return super().login()

        def offline(*args, **kwargs):
            kwargs.update(client_factory=TemporaryFailure, delay_scale=0)
            return run_accounts(*args, **kwargs)

        with patch("qt_ui.run_accounts", side_effect=offline):
            self.window.tick()
            pump(lambda: not self.window.busy)
            saved = Store(self.folder.name).data
            self.assertEqual(saved["automation"]["state"], "waiting_retry")
            self.assertEqual(saved["automation"]["summary"]["success"], 1)
            for extra in (1, 2):
                moment = now_shanghai() + timedelta(minutes=6 * extra)
                with patch("qt_ui.now_shanghai", return_value=moment):
                    self.window.tick()
                    pump(lambda: not self.window.busy)
            self.window.tick()
            self.assertFalse(self.window.busy)
        self.assertEqual(attempts.count("mock-private-a"), 1)
        self.assertEqual(attempts.count("mock-private-b"), 3)
        self.assertEqual(self.store.data["automation"]["retry_count"], 2)
        self.assertIsNone(self.store.data["automation"]["next_retry"])
        self.assertIn("上限", self.window.schedule_auto_status.text())


if __name__ == "__main__":
    unittest.main()
