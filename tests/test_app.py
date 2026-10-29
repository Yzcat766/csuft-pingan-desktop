import json
import sys
import tempfile
import threading
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from api import ApiError, RequestCancelled, SchoolClient, build_sign_payload, distance, signature
from runner import in_time_window, run_accounts
from storage import SHANGHAI, Store, defaults, import_accounts, new_account, next_run, schedule_due, validate_schedule


def task():
    return {"taskId": "task-a", "taskName": "测试任务", "scanType": "1", "isLateStuTakePhoto": 0, "openTakePhoto": 0, "signStartTime": "00:00:00", "signEndTime": "23:59:59", "locationAccuracy": "200", "dormitoryRegisterVO": {"roomId": "room-a", "locationLat": "28.131086", "locationLng": "112.994658"}}


class Session:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []
    def request(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        payload = self.responses.pop(0)
        class Response:
            def raise_for_status(self): pass
            def json(self): return payload
        return Response()
    def close(self): pass


class ApiTests(unittest.TestCase):
    def test_signature_matches_original_node_implementation(self):
        self.assertEqual(signature("https://simp.csuft.edu.cn/api/flySource-yxgl/dormSignRecord/stuSign?x=1", "test-token", "1770000000123"), "b020b9d847d110f580308d229036762a1.MTc3MDAwMDAwMDEyMw==")

    def test_distance_matches_original(self):
        self.assertEqual(distance(28.131086, 112.994658, 28.132086, 112.995658), 148.4)

    def test_login_and_sign_recheck(self):
        session = Session([{"access_token": "test-token"}, {"success": True, "data": {"signStatus": 3, "signDate": "2026-10-10"}}, {"success": True}, {"success": True, "data": {"signStatus": 0, "signDate": "2026-10-10"}}])
        client = SchoolClient("fake-openid", session)
        client.login()
        status = client.sign(task(), 28.131086, 112.994658)
        self.assertEqual(status["signStatus"], 0)
        self.assertFalse(session.trust_env)
        self.assertEqual(session.calls[0][2]["data"]["grant_type"], "wxapp")
        self.assertEqual(session.calls[0][2]["data"]["openid"], "fake-openid")
        self.assertEqual(session.calls[2][0], "POST")
        payload = session.calls[2][2]["json"]
        self.assertEqual(payload["roomId"], "room-a")
        self.assertEqual(payload["locationAccuracy"], 0)
        self.assertEqual(len(payload["stuTaskId"]), 32)
        self.assertIn("Flysource-Sign", session.calls[2][2]["headers"])

    def test_already_signed_never_posts(self):
        session = Session([{"access_token": "test-token"}, {"success": True, "data": {"signStatus": 0}}])
        client = SchoolClient("fake", session)
        client.login()
        client.sign(task(), 28.13, 112.99)
        self.assertEqual(len(session.calls), 2)

    def test_cancel_between_status_and_submit_prevents_post(self):
        cancel = threading.Event()
        class CancellingSession(Session):
            def request(self, method, url, **kwargs):
                response = super().request(method, url, **kwargs)
                if len(self.calls) == 2: cancel.set()
                return response
        session = CancellingSession([{"access_token": "test-token"}, {"success": True, "data": {"signStatus": 3, "signDate": "2026-10-10"}}])
        client = SchoolClient("fake", session)
        client.cancel = cancel
        client.login()
        with self.assertRaises(RequestCancelled):
            client.sign(task(), 28.131086, 112.994658)
        self.assertEqual(len(session.calls), 2)

    def test_failed_final_status_is_not_success(self):
        session = Session([{"access_token": "test-token"}, {"success": True, "data": {"signStatus": 3, "signDate": "2026-10-10"}}, {"success": True}, {"success": True, "data": {"signStatus": 3}}])
        client = SchoolClient("fake", session)
        client.login()
        with self.assertRaisesRegex(ApiError, "仍显示未签到"):
            client.sign(task(), 28.131086, 112.994658)

    def test_photo_and_distance_gates(self):
        sample = task()
        sample["openTakePhoto"] = 1
        with self.assertRaisesRegex(ApiError, "照片"):
            build_sign_payload(sample, {"signDate": "2026-10-10"}, 28.131086, 112.994658)
        sample["openTakePhoto"] = 0
        with self.assertRaisesRegex(ApiError, "超出"):
            build_sign_payload(sample, {"signDate": "2026-10-10"}, 0, 0)

    def test_rejects_missing_token_and_non_list_tasks(self):
        client = SchoolClient("fake", Session([{"success": True}]))
        with self.assertRaisesRegex(ApiError, "OpenID"):
            client.login()
        client = SchoolClient("fake", Session([{"data": []}]))
        client.token = "test"
        with self.assertRaisesRegex(ApiError, "格式"):
            client.list_tasks()


class ScheduleTests(unittest.TestCase):
    def test_daily_trigger_only_once(self):
        planned = {**defaults()["schedule"], "enabled": True}
        moment = datetime(2026, 10, 10, 21, 6, tzinfo=SHANGHAI)
        self.assertTrue(schedule_due(planned, moment))
        planned["last_trigger"] = "2026-10-10@21:05"
        self.assertFalse(schedule_due(planned, moment))
        self.assertEqual(next_run(planned, moment).date().isoformat(), "2026-10-11")

    def test_weekdays_and_selected_dates(self):
        saturday = datetime(2026, 10, 10, 22, 0, tzinfo=SHANGHAI)
        planned = {**defaults()["schedule"], "enabled": True, "mode": "weekdays"}
        self.assertFalse(schedule_due(planned, saturday))
        self.assertEqual(next_run(planned, saturday).date().isoformat(), "2026-10-12")
        planned.update(mode="dates", dates=["2026-10-10"])
        self.assertTrue(schedule_due(planned, saturday))

    def test_invalid_schedule_rejected(self):
        for time in ("25:00", "12:70", "1:05", "nan"):
            with self.assertRaises(ValueError):
                validate_schedule({**defaults()["schedule"], "time": time})
        with self.assertRaises(ValueError):
            validate_schedule({**defaults()["schedule"], "dates": ["2026-02-30"]})

    def test_cross_midnight_time_window(self):
        self.assertTrue(in_time_window("22:00:00", "02:00:00", datetime(2026, 10, 10, 1, 0)))
        self.assertFalse(in_time_window("22:00:00", "02:00:00", datetime(2026, 10, 10, 12, 0)))


class StorageAndRunnerTests(unittest.TestCase):
    def test_persistence_and_import(self):
        with tempfile.TemporaryDirectory() as directory:
            store = Store(directory)
            store.data["accounts"].append(new_account("test-openid", "测试"))
            store.save()
            store.data["appearance"]["theme"] = "dark"
            store.save()
            self.assertEqual(Store(directory).data["appearance"]["theme"], "dark")
            self.assertTrue((Path(directory) / "config.json.bak").exists())
            path = Path(directory) / ".env"
            path.write_text('PORT=3001\nopenid="id1,id2,id1"\n', encoding="utf-8")
            self.assertEqual(import_accounts(path), ["id1", "id2"])
            path = Path(directory) / "openid.json"
            path.write_text('{"openid":"id3"}', encoding="utf-8")
            self.assertEqual(import_accounts(path), ["id3"])

    def test_empty_task_is_skipped_and_other_account_continues(self):
        class NoTasks:
            def __init__(self, openid): self.openid = openid
            def login(self):
                if self.openid == "bad": raise ApiError("failed")
                return {"userName": "测试"}
            def list_tasks(self): return []
            def close(self): pass
        results = run_accounts([new_account("bad", "a"), new_account("good", "b")], lambda *args: None, client_factory=NoTasks, delay_scale=0)
        self.assertEqual([result["status"] for result in results], ["failure", "skipped"])

    def test_legacy_account_success_cannot_skip_other_tasks(self):
        account = new_account("test", "a")
        moment = datetime(2026, 10, 10, 21, 30, tzinfo=SHANGHAI)
        from desktop_common import DemoClient
        with patch("runner.now_shanghai", return_value=moment):
            results = run_accounts([account], lambda *args: None, scheduled=True, success_dates={account["id"]: "2026-10-10"}, client_factory=DemoClient, delay_scale=0)
        self.assertEqual(results[0]["status"], "success")

    def test_cancel_never_opens_network(self):
        cancel = threading.Event()
        cancel.set()
        result = run_accounts([new_account("test", "a")], lambda *args: None, cancel, client_factory=lambda value: self.fail("network must not be called"), delay_scale=0)
        self.assertEqual(result[0]["status"], "cancelled")


if __name__ == "__main__":
    unittest.main()
