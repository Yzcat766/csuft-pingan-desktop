import copy
import unittest
from unittest.mock import patch
from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QApplication, QDialog, QPlainTextEdit
import test_gui as gui
from qt_ui import OffsetChoice, AccountDialog
from storage import Store
from test_position_offset import OfflineServer, FixedRandom, CENTER
from test_app import task
from runner import run_accounts


class OffsetGuiTests(unittest.TestCase):
    setUp = gui.GuiTests.setUp
    tearDown = gui.GuiTests.tearDown
    accounts = gui.GuiTests.accounts

    def prepare(self):
        self.accounts()
        self.store.data["locations"][0].update(latitude=CENTER[0], longitude=CENTER[1])
        OfflineServer.calls, OfflineServer.errors, OfflineServer.signed = [], [], set()
        OfflineServer.sample, OfflineServer.on_sign, OfflineServer.on_query = (
            task(),
            None,
            None,
        )

    def offline(self, *args, **kwargs):
        kwargs.update(
            client_factory=OfflineServer, delay_scale=0, random_source=FixedRandom()
        )
        return run_accounts(*args, **kwargs)

    def test_global_validation_saved_status_and_draft_survives_refresh(self):
        choice = self.window.global_offset
        self.assertEqual(choice.checked_value()["mode"], "off")
        choice.mode.setCurrentIndex(choice.mode.findData("on"))
        choice.radius.setText("nan")
        self.window.save_global_location()
        self.assertEqual(self.store.data["global_offset"]["mode"], "off")
        self.assertIn("有限数字", self.window.notice.text())
        self.window.refresh_locations()
        self.assertEqual(choice.radius.text(), "nan")
        choice.radius.setText("20")
        self.assertIn("未保存", self.window.global_dirty.text())
        self.window.save_global_location()
        self.assertIn("已保存", self.window.global_dirty.text())
        self.assertEqual(
            Store(self.folder.name).data["global_offset"], {"mode": "on", "radius": 20}
        )

    def test_account_override_and_scroll_preserve_validation(self):
        self.accounts()
        dialog = AccountDialog(self.window, self.store.data["accounts"][0])
        try:
            dialog.show()
            dialog.offset_choice.mode.setCurrentIndex(
                dialog.offset_choice.mode.findData("on")
            )
            dialog.offset_choice.radius.setText("501")
            dialog.save()
            self.assertIsNone(dialog.values)
            self.assertIn("500", dialog.notice.text())
            dialog.offset_choice.radius.setText("3")
            dialog.save()
            self.assertEqual(dialog.values["offset"], {"mode": "on", "radius": 3})
        finally:
            dialog.close()

    def test_batch_preview_uses_frozen_center_and_radius_and_cancel_never_submits(self):
        self.prepare()
        self.store.data["global_offset"] = {"mode": "on", "radius": 20}
        with patch("qt_ui.run_accounts", side_effect=self.offline):
            self.window.start_run()
            request = self.window.pending_request
            self.assertEqual(request["accounts"][0]["run_offset"]["radius"], 20)
            self.store.data["global_offset"]["radius"] = 50
            self.window.preview_dialog.reject()
            QApplication.processEvents()
        self.assertFalse(self.window.busy)
        self.assertEqual(OfflineServer.calls, [])

    def test_actual_worker_persists_position_before_submission_and_shows_details(self):
        self.prepare()
        self.store.data["global_offset"] = {"mode": "on", "radius": 20}
        self.window.confirm_preview.setChecked(False)

        def inspect_checkpoint(client):
            loaded = Store(self.folder.name)
            entry = loaded.data["position_cache"][
                next(
                    a["id"]
                    for a in loaded.data["accounts"]
                    if a["openid"] == client.openid
                )
            ]
            self.assertEqual(entry["submission_count"], 1)
            self.assertEqual(entry["position"]["radius"], 20)

        OfflineServer.on_sign = inspect_checkpoint
        with patch("qt_ui.run_accounts", side_effect=self.offline):
            self.window.start_run()
            gui.pump(lambda: not self.window.busy)
        result = self.store.data["results"][0]
        self.assertEqual(result["status"], "success")
        self.assertIn("实际偏移", self.window.preview_text.text())
        self.assertEqual(
            Store(self.folder.name).data["position_cache"][result["account_id"]][
                "unresolved"
            ],
            False,
        )
        captured = []

        def check_dialog():
            dialog = next(
                d
                for d in self.window.findChildren(QDialog)
                if d.isVisible() and d.windowTitle() == "任务详情"
            )
            captured.append(dialog.findChild(QPlainTextEdit).toPlainText())
            dialog.accept()

        QTimer.singleShot(30, check_dialog)
        self.window.show_detail(result)
        for text in (
            "中心坐标",
            "配置半径",
            "实际偏移",
            "选用阶段",
            "候选次数",
            "调整原因",
        ):
            self.assertIn(text, captured[0])
        self.assertNotIn("mock-private", captured[0])

    def test_worker_checkpoint_failure_sends_no_school_submission(self):
        self.prepare()
        self.store.data["global_offset"] = {"mode": "on", "radius": 20}
        self.window.confirm_preview.setChecked(False)
        with patch("qt_ui.run_accounts", side_effect=self.offline), patch.object(
            self.store, "save", side_effect=OSError("mock read only")
        ):
            self.window.start_run()
            gui.pump(lambda: not self.window.busy)
        self.assertFalse(any(call[0] == "submit" for call in OfflineServer.calls))

    def test_restored_settings_reload_saved_offset(self):
        self.store.data["global_offset"] = {"mode": "on", "radius": 13}
        self.window.restored("mock rollback")
        self.assertEqual(
            self.window.global_offset.checked_value(), {"mode": "on", "radius": 13}
        )


if __name__ == "__main__":
    unittest.main()
