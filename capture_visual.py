"""Whole-app visual QA: actual Qt pages and dialogs, blocked HTTP, temporary data."""

import argparse
import json
import tempfile
from pathlib import Path
from unittest.mock import patch
from PySide6.QtCore import QPoint, QTimer
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QDialog, QPlainTextEdit, QMessageBox
from app import create_application, App
from qt_ui import PAGES, PAGE_INDEX
from storage import Store, new_account
from desktop_common import DemoClient
from runner import run_accounts
from capture_ui import wait_for
from ui_components import SettingRow, WrapToolbar


def capture(output, suffix=""):
    create_application()
    output.mkdir(exist_ok=True)
    report = {
        "network": "blocked",
        "checks": [],
        "dialog_checks": [],
        "screenshots": [],
    }
    with tempfile.TemporaryDirectory(prefix="visual-qa-") as folder, patch(
        "requests.Session.request", side_effect=AssertionError("Real HTTP forbidden")
    ):
        for state in ("empty", "normal", "long"):
            store = Store(Path(folder) / state)
            if state != "empty":
                store.data["accounts"] = [
                    new_account("mock-visual-a", "我的账号 · 模拟"),
                    new_account("mock-visual-b", "备用账号 · 模拟"),
                ]
            window = App(store, demo=True)
            window.timer.stop()
            window.show()
            QApplication.processEvents()
            report["device_pixel_ratio"] = window.devicePixelRatioF()
            try:
                if state != "empty":
                    window.confirm_preview.setChecked(False)

                    def offline(*args, **kwargs):
                        kwargs.update(client_factory=DemoClient, delay_scale=0)
                        return run_accounts(*args, **kwargs)

                    with patch("qt_ui.run_accounts", side_effect=offline):
                        window.start_run()
                        wait_for(lambda: not window.busy)
                    assert all(r["status"] == "success" for r in store.data["results"])
                if state == "long":
                    store.data["accounts"][0]["label"] = (
                        "模拟账号 · " + "长名称检查" * 22
                    )
                    store.data["locations"][0]["name"] = (
                        "模拟位置 · " + "宿舍楼与位置名称" * 25
                    )
                    store.data["global_location"] = {
                        "mode": "specified",
                        "location_id": store.data["locations"][0]["id"],
                    }
                    window.global_choice.refresh(store.data["global_location"])
                    for result in store.data["results"]:
                        result["message"] = "模拟长消息，打开详情查看完整内容。" * 80
                    window.journal.append(
                        {
                            "step": "长消息验证",
                            "message": "模拟长日志，选中可阅读完整内容。" * 200,
                        }
                    )
                    window.journal.flush()
                    window.log_page.refresh_batches()
                    window.data_path.setText(
                        "D:\\模拟路径\\" + "长目录与空格 " * 30 + "data"
                    )
                    window.data_path.setToolTip(window.data_path.text())
                    window.schedule_mode.setCurrentIndex(
                        window.schedule_mode.findData("dates")
                    )
                    window.schedule_dates.setPlainText("2026-12-12\n2026-12-13")
                    window.task_rule_mode.setCurrentIndex(
                        window.task_rule_mode.findData("exact_name")
                    )
                    window.task_rule_name.setText("模拟任务 · " + "晚间签到" * 30)
                    window.refresh()

                def shot(name, widget=window):
                    path = output / f"visual-{name}{suffix}.png"
                    assert widget.grab().save(str(path))
                    report["screenshots"].append(path.name)

                def idle_log():
                    wait_for(
                        lambda: window.log_page.worker is None
                        and not window.log_page.pending
                        and not window.log_page.debounce.isActive()
                        and not window.log_page.follow_timer.isActive()
                    )

                for theme in ("light", "dark"):
                    window.theme = theme
                    window.apply_theme()
                    for size in ((740, 540), (960, 650), (1240, 840), (1600, 1000)):
                        window.resize(*size)
                        for page, (_, key, _) in enumerate(PAGES):
                            window.navigate(page)
                            if key == "log":
                                idle_log()
                                if state == "long" and window.log_page.table.rowCount():
                                    window.log_page.table.selectRow(
                                        window.log_page.table.rowCount() - 1
                                    )
                                    assert (
                                        len(window.log_page.detail.toPlainText()) > 2000
                                    )
                            QApplication.processEvents()
                            QTest.qWait(15)
                            scroll = window.stack.currentWidget()
                            assert scroll.horizontalScrollBar().maximum() == 0, (
                                state,
                                theme,
                                size,
                                key,
                                scroll.horizontalScrollBar().maximum(),
                            )
                            for row in scroll.widget().findChildren(SettingRow):
                                if not row.isVisible():
                                    continue
                                control = row.control
                                top = control.mapTo(row, QPoint(0, 0))
                                assert (
                                    top.x() >= 0
                                    and top.x() + control.width() <= row.width() + 1
                                ), (
                                    key,
                                    row.title.text(),
                                    row.width(),
                                    control.width(),
                                    top.x(),
                                )
                                if row.stacked:
                                    assert (
                                        top.y() >= row.copy.geometry().bottom()
                                    ), row.title.text()
                                else:
                                    assert (
                                        row.copy.geometry().right() < top.x()
                                    ), row.title.text()
                            for toolbar in scroll.widget().findChildren(WrapToolbar):
                                for control in toolbar.controls:
                                    if control.isVisible():
                                        top = control.mapTo(toolbar, QPoint(0, 0))
                                        assert (
                                            top.x() + control.width()
                                            <= toolbar.width() + 1
                                        ), (key, "toolbar overflow")
                            report["checks"].append(
                                {
                                    "state": state,
                                    "theme": theme,
                                    "size": size,
                                    "page": key,
                                    "horizontal_scroll": 0,
                                }
                            )
                            if size == (1240, 840) and state in ("normal", "long"):
                                scroll.verticalScrollBar().setValue(0)
                                QApplication.processEvents()
                                shot(f"{state}-{key}-{theme}")
                                if key == "settings":
                                    scroll.ensureWidgetVisible(window.data_path)
                                    QApplication.processEvents()
                                    shot(f"{state}-data-{theme}")
                    if state == "normal":
                        window.resize(1240, 840)
                        window.navigate("schedule")
                        window.schedule_mode.setCurrentIndex(
                            window.schedule_mode.findData("dates")
                        )
                        errors = []

                        def date_shot():
                            try:
                                dialog = next(
                                    d
                                    for d in window.findChildren(QDialog)
                                    if d.isVisible()
                                    and d.windowTitle() == "编辑指定日期"
                                )
                                dialog.findChild(QPlainTextEdit).setPlainText(
                                    "2026-12-12\n2026-12-13"
                                )
                                QApplication.processEvents()
                                shot(f"dates-{theme}", dialog)
                                dialog.reject()
                                report["dialog_checks"].append(
                                    {"theme": theme, "kind": "date-editor-cancel"}
                                )
                            except Exception as error:
                                errors.append(str(error))
                                for d in window.findChildren(QDialog):
                                    d.reject()

                        QTimer.singleShot(40, date_shot)
                        window.edit_schedule_dates()
                        assert not errors, errors
                        from data_management import create_backup

                        backup = Path(folder) / f"mock-{theme}.pingan-backup"
                        create_backup(
                            store.snapshot(),
                            store.directory,
                            backup,
                            "mock-preview-password",
                        )

                        def cancel_confirmation(name):
                            hits = []
                            timer = QTimer(window)

                            def inspect():
                                dialogs = [
                                    d
                                    for d in window.findChildren(QMessageBox)
                                    if d.isVisible()
                                ]
                                if dialogs:
                                    timer.stop()
                                    shot(f"preview-{name}-{theme}", dialogs[0])
                                    hits.append(True)
                                    dialogs[0].reject()

                            timer.timeout.connect(inspect)
                            timer.start(20)
                            return timer, hits

                        before = store.path.read_bytes()
                        timer, hits = cancel_confirmation("备份恢复预览")
                        with patch(
                            "reliability_ui.QFileDialog.getOpenFileName",
                            return_value=(str(backup), ""),
                        ), patch.object(
                            window,
                            "ask_password",
                            return_value=("mock-preview-password", False),
                        ):
                            window.restore_dialog()
                        wait_for(lambda: window.data_job is None and bool(hits))
                        timer.stop()
                        assert store.path.read_bytes() == before
                        report["dialog_checks"].append(
                            {
                                "theme": theme,
                                "kind": "actual-backup-restore-preview-cancel",
                                "data_mutation": False,
                            }
                        )
                        timer, hits = cancel_confirmation("数据模式迁移预览")
                        destination = Path(folder) / f"mock-mode-{theme}"
                        try:
                            window.demo = False
                            original_scrub = window.scrub
                            with patch(
                                "reliability_ui.local_directory",
                                return_value=destination,
                            ), patch.object(
                                window,
                                "scrub",
                                side_effect=lambda text: original_scrub(text).replace(
                                    str(Path(folder)), "D:\\模拟数据"
                                ),
                            ):
                                window.migrate_dialog()
                        finally:
                            window.demo = True
                            timer.stop()
                        assert hits and not destination.exists()
                        assert store.path.read_bytes() == before
                        report["dialog_checks"].append(
                            {
                                "theme": theme,
                                "kind": "actual-data-mode-preview-cancel",
                                "data_mutation": False,
                            }
                        )
                        dialog = window.show_text(
                            "账号检查结果",
                            "模拟账号：可登录（未提交打卡）\n只读查询任务；不代表真实学校接口可用。",
                        )
                        QTest.qWait(30)
                        shot(f"preview-账号检查结果-{theme}", dialog)
                        dialog.close()
                        report["dialog_checks"].append(
                            {"theme": theme, "kind": "account-check-viewer"}
                        )
                        captured = []

                        def confirm_shot():
                            dialog = next(
                                d
                                for d in window.findChildren(QMessageBox)
                                if d.isVisible()
                            )
                            shot(f"delete-confirm-{theme}", dialog)
                            captured.append(True)
                            dialog.done(QMessageBox.StandardButton.No)

                        QTimer.singleShot(30, confirm_shot)
                        assert not window.confirm(
                            "删除账号", "删除模拟账号？取消会保留账号与历史。"
                        )
                        assert captured
                        report["dialog_checks"].append(
                            {"theme": theme, "kind": "delete-cancel"}
                        )
            finally:
                window.shutdown()
    path = output / f"visual-layout-report{suffix}.json"
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(
        json.dumps(
            {
                "checks": len(report["checks"]),
                "dialogs": len(report["dialog_checks"]),
                "screenshots": len(report["screenshots"]),
                "dpr": report["device_pixel_ratio"],
            }
        )
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--suffix", default="")
    args = parser.parse_args()
    capture(Path(__file__).resolve().parent / "screenshots", args.suffix)
