"""Offline screenshots and layout checks of actual Qt widgets, never user data."""

import argparse
import json
import tempfile
import time
from pathlib import Path
from unittest.mock import patch
from PySide6.QtCore import QTime, QTimer
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QPushButton, QDialog, QPlainTextEdit
from app import create_application, App, AccountDialog
from qt_ui import PAGES, PAGE_INDEX, LocationDialog
from desktop_common import DemoClient
from storage import Store, new_account


def wait_for(check, timeout=8):
    deadline = time.monotonic() + timeout
    while not check() and time.monotonic() < deadline:
        QApplication.processEvents()
        QTest.qWait(10)
    if not check():
        raise RuntimeError("离线界面任务超时")


class FailureClient(DemoClient):
    def sign(self, task, lat, lng):
        # A rejected/failing final status must remain a failure in the real runner.
        return {
            "signStatus": 3,
            "signDate": self.get_status(task["taskId"])["signDate"],
        }


def capture(output, suffix):
    output.mkdir(parents=True, exist_ok=True)
    application = create_application()
    report = {
        "scale": suffix,
        "network": "blocked",
        "checks": [],
        "screenshots": [],
        "dialog_checks": [],
        "populated_log_checks": [],
        "offset_checks": [],
    }
    with tempfile.TemporaryDirectory(prefix="pingan-ui-qa-") as folder, patch(
        "requests.Session.request", side_effect=AssertionError("Real network forbidden")
    ):
        store = Store(folder)
        store.data["accounts"] = [
            new_account("mock-qa-a", "我的账号 · 模拟"),
            new_account("mock-qa-b", "备用账号 · 模拟"),
        ]
        store.data["global_offset"] = {"mode": "on", "radius": 20}
        window = App(store, demo=True)
        window.timer.stop()
        window.show()
        QTest.qWait(120)
        report["device_pixel_ratio"] = window.devicePixelRatioF()

        def shot(name, page=None):
            if page is not None:
                window.navigate(page)
                if page == "log" or page == PAGE_INDEX["log"]:
                    wait_for(
                        lambda: window.log_page.worker is None
                        and not window.log_page.pending
                        and not window.log_page.debounce.isActive()
                        and not window.log_page.follow_timer.isActive()
                    )
            window.notice.hide()
            QApplication.processEvents()
            QTest.qWait(40)
            path = output / f"{name}{suffix}.png"
            if not window.grab().save(str(path)):
                raise RuntimeError("截图无法保存")
            report["screenshots"].append(path.name)

        def location_shots(theme):
            for editing in (False, True):
                dialog = (
                    window.edit_location(store.data["locations"][0]["id"])
                    if editing
                    else window.new_location()
                )
                QApplication.processEvents()
                QTest.qWait(30)
                assert dialog.width() >= 470
                for field in (dialog.name, dialog.latitude, dialog.longitude):
                    assert field.width() >= 170
                    assert field.isVisible()
                path = (
                    output
                    / f"{theme}-location-{'edit' if editing else 'create'}{suffix}.png"
                )
                dialog.grab().save(str(path))
                report["screenshots"].append(path.name)
                report["dialog_checks"].append(
                    {
                        "theme": theme,
                        "mode": "edit" if editing else "create",
                        "width": dialog.width(),
                        "height": dialog.height(),
                    }
                )
                dialog.reject()

        try:
            for theme in ("dark", "light"):
                window.theme = theme
                window.apply_theme()
                for size in ((740, 540), (960, 650), (1240, 840), (1440, 960)):
                    window.resize(*size)
                    for page in range(len(PAGES)):
                        window.navigate(page)
                        QApplication.processEvents()
                        scroll = window.stack.currentWidget()
                        if (
                            scroll.widget().width() < scroll.viewport().width()
                            or scroll.widget().height() < scroll.viewport().height()
                        ):
                            raise AssertionError("滚动页面尺寸错误")
                        if (
                            window.primary_action.geometry().right()
                            >= window.primary_action.parentWidget().width()
                        ):
                            raise AssertionError("主操作超出窗口")
                        if (
                            window.log_toggle.geometry().right()
                            >= window.log_toggle.parentWidget().width()
                        ):
                            raise AssertionError("日志按钮超出窗口")
                        report["checks"].append(
                            {
                                "theme": theme,
                                "size": list(size),
                                "page": page,
                                "horizontal_scroll": scroll.horizontalScrollBar().maximum(),
                                "vertical_scroll": scroll.verticalScrollBar().maximum(),
                            }
                        )
                    window.navigate("location")
                    assert window.global_offset.radius.width() >= 130
                    dialog = AccountDialog(window, store.data["accounts"][0])
                    dialog.resize(480, 440)
                    dialog.show()
                    QApplication.processEvents()
                    dialog.form_scroll.ensureWidgetVisible(dialog.offset_choice)
                    QApplication.processEvents()
                    assert dialog.offset_choice.mode.width() > 130
                    assert dialog.form_scroll.verticalScrollBar().maximum() >= 0
                    report["offset_checks"].append(
                        {
                            "theme": theme,
                            "window_size": list(size),
                            "account_dialog": [dialog.width(), dialog.height()],
                            "radius_width": window.global_offset.radius.width(),
                        }
                    )
                    dialog.reject()
            window.resize(1240, 840)
            window.theme = "dark"
            window.apply_theme()
            window.confirm_preview.setChecked(False)
            window.notice.hide()
            window.start_run()
            shot("dark-running", "overview")
            wait_for(lambda: not window.busy)
            assert all(
                result["status"] == "success" for result in store.data["results"]
            )
            for theme in ("dark", "light"):
                window.theme = theme
                window.apply_theme()
                for size in ((740, 540), (960, 650), (1240, 840), (1440, 960)):
                    window.resize(*size)
                    window.navigate("log")
                    wait_for(
                        lambda: window.log_page.worker is None
                        and not window.log_page.pending
                        and not window.log_page.debounce.isActive()
                        and not window.log_page.follow_timer.isActive()
                    )
                    assert window.log_page.table.rowCount() > 0
                    assert window.log_page.table.width() > 300
                    scroll = window.stack.currentWidget()
                    assert scroll.widget().width() >= scroll.viewport().width()
                    report["populated_log_checks"].append(
                        {
                            "theme": theme,
                            "size": list(size),
                            "rows": window.log_page.table.rowCount(),
                            "horizontal_scroll": scroll.horizontalScrollBar().maximum(),
                            "vertical_scroll": scroll.verticalScrollBar().maximum(),
                        }
                    )
            window.resize(1240, 840)
            window.theme = "dark"
            window.apply_theme()
            shot("dark-overview", "overview")
            shot("dark-offset-settings", "location")
            window.navigate("overview")
            window.stack.currentWidget().ensureWidgetVisible(window.preview_card)
            QTest.qWait(30)
            shot("offset-final-position")
            detail_failures = []

            def capture_detail():
                try:
                    dialog = next(
                        d
                        for d in window.findChildren(QDialog)
                        if d.isVisible() and d.windowTitle() == "任务详情"
                    )
                    text = dialog.findChild(QPlainTextEdit)
                    assert "实际偏移" in text.toPlainText()
                    assert "中心坐标" in text.toPlainText()
                    dialog.resize(760, 740)
                    QApplication.processEvents()
                    path = output / f"offset-result{suffix}.png"
                    dialog.grab().save(str(path))
                    report["screenshots"].append(path.name)
                    dialog.accept()
                except Exception as error:
                    detail_failures.append(str(error))
                    for dialog in window.findChildren(QDialog):
                        if dialog.isVisible():
                            dialog.reject()

            QTimer.singleShot(50, capture_detail)
            window.show_detail(store.data["results"][0])
            assert not detail_failures, detail_failures
            shot("dark-accounts", "accounts")
            window.confirm_preview.setChecked(True)
            store.data["accounts"][1]["enabled"] = False
            window.refresh_accounts()
            window.start_run(
                scope="single", identities=[store.data["accounts"][1]["id"]]
            )
            QTest.qWait(50)
            path = output / f"batch-preview{suffix}.png"
            window.preview_dialog.grab().save(str(path))
            report["screenshots"].append(path.name)
            window.preview_dialog.reject()
            store.data["accounts"][1]["enabled"] = True
            window.refresh_accounts()
            window.confirm_preview.setChecked(False)
            shot("dark-locations", "location")
            shot("dark-history", "history")
            shot("dark-run-logs", "log")
            window.navigate("location")
            location_shots("dark")
            shot("dark-settings", "settings")
            scroll = window.stack.currentWidget()
            scroll.ensureWidgetVisible(window.security_status)
            QTest.qWait(50)
            shot("dark-data-security")
            scroll.verticalScrollBar().setValue(0)
            from task_selection import check_accounts
            import threading

            checked = check_accounts(
                store.data["accounts"],
                store.snapshot(),
                lambda _: None,
                threading.Event(),
                DemoClient,
            )
            text = "\n\n".join(
                f"{value['label']}：可登录（未提交打卡）\n"
                + "\n".join(
                    f"{item['name']} · {item['date']} · {item['start']}–{item['end']}"
                    for item in value["tasks"]
                )
                + "\n"
                + "\n".join(value["issues"])
                for value in checked
            )
            result_dialog = window.show_text(
                "检查账号 · 不代表实际打卡成功", text, next_steps=True
            )
            QTest.qWait(50)
            path = output / f"account-check{suffix}.png"
            result_dialog.grab().save(str(path))
            report["screenshots"].append(path.name)
            result_dialog.close()
            from qt_ui import RunWorker
            from execution import make_request

            chooser_worker = RunWorker(
                make_request(store.snapshot()), threading.Event(), True, window
            )
            window.choose_worker_task(
                {"id": "demo", "label": "模拟账号"},
                [
                    {
                        "task_id": "demo-first",
                        "name": "晚间签到 · 模拟",
                        "date": checked[0]["tasks"][0]["date"],
                        "start": "20:00:00",
                        "end": "22:00:00",
                    },
                    {
                        "task_id": "demo-second",
                        "name": "晚间签到另一项 · 模拟",
                        "date": checked[0]["tasks"][0]["date"],
                        "start": "20:00:00",
                        "end": "23:00:00",
                    },
                ],
                chooser_worker,
            )
            QTest.qWait(50)
            path = output / f"task-selection{suffix}.png"
            window.task_dialog.grab().save(str(path))
            report["screenshots"].append(path.name)
            window.task_dialog.reject()
            chooser_worker.deleteLater()
            window.schedule_time.setTime(QTime(21, 5))
            shot("dark-schedule", "schedule")
            window.theme = "light"
            window.apply_theme()
            shot("light-accounts", "accounts")
            shot("light-overview", "overview")
            shot("light-offset-settings", "location")
            shot("light-run-logs", "log")
            window.navigate("location")
            location_shots("light")
            dialog = AccountDialog(window, store.data["accounts"][0])
            dialog.show()
            QTest.qWait(50)
            path = output / f"account-dialog{suffix}.png"
            dialog.grab().save(str(path))
            report["screenshots"].append(path.name)
            dialog.reject()
            window.theme = "dark"
            window.apply_theme()
            store.data["results"].clear()
            store.data["success_dates"].clear()
            with patch("qt_ui.DemoClient", FailureClient):
                window.start_run()
                wait_for(lambda: not window.busy)
            assert store.data["results"][0]["status"] == "failure"
            window.history_result.setCurrentIndex(
                window.history_result.findData("failure")
            )
            shot("dark-failure", "history")
            shot("dark-account-failure", "accounts")
            window.open_result_logs(store.data["results"][0])
            shot("dark-error-logs", "log")
            window.log_page.clear_filters()
            assert window.account_table.horizontalScrollBar().maximum() == 0
            for row in range(window.account_table.rowCount()):
                actions = window.account_table.cellWidget(row, 5)
                for control in actions.findChildren(QPushButton):
                    assert (
                        control.geometry().right() < actions.width()
                    ), "账号操作按钮超出单元格"
            window.start_run()
            window.cancel_run()
            wait_for(lambda: not window.busy)
            assert store.data["results"][0]["status"] == "cancelled"
            window.history_result.setCurrentIndex(
                window.history_result.findData("cancelled")
            )
            shot("dark-cancelled", "history")
            window.reset_history_filters()
            window.log_toggle.setChecked(True)
            shot("dark-logs", "overview")
            window.log_toggle.setChecked(False)
            store.data["accounts"] = []
            window.refresh()
            shot("dark-empty", "accounts")
            window.resize(740, 540)
            window.toggle_sidebar()
            shot("dark-small", "location")
            assert not window.busy
        finally:
            if window.busy:
                window.cancel_run()
                wait_for(lambda: not window.busy)
            window.shutdown()
            wait_for(lambda: window._destroyed)
            QApplication.processEvents()
    path = output / f"layout-report{suffix}.json"
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(
        json.dumps(
            {
                "report": str(path.resolve()),
                "dpr": report["device_pixel_ratio"],
                "layout_checks": len(report["checks"]),
                "screenshots": len(report["screenshots"]),
            },
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output", type=Path, default=Path(__file__).resolve().parent / "screenshots"
    )
    parser.add_argument("--suffix", default="")
    args = parser.parse_args()
    capture(args.output, args.suffix)
