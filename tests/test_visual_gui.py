import copy
import unittest
from unittest.mock import patch
from PySide6.QtCore import QTimer, Qt
from PySide6.QtWidgets import (
    QApplication,
    QDialog,
    QPlainTextEdit,
    QDialogButtonBox,
    QDateEdit,
    QPushButton,
)
from PySide6.QtTest import QTest
import test_gui as gui
from ui_components import SettingRow, WrapToolbar


class VisualGuiTests(unittest.TestCase):
    setUp = gui.GuiTests.setUp
    tearDown = gui.GuiTests.tearDown
    accounts = gui.GuiTests.accounts

    def date_action(self, action):
        failures = []

        def inspect():
            try:
                dialog = next(
                    d
                    for d in self.window.findChildren(QDialog)
                    if d.isVisible() and d.windowTitle() == "编辑指定日期"
                )
                action(dialog)
            except Exception as error:
                failures.append(error)
                for dialog in self.window.findChildren(QDialog):
                    dialog.reject()

        QTimer.singleShot(30, inspect)
        self.window.edit_schedule_dates()
        if failures:
            raise failures[0]

    def test_date_dialog_cancel_keeps_draft_and_saved_plan(self):
        w = self.window
        w.schedule_dates.setPlainText("2026-12-12")
        before = copy.deepcopy(self.store.data["schedule"])

        def cancel(dialog):
            dialog.findChild(QPlainTextEdit).setPlainText("2026-12-13")
            dialog.reject()

        self.date_action(cancel)
        self.assertEqual(w.schedule_dates.toPlainText(), "2026-12-12")
        self.assertEqual(self.store.data["schedule"], before)

    def test_date_calendar_add_accepts_only_draft_and_deduplicates(self):
        w = self.window
        w.schedule_dates.setPlainText("")
        original = copy.deepcopy(self.store.data["schedule"])

        def accept(dialog):
            picker = dialog.findChild(QDateEdit)
            add = next(
                b for b in dialog.findChildren(QPushButton) if b.text() == "添加日期"
            )
            add.click()
            add.click()
            self.assertEqual(
                dialog.findChild(QPlainTextEdit).toPlainText(),
                picker.date().toString("yyyy-MM-dd"),
            )
            dialog.findChild(QDialogButtonBox).button(
                QDialogButtonBox.StandardButton.Save
            ).click()

        self.date_action(accept)
        self.assertEqual(len(w.schedule_dates.toPlainText().splitlines()), 1)
        self.assertEqual(self.store.data["schedule"], original)
        self.assertIn("草稿", w.schedule_dates_summary.text())

    def test_date_validation_keeps_dialog_input_and_does_not_apply(self):
        self.window.schedule_dates.setPlainText("2026-12-12")

        def invalid(dialog):
            editor = dialog.findChild(QPlainTextEdit)
            editor.setPlainText("2026-13-99")
            dialog.findChild(QDialogButtonBox).button(
                QDialogButtonBox.StandardButton.Save
            ).click()
            self.assertTrue(dialog.isVisible())
            self.assertEqual(editor.toPlainText(), "2026-13-99")
            self.assertEqual(self.window.schedule_dates.toPlainText(), "2026-12-12")
            dialog.reject()

        self.date_action(invalid)

    def test_hidden_date_summary_and_name_do_not_clear_values_or_run(self):
        w = self.window
        w.navigate("schedule")
        w.schedule_dates.setPlainText("2026-12-12")
        with patch.object(w, "start_run") as run:
            for rule in ("dates", "daily", "weekdays", "dates"):
                w.schedule_mode.setCurrentIndex(w.schedule_mode.findData(rule))
                QApplication.processEvents()
                self.assertEqual(w.schedule_date_control.isVisible(), rule == "dates")
                self.assertEqual(w.schedule_dates.toPlainText(), "2026-12-12")
            run.assert_not_called()

    def test_theme_and_row_reflow_preserve_account_filter_selection_and_drafts(self):
        self.accounts()
        self.store.save()
        w = self.window
        w.navigate("accounts")
        w.account_search.setText("备用")
        w.account_table.selectRow(0)
        selected = [a["id"] for a in w.selected_accounts()]
        w.schedule_dates.setPlainText("2026-12-12")
        w.global_offset.radius.setText("29.5")
        before = self.store.path.read_bytes()
        for theme in ("light", "dark"):
            w.theme = theme
            w.apply_theme()
            w.resize(740, 540)
            QApplication.processEvents()
            self.assertEqual(w.account_search.text(), "备用")
            self.assertEqual([a["id"] for a in w.selected_accounts()], selected)
            self.assertEqual(w.schedule_dates.toPlainText(), "2026-12-12")
            self.assertEqual(w.global_offset.radius.text(), "29.5")
        self.assertEqual(self.store.path.read_bytes(), before)

    def test_history_refresh_keeps_same_result_after_insertion(self):
        self.accounts()
        w = self.window
        identity = self.store.data["accounts"][0]["id"]
        self.store.data["results"] = [
            {
                "account_id": identity,
                "task_id": str(i),
                "label": "模拟",
                "date": "2026-10-10",
                "finished_at": f"2026-10-10T12:{i:02}:00+08:00",
                "status": "success",
                "message": "模拟结果",
            }
            for i in range(30)
        ]
        w.refresh_history()
        w.navigate("history")
        w.history_table.selectRow(12)
        self.store.data["results"].insert(
            0,
            {
                **self.store.data["results"][0],
                "task_id": "new",
                "finished_at": "2026-10-10T13:00:00+08:00",
            },
        )
        w.refresh_history()
        self.assertEqual(
            w.display_results[w.history_table.currentRow()]["task_id"], "12"
        )

    def test_wrapping_log_toolbar_keeps_filter_and_keyboard_focus(self):
        w = self.window
        w.navigate("log")
        page = w.log_page
        page.keyword.setText("长文本条件")
        page.date_mode.setCurrentIndex(1)
        page.keyword.setFocus()
        for size in ((740, 540), (1440, 960)):
            w.resize(*size)
            QApplication.processEvents()
            self.assertEqual(page.keyword.text(), "长文本条件")
            self.assertEqual(page.date_mode.currentIndex(), 1)
            self.assertEqual(QApplication.focusWidget(), page.keyword)
            self.assertTrue(page.date_filter.isVisible())
        page.keyword.clear()
        QTest.keyClicks(page.keyword, "token")
        self.assertEqual(page.keyword.text(), "token")

    def test_setting_rows_have_plain_accessible_copy_and_focusable_switches(self):
        w = self.window
        w.navigate("settings")
        rows = w.findChildren(SettingRow)
        self.assertGreater(len(rows), 20)
        for row in rows:
            self.assertEqual(row.title.textFormat(), Qt.TextFormat.PlainText)
            self.assertTrue(row.control.accessibleName())
        self.assertEqual(w.prevent_sleep.focusPolicy(), Qt.FocusPolicy.StrongFocus)
        self.assertIn(
            "未实现",
            next(r.control.text() for r in rows if r.title.text() == "睡眠与关机"),
        )


if __name__ == "__main__":
    unittest.main()
