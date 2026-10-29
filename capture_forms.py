"""Offline real-widget form QA at the process's Qt scale factor."""

import argparse
import json
import tempfile
from pathlib import Path
from unittest.mock import patch
from PySide6.QtCore import Qt, QPoint, QTimer
from PySide6.QtTest import QTest
from PySide6.QtWidgets import (
    QApplication,
    QComboBox,
    QAbstractSpinBox,
    QLineEdit,
    QDialog,
    QDialogButtonBox,
    QStyle,
    QStyleOptionComboBox,
    QStyleOptionSpinBox,
)
from app import create_application, App
from qt_ui import AccountDialog, LocationDialog
from storage import Store, new_account


def capture(output, suffix=""):
    create_application()
    report = {
        "network": "blocked",
        "checks": [],
        "dialog_checks": [],
        "screenshots": [],
    }
    with tempfile.TemporaryDirectory(prefix="form-qa-") as folder, patch(
        "requests.Session.request", side_effect=AssertionError("Real HTTP forbidden")
    ):
        store = Store(folder)
        store.data["accounts"] = [new_account("mock-form-account", "模拟账号")]
        window = App(store, demo=True)
        window.timer.stop()
        window.show()
        QApplication.processEvents()
        report["device_pixel_ratio"] = window.devicePixelRatioF()

        def choose(field, value):
            field.setCurrentIndex(field.findData(value))

        def field_check(field, sample=None):
            if not field.isVisible():
                return
            if isinstance(field, QComboBox):
                opt = QStyleOptionComboBox()
                field.initStyleOption(opt)
                rect = field.style().subControlRect(
                    QStyle.ComplexControl.CC_ComboBox,
                    opt,
                    QStyle.SubControl.SC_ComboBoxEditField,
                    field,
                )
                text = max(
                    (field.itemText(i) for i in range(field.count())),
                    key=lambda s: field.fontMetrics().horizontalAdvance(s),
                    default="",
                )
                assert rect.width() >= field.fontMetrics().horizontalAdvance(text), (
                    field.accessibleName(),
                    text,
                    rect.width(),
                )
            elif isinstance(field, QAbstractSpinBox):
                text = sample or field.text()
                editor = field.findChild(QLineEdit)
                assert (
                    editor.width() >= field.fontMetrics().horizontalAdvance(text) + 4
                ), (field.accessibleName(), text, editor.width())
                opt = QStyleOptionSpinBox()
                field.initStyleOption(opt)
                for control in (
                    QStyle.SubControl.SC_SpinBoxUp,
                    QStyle.SubControl.SC_SpinBoxDown,
                ):
                    rect = field.style().subControlRect(
                        QStyle.ComplexControl.CC_SpinBox, opt, control, field
                    )
                    if (
                        field.buttonSymbols()
                        != QAbstractSpinBox.ButtonSymbols.NoButtons
                    ):
                        assert rect.width() > 0 and rect.right() < field.width(), (
                            field.accessibleName(),
                            rect,
                            field.width(),
                        )
            assert field.height() >= field.fontMetrics().height() + 20, (
                field.accessibleName(),
                field.height(),
                field.fontMetrics().height(),
            )
            assert field.width() <= field.maximumWidth()

        def shot(name, widget=window):
            target = output / f"forms-{name}{suffix}.png"
            assert widget.grab().save(str(target))
            report["screenshots"].append(target.name)

        try:
            for theme in ("dark", "light"):
                window.theme = theme
                window.apply_theme()
                for size in ((740, 540), (960, 650), (1240, 840), (1600, 1000)):
                    window.resize(*size)
                    for state in (
                        "schedule-basic",
                        "schedule-full",
                        "schedule-no-retry",
                        "location-on",
                        "location-off",
                        "settings",
                    ):
                        if state.startswith("schedule"):
                            window.navigate("schedule")
                            choose(
                                window.schedule_mode,
                                "dates" if state == "schedule-full" else "weekdays",
                            )
                            choose(
                                window.task_rule_mode,
                                (
                                    "exact_name"
                                    if state == "schedule-full"
                                    else "unique_current"
                                ),
                            )
                            window.schedule_retry_enabled.setChecked(
                                state != "schedule-no-retry"
                            )
                            window.retry_count.setValue(5)
                            window.retry_interval.setValue(1440)
                            window.schedule_dates.setPlainText("2026-12-12\n2026-12-13")
                            window.task_rule_name.setText("宿舍晚间平安签到 · 模拟")
                            fields = (
                                window.schedule_time,
                                window.schedule_mode,
                                window.retry_count,
                                window.retry_interval,
                                window.task_rule_mode,
                            )
                        elif state.startswith("location"):
                            window.navigate("location")
                            choose(window.global_choice.mode, "specified")
                            choose(
                                window.global_offset.mode,
                                "on" if state.endswith("on") else "off",
                            )
                            window.global_offset.radius.setText("500")
                            fields = (
                                window.global_choice.mode,
                                window.global_choice.position,
                                window.global_offset.mode,
                                window.global_offset.radius,
                            )
                        else:
                            window.navigate("settings")
                            window.retention_days.setValue(3650)
                            fields = (
                                window.retention_days,
                                window.theme_combo,
                                window.close_behavior,
                            )
                        QApplication.processEvents()
                        QTest.qWait(12)
                        scroll = window.stack.currentWidget()
                        assert scroll.horizontalScrollBar().maximum() == 0, (
                            theme,
                            size,
                            state,
                            scroll.horizontalScrollBar().maximum(),
                        )
                        for field in fields:
                            field_check(field)
                        if state == "schedule-full":
                            assert (
                                window.schedule_date_control.isVisible()
                                and window.task_rule_name.isVisible()
                            )
                        if state == "schedule-no-retry":
                            assert not window.retry_fields.isVisible()
                        if state == "location-off":
                            assert not window.global_offset.radius_section.isVisible()
                        report["checks"].append(
                            {
                                "theme": theme,
                                "size": size,
                                "state": state,
                                "horizontal_scroll": 0,
                                "fields": [
                                    {
                                        "name": f.accessibleName(),
                                        "width": f.width(),
                                        "height": f.height(),
                                        "visible": f.isVisible(),
                                    }
                                    for f in fields
                                ],
                            }
                        )
                        if size == (1240, 840):
                            scroll.verticalScrollBar().setValue(0)
                            if state == "settings":
                                scroll.ensureWidgetVisible(window.theme_combo)
                            QApplication.processEvents()
                            shot(f"{state}-{theme}")

                # Native calendar popup remains usable, including the arrow.
                window.navigate("history")
                window.history_date_mode.setCurrentIndex(2)
                QApplication.processEvents()
                calendar = window.history_date.calendarWidget()
                QTest.mouseClick(
                    window.history_date,
                    Qt.MouseButton.LeftButton,
                    pos=QPoint(
                        window.history_date.width() - 10,
                        window.history_date.height() // 2,
                    ),
                )
                QApplication.processEvents()
                assert calendar.isVisible(), "Native calendar arrow must open the popup"
                QTest.keyClick(calendar, Qt.Key.Key_Escape)
                QApplication.processEvents()
                report["calendar_popup"] = "passed"

                for size in ((380, 480), (560, 660)):
                    for kind in ("account", "location-error"):
                        dialog = (
                            AccountDialog(window, store.data["accounts"][0])
                            if kind == "account"
                            else LocationDialog(window)
                        )
                        dialog.resize(*size)
                        dialog.show()
                        if kind == "account":
                            choose(dialog.offset_choice.mode, "on")
                            dialog.offset_choice.radius.setText("500")
                            dialog.form_scroll.ensureWidgetVisible(dialog.offset_choice)
                            fields = (
                                dialog.name,
                                dialog.location_choice.mode,
                                dialog.offset_choice.mode,
                                dialog.offset_choice.radius,
                            )
                        else:
                            dialog.name.setText("模拟位置")
                            dialog.latitude.setText("91")
                            dialog.longitude.setText("112.994905")
                            dialog.save()
                            fields = (dialog.name, dialog.latitude, dialog.longitude)
                        QApplication.processEvents()
                        QTest.qWait(15)
                        for field in fields:
                            field_check(field)
                        if kind == "account":
                            assert (
                                dialog.form_scroll.horizontalScrollBar().maximum() == 0
                            )
                        else:
                            assert dialog.errors["latitude"].isVisible()
                        controls = dialog.findChild(QDialogButtonBox)
                        assert all(
                            b.isVisible()
                            and b.geometry().right() < b.parentWidget().width()
                            for b in controls.buttons()
                        )
                        report["dialog_checks"].append(
                            {
                                "theme": theme,
                                "requested_size": size,
                                "actual_size": [dialog.width(), dialog.height()],
                                "kind": kind,
                            }
                        )
                        if size == (560, 660):
                            shot(f"{kind}-{theme}", dialog)
                        dialog.reject()

                failures = []

                def inspect_backup():
                    try:
                        dialog = next(
                            d
                            for d in window.findChildren(QDialog)
                            if d.isVisible() and d.windowTitle() == "备份布局验证"
                        )
                        dialog.resize(560, 320)
                        QApplication.processEvents()
                        for field in dialog.findChildren(QLineEdit):
                            field_check(field)
                        shot(f"backup-{theme}", dialog)
                        report["dialog_checks"].append(
                            {"theme": theme, "kind": "backup"}
                        )
                        dialog.reject()
                    except Exception as error:
                        failures.append(str(error))
                        for dialog in window.findChildren(QDialog):
                            dialog.reject()

                QTimer.singleShot(30, inspect_backup)
                assert window.ask_password("备份布局验证", True) is None
                assert not failures, failures
        finally:
            window.shutdown()
    path = output / f"forms-layout-report{suffix}.json"
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(
        json.dumps(
            {
                "checks": len(report["checks"]),
                "dialog_checks": len(report["dialog_checks"]),
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
