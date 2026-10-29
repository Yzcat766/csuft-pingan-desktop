"""Exercise real Qt overview states with mock clients and blocked HTTP."""

import argparse
import copy
import json
import tempfile
import threading
from pathlib import Path
from unittest.mock import patch
from PySide6.QtCore import QPoint
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication
from app import create_application, App
from storage import Store, new_account
from desktop_common import DemoClient
from api import ApiError, RequestCancelled
from capture_ui import wait_for


class GateClient(DemoClient):
    reached = threading.Event()
    release = threading.Event()

    def sign(self, task, lat, lng):
        type(self).reached.set()
        while not type(self).release.wait(0.02):
            if self.cancel.is_set():
                raise RequestCancelled("模拟任务已取消")
        return super().sign(task, lat, lng)


class PartialClient(DemoClient):
    def __init__(self, openid):
        super().__init__(openid)
        self.openid = openid

    def sign(self, task, lat, lng):
        if self.openid.endswith("b"):
            raise ApiError("模拟账号需要重新确认登录", "authentication")
        return super().sign(task, lat, lng)


class TransientClient(DemoClient):
    def sign(self, task, lat, lng):
        raise ApiError("模拟连接超时，尚未确认结果", "network_timeout", True, True)


def capture(output, suffix=""):
    create_application()
    output.mkdir(parents=True, exist_ok=True)
    report = {"network": "blocked", "checks": [], "screenshots": []}
    with tempfile.TemporaryDirectory(prefix="overview-qa-") as folder, patch(
        "requests.Session.request", side_effect=AssertionError("Real network forbidden")
    ):
        store = Store(folder)
        window = App(store, demo=True)
        window.timer.stop()
        window.show()
        QTest.qWait(40)
        report["device_pixel_ratio"] = window.devicePixelRatioF()

        def shots(state):
            for theme in ("dark", "light"):
                window.theme = theme
                window.apply_theme()
                for width, height in ((740, 540), (960, 650), (1240, 840), (1440, 960)):
                    window.resize(width, height)
                    window.navigate("overview")
                    window.stack.currentWidget().verticalScrollBar().setValue(0)
                    window.notice.hide()
                    QApplication.processEvents()
                    QTest.qWait(20)
                    viewport = window.stack.currentWidget().viewport()
                    assert (
                        window.primary_action.geometry().right()
                        < window.primary_action.parentWidget().width()
                    )
                    if window.busy:
                        for control in (
                            window.cancel_button,
                            window.run_stage,
                            window.progress,
                        ):
                            position = control.mapTo(viewport, QPoint(0, 0))
                            assert (
                                position.y() >= 0
                                and position.y() + control.height() <= viewport.height()
                            ), (
                                state,
                                theme,
                                width,
                                height,
                                control.objectName(),
                                position.y(),
                                viewport.height(),
                            )
                    if window.pending_results is not None or window.journal_error:
                        position = window.retry_save_button.mapTo(
                            viewport, QPoint(0, 0)
                        )
                        assert (
                            position.y() + window.retry_save_button.height()
                            <= viewport.height()
                        )
                    report["checks"].append(
                        {
                            "state": state,
                            "theme": theme,
                            "size": [width, height],
                            "primary_visible": True,
                            "cancel_visible_without_scroll": window.busy,
                        }
                    )
                    if (width, height) == (1240, 840):
                        path = output / f"overview-{state}-{theme}{suffix}.png"
                        assert window.grab().save(str(path))
                        report["screenshots"].append(path.name)

        def reset():
            store.data["results"] = []
            store.data["overview_batches"] = []
            store.data["success_dates"] = {}
            store.data["task_success_dates"] = {}
            store.data["position_cache"] = {}
            store.data["automation"] = None
            store.data["schedule"]["enabled"] = False
            window.load_schedule()
            window.refresh()
            window.timer.stop()

        def run(client, scheduled=False):
            window.resize(1240, 840)
            window.confirm_preview.setChecked(False)
            with patch("qt_ui.DemoClient", client):
                window.start_run(scheduled=scheduled)
                wait_for(lambda: not window.busy)
            window.timer.stop()
            window.refresh()

        try:
            shots("empty")
            store.data["accounts"] = [
                new_account("mock-overview-a", "主账号 · 模拟"),
                new_account("mock-overview-b", "备用账号 · 模拟"),
            ]
            store.data["global_offset"] = {"mode": "on", "radius": 20}
            store.data["onboarding_done"] = True
            window.refresh()
            shots("idle")
            store.data["schedule"]["enabled"] = True
            window.load_schedule()
            window.refresh()
            shots("plan-enabled")
            reset()
            GateClient.reached.clear()
            GateClient.release.clear()
            with patch("qt_ui.DemoClient", GateClient):
                window.confirm_preview.setChecked(False)
                window.start_run()
                wait_for(GateClient.reached.is_set)
                shots("running")
                GateClient.release.set()
                wait_for(lambda: not window.busy)
            shots("success")
            reset()
            run(PartialClient)
            assert {r["status"] for r in store.data["results"]} == {
                "success",
                "failure",
            }
            shots("partial-failure")
            reset()
            store.data["schedule"].update(enabled=True, time="00:00")
            window.load_schedule()
            run(TransientClient, scheduled=True)
            assert store.data["automation"]["state"] == "waiting_retry"
            assert "重试" in window.stat_values[3].text()
            shots("retry")
            reset()
            original = copy.deepcopy(store.data["accounts"][0]["location"])
            store.data["accounts"][0]["location"] = {
                "mode": "specified",
                "location_id": "missing",
            }
            window.refresh()
            assert window.attention_card.isVisible()
            shots("config-error")
            store.data["accounts"][0]["location"] = original
            reset()
            with patch.object(store, "save", side_effect=OSError("mock read only")):
                run(DemoClient)
                assert window.pending_results is not None
                shots("save-failure")
            window.retry_results()
            window.timer.stop()
            reset()
            store.data["onboarding_done"] = False
            window.refresh()
            shots("guide")
            window.dismiss_onboarding()
            assert Store(folder).data["onboarding_done"]
            assert not store.data["schedule"]["enabled"]
            shots("guide-skipped")
        finally:
            GateClient.release.set()
            if window.busy:
                window.cancel_run()
                wait_for(lambda: not window.busy)
            window.shutdown()
    path = output / f"overview-layout-report{suffix}.json"
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(
        json.dumps(
            {
                "checks": len(report["checks"]),
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
