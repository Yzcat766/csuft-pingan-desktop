import copy
import sys
import unittest
from pathlib import Path
from unittest.mock import patch
from PySide6.QtCore import Qt
from PySide6.QtGui import QTextCursor
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QPushButton, QLabel

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import test_gui as gui
from qt_ui import PAGE_INDEX, PAGES, LocationDialog
from locations import saved_location
from execution import make_request
from log_page import append_quick
from logbook import event_text
from storage import Store


class LogLocationGuiTests(unittest.TestCase):
    setUp = gui.GuiTests.setUp
    tearDown = gui.GuiTests.tearDown
    accounts = gui.GuiTests.accounts
    offline = gui.GuiTests.offline

    def idle_logs(self):
        page = self.window.log_page
        gui.pump(
            lambda: page.worker is None
            and not page.pending
            and not page.debounce.isActive()
            and not page.follow_timer.isActive()
        )

    def execute(self):
        self.window.confirm_preview.setChecked(False)
        with patch("qt_ui.run_accounts", side_effect=self.offline):
            self.window.start_run()
            gui.pump(lambda: not self.window.busy)
        self.idle_logs()

    def test_navigation_shortcuts_full_log_entry_and_reason_are_named(self):
        self.accounts()
        self.execute()
        self.window.open_full_logs()
        self.idle_logs()
        self.assertEqual(self.window.stack.currentIndex(), PAGE_INDEX["log"])
        self.window.open_result_logs(self.store.data["results"][0])
        self.idle_logs()
        page = self.window.log_page
        self.assertEqual(
            page.account_filter.currentData(),
            self.store.data["results"][0]["account_id"],
        )
        self.assertEqual(
            page.record["batch_id"], self.store.data["results"][0]["run_id"]
        )
        self.assertIn("筛选", page.conditions.text())
        self.assertTrue(
            all(
                e["account_id"] == page.account_filter.currentData()
                for e in page.display_events
            )
        )
        page.clear_filters()
        self.idle_logs()
        self.assertIsNone(page.account_filter.currentData())
        self.window.activateWindow()
        self.window.setFocus()
        QTest.keyClick(self.window, Qt.Key.Key_7, Qt.KeyboardModifier.ControlModifier)
        self.assertEqual(self.window.stack.currentIndex(), PAGE_INDEX["settings"])
        self.assertEqual(len(self.window.nav_buttons), len(PAGES))

    def test_filter_and_selection_remain_stable_on_live_append_and_latest(self):
        self.accounts()
        request = make_request(self.store.data)
        self.window.journal.start(request)
        for i in range(80):
            self.window.record_event(
                {
                    "message": f"测试行 {i}\n可换行",
                    "step": "登录",
                    "account_id": request["accounts"][0]["id"],
                    "account_label": "模拟",
                }
            )
        self.window.flush_journal()
        self.window.navigate("log")
        self.idle_logs()
        page = self.window.log_page
        page.table.verticalScrollBar().setValue(0)
        page.table.selectRow(1)
        selected = page.table.item(1, 4)
        detail = page.detail.toPlainText()
        self.assertFalse(page.following)
        self.window.record_event({"message": "新消息", "step": "提交"})
        self.window.flush_journal()
        self.idle_logs()
        self.assertIs(page.table.item(1, 4), selected)
        self.assertTrue(selected.isSelected())
        self.assertEqual(page.detail.toPlainText(), detail)
        self.assertEqual(page.table.verticalScrollBar().value(), 0)
        page.keyword.setText("测试行 1")
        self.idle_logs()
        before = page.keyword.text()
        self.window.record_event({"message": "测试行 100"})
        self.window.flush_journal()
        self.idle_logs()
        self.assertEqual(page.keyword.text(), before)
        page.go_latest()
        self.idle_logs()
        self.assertTrue(page.following)
        self.assertEqual(
            page.table.verticalScrollBar().value(),
            page.table.verticalScrollBar().maximum(),
        )

    def test_quick_log_preserves_reading_selection(self):
        editor = self.window.log_text
        editor.setPlainText("\n".join(f"行 {i}" for i in range(100)))
        cursor = editor.textCursor()
        cursor.setPosition(0)
        cursor.setPosition(3, QTextCursor.MoveMode.KeepAnchor)
        editor.setTextCursor(cursor)
        old = cursor.selectedText()
        editor.verticalScrollBar().setValue(0)
        append_quick(editor, "新行")
        self.assertEqual(editor.textCursor().selectedText(), old)
        self.assertEqual(editor.verticalScrollBar().value(), 0)

    def test_same_name_accounts_filter_by_id_after_rename_and_delete(self):
        self.accounts()
        for a in self.store.data["accounts"]:
            a["label"] = "同名"
        self.execute()
        record = self.store.data["results"][0]
        self.store.data["accounts"][0]["label"] = "改名"
        self.store.data["accounts"].clear()
        self.window.refresh()
        self.window.open_result_logs(record)
        self.idle_logs()
        page = self.window.log_page
        self.assertEqual(page.account_filter.currentData(), record["account_id"])
        self.assertTrue(
            all(e["account_id"] == record["account_id"] for e in page.display_events)
        )
        self.assertIn("同名", page.account_filter.currentText())

    def test_old_result_has_no_guessed_batch_and_corrupted_record_is_visible(self):
        self.window.open_result_logs(
            {"account_id": "old", "status": "failure", "message": "旧结果"}
        )
        self.idle_logs()
        self.assertIsNone(self.window.log_page.record)
        self.assertIn("没有", self.window.log_page.warning.text())
        folder = self.window.journal.root / "bad-record"
        folder.mkdir(parents=True)
        path = folder / "meta.json"
        path.write_text("broken", encoding="utf-8")
        self.window.log_page.refresh_batches()
        self.idle_logs()
        page = self.window.log_page
        self.assertIn("损坏", page.batch_choice.currentText())
        self.assertTrue(page.warning.isVisible())
        self.assertEqual(path.read_text(encoding="utf-8"), "broken")

    def test_export_uses_current_filters_and_visible_feedback(self):
        self.accounts()
        self.execute()
        self.window.open_result_logs(self.store.data["results"][0])
        self.idle_logs()
        page = self.window.log_page
        destination = Path(self.folder.name) / "导出结果.txt"
        with patch(
            "log_page.QFileDialog.getSaveFileName", return_value=(str(destination), "")
        ):
            page.export_dialog()
            self.idle_logs()
        text = destination.read_text(encoding="utf-8")
        self.assertNotIn("mock-private", text)
        self.assertNotIn("mock-secret-token", text)
        self.assertIn("已导出", self.window.notice.text())
        selected_id = page.account_filter.currentData()
        self.assertIn(selected_id[:6], text)
        other = next(
            a["id"] for a in self.store.data["accounts"] if a["id"] != selected_id
        )
        self.assertNotIn(other[:6], text)

    def test_journal_save_failure_blocks_exit_until_retry(self):
        self.accounts()
        self.window.journal.start(make_request(self.store.data))
        self.window.record_event({"message": "仍在内存"})
        with patch("logbook.atomic_json", side_effect=OSError("模拟只读")):
            self.assertFalse(self.window.flush_journal())
            self.window.request_exit()
            self.assertFalse(self.window._destroyed)
            self.assertTrue(self.window.journal.dirty)
            self.assertIn("写入权限", self.window.notice.text())
        self.assertTrue(self.window.flush_journal())
        self.window.request_exit()
        gui.pump(lambda: self.window._destroyed)
        loaded = self.window.journal.list_batches()["batches"][0]
        self.assertEqual(self.window.journal.query(loaded)["total"], 1)

    def test_new_location_cancel_and_validation_do_not_mutate_or_apply(self):
        self.accounts()
        before = copy.deepcopy(self.store.data)
        dialog = self.window.new_location()
        self.assertEqual(dialog.windowTitle(), "新建位置")
        self.assertEqual(dialog.save_button.text(), "创建位置")
        self.assertFalse(dialog.name.text())
        self.assertFalse(dialog.latitude.text())
        dialog.name.setText("未完成")
        dialog.latitude.setText("nan")
        dialog.longitude.setText("112")
        dialog.save()
        self.assertIn("有限", dialog.errors["latitude"].text())
        self.assertEqual(self.store.data, before)
        dialog.reject()
        self.assertEqual(self.store.data, before)
        second = self.window.new_location()
        self.assertFalse(second.name.text())
        second.reject()
        self.assertFalse(hasattr(self.window, "account_location_table"))
        self.assertFalse(hasattr(self.window, "location_name"))
        self.assertFalse(hasattr(self.window, "location_edit_id"))

    def test_create_preserves_global_draft_selects_created_and_enter_saves_once(self):
        self.accounts()
        before_global = copy.deepcopy(self.store.data["global_location"])
        before_accounts = copy.deepcopy(self.store.data["accounts"])
        self.window.global_choice.mode.setCurrentIndex(
            self.window.global_choice.mode.findData("task")
        )
        dialog = self.window.new_location()
        dialog.name.setText("新位置")
        dialog.latitude.setText("28.1234567890123")
        dialog.longitude.setText("112.99")
        dialog.longitude.setFocus()
        QTest.keyClick(dialog.longitude, Qt.Key.Key_Return)
        self.assertEqual(len(self.store.data["locations"]), 2)
        created = self.store.data["locations"][-1]
        self.assertEqual(self.window.selected_location()["id"], created["id"])
        self.assertEqual(self.window.global_choice.value()["mode"], "task")
        self.assertIn("未保存", self.window.global_dirty.text())
        self.assertEqual(self.store.data["global_location"], before_global)
        self.assertEqual(self.store.data["accounts"], before_accounts)
        self.assertIn("位置已创建", self.window.notice.text())
        self.window.refresh()
        self.assertEqual(self.window.global_choice.value()["mode"], "task")

    def test_sorted_edit_has_stable_id_cancel_keeps_data_and_refs_explain_future(self):
        position = saved_location("其它位置", 28, 112)
        self.store.data["locations"].append(position)
        self.window.refresh_locations()
        self.window.location_table.sortItems(0, Qt.SortOrder.DescendingOrder)
        row = next(
            r
            for r in range(self.window.location_table.rowCount())
            if self.window.location_table.item(r, 0).data(Qt.ItemDataRole.UserRole)
            == position["id"]
        )
        self.window.location_table.selectRow(row)
        dialog = self.window.edit_location()
        self.assertEqual(dialog.identity, position["id"])
        self.assertEqual(dialog.windowTitle(), "编辑位置")
        self.assertEqual(dialog.save_button.text(), "保存修改")
        dialog.name.setText("修改位置")
        self.window.location_table.sortItems(0, Qt.SortOrder.AscendingOrder)
        self.window.refresh_locations()
        dialog.save()
        saved = next(
            p for p in self.store.data["locations"] if p["id"] == position["id"]
        )
        self.assertEqual(saved["name"], "修改位置")
        self.assertEqual(self.store.data["locations"][0]["name"], "中南林业科技大学")
        original = copy.deepcopy(self.store.data)
        dialog = self.window.edit_location(self.store.data["locations"][0]["id"])
        self.assertTrue(
            any("后续任务" in item.text() for item in dialog.findChildren(QLabel))
        )
        dialog.latitude.setText("0")
        dialog.reject()
        self.assertEqual(self.store.data, original)

    def test_location_save_failure_keeps_fields_and_config_then_retries(self):
        before = copy.deepcopy(self.store.data)
        dialog = self.window.new_location()
        dialog.name.setText("保存失败测试")
        dialog.latitude.setText("28")
        dialog.longitude.setText("112")
        with patch.object(self.store, "save", side_effect=OSError("模拟只读")):
            dialog.save()
        self.assertEqual(self.store.data, before)
        self.assertEqual(dialog.name.text(), "保存失败测试")
        self.assertEqual(dialog.result(), 0)
        self.assertIn("内容已保留", dialog.notice.text())
        dialog.save()
        self.assertEqual(
            Store(self.folder.name).data["locations"][-1]["name"], "保存失败测试"
        )

    def test_referenced_edit_affects_future_config_but_not_running_snapshot(self):
        self.accounts()
        request = make_request(self.store.data)
        location = self.store.data["locations"][0]
        latitude = location["latitude"]
        dialog = self.window.edit_location(location["id"])
        dialog.latitude.setText("29")
        dialog.save()
        self.assertEqual(request["accounts"][0]["run_location"]["latitude"], latitude)
        self.assertEqual(
            make_request(self.store.data)["accounts"][0]["run_location"]["latitude"], 29
        )
        self.window.delete_location(location["id"])
        self.assertIn("全局", self.window.notice.text())
        self.assertEqual(len(self.store.data["locations"]), 1)

    def test_clipboard_success_and_unavailable_feedback_are_honest(self):
        from desktop_common import copy_text
        from unittest.mock import Mock

        clipboard = Mock()
        clipboard.text.return_value = "mock-copy"
        with patch("PySide6.QtWidgets.QApplication.clipboard", return_value=clipboard):
            self.assertTrue(copy_text("mock-copy"))
        clipboard.setText.assert_called_once_with("mock-copy")
        clipboard.text.return_value = ""
        with patch("PySide6.QtWidgets.QApplication.clipboard", return_value=clipboard):
            self.assertFalse(copy_text("mock-copy"))

    def test_log_copy_passes_sanitized_selected_content(self):
        self.accounts()
        self.execute()
        self.window.open_full_logs()
        self.idle_logs()
        page = self.window.log_page
        page.table.selectRow(0)
        with patch("log_page.copy_text", return_value=True) as copy:
            page.copy_selected()
            self.assertNotIn("mock-private", copy.call_args.args[0])
            self.assertNotIn("mock-secret-token", copy.call_args.args[0])
            self.assertIn("已复制", self.window.notice.text())
        with patch("log_page.copy_text", return_value=False):
            page.copy_selected()
            self.assertIn("剪贴板暂不可用", self.window.notice.text())

    def test_location_row_menu_applies_only_its_stable_id(self):
        position = saved_location("额外位置", 28, 112)
        self.store.data["locations"].append(position)
        self.window.refresh_locations()
        self.window.location_table.sortItems(0, Qt.SortOrder.DescendingOrder)
        row = next(
            r
            for r in range(self.window.location_table.rowCount())
            if self.window.location_table.item(r, 0).data(Qt.ItemDataRole.UserRole)
            == position["id"]
        )
        more = next(
            b
            for b in self.window.location_table.cellWidget(row, 3).findChildren(
                QPushButton
            )
            if b.text() == "更多"
        )
        more.menu().actions()[0].trigger()
        self.assertEqual(
            self.store.data["global_location"]["location_id"], position["id"]
        )

    def test_old_batch_paging_cannot_override_newer_reason_jump(self):
        import threading

        self.accounts()
        ids = []
        for i in range(52):
            request = make_request(self.store.data)
            ids.append(request["run_id"])
            self.window.journal.start(request)
            self.window.journal.finish([])
        page = self.window.log_page
        started, resume = threading.Event(), threading.Event()
        original = self.window.journal.list_batches

        def delayed(offset=0, limit=50):
            if offset == 50:
                started.set()
                resume.wait(2)
            return original(offset, limit)

        with patch.object(self.window.journal, "list_batches", side_effect=delayed):
            self.window.open_result_logs(
                {"run_id": ids[0], "account_id": self.store.data["accounts"][0]["id"]}
            )
            gui.pump(started.is_set)
            self.window.open_result_logs(
                {"run_id": ids[-1], "account_id": self.store.data["accounts"][1]["id"]}
            )
            resume.set()
            self.idle_logs()
        self.assertEqual(page.record["batch_id"], ids[-1])
        self.assertEqual(
            page.account_filter.currentData(), self.store.data["accounts"][1]["id"]
        )


if __name__ == "__main__":
    unittest.main()
