import hashlib
import json
import sys
import tempfile
import threading
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from api import ApiError, SchoolClient, build_sign_payload, distance, js_number, signature
from locations import TASK_LOCATION, coordinates, resolve_location, saved_location, university_location
from runner import run_accounts
from storage import SHANGHAI, Store, defaults, new_account
from test_app import Session, task


class MockClient:
    submitted = []
    on_login = None
    sample = None
    final_status = 0
    token = "mock-secret-token"

    def __init__(self, openid):
        self.openid = openid
        self.signed = False

    def login(self):
        if type(self).on_login:
            type(self).on_login(self.openid)
        return {"userName": "模拟用户"}

    def list_tasks(self): return [{"taskId": "task-a"}]
    def get_task(self, identity): return type(self).sample or task()
    def get_status(self, identity):
        return {"signStatus": type(self).final_status if self.signed else 3, "signDate": "2026-10-10"}

    def sign(self, sample, lat, lng):
        payload = build_sign_payload(sample, self.get_status(sample["taskId"]), lat, lng)
        type(self).submitted.append((self.openid, payload))
        self.signed = True
        return self.get_status(sample["taskId"])

    def close(self): pass


class LocationTests(unittest.TestCase):
    def test_new_configuration_defaults_to_csuft_and_persists(self):
        with tempfile.TemporaryDirectory() as folder:
            store = Store(folder)
            account = new_account("mock-default")
            store.data["accounts"] = [account]
            selected = resolve_location(account, store.data)
            self.assertEqual((selected["name"], selected["latitude"], selected["longitude"]), ("中南林业科技大学", 28.131567, 112.994905))
            self.assertEqual(selected["mode"], "specified")
            store.save()
            restored = Store(folder)
            self.assertEqual(resolve_location(restored.data["accounts"][0], restored.data), selected)

    def test_existing_global_choice_and_deleted_preset_stay_saved(self):
        with tempfile.TemporaryDirectory() as folder:
            store = Store(folder)
            store.data.update(locations=[], global_location=dict(TASK_LOCATION))
            store.save()
            restored = Store(folder)
            self.assertEqual(restored.data["locations"], [])
            self.assertEqual(restored.data["global_location"], TASK_LOCATION)
        original = defaults()
        changed = defaults()
        changed["locations"][0]["latitude"] = 0
        self.assertEqual(original["locations"][0]["latitude"], 28.131567)

    def test_csuft_default_mock_submission_has_no_offset(self):
        config = defaults()
        campus = university_location()
        MockClient.sample = {**task(), "dormitoryRegisterVO": {"roomId": "mock-room", "locationLat": campus["latitude"], "locationLng": campus["longitude"]}}
        with patch("runner.random.random", side_effect=AssertionError("offset prohibited")):
            results = run_accounts([new_account("mock-default")], lambda *args: None, client_factory=MockClient, delay_scale=0, location_config=config)
        self.assertEqual(results[0]["status"], "success")
        self.assertEqual(results[0]["preview"]["distance"], 0)
        self.assertEqual((MockClient.submitted[0][1]["signLat"], MockClient.submitted[0][1]["signLng"]), (28.131567, 112.994905))

    def setUp(self):
        self.config = defaults()
        self.first = saved_location("位置一", "28.1310861234567", "112.994658987654")
        self.second = saved_location("位置二", 28.1312, 112.9947)
        self.config["locations"] = [self.first, self.second]
        self.config["global_location"] = {"mode": "specified", "location_id": self.first["id"]}
        MockClient.submitted, MockClient.on_login, MockClient.sample, MockClient.final_status = [], None, None, 0

    def run_mock(self, accounts, **kwargs):
        events = []
        with patch("runner.now_shanghai", return_value=datetime(2026, 10, 10, 12, tzinfo=SHANGHAI)):
            results = run_accounts(accounts, lambda kind, value: events.append((kind, value)), client_factory=MockClient, delay_scale=0, location_config=self.config, **kwargs)
        return results, events

    def test_reject_empty_nonfinite_out_of_range_coordinates(self):
        for bad in ("", " ", None, "abc", "nan", "inf", "-inf", float("nan"), True, [], "1e999"):
            for axis in range(2):
                with self.subTest(value=bad, axis=axis), self.assertRaises(ValueError):
                    coordinates(bad if axis == 0 else 0, bad if axis == 1 else 0)
        for lat, lng in ((90.000001, 0), (-90.000001, 0), (0, 180.000001), (0, -180.000001)):
            with self.subTest(lat=lat, lng=lng), self.assertRaises(ValueError): coordinates(lat, lng)
        self.assertEqual(coordinates(-90, 180), (-90, 180))
        self.assertEqual(coordinates(90, -180), (90, -180))
        with self.assertRaises(ValueError): saved_location(" ", 0, 0)

    def test_global_account_override_and_explicit_task(self):
        account = new_account("mock-id")
        self.assertEqual(resolve_location(account, self.config)["name"], "位置一")
        account["location"] = {"mode": "specified", "location_id": self.second["id"]}
        self.assertEqual(resolve_location(account, self.config)["name"], "位置二")
        account["location"] = dict(TASK_LOCATION)
        self.assertEqual(resolve_location(account, self.config)["mode"], "task")

    def test_legacy_accounts_stay_task_default_after_global_change(self):
        with tempfile.TemporaryDirectory() as folder:
            old = {"version": 1, "accounts": [{"id": "old", "label": "旧账号", "openid": "mock-old"}]}
            Path(folder, "config.json").write_text(json.dumps(old), encoding="utf-8")
            store = Store(folder)
            store.data.update(locations=self.config["locations"], global_location=self.config["global_location"])
            store.save()
            restored = Store(folder)
            self.assertEqual(restored.data["version"], 3)
            self.assertEqual(resolve_location(restored.data["accounts"][0], restored.data)["mode"], "task")

    def test_locations_persist_edit_and_delete_with_account_choice(self):
        with tempfile.TemporaryDirectory() as folder:
            store = Store(folder)
            account = new_account("mock-id")
            account["location"] = {"mode": "specified", "location_id": self.second["id"]}
            store.data.update(locations=self.config["locations"], global_location=self.config["global_location"], accounts=[account])
            store.save()
            store.data["locations"][1] = saved_location("改名", 28.1313, 112.9948, self.second["id"])
            store.save()
            restored = Store(folder)
            choice = resolve_location(restored.data["accounts"][0], restored.data)
            self.assertEqual((choice["name"], choice["latitude"]), ("改名", 28.1313))
            self.assertEqual(restored.data["locations"][0]["latitude"], self.first["latitude"])
            restored.data["accounts"][0]["location"] = dict(TASK_LOCATION)
            restored.data["locations"].pop(1)
            restored.save()
            self.assertEqual(len(Store(folder).data["locations"]), 1)

    def test_missing_reference_does_not_fallback_or_open_network(self):
        account = new_account("mock-id")
        account["location"] = {"mode": "specified", "location_id": "missing"}
        with patch.object(MockClient, "login", side_effect=AssertionError("network prohibited")):
            results, _ = self.run_mock([account])
        self.assertEqual(results[0]["status"], "failure")
        self.assertIn("重新选择", results[0]["message"])

    def test_store_rejects_invalid_coordinate_and_dangling_reference(self):
        with tempfile.TemporaryDirectory() as folder:
            store = Store(folder)
            store.data.update(locations=[{**self.first, "latitude": "nan"}])
            with self.assertRaises(ValueError): store.save()
            store.data.update(locations=[], global_location=self.config["global_location"])
            with self.assertRaises(ValueError): store.save()
            self.assertFalse(store.path.exists())

    def test_exact_coordinates_preview_distance_payload_and_digest(self):
        with patch("runner.random.random", side_effect=AssertionError("random offset prohibited")):
            results, events = self.run_mock([new_account("mock-id")])
        preview = next(value for kind, value in events if kind == "preview")
        payload = MockClient.submitted[0][1]
        self.assertEqual((payload["signLat"], payload["signLng"]), (self.first["latitude"], self.first["longitude"]))
        self.assertEqual((preview["latitude"], preview["longitude"]), (payload["signLat"], payload["signLng"]))
        self.assertEqual(preview["distance"], payload["locationAccuracy"])
        self.assertEqual(preview["distance"], distance(payload["signLat"], payload["signLng"], 28.131086, 112.994658))
        # An independent expectation, not a digest built with the implementation helper.
        source = {"latitude": "28.1310861234567", "longitude": "112.994658987654", "locationAccuracy": "0.1", "signDate": "2026-10-10", "taskId": "task-a", "fileId": ""}
        self.assertEqual(payload["stuTaskId"], hashlib.md5(json.dumps(source, separators=(",", ":")).encode()).hexdigest())
        self.assertEqual(results[0]["server_status"]["signStatus"], 0)

    def test_request_signature_and_digest_use_submitted_values(self):
        session = Session([{"access_token": "mock-token"}, {"success": True, "data": {"signStatus": 3, "signDate": "2026-10-10"}}, {"success": True}, {"success": True, "data": {"signStatus": 0}}])
        client = SchoolClient("mock-id", session)
        with patch("api.time.time", return_value=1770000000.123):
            client.login()
            client.sign(task(), self.first["latitude"], self.first["longitude"])
        method, url, arguments = session.calls[2]
        self.assertEqual(method, "POST")
        self.assertEqual(arguments["headers"]["Flysource-Sign"], signature(url, "mock-token", "1770000000123"))
        payload = arguments["json"]
        self.assertEqual(payload["signLat"], self.first["latitude"])
        self.assertEqual(payload, build_sign_payload(task(), {"signDate": "2026-10-10"}, payload["signLat"], payload["signLng"]))

    def test_small_numbers_keep_signature_precision(self):
        self.assertEqual(js_number(0.0000011234567890123), "0.0000011234567890123")
        self.assertEqual(js_number(1e-7), "1e-7")
        self.assertEqual(js_number(-0.0), "0")

    def test_batch_freezes_all_accounts_before_login(self):
        accounts = [new_account("mock-a"), new_account("mock-b")]
        accounts[1]["location"] = {"mode": "specified", "location_id": self.second["id"]}
        def mutate_config(openid):
            self.config["locations"][0]["latitude"] = 0
            self.config["locations"][1]["latitude"] = 0
            accounts[1]["location"] = dict(TASK_LOCATION)
        MockClient.on_login = mutate_config
        with patch("runner.random.random", side_effect=AssertionError("offset prohibited")):
            results, _ = self.run_mock(accounts)
        self.assertEqual([item["status"] for item in results], ["success", "success"])
        self.assertEqual([payload["signLat"] for _, payload in MockClient.submitted], [28.1310861234567, 28.1312])

    def test_manual_batch_scheduled_select_same_positions(self):
        account = new_account("mock-a")
        for scheduled, items in ((False, [account]), (False, [account, new_account("mock-b")]), (True, [account])):
            MockClient.submitted = []
            results, _ = self.run_mock(items, scheduled=scheduled)
            self.assertTrue(all(item["status"] == "success" for item in results))
            self.assertTrue(all(payload["signLat"] == self.first["latitude"] for _, payload in MockClient.submitted))

    def test_default_mode_keeps_original_random_offset_and_rounding(self):
        account = new_account("mock-id")
        account["location"] = dict(TASK_LOCATION)
        with patch("runner.random.random", side_effect=[0.12, 0.23]):
            results, events = self.run_mock([account])
        payload = MockClient.submitted[0][1]
        self.assertEqual((payload["signLat"], payload["signLng"]), (28.131206, 112.994888))
        self.assertEqual(next(value for kind, value in events if kind == "preview")["latitude"], payload["signLat"])

    def test_out_of_range_is_previewed_and_never_submitted(self):
        self.config["locations"][0].update(latitude=0, longitude=0)
        results, events = self.run_mock([new_account("mock-id")])
        preview = next(value for kind, value in events if kind == "preview")
        self.assertGreater(preview["distance"], preview["allowed_distance"])
        self.assertFalse(preview["allowed"])
        self.assertEqual(results[0]["status"], "failure")
        self.assertIn("定位设置", results[0]["message"])
        self.assertEqual(MockClient.submitted, [])

    def test_distance_boundary_photo_and_bad_task_are_blocked(self):
        measured = distance(self.first["latitude"], self.first["longitude"], 28.131086, 112.994658)
        for changes in ({"locationAccuracy": measured}, {"openTakePhoto": 1}, {"locationAccuracy": "nan"}, {"dormitoryRegisterVO": {"roomId": "a", "locationLat": 100, "locationLng": 0}}):
            with self.subTest(changes=changes):
                MockClient.sample = {**task(), **changes}
                results, events = self.run_mock([new_account("mock-id")])
                self.assertEqual(results[0]["status"], "failure")
                self.assertEqual(MockClient.submitted, [])
                self.assertFalse(next(value for kind, value in events if kind == "preview")["allowed"])

    def test_outside_window_defers_to_server_and_late_photo_blocks(self):
        MockClient.sample = {**task(), "signStartTime": "20:00:00", "signEndTime": "21:00:00"}
        results, events = self.run_mock([new_account("mock-id")])
        self.assertEqual(results[0]["status"], "success")
        self.assertTrue(any("服务端判断" in str(value) for _, value in events))
        MockClient.submitted = []
        MockClient.sample["isLateStuTakePhoto"] = 1
        results, _ = self.run_mock([new_account("mock-id")])
        self.assertEqual(results[0]["status"], "failure")
        self.assertIn("照片", results[0]["message"])
        self.assertEqual(MockClient.submitted, [])

    def test_preview_rejection_or_cancellation_prevents_submit(self):
        results, _ = self.run_mock([new_account("mock-id")], preview_handler=lambda preview: False)
        self.assertEqual(results[0]["status"], "cancelled")
        self.assertEqual(MockClient.submitted, [])
        cancel = threading.Event()
        def approve(preview):
            cancel.set()
            return True
        results, _ = self.run_mock([new_account("mock-id")], cancel=cancel, preview_handler=approve)
        self.assertEqual(results[0]["status"], "cancelled")
        self.assertEqual(MockClient.submitted, [])

    def test_fake_submit_success_is_not_reported_as_signed(self):
        MockClient.final_status = 3
        results, _ = self.run_mock([new_account("mock-id")])
        self.assertEqual(results[0]["status"], "failure")
        self.assertIn("仍显示未签到", results[0]["message"])

    def test_logs_results_and_preview_scrub_openid_and_token(self):
        account = new_account("mock-private-openid", "模拟账号")
        with patch.object(MockClient, "sign", side_effect=ApiError("server echo mock-private-openid mock-secret-token")):
            results, events = self.run_mock([account])
        output = json.dumps([results, events], ensure_ascii=False)
        self.assertNotIn("mock-private-openid", output)
        self.assertNotIn("mock-secret-token", output)
        self.assertIn("已隐藏", output)

    def test_cli_uses_snapshot_location_without_network(self):
        import app
        with tempfile.TemporaryDirectory() as folder:
            store = Store(folder)
            store.data.update(self.config)
            store.data["accounts"] = [new_account("mock-id")]
            real_runner = run_accounts
            def run_offline(*args, **kwargs):
                return real_runner(*args, **kwargs, client_factory=MockClient, delay_scale=0)
            with patch.object(app, "run_accounts", side_effect=run_offline), patch("runner.now_shanghai", return_value=datetime(2026, 10, 10, 12, tzinfo=SHANGHAI)):
                self.assertEqual(app.run_cli(store, False), 0)
            self.assertEqual(MockClient.submitted[0][1]["signLat"], self.first["latitude"])
            self.assertEqual(Store(folder).data["results"][0]["preview"]["latitude"], self.first["latitude"])


if __name__ == "__main__": unittest.main()
