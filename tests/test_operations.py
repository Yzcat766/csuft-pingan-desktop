import copy, json, sys, tempfile, unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import requests
from api import ApiError, SchoolClient, redact
from execution import make_request, merge_results, today_succeeded
from scheduling import *
from storage import Store, defaults, new_account, SHANGHAI, validate_schedule
from runner import run_accounts
from test_locations import MockClient

NOW = datetime(2026, 10, 10, 12, 0, tzinfo=SHANGHAI)


class OperationsTests(unittest.TestCase):
    def setUp(self):
        MockClient.submitted = []
        MockClient.on_login = None
        MockClient.sample = None
        MockClient.final_status = 0
        self.data = defaults()
        self.a = new_account("mock-a", "甲")
        self.b = new_account("mock-b", "乙")
        self.b["enabled"] = False
        self.data["accounts"] = [self.a, self.b]
        self.data["schedule"].update(enabled=True, time="11:00")

    def result(self, identity, status="failure", **kwargs):
        return {
            "account_id": identity,
            "label": "模拟",
            "date": NOW.date().isoformat(),
            "status": status,
            "message": "模拟",
            "retryable": True,
            "task_id": "task-a",
            "task_window": {"start": "00:00:00", "end": "23:59:59"},
            **kwargs,
        }

    def test_all_selected_single_and_no_empty_fallback(self):
        self.assertEqual(
            [a["id"] for a in make_request(self.data, "all", moment=NOW)["accounts"]],
            [self.a["id"]],
        )
        for scope in ("selected", "single"):
            request = make_request(self.data, scope, [self.b["id"]], NOW)
            self.assertEqual(request["count"], 1)
            self.assertFalse(request["accounts"][0]["enabled"])
        with self.assertRaisesRegex(ValueError, "不会退回"):
            make_request(self.data, "selected", [], NOW)
        with self.assertRaisesRegex(ValueError, "不存在"):
            make_request(self.data, "single", ["missing"], NOW)
        self.assertFalse(self.b["enabled"])

    def test_request_is_frozen_and_today_range_skips_success(self):
        request = make_request(self.data, "all", moment=NOW)
        self.data["locations"][0]["latitude"] = 29
        self.a["openid"] = "changed"
        self.assertNotEqual(request["accounts"][0]["openid"], self.a["openid"])
        self.assertEqual(request["accounts"][0]["run_location"]["latitude"], 28.131567)
        self.data["success_dates"][self.a["id"]] = NOW.date().isoformat()
        self.assertEqual(make_request(self.data, 'today', moment=NOW)['count'], 1)

    def test_past_schedule_choices_and_cancel_are_explicit(self):
        previous = defaults()["schedule"]
        proposed = validate_schedule({**previous, "enabled": True, "time": "11:00"})
        self.assertIsNone(prepare_schedule(previous, proposed, NOW, None))
        next_plan = prepare_schedule(previous, proposed, NOW, "next")
        self.assertFalse(schedule_due(next_plan, NOW))
        self.assertGreater(next_run(next_plan, NOW), NOW)
        now_plan = prepare_schedule(previous, proposed, NOW, "now")
        self.assertTrue(schedule_due(now_plan, NOW))
        self.assertFalse(previous["enabled"])

    def test_unchanged_past_plan_retains_startup_catchup(self):
        self.assertTrue(schedule_due(self.data["schedule"], NOW))
        same = prepare_schedule(
            self.data["schedule"], copy.deepcopy(self.data["schedule"]), NOW
        )
        self.assertEqual(same, self.data["schedule"])
        self.assertTrue(schedule_due(same, NOW))

    def test_finite_retry_interval_and_success_dedup(self):
        request = make_request(self.data, "automatic", moment=NOW)
        begin_attempt(self.data, request, NOW)
        failed = self.result(self.a["id"], run_id=request["run_id"])
        finish_attempt(self.data, request, [failed], NOW)
        self.assertEqual(self.data["automation"]["summary"]["failure"], 1)
        self.assertEqual(due_action(self.data, NOW + timedelta(minutes=4)), None)
        for n in (1, 2):
            moment = NOW + timedelta(minutes=5 * n)
            self.assertEqual(due_action(self.data, moment), "retry")
            again = make_request(
                self.data, "retry", pending_ids(self.data, moment), moment
            )
            begin_attempt(self.data, again, moment, True)
            finish_attempt(self.data, again, [failed], moment)
            self.assertEqual(self.data["automation"]["retry_count"], n)
        self.assertIsNone(self.data["automation"]["next_retry"])
        self.assertIsNone(due_action(self.data, NOW + timedelta(minutes=20)))

    def test_retry_only_transient_failure_cancel_never_queues(self):
        self.b["enabled"] = True
        request = make_request(self.data, "automatic", moment=NOW)
        begin_attempt(self.data, request, NOW)
        results = [
            self.result(self.a["id"], retryable=False),
            self.result(self.b["id"], status="success"),
        ]
        merge_results(self.data, results)
        finish_attempt(self.data, request, results, NOW)
        self.assertEqual(pending_ids(self.data, NOW), [])
        self.assertIsNone(self.data["automation"]["next_retry"])
        request = make_request(self.data, "automatic", moment=NOW)
        begin_attempt(self.data, request, NOW)
        finish_attempt(self.data, request, [self.result(self.a["id"])], NOW, True)
        self.assertEqual(self.data["automation"]["state"], "cancelled")
        self.assertIsNone(self.data["automation"]["next_retry"])

    def test_retry_disabled_plan_changed_and_day_or_window_boundary(self):
        for mutate in ("disable", "change", "cross_day", "window"):
            self.setUp()
            moment = NOW if mutate != "cross_day" else NOW.replace(hour=23, minute=58)
            request = make_request(self.data, "automatic", moment=moment)
            begin_attempt(self.data, request, moment)
            failed = self.result(self.a["id"])
            if mutate == "disable":
                self.data["schedule"]["enabled"] = False
            if mutate == "change":
                self.data["schedule"]["time"] = "10:00"
            if mutate == "window":
                failed["task_window"]["end"] = "12:02:00"
            finish_attempt(self.data, request, [failed], moment)
            self.assertIsNone(self.data["automation"]["next_retry"])

    def test_delayed_retry_does_not_enter_expired_window(self):
        request = make_request(self.data, "automatic", moment=NOW)
        begin_attempt(self.data, request, NOW)
        failed = self.result(
            self.a["id"], task_window={"start": "00:00:00", "end": "12:10:00"}
        )
        finish_attempt(self.data, request, [failed], NOW)
        self.assertEqual(due_action(self.data, NOW + timedelta(minutes=5)), "retry")
        self.assertIsNone(due_action(self.data, NOW + timedelta(minutes=12)))

    def test_interrupted_restart_recovers_once_with_gap_and_cap(self):
        request = make_request(self.data, "automatic", moment=NOW)
        begin_attempt(self.data, request, NOW)
        with tempfile.TemporaryDirectory() as folder:
            store = Store(folder)
            store.data = copy.deepcopy(self.data)
            store.save()
            loaded = Store(folder)
            self.assertTrue(
                recover_interrupted(loaded.data, NOW + timedelta(minutes=1))
            )
            self.assertIsNone(due_action(loaded.data, NOW + timedelta(minutes=1)))
            self.assertEqual(
                due_action(loaded.data, NOW + timedelta(minutes=5)), "retry"
            )
            loaded.save()
            again = Store(folder)
            self.assertFalse(
                recover_interrupted(again.data, NOW + timedelta(minutes=2))
            )
            self.assertEqual(again.data["automation"]["retry_count"], 0)
            again.data["automation"].update(state="running", retry_count=2)
            recover_interrupted(again.data, NOW)
            self.assertIsNone(again.data["automation"]["next_retry"])

    def test_checkpoints_keep_success_without_duplicate_history(self):
        self.b["enabled"] = True
        request = make_request(self.data, "automatic", moment=NOW)
        begin_attempt(self.data, request, NOW)
        result = self.result(self.a["id"], status="success", run_id=request["run_id"])
        merge_results(self.data, [result])
        checkpoint_result(self.data, request["run_id"], result)
        merge_results(self.data, [result])
        self.assertEqual(len(self.data["results"]), 1)
        recover_interrupted(self.data, NOW)
        self.assertNotIn(self.a["id"], pending_ids(self.data, NOW))
        self.assertIn(self.b["id"], pending_ids(self.data, NOW))

    def test_old_configuration_gains_retry_and_close_defaults_without_editing_file(
        self,
    ):
        with tempfile.TemporaryDirectory() as folder:
            raw = {
                "accounts": [
                    {"id": "old", "label": "旧", "openid": "mock-old", "enabled": False}
                ],
                "schedule": {
                    "enabled": True,
                    "mode": "daily",
                    "time": "11:00",
                    "dates": [],
                    "last_trigger": "2026-10-10@11:00",
                },
                "appearance": {"theme": "light"},
            }
            path = Path(folder) / "config.json"
            path.write_text(json.dumps(raw), encoding="utf-8")
            before = path.read_bytes()
            store = Store(folder)
            self.assertEqual(store.data["schedule"]["retry"]["max_retries"], 2)
            self.assertEqual(store.data["appearance"]["close_behavior"], "tray")
            self.assertFalse(store.data["accounts"][0]["enabled"])
            self.assertEqual(store.data["accounts"][0]["location"]["mode"], "task")
            self.assertEqual(path.read_bytes(), before)

    def test_uncertain_post_retry_queries_status_before_no_duplicate_submit(self):
        class Server(MockClient):
            posted = 0
            signed = False

            def get_status(self, task_id):
                return {
                    "signStatus": 0 if type(self).signed else 3,
                    "signDate": "2026-10-10",
                }

            def sign(self, task, lat, lng):
                type(self).posted += 1
                type(self).signed = True
                raise ApiError("模拟提交响应超时", "network_timeout", True, True)

        request = make_request(self.data, "automatic", moment=NOW)
        with patch("runner.now_shanghai", return_value=NOW):
            first = run_accounts(
                request["accounts"],
                lambda *a: None,
                client_factory=Server,
                delay_scale=0,
                location_config=request["config"],
            )
            self.assertTrue(first[0]["retryable"])
            self.assertTrue(first[0]["uncertain"])
            self.assertEqual(first[0]["status"], "failure")
            second = run_accounts(
                request["accounts"],
                lambda *a: None,
                client_factory=Server,
                delay_scale=0,
                location_config=request["config"],
                retry_context={
                    "date": "2026-10-10",
                    "task_ids": {self.a["id"]: "task-a"},
                },
            )
            self.assertEqual(second[0]["status"], "success")
            self.assertEqual(second[0]["success_kind"], "already_signed")
            self.assertEqual(Server.posted, 1)

    def test_retry_task_and_date_changes_block_submit(self):
        request = make_request(self.data, "all", moment=NOW)
        MockClient.submitted = []
        MockClient.on_login = None
        MockClient.sample = None
        MockClient.final_status = 0
        with patch("runner.now_shanghai", return_value=NOW):
            results = run_accounts(
                request["accounts"],
                lambda *a: None,
                client_factory=MockClient,
                delay_scale=0,
                location_config=request["config"],
                retry_context={
                    "date": "2026-10-10",
                    "task_ids": {self.a["id"]: "wrong"},
                },
            )
        self.assertEqual(results[0]["error_code"], "retry_task_changed")
        self.assertFalse(results[0]["retryable"])
        self.assertEqual(MockClient.submitted, [])

    def test_http_errors_are_classified_without_credentials(self):
        for error, retryable in (
            (requests.Timeout("secret"), True),
            (requests.ConnectionError("secret"), True),
            (requests.exceptions.SSLError("secret"), False),
            (requests.RequestException("secret"), False),
        ):
            session = unittest.mock.Mock()
            session.request.side_effect = error
            client = SchoolClient("mock-private", session)
            client.token = "mock-token"
            with self.assertRaises(ApiError) as raised:
                client._request("POST", "flySource-yxgl/dormSignTask/stuSign")
            self.assertEqual(raised.exception.retryable, retryable)
            self.assertEqual(raised.exception.uncertain, retryable)
            self.assertNotIn("secret", str(raised.exception))
        for code, retryable in ((401, False), (403, False), (429, True), (503, True)):
            response = unittest.mock.Mock(status_code=code)
            session = unittest.mock.Mock()
            session.request.side_effect = requests.HTTPError(response=response)
            client = SchoolClient("mock-private", session)
            client.token = "mock-token"
            with self.assertRaises(ApiError) as raised:
                client._request("GET", "status")
            self.assertEqual(raised.exception.retryable, retryable)
            self.assertFalse(raised.exception.uncertain)
        self.assertNotIn("abc-secret", redact("access_token=abc-secret"))
        self.assertNotIn("abc-secret", redact("Authorization: Bearer abc-secret"))

    def test_actual_post_guard_blocks_changed_date_and_query_timeout_is_uncertain(self):
        client = SchoolClient("mock-private")
        client.get_status = unittest.mock.Mock(
            return_value={"signStatus": 3, "signDate": "2026-10-11"}
        )
        client.submission_guard = unittest.mock.Mock(
            side_effect=ApiError("任务日期变化", "retry_rule")
        )
        with patch.object(client, "_request") as request:
            with self.assertRaises(ApiError):
                client.sign({"taskId": "task-a"}, 28, 112)
            request.assert_not_called()
        client.submission_guard = None
        client.get_status.side_effect = [
            {"signStatus": 3, "signDate": "2026-10-10"},
            ApiError("模拟查询超时", "network_timeout", True),
        ]
        with patch("api.build_sign_payload", return_value={}), patch.object(
            client, "_request", return_value={"success": True}
        ):
            with self.assertRaises(ApiError) as raised:
                client.sign({"taskId": "task-a"}, 28, 112)
            self.assertTrue(raised.exception.uncertain)
            self.assertTrue(raised.exception.retryable)
        client.close()


if __name__ == "__main__":
    unittest.main()
