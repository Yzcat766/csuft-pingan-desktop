"""Desktop entry point and compatible headless execution."""

import argparse
import os
import sys
import tempfile
from pathlib import Path
from PySide6.QtCore import QTimer, Qt
from PySide6.QtGui import QFont, QGuiApplication
from PySide6.QtWidgets import QApplication, QMessageBox
from qt_ui import App, AccountDialog
from desktop_common import app_directory, SingleInstance, DemoClient
from runner import run_accounts
from logbook import LogBook
from storage import Store, new_account, now_shanghai
from data_management import selected_directory
from windows_integration import SleepGuard
from api import redact
from position_offset import checkpoint_position
from overview import record_batch
from ui_components import install_dialog_presentation


def create_application():
    QGuiApplication.setHighDpiScaleFactorRoundingPolicy(
        Qt.HighDpiScaleFactorRoundingPolicy.PassThrough
    )
    application = QApplication.instance() or QApplication(sys.argv[:1])
    application.setStyle("Fusion")
    font = QFont("Microsoft YaHei UI")
    font.setPixelSize(14)
    application.setFont(font)
    application.setQuitOnLastWindowClosed(False)
    install_dialog_presentation(application)
    return application


def run_cli(store, scheduled):
    from execution import make_request, merge_results
    from scheduling import (
        due_action,
        pending_ids,
        begin_attempt,
        finish_attempt,
        checkpoint_task,
        checkpoint_result,
        recover_interrupted,
    )

    moment = now_shanghai()
    if scheduled and recover_interrupted(store.data, moment):
        store.save()
    action = due_action(store.data, moment) if scheduled else None
    lines = []
    request = None
    journal = LogBook(store.directory, [a["openid"] for a in store.data["accounts"]])

    def emit(kind, value):
        if kind == "log_event":
            journal.append(value)
        elif kind == "log":
            lines.append(f"[{now_shanghai():%H:%M:%S}] {value}")
            if sys.stdout:
                print(value, flush=True)
        elif scheduled and request is not None and kind == "task":
            checkpoint_task(store.data, request["run_id"], value)
            store.save()
        elif kind == "position":
            checkpoint_position(store.data, value)
            store.save()
        elif request is not None and kind == "result":
            journal.checkpoint_result(value)
            journal.flush()
            if not scheduled:
                return
            merge_results(store.data, [value])
            checkpoint_result(store.data, request["run_id"], value)
            store.save()

    if scheduled and action is None:
        emit("log", "今天不符合计划、已触发或未到重试时间，跳过。")
        store.write_log(lines, "跳过")
        return 0
    scope = "retry" if action == "retry" else "automatic" if scheduled else "all"
    try:
        request = make_request(
            store.snapshot(),
            scope,
            pending_ids(store.data, moment) if action == "retry" else None,
            moment,
        )
    except ValueError as error:
        emit("log", str(error))
        if scheduled:
            empty = {
                "run_id": __import__("uuid").uuid4().hex,
                "accounts": [],
                "count": 0,
                "scope_label": "自动计划：无待执行账号",
            }
            journal.start(empty)
            journal.append(
                {"message": str(error), "step": "批次结果", "level": "warning"}
            )
            journal.finish([], "skipped")
            begin_attempt(store.data, empty, moment)
            finish_attempt(store.data, empty, [], moment)
            store.data["automation"]["reason"] = str(error)
            store.save()
        store.write_log(lines, "跳过")
        return 0 if scheduled else 1
    if action == "retry":
        cycle = store.data["automation"]
        request["retry_context"] = {
            "date": cycle["date"],
            "task_ids": dict(cycle.get("task_ids", {})),
        }
    journal.start(request)
    journal.append(
        {
            "message": f"执行范围：{request['scope_label']}；共 {request['count']} 个账号。",
            "step": "批次",
        }
    )
    if scheduled:
        begin_attempt(store.data, request, moment, action == "retry")
        store.save()
    emit("log", f"执行范围：{request['scope_label']}；共 {request['count']} 个账号。")
    with SleepGuard(store.data["reliability"].get("prevent_sleep", False)):
        results = run_accounts(
            request["accounts"],
            emit,
            scheduled=scheduled,
            success_dates=request["config"]["success_dates"],
            location_config=request["config"],
            run_id=request["run_id"],
            retry_context=request.get("retry_context"),
            preview_handler=None,
            position_handler=lambda value: save_cli_position(store, value),
        )
    journal.finish(results)
    merge_results(store.data, results)
    record_batch(store.data, request, results, now_shanghai())
    if scheduled:
        finish_attempt(store.data, request, results, now_shanghai())
    store.save()
    failed = any(r["status"] == "failure" for r in results)
    store.write_log(
        lines,
        (
            "失败"
            if failed
            else "成功" if any(r["status"] == "success" for r in results) else "跳过"
        ),
    )
    return int(failed)


def save_cli_position(store, value):
    checkpoint_position(store.data, value)
    store.save()
    return True


def main():
    parser = argparse.ArgumentParser(description="平安签到桌面软件")
    parser.add_argument("--data-dir", type=Path, default=None)
    parser.add_argument(
        "--startup", action="store_true", help="Windows 登录启动入口，遵守启动隐藏偏好"
    )
    parser.add_argument(
        "--run-once", action="store_true", help="运行一次签到，不打开界面"
    )
    parser.add_argument(
        "--scheduled", action="store_true", help="与 --run-once 一起使用，遵守计划"
    )
    parser.add_argument(
        "--demo", action="store_true", help="离线演示，不连接学校接口，不读取现有账号"
    )
    parser.add_argument(
        "--smoke-test", action="store_true", help="验证启动与托盘后退出，不执行签到"
    )
    args = parser.parse_args()
    temporary = tempfile.TemporaryDirectory(prefix="csuft-demo-") if args.demo else None
    instance = None
    try:
        directory = (
            Path(temporary.name) / "data"
            if temporary
            else (args.data_dir or selected_directory(app_directory())).resolve()
        )
        instance = SingleInstance(directory)
        store = Store(directory)
        if args.demo:
            store.data["accounts"] = [
                new_account("demo-openid-a", "我的账号 · 演示"),
                new_account("demo-openid-b", "备用账号 · 演示"),
            ]
        if args.run_once:
            if args.demo:
                raise ValueError("--demo 用于界面演示，请勿与 --run-once 同时使用。")
            return run_cli(store, args.scheduled)
        application = create_application()
        window = App(store, args.demo, quit_on_shutdown=True)
        window.show()
        if not args.smoke_test and store.data["reliability"].get("start_hidden"):
            QTimer.singleShot(0, lambda: window.on_close(force_tray=True))
        errors = []
        if args.smoke_test:
            window.timer.stop()

            def check():
                try:
                    if args.demo:
                        from data_management import create_backup, read_backup
                        from position_offset import arc_distance
                        from overview import today_metrics

                        window.navigate("schedule")
                        QApplication.processEvents()
                        assert (
                            window.schedule_time.width()
                            <= window.schedule_time.maximumWidth()
                        )
                        assert (
                            window.schedule_time.width()
                            < window.stack.currentWidget().width() / 2
                        )
                        window.schedule_mode.setCurrentIndex(
                            window.schedule_mode.findData("dates")
                        )
                        window.schedule_dates.setPlainText("2026-12-12")
                        assert window.schedule_date_control.isVisible()
                        window.schedule_mode.setCurrentIndex(
                            window.schedule_mode.findData("daily")
                        )
                        assert not window.schedule_date_control.isVisible()
                        assert window.schedule_dates.toPlainText() == "2026-12-12"
                        window.schedule_retry_enabled.setChecked(False)
                        assert not window.retry_fields.isVisible()
                        window.schedule_retry_enabled.setChecked(True)
                        assert window.retry_fields.isVisible()
                        window.load_schedule()
                        window.navigate("overview")

                        assert store.data["global_offset"]["mode"] == "off"
                        store.data["global_offset"] = {"mode": "on", "radius": 20}
                        simulated = run_accounts(
                            store.data["accounts"],
                            lambda *args: None,
                            client_factory=DemoClient,
                            delay_scale=0,
                            location_config=store.snapshot(),
                            position_handler=lambda value: save_cli_position(
                                store, value
                            ),
                        )
                        assert all(r["status"] == "success" for r in simulated)
                        for result in simulated:
                            checkpoint_position(store.data, result["position_state"])
                            preview = result["preview"]
                            assert (
                                arc_distance(
                                    (
                                        preview["center_latitude"],
                                        preview["center_longitude"],
                                    ),
                                    (preview["latitude"], preview["longitude"]),
                                )
                                <= 20 + 1e-7
                            )
                            assert result["submission_count"] == 1

                        for result in simulated:
                            result["batch_id"] = "demo-smoke-batch"
                        store.data["results"] = simulated
                        record_batch(
                            store.data,
                            {
                                "run_id": "demo-smoke-batch",
                                "count": 2,
                                "scope": "all",
                                "scope_label": "离线模拟批次",
                            },
                            simulated,
                            now_shanghai(),
                        )
                        assert (
                            today_metrics(
                                store.data, now_shanghai().date().isoformat()
                            )["success"]
                            == 2
                        )
                        window.refresh()
                        assert window.recent_table.rowCount() == 1

                        store.save()
                        assert (
                            Store(store.directory).data["accounts"][0]["openid"]
                            == "demo-openid-a"
                        )
                        assert (
                            Store(store.directory).data["global_offset"]["radius"] == 20
                        )
                        assert len(Store(store.directory).data["position_cache"]) == 2
                        assert len(Store(store.directory).data["overview_batches"]) == 1
                        backup = Path(temporary.name) / "smoke.pingan-backup"
                        create_backup(
                            store.snapshot(),
                            store.directory,
                            backup,
                            "offline-smoke-password",
                        )
                        assert (
                            read_backup(backup, "offline-smoke-password")["data"][
                                "accounts"
                            ][0]["openid"]
                            == "demo-openid-a"
                        )
                    window.on_close()
                    if (
                        window.isVisible()
                        or not window.tray
                        or not window.tray.icon.isVisible()
                    ):
                        raise RuntimeError("隐藏到系统托盘检查失败。")
                    window.restore_window()
                    if not window.isVisible() or window.tray.icon.isVisible():
                        raise RuntimeError("从托盘恢复检查失败。")
                except Exception as error:
                    errors.append(str(error))
                finally:
                    window.shutdown()

            QTimer.singleShot(500, check)
        application.exec()
        if errors:
            raise RuntimeError(errors[0])
        return 0
    except Exception as error:
        # Demo writes only to its temporary directory, never a user's existing data.
        report_dir = (
            Path(temporary.name)
            if temporary
            else (args.data_dir or app_directory() / "data")
        )
        report_dir.mkdir(parents=True, exist_ok=True)
        (report_dir / "startup-error.log").write_text(
            redact(str(error)), encoding="utf-8"
        )
        if sys.stderr:
            print(redact(str(error)), file=sys.stderr)
        if not args.run_once and not args.smoke_test:
            application = create_application()
            if not args.startup:
                QMessageBox.critical(None, "启动失败", redact(str(error)))
        return 1
    finally:
        if instance:
            instance.close()
        if temporary:
            temporary.cleanup()


if __name__ == "__main__":
    raise SystemExit(main())
