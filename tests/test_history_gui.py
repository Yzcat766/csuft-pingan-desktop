import copy
import json
import os
import unittest
from pathlib import Path
from unittest.mock import patch
from PySide6.QtCore import Qt, QTimer, QItemSelectionModel
from PySide6.QtWidgets import QApplication, QMessageBox
from PySide6.QtTest import QTest
import test_gui as gui
from storage import Store, new_account, now_shanghai
from overview import record_batch
from test_history_storage import result, seed_position


class HistoryGuiTests(unittest.TestCase):
    setUp = gui.GuiTests.setUp
    tearDown = gui.GuiTests.tearDown

    def seed(self):
        s, w = self.store, self.window
        s.data["accounts"] = [new_account("mock-history-qt-a", "账号A"), new_account("mock-history-qt-b", "账号B")]
        for account, identity in zip(s.data["accounts"], ("account-a", "account-b")):
            account["id"] = identity
        rows = [result(0), result(1, "failure", account="account-b"),
                result(2, "failure", "batch-b"), result(3, "skipped", "batch-b", "account-b"),
                result(4, "success", "batch-c")]
        s.data["results"] = rows
        for batch in ("batch-a", "batch-b", "batch-c"):
            values = [r for r in rows if r["batch_id"] == batch]
            record_batch(s.data, {"run_id": batch, "scope_label": "模拟批次", "count": len(values)}, values, now_shanghai())
        s.data["success_dates"] = {"account-a": "2026-10-10"}
        s.data["task_success_dates"] = {"account-a": {"task-0": "2026-10-10"}}
        s.data["automation"] = {"state": "waiting_retry", "retry_count": 1}
        seed_position(s.data)
        s.save()
        w.refresh()
        w.navigate("history")
        QApplication.processEvents()
        return s, w

    def select(self, rows):
        table = self.window.history_table
        table.clearSelection()
        for row in rows:
            table.selectionModel().select(table.model().index(row, 0),
                QItemSelectionModel.SelectionFlag.Select | QItemSelectionModel.SelectionFlag.Rows)
        QApplication.processEvents()

    def disk(self):
        return self.store.path.read_bytes(), self.store.history_path.read_bytes()

    def test_multiselect_button_deletes_stable_ids_after_sort(self):
        s, w = self.seed()
        table = w.history_table
        # Real Ctrl clicks must select two rows without opening a detail dialog.
        for row, modifiers in ((0, Qt.KeyboardModifier.NoModifier), (2, Qt.KeyboardModifier.ControlModifier)):
            QTest.mouseClick(table.viewport(), Qt.MouseButton.LeftButton, modifiers,
                             table.visualItemRect(table.item(row, 0)).center())
        ids = w.selected_history_ids()
        self.assertEqual(len(ids), 2)
        table.setSortingEnabled(True)
        table.sortItems(0, Qt.SortOrder.DescendingOrder)
        self.assertEqual(w.selected_history_ids(), ids)
        before = {r["record_id"] for r in s.data["results"]}
        with patch.object(w, "confirm", return_value=True) as confirm:
            w.history_delete_selected.click()
        self.assertIn("2 条", confirm.call_args.args[1])
        self.assertEqual({r["record_id"] for r in Store(s.directory).data["results"]}, before - ids)
        self.assertEqual(table.rowCount(), 3)
        self.assertFalse(w.selected_history_ids())
        self.assertFalse(w.history_delete_selected.isEnabled())

    def test_filtered_delete_captures_ids_when_filter_and_rows_change(self):
        s, w = self.seed()
        w.history_result.setCurrentIndex(w.history_result.findData("failure"))
        ids = {r["record_id"] for r in w.display_results}
        self.assertEqual(len(ids), 2)

        def confirm(title, text):
            self.assertIn("2 条", text)
            w.history_result.setCurrentIndex(0)
            row = result(10, "failure", "new-batch")
            row["record_id"] = "new-after-confirm"
            s.data["results"].insert(0, row)
            w.refresh_history()
            return True

        with patch.object(w, "confirm", side_effect=confirm):
            w.history_delete_filtered.click()
        remaining = Store(s.directory).data["results"]
        self.assertFalse(ids & {r["record_id"] for r in remaining})
        self.assertIn("new-after-confirm", {r["record_id"] for r in remaining})
        self.assertEqual(len(remaining), 4)

    def test_clear_all_ignores_filters_and_keeps_business_state(self):
        s, w = self.seed()
        w.history_result.setCurrentIndex(w.history_result.findData("skipped"))
        self.assertEqual(w.history_table.rowCount(), 1)
        before = s.snapshot()
        config = s.path.read_bytes()
        with patch.object(w, "confirm", return_value=True) as confirm:
            w.history_clear.click()
        self.assertIn("5 条", confirm.call_args.args[1])
        self.assertIn("移除 3 个", confirm.call_args.args[1])
        self.assertEqual(w.history_table.rowCount(), 0)
        self.assertTrue(w.history_empty.isVisible())
        self.assertFalse(w.history_clear.isEnabled())
        self.assertEqual(w.recent_table.rowCount(), 0)
        after = Store(s.directory).snapshot()
        for key in before:
            if key not in ("results", "overview_batches"):
                self.assertEqual(before[key], after[key], key)
        self.assertEqual(config, s.path.read_bytes())

    def test_real_confirmation_cancel_is_zero_disk_and_memory_change(self):
        s, w = self.seed()
        before, disk = s.snapshot(), self.disk()
        failures, inspected = [], []

        def cancel():
            try:
                dialog = next(d for d in w.findChildren(QMessageBox) if d.isVisible())
                self.assertIn("5 条", dialog.text())
                inspected.append(True)
                QTest.mouseClick(next(b for b in dialog.buttons() if b.text() == "取消"), Qt.MouseButton.LeftButton)
            except Exception as error:
                failures.append(error)
                for dialog in w.findChildren(QMessageBox):
                    dialog.reject()

        QTimer.singleShot(20, cancel)
        w.history_clear.click()
        if failures:
            raise failures[0]
        self.assertTrue(inspected)
        self.assertEqual(before, s.snapshot())
        self.assertEqual(disk, self.disk())

    def test_delete_replace_failure_keeps_ui_memory_disk_then_retry_works(self):
        s, w = self.seed()
        self.select([0, 1])
        before, disk, selected = s.snapshot(), self.disk(), w.selected_history_ids()
        real_replace = os.replace

        def failure(source, destination):
            if Path(destination).name == "history.json":
                raise OSError("INJECTED Qt history replacement failure")
            return real_replace(source, destination)

        with patch.object(w, "confirm", return_value=True), patch("history_storage.os.replace", side_effect=failure):
            w.history_delete_selected.click()
        self.assertIn("INJECTED", w.notice.text())
        self.assertEqual(before, s.snapshot())
        self.assertEqual(disk, self.disk())
        self.assertEqual(selected, w.selected_history_ids())
        self.assertEqual(w.history_table.rowCount(), 5)
        print("FAULT Qt deletion: INJECTED Qt history replacement failure; UI=5, memory=5, disk=5; error visible")
        with patch.object(w, "confirm", return_value=True):
            w.history_delete_selected.click()
        self.assertEqual(len(Store(s.directory).data["results"]), 3)
        self.assertEqual(w.history_table.rowCount(), 3)
        print("RECOVERY Qt deletion: retry saved 3 remaining records; restart=3; PASS")

    def test_running_or_unsaved_results_disable_all_delete_actions(self):
        s, w = self.seed()
        self.select([0])
        disk = self.disk()
        for field, value, reset in (("busy", True, False), ("pending_results", [], None),
                                    ("pending_request", {"run_id": "preview"}, None),
                                    ("data_job", object(), None)):
            setattr(w, field, value)
            w.update_history_actions()
            self.assertTrue(all(not b.isEnabled() for b in (w.history_clear, w.history_delete_filtered, w.history_delete_selected)))
            with patch.object(w, "confirm") as confirm:
                w.delete_history("all")
            confirm.assert_not_called()
            self.assertEqual(disk, self.disk())
            setattr(w, field, reset)
        w.update_history_actions()
        self.assertTrue(w.history_clear.isEnabled())

    def test_execution_result_file_failure_retains_pending_results_until_retry(self):
        s, w = self.seed()
        s.data["global_location"] = {"mode": "task"}
        s.save()
        w.confirm_preview.setChecked(False)
        real_replace = os.replace

        def fail_result_save(source, destination):
            if Path(destination).name == "history.json":
                raise OSError("INJECTED execution result replacement failure")
            return real_replace(source, destination)

        # App(demo=True) runs the real runner with DemoClient's current-date mock HTTP.
        with patch("history_storage.os.replace", side_effect=fail_result_save):
            w.start_run()
            gui.pump(lambda: not w.busy)
        self.assertEqual(len(w.pending_results), 2)
        self.assertTrue(all(r["status"] == "success" for r in w.pending_results))
        self.assertEqual(len(s.data["results"]), 5)
        self.assertEqual(len(Store(s.directory).data["results"]), 5)
        self.assertFalse(w.history_clear.isEnabled())
        w.retry_results()
        w.timer.stop()
        self.assertIsNone(w.pending_results)
        self.assertEqual(len(Store(s.directory).data["results"]), 7)
        saved = Store(s.directory).data
        for account in saved["accounts"]:
            self.assertTrue(saved["task_success_dates"][account["id"]])
        w.retry_results()
        self.assertEqual(len(Store(s.directory).data["results"]), 7)

    def test_confirmation_blocks_new_execution_and_timer_then_resumes(self):
        s, w = self.seed()
        w.timer.start(1000)
        before = self.disk()

        def cancel(title, text):
            self.assertFalse(w.timer.isActive())
            self.assertTrue(w.history_delete_pending)
            with patch.object(w, "begin_run") as begin:
                w.start_run()
                w.tick()
                begin.assert_not_called()
            self.assertIsNone(w.pending_request)
            self.assertFalse(w.data_available())
            return False

        with patch.object(w, "confirm", side_effect=cancel):
            w.history_clear.click()
        self.assertTrue(w.timer.isActive())
        w.timer.stop()
        self.assertEqual(self.disk(), before)

    def test_multiple_selection_survives_refresh_and_theme_change(self):
        s, w = self.seed()
        self.select([1, 3])
        ids = w.selected_history_ids()
        row = result(20, batch="inserted")
        row["record_id"] = "inserted-record"
        s.data["results"].insert(0, row)
        for theme in ("light", "dark"):
            w.theme = theme
            w.apply_theme()
            w.refresh_history()
            QApplication.processEvents()
            self.assertEqual(w.selected_history_ids(), ids)
            self.assertTrue(w.history_delete_selected.isEnabled())
            output = Path(__file__).resolve().parents[1] / "build" / f"history-ui-{theme}.png"
            output.parent.mkdir(exist_ok=True)
            self.assertTrue(w.grab().save(str(output)))

    def test_sorted_detail_uses_record_id_not_display_row_number(self):
        s, w = self.seed()
        w.history_table.setSortingEnabled(True)
        w.history_table.sortItems(0, Qt.SortOrder.DescendingOrder)
        identity = w.history_table.item(0, 0).data(Qt.ItemDataRole.UserRole)
        expected = next(r for r in s.data["results"] if r["record_id"] == identity)
        with patch.object(w, "show_detail") as detail:
            w.history_detail(0)
        self.assertEqual(detail.call_args.args[0], expected)

    def test_credential_migration_rollback_keeps_deleted_history_deleted(self):
        s, w = self.seed()
        # Real protected old schema containing stale history.
        old = s.snapshot()
        (s.directory / "migration-rollback.dpapi").write_text(json.dumps({
            "scheme": "dpapi-user-v1", "value": s.protector.protect(json.dumps(old))}), encoding="utf-8")
        with patch.object(w, "confirm", return_value=True):
            w.history_clear.click()
            w.rollback_credentials()
            gui.pump(lambda: w.data_job is None)
        self.assertEqual(s.data["results"], [])
        self.assertEqual(Store(s.directory).data["results"], [])
        self.assertEqual(s.data["overview_batches"], [])
        self.assertFalse(s.data["schedule"]["enabled"])
