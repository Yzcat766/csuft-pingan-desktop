import copy
import unittest
from datetime import timedelta
from unittest.mock import patch
from PySide6.QtCore import Qt, QPoint
from PySide6.QtWidgets import QDialog
import test_gui as gui
from overview import record_batch
from readiness import check_ready
from desktop_common import DemoClient
from execution import make_request
from storage import Store, now_shanghai
from overview import today_metrics


class OverviewGuiTests(unittest.TestCase):
    setUp = gui.GuiTests.setUp
    tearDown = gui.GuiTests.tearDown
    accounts = gui.GuiTests.accounts

    def test_empty_overview_has_one_setup_entry_without_duplicate_status_blocks(self):
        self.window.refresh()
        self.assertTrue(self.window.overview_empty.isVisible())
        for widget in (
            self.window.stat_widget,
            self.window.readiness_card,
            self.window.onboarding_card,
            self.window.attention_card,
            self.window.recent_card,
        ):
            self.assertFalse(widget.isVisible())

    def test_closed_plan_is_normal_collapsed_and_does_not_disable_manual_button(self):
        self.accounts()
        self.window.refresh_stats()
        self.assertIn("自动打卡已关闭", self.window.stat_values[3].text())
        self.assertIn("手动执行不受", self.window.readiness_state.text())
        self.assertFalse(self.window.readiness_details.isVisible())
        self.assertTrue(self.window.primary_action.isEnabled())
        self.assertEqual(self.window.primary_action.text(), "打卡全部启用账号（2）")
        self.assertIn("未知", self.window.network_state.text())

    def test_repeat_refresh_uses_cached_local_checks_without_disk_probe_or_network(
        self,
    ):
        self.accounts()
        self.window._readiness_signature = None
        with patch("overview_ui.check_ready", wraps=check_ready) as checker:
            for _ in range(20):
                self.window.refresh_stats()
            self.assertEqual(checker.call_count, 1)
            self.assertFalse(checker.call_args.kwargs["probe"])
            self.store.data["accounts"][0]["location"] = {
                "mode": "specified",
                "location_id": "missing",
            }
            self.window.refresh_stats()
            self.assertEqual(checker.call_count, 2)
        self.assertTrue(self.window.attention_card.isVisible())

    def test_failure_after_success_and_disabled_account_statistics_are_explained(self):
        self.accounts()
        first, second = self.store.data["accounts"]
        second["enabled"] = False
        now = now_shanghai()
        self.store.data["results"] = [
            {
                "account_id": second["id"],
                "label": "备用",
                "date": now.date().isoformat(),
                "status": "success",
                "task_id": "a",
                "finished_at": now.isoformat(),
                "message": "模拟",
            },
            {
                "account_id": second["id"],
                "label": "备用",
                "date": now.date().isoformat(),
                "status": "failure",
                "task_id": "b",
                "finished_at": (now + timedelta(seconds=1)).isoformat(),
                "message": "模拟失败",
            },
        ]
        self.window.refresh()
        self.assertEqual(self.window.stat_values[0].text(), "1")
        self.assertEqual(self.window.stat_values[1].text(), "1")
        self.assertIn("失败 1", self.window.stat_values[2].text())
        self.assertIn("不代表全部", self.window.today_scope.text())

    def test_batch_rows_and_sorted_clicks_keep_stable_batch_identity(self):
        self.accounts()
        now = now_shanghai()
        for index in range(2):
            request = make_request(self.store.data, moment=now)
            result = {
                "account_id": self.store.data["accounts"][0]["id"],
                "label": "模拟",
                "status": "success",
                "date": now.date().isoformat(),
                "finished_at": (now + timedelta(minutes=index)).isoformat(),
                "run_id": request["run_id"],
                "message": "模拟",
            }
            self.store.data["results"].append(result)
            record_batch(
                self.store.data, request, [result], now + timedelta(minutes=index)
            )
        self.window.recent_table.setSortingEnabled(True)
        self.window.refresh_recent()
        self.window.recent_table.sortItems(0, Qt.SortOrder.AscendingOrder)
        expected = self.window.recent_table.item(0, 0).data(Qt.ItemDataRole.UserRole)[
            "batch_id"
        ]
        with patch.object(self.window, "show_batch_detail") as show:
            self.window.recent_detail(0)
        self.assertEqual(show.call_args.args[0]["batch_id"], expected)
        self.assertEqual(self.window.recent_table.rowCount(), 2)

    def test_run_card_cancel_and_stage_visible_without_scrolling_in_common_window(self):
        self.accounts()
        self.window.resize(960, 650)
        self.window.confirm_preview.setChecked(False)
        with patch(
            "qt_ui.run_accounts", side_effect=gui.GuiTests.offline.__get__(self)
        ):
            self.window.start_run()
            self.assertTrue(self.window.run_card.isVisible())
            viewport = self.window.stack.currentWidget().viewport()
            for widget in (
                self.window.cancel_button,
                self.window.run_stage,
                self.window.progress,
            ):
                point = widget.mapTo(viewport, QPoint(0, 0))
                self.assertGreaterEqual(point.y(), 0)
                self.assertLessEqual(point.y() + widget.height(), viewport.height())
            self.window.cancel_run()
            gui.pump(lambda: not self.window.busy)

    def test_position_details_stay_collapsed_and_remain_available(self):
        self.accounts()
        self.window.confirm_preview.setChecked(False)
        with patch("qt_ui.DemoClient", DemoClient):
            self.window.start_run()
            gui.pump(lambda: not self.window.busy)
        self.assertFalse(self.window.preview_toggle.isChecked())
        self.assertIn("纬度", self.window.preview_text.text())
        self.assertEqual(self.window.recent_table.rowCount(), 1)
        self.assertNotIn("成功 2", self.window.footer_status.text())

    def test_result_save_failure_priority_and_retry_keep_batch_metadata(self):
        self.accounts()
        self.window.pending_results = []
        self.window.refresh_stats()
        self.assertTrue(self.window.save_alert.isVisible())
        self.assertTrue(self.window.retry_save_button.isVisible())
        self.assertLess(self.window.save_alert.y(), self.window.readiness_card.y())
        self.window.retry_results()
        self.assertIsNone(self.window.pending_results)
        self.assertFalse(self.window.save_alert.isVisible())

    def test_legacy_log_failure_remains_actionable_until_real_retry_succeeds(self):
        self.window.legacy_log_pending = (["模拟日志"], "失败")
        self.window.refresh_stats()
        with patch.object(
            self.store, "write_log", side_effect=OSError("mock read only")
        ):
            self.assertFalse(self.window.retry_overview_logs())
        self.assertTrue(self.window.save_alert.isVisible())
        self.assertIsNotNone(self.window.legacy_log_pending)
        self.assertTrue(self.window.retry_overview_logs())
        self.assertIsNone(self.window.legacy_log_pending)

    def test_onboarding_can_complete_without_automatic_plan_and_stays_closed_after_restart(
        self,
    ):
        self.accounts()
        self.window.open_onboarding()
        dialog = next(
            d
            for d in self.window.findChildren(QDialog)
            if d.windowTitle() == "首次使用引导"
        )
        self.window.dismiss_onboarding()
        dialog.close()
        loaded = Store(self.folder.name)
        self.assertTrue(loaded.data["onboarding_done"])
        self.assertFalse(loaded.data["schedule"]["enabled"])
        self.assertFalse(loaded.data["reliability"]["autostart"])

    def test_cross_day_clears_transient_batch_summary_without_deleting_history(self):
        self.accounts()
        now = now_shanghai()
        request = make_request(self.store.data, moment=now)
        record_batch(self.store.data, request, [], now)
        self.window.refresh()
        self.window.refresh_overview_state(now + timedelta(days=1))
        self.assertEqual(self.window.batch_summary.text(), "")
        self.assertEqual(self.window.recent_table.rowCount(), 1)


if __name__ == "__main__":
    unittest.main()
