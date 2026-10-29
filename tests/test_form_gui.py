import unittest
from unittest.mock import patch
from PySide6.QtCore import Qt, QTime
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QDialog, QLineEdit, QDialogButtonBox
import test_gui as gui
from qt_ui import AccountDialog, LocationDialog
from form_layout import size_field
from storage import Store, defaults


class FormGuiTests(unittest.TestCase):
    setUp = gui.GuiTests.setUp
    tearDown = gui.GuiTests.tearDown
    accounts = gui.GuiTests.accounts

    def choose(self, field, value):
        field.setCurrentIndex(field.findData(value))
        QApplication.processEvents()

    def test_conditional_schedule_rows_preserve_all_unsaved_values(self):
        w = self.window
        w.navigate("schedule")
        self.choose(w.schedule_mode, "dates")
        w.schedule_dates.setPlainText("2026-12-12\n2026-12-13")
        self.choose(w.task_rule_mode, "exact_name")
        w.task_rule_name.setText("宿舍签到")
        w.retry_count.setValue(4)
        w.retry_interval.setValue(1440)
        self.choose(w.schedule_mode, "daily")
        self.choose(w.task_rule_mode, "unique_current")
        w.schedule_retry_enabled.setChecked(False)
        for field in (w.schedule_date_control, w.task_rule_name, w.retry_fields):
            self.assertFalse(field.isVisible())
        self.assertFalse(
            w.schedule_form.labelForField(w.schedule_date_control).isVisible()
        )
        self.assertFalse(w.task_form.labelForField(w.task_rule_name).isVisible())
        self.assertFalse(w.retry_form.labelForField(w.retry_count).isVisible())
        self.choose(w.schedule_mode, "dates")
        self.choose(w.task_rule_mode, "exact_name")
        w.schedule_retry_enabled.setChecked(True)
        self.assertEqual(w.schedule_dates.toPlainText(), "2026-12-12\n2026-12-13")
        self.assertEqual(w.task_rule_name.text(), "宿舍签到")
        self.assertEqual((w.retry_count.value(), w.retry_interval.value()), (4, 1440))
        self.assertTrue(w.schedule_date_control.isVisible())
        self.assertIn("未保存", w.schedule_badge.text())

    def test_hidden_valid_fields_persist_and_reopen_without_enabling_plan(self):
        w = self.window
        w.schedule_dates.setPlainText("2026-12-12")
        w.task_rule_name.setText("保留名称")
        w.retry_count.setValue(5)
        w.retry_interval.setValue(1440)
        w.schedule_retry_enabled.setChecked(False)
        w.save_schedule()
        saved = Store(self.folder.name).data["schedule"]
        self.assertEqual(saved["dates"], ["2026-12-12"])
        self.assertEqual(saved["task_rule"]["name"], "保留名称")
        self.assertEqual(
            saved["retry"],
            {"enabled": False, "max_retries": 5, "interval_minutes": 1440},
        )
        self.assertFalse(saved["enabled"])
        w.load_schedule()
        self.assertEqual(w.schedule_dates.toPlainText(), "2026-12-12")

    def test_inactive_invalid_date_draft_does_not_block_or_poison_saved_plan(self):
        w = self.window
        w.schedule_dates.setPlainText("unfinished-date")
        w.schedule_time.setTime(QTime(22, 31))
        w.save_schedule()
        saved = Store(self.folder.name).data["schedule"]
        self.assertEqual(saved["time"], "22:31")
        self.assertEqual(saved["dates"], [])
        self.assertEqual(w.schedule_dates.toPlainText(), "unfinished-date")
        self.choose(w.schedule_mode, "dates")
        w.save_schedule()
        self.assertIn("计划未保存", w.notice.text())
        self.assertEqual(Store(self.folder.name).data["schedule"]["mode"], "daily")

    def test_restore_discards_draft_only_on_explicit_restore(self):
        w = self.window
        w.schedule_dates.setPlainText("2026-12-12")
        self.choose(w.schedule_mode, "dates")
        self.choose(w.schedule_mode, "daily")
        self.assertEqual(w.schedule_dates.toPlainText(), "2026-12-12")
        w.load_schedule()
        self.assertEqual(w.schedule_dates.toPlainText(), "")

    def test_offset_off_hides_radius_preserves_valid_value_and_restart(self):
        w = self.window
        w.navigate("location")
        choice = w.global_offset
        self.choose(choice.mode, "on")
        choice.radius.setText("37.5")
        self.choose(choice.mode, "off")
        self.assertFalse(choice.radius_section.isVisible())
        w.save_global_location()
        self.assertEqual(
            Store(self.folder.name).data["global_offset"],
            {"mode": "off", "radius": 37.5},
        )
        self.choose(choice.mode, "on")
        self.assertEqual(choice.radius.text(), "37.5")

    def test_offset_inactive_invalid_draft_keeps_last_saved_valid_radius(self):
        w = self.window
        choice = w.global_offset
        self.choose(choice.mode, "on")
        choice.radius.setText("25")
        w.save_global_location()
        choice.radius.setText("nan")
        self.choose(choice.mode, "off")
        w.save_global_location()
        self.assertEqual(
            Store(self.folder.name).data["global_offset"], {"mode": "off", "radius": 25}
        )
        self.choose(choice.mode, "on")
        self.assertEqual(choice.radius.text(), "nan")
        w.save_global_location()
        self.assertEqual(Store(self.folder.name).data["global_offset"]["mode"], "off")

    def test_short_fields_do_not_grow_with_large_window_and_long_path_expands(self):
        w = self.window
        for size in ((960, 650), (1600, 1000)):
            w.resize(*size)
            w.navigate("schedule")
            QApplication.processEvents()
            for field in (
                w.schedule_time,
                w.retry_count,
                w.retry_interval,
                w.schedule_mode,
                w.task_rule_mode,
            ):
                self.assertLessEqual(field.width(), field.property("formTargetWidth"))
                self.assertGreaterEqual(
                    field.height(), field.fontMetrics().height() + 20
                )
            self.assertEqual(
                w.schedule_time.mapTo(w, w.schedule_time.rect().topRight()).x(),
                w.schedule_mode.mapTo(w, w.schedule_mode.rect().topRight()).x(),
            )
        w.navigate("settings")
        QApplication.processEvents()
        self.assertGreater(w.data_path.width(), w.retention_days.width() * 3)
        self.assertEqual(w.data_path.toolTip(), w.data_path.text())

    def test_retry_group_stacks_without_losing_native_keyboard_editing(self):
        w = self.window
        w.navigate("schedule")
        setting_row = w.retry_form.rows[w.retry_count]
        setting_row.resize(160, 170)
        setting_row.reflow()
        self.assertTrue(setting_row.stacked)
        w.retry_count.setFocus()
        before = w.retry_count.value()
        QTest.keyClick(w.retry_count, Qt.Key.Key_Up)
        self.assertEqual(w.retry_count.value(), before + 1)
        self.assertTrue(w.retry_count.accessibleName())

    def test_account_cancel_leaves_saved_account_unchanged(self):
        self.accounts()
        before = self.store.data["accounts"][0].copy()
        dialog = AccountDialog(self.window, before)
        dialog.show()
        dialog.name.setText("草稿")
        dialog.offset_choice.mode.setCurrentIndex(
            dialog.offset_choice.mode.findData("on")
        )
        dialog.offset_choice.radius.setText("123")
        dialog.reject()
        self.assertIsNone(dialog.values)
        self.assertEqual(self.store.data["accounts"][0], before)

    def test_location_errors_are_visible_and_do_not_save_invalid_coordinates(self):
        dialog = LocationDialog(self.window)
        dialog.show()
        dialog.name.setText("测试")
        dialog.latitude.setText("999")
        dialog.longitude.setText("bad")
        dialog.save()
        self.assertTrue(dialog.errors["latitude"].isVisible())
        dialog.latitude.setText("28.131567")
        dialog.save()
        self.assertTrue(dialog.errors["longitude"].isVisible())
        self.assertTrue(dialog.save_button.isVisible())
        dialog.reject()

    def test_backup_password_inputs_use_shared_width_and_accessibility(self):
        from PySide6.QtCore import QTimer

        observations = []

        def inspect():
            dialog = next(
                d
                for d in self.window.findChildren(QDialog)
                if d.isVisible() and d.windowTitle() == "布局备份"
            )
            fields = dialog.findChildren(QLineEdit)
            observations.extend((f.accessibleName(), f.maximumWidth()) for f in fields)
            dialog.reject()

        QTimer.singleShot(30, inspect)
        self.assertIsNone(self.window.ask_password("布局备份", True))
        self.assertEqual(len(observations), 2)
        self.assertTrue(all(name and width < 600 for name, width in observations))

    def test_legacy_defaults_load_and_never_execute_on_rule_change(self):
        self.store.data["schedule"] = {
            key: value
            for key, value in defaults()["schedule"].items()
            if key not in ("retry", "task_rule")
        }
        self.store.save()
        self.store.data = Store(self.folder.name).data
        self.window.load_schedule()
        with patch.object(self.window, "start_run") as start:
            self.choose(self.window.schedule_mode, "dates")
            self.window.schedule_retry_enabled.setChecked(False)
            self.choose(self.window.task_rule_mode, "exact_name")
            start.assert_not_called()
        self.assertFalse(self.store.data["schedule"]["enabled"])


if __name__ == "__main__":
    unittest.main()
