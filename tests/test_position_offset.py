import copy
import json
import math
import random
import sys
import tempfile
import threading
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from api import (
    ApiError,
    RequestCancelled,
    SchoolClient,
    build_sign_payload,
    distance,
    rejection_code,
    signature,
)
from execution import make_request
from locations import TASK_LOCATION, normalize_locations, resolve_location
from position_offset import (
    candidate,
    arc_distance,
    choose_position,
    validate_offset,
    resolve_offset,
    validate_selected,
    checkpoint_position,
    MAX_CANDIDATES,
)
from runner import run_accounts
from storage import Store, defaults, new_account, SHANGHAI
from test_app import task, Session
from data_management import create_backup, read_backup, restore_backup
from scheduling import (
    begin_attempt,
    checkpoint_result,
    recover_interrupted,
    finish_attempt,
)

NOW = datetime(2026, 10, 10, 12, tzinfo=SHANGHAI)
CENTER = (28.131086, 112.994658)


class FixedRandom:
    def random(self):
        return 0.5


class OfflineServer:
    calls = []
    errors = []
    signed = set()
    sample = task()
    token = "mock-token"
    on_sign = None
    on_query = None

    def __init__(self, openid):
        self.openid = openid

    def login(self):
        return {"userName": "模拟用户"}

    def list_tasks(self):
        return [{"taskId": self.sample["taskId"]}]

    def get_task(self, identity):
        return copy.deepcopy(type(self).sample)

    def get_status(self, identity):
        type(self).calls.append(("query", self.openid, identity))
        if type(self).on_query:
            type(self).on_query(self)
        return {
            "signStatus": 0 if self.openid in type(self).signed else 3,
            "signDate": "2026-10-10",
        }

    def sign(self, sample, lat, lng):
        payload = build_sign_payload(
            sample, self.get_status(sample["taskId"]), lat, lng
        )
        if getattr(self, "submission_guard", None):
            self.submission_guard(self.get_status(sample["taskId"]))
        if self.cancel.is_set():
            raise RequestCancelled("模拟取消")
        type(self).calls.append(("submit", self.openid, payload))
        if type(self).on_sign:
            type(self).on_sign(self)
        if type(self).errors:
            raise type(self).errors.pop(0)
        type(self).signed.add(self.openid)
        return self.get_status(sample["taskId"])

    def close(self):
        pass


class OffsetTests(unittest.TestCase):
    def setUp(self):
        self.network = patch(
            "requests.Session.request",
            side_effect=AssertionError("Real network forbidden"),
        )
        self.network.start()
        self.addCleanup(self.network.stop)
        self.config = defaults()
        self.config["locations"][0].update(latitude=CENTER[0], longitude=CENTER[1])
        self.config["global_offset"] = {"mode": "on", "radius": 20}
        self.account = new_account("mock-offset", "偏移模拟")
        self.config["accounts"] = [self.account]
        OfflineServer.calls, OfflineServer.errors, OfflineServer.signed = [], [], set()
        OfflineServer.sample, OfflineServer.on_sign, OfflineServer.on_query = (
            task(),
            None,
            None,
        )
        self.events = []

    def run_mock(self, config=None, accounts=None, **kwargs):
        config = config if config is not None else self.config
        with patch("runner.now_shanghai", return_value=NOW), patch(
            "storage.now_shanghai", return_value=NOW
        ):
            return run_accounts(
                accounts if accounts is not None else config["accounts"],
                lambda k, v: self.events.append((k, copy.deepcopy(v))),
                client_factory=OfflineServer,
                delay_scale=0,
                location_config=config,
                random_source=FixedRandom(),
                **kwargs
            )

    def posts(self):
        return [v for v in OfflineServer.calls if v[0] == "submit"]

    def persist(self, value):
        checkpoint_position(self.config, value)
        return True

    def test_defaults_and_legacy_migration_do_not_enable(self):
        data = defaults()
        self.assertEqual(data["global_offset"]["mode"], "off")
        self.assertEqual(resolve_offset(new_account("mock"), data)["mode"], "off")
        data.pop("global_offset")
        data["accounts"] = [{"id": "old", "openid": "mock", "label": "old"}]
        normalize_locations(data, True)
        self.assertEqual(resolve_offset(data["accounts"][0], data)["mode"], "off")
        self.assertEqual(data["accounts"][0]["location"], TASK_LOCATION)

    def test_inheritance_independent_of_location_and_account_overrides(self):
        self.account["location"] = dict(TASK_LOCATION)
        self.assertEqual(resolve_offset(self.account, self.config)["radius"], 20)
        self.account["offset"] = {"mode": "off", "radius": 0}
        self.assertEqual(resolve_offset(self.account, self.config)["mode"], "off")
        self.account["offset"] = {"mode": "on", "radius": 3}
        self.assertEqual(resolve_offset(self.account, self.config)["radius"], 3)

    def test_radius_rejects_nonfinite_negative_bool_empty_and_over_limit(self):
        for bad in (-1, 501, float("inf"), float("nan"), "", None, True, [], "1e999"):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                validate_offset({"mode": "on", "radius": bad})
        for value in (0, "0.1", 500):
            self.assertEqual(
                validate_offset({"mode": "on", "radius": value})["radius"], float(value)
            )

    def test_uniform_circle_and_poles_and_dateline(self):
        rng = random.Random(45)
        for center in (CENTER, (90, 180), (-90, -180), (0, 179.99999), (89.999999, 0)):
            lengths = []
            for _ in range(1000):
                point = candidate(center, 500, rng)
                self.assertTrue(-90 <= point[0] <= 90 and -180 <= point[1] <= 180)
                measured = arc_distance(center, point)
                self.assertLessEqual(measured, 500 + 1e-7)
                lengths.append(measured**2)
            self.assertAlmostEqual(
                sum(lengths) / len(lengths) / 500**2, 0.5, delta=0.035
            )

    def test_zero_radius_never_calls_random_source(self):
        rng = unittest.mock.Mock()
        rng.random.side_effect = AssertionError("zero radius must not sample")
        selected = choose_position(task(), CENTER, 0, rng)
        self.assertEqual((selected["latitude"], selected["longitude"]), CENTER)
        self.assertEqual(selected["candidate_count"], 1)
        self.assertEqual(selected["stage"], "中心点")

    def test_original_radius_candidate(self):
        selected = choose_position(task(), CENTER, 20, FixedRandom())
        self.assertEqual(selected["stage"], "原半径")
        self.assertGreater(selected["offset_distance"], 0)
        self.assertLessEqual(selected["offset_distance"], 20)

    def test_shrinking_range_uses_finite_stages(self):
        sample = {**task(), "locationAccuracy": 10}
        selected = choose_position(sample, CENTER, 20, FixedRandom())
        self.assertEqual(selected["stage"], "缩小范围")
        self.assertEqual(selected["factor"], 0.5)
        self.assertEqual(selected["candidate_count"], 9)
        self.assertEqual(selected["candidate_stages"][0]["rejected"], 8)

    def test_all_random_candidates_fail_then_center_passes(self):
        selected = choose_position(
            {**task(), "locationAccuracy": 1}, CENTER, 500, FixedRandom()
        )
        self.assertEqual(selected["stage"], "中心点")
        self.assertEqual(selected["candidate_count"], MAX_CANDIDATES)
        self.assertEqual(selected["offset_distance"], 0)

    def test_all_stages_fail_stop_without_submit_and_other_account_continues(self):
        self.config["locations"][0].update(latitude=0, longitude=0)
        other = new_account("mock-good")
        other["location"] = dict(TASK_LOCATION)
        self.config["accounts"].append(other)
        result = self.run_mock()
        self.assertEqual(result[0]["error_code"], "offset_local")
        self.assertEqual(result[1]["status"], "success")
        self.assertEqual(sum(s["tried"] for s in result[0]["candidate_stages"]), 33)
        self.assertIn("调整中心", result[0]["message"])
        self.assertEqual([p[1] for p in self.posts()], ["mock-good"])

    def test_invalid_task_rules_stop_before_sampling_and_submission(self):
        for bad in (None, 0, -1, "nan", "abc", True):
            OfflineServer.sample = {**task(), "locationAccuracy": bad}
            with patch(
                "position_offset.candidate",
                side_effect=AssertionError("must not sample"),
            ):
                result = self.run_mock()[0]
            self.assertEqual(result["status"], "failure")
        self.assertEqual(self.posts(), [])

    def test_strict_boundary_and_margin_use_actual_rounded_distance(self):
        point = candidate(CENTER, 20, FixedRandom())
        measured = distance(*point, *CENTER)
        value = {
            "latitude": point[0],
            "longitude": point[1],
            "center_latitude": CENTER[0],
            "center_longitude": CENTER[1],
            "radius": 20,
        }
        for limit in (measured, measured + 0.1, measured + 0.5):
            with self.assertRaises(ApiError):
                validate_selected({**task(), "locationAccuracy": limit}, value)
        result = validate_selected({**task(), "locationAccuracy": measured + 2}, value)
        self.assertEqual(result["distance"], measured)
        self.assertLessEqual(result["margin"], 1)
        self.assertLess(result["margin"], result["allowed_distance"])

    def test_outside_radius_candidate_is_never_submitted(self):
        with patch("position_offset.candidate", return_value=(0, 0)):
            result = self.run_mock()[0]
        self.assertEqual(result["preview"]["stage"], "中心点")
        self.assertEqual(len(self.posts()), 1)
        self.assertEqual(
            (self.posts()[0][2]["signLat"], self.posts()[0][2]["signLng"]), CENTER
        )

    def test_preview_payload_digest_share_final_values_and_positions_unchanged(self):
        before = copy.deepcopy(self.config["locations"])
        result = self.run_mock()[0]
        preview = result["preview"]
        payload = self.posts()[0][2]
        self.assertEqual(
            (preview["latitude"], preview["longitude"]),
            (payload["signLat"], payload["signLng"]),
        )
        self.assertEqual(preview["distance"], payload["locationAccuracy"])
        self.assertEqual(
            payload,
            build_sign_payload(
                task(),
                {"signDate": "2026-10-10"},
                preview["latitude"],
                preview["longitude"],
            ),
        )
        self.assertEqual(self.config["locations"], before)

    def test_task_mode_uses_unperturbed_center_without_legacy_random(self):
        self.account["location"] = dict(TASK_LOCATION)
        with patch("runner.random.random", side_effect=AssertionError("double offset")):
            result = self.run_mock()[0]
        self.assertEqual(
            (
                result["preview"]["center_latitude"],
                result["preview"]["center_longitude"],
            ),
            CENTER,
        )

    def test_disabled_specified_coordinates_unchanged_and_legacy_task_compatible(self):
        self.config["global_offset"]["mode"] = "off"
        with patch(
            "runner.random.random", side_effect=AssertionError("specified offset")
        ):
            result = self.run_mock()[0]
        self.assertEqual(
            (result["preview"]["latitude"], result["preview"]["longitude"]), CENTER
        )
        OfflineServer.signed.clear()
        self.account["location"] = dict(TASK_LOCATION)
        with patch("runner.random.random", return_value=0.01):
            result = self.run_mock()[0]
        self.assertEqual(result["preview"]["latitude"], round(CENTER[0] + 0.00001, 6))

    def test_running_snapshot_survives_config_edits(self):
        request = make_request(self.config, moment=NOW)
        self.config["global_offset"]["radius"] = 300
        self.config["locations"][0]["latitude"] = 0
        result = self.run_mock(accounts=request["accounts"])[0]
        self.assertEqual(result["status"], "success")
        self.assertEqual(result["preview"]["radius"], 20)
        self.assertEqual(result["preview"]["center_latitude"], CENTER[0])

    def test_network_retry_restores_same_coordinates_and_configuration(self):
        OfflineServer.errors = [ApiError("模拟超时", "network_timeout", True, True)]
        first = self.run_mock(scheduled=True, position_handler=self.persist)[0]
        self.assertTrue(first["uncertain"])
        self.config["global_offset"]["radius"] = 500
        self.config["locations"][0]["latitude"] = 0
        with patch(
            "position_offset.candidate",
            side_effect=AssertionError("retry must not re-randomize"),
        ):
            second = self.run_mock(
                scheduled=True,
                retry_context={
                    "date": "2026-10-10",
                    "task_ids": {self.account["id"]: "task-a"},
                },
                position_handler=self.persist,
            )[0]
        self.assertEqual(second["status"], "success")
        self.assertEqual(first["preview"]["latitude"], second["preview"]["latitude"])
        self.assertEqual(self.posts()[0][2], self.posts()[1][2])
        self.assertEqual(second["submission_count"], 2)

    def test_retry_keeps_task_a_coordinate_after_manual_task_b_same_account(self):
        OfflineServer.errors = [ApiError("模拟超时", "network_timeout", True, True)]
        first = self.run_mock(scheduled=True, position_handler=self.persist)[0]
        OfflineServer.sample = {**task(), "taskId": "task-b"}
        self.config["global_offset"]["radius"] = 40
        self.run_mock(position_handler=self.persist)
        self.assertEqual(
            self.config["position_cache"][self.account["id"]]["task_id"], "task-b"
        )
        self.assertIn(
            "task-a",
            self.config["position_cache"][self.account["id"]]["task_positions"],
        )
        OfflineServer.sample = task()
        OfflineServer.signed.clear()
        with patch(
            "position_offset.candidate",
            side_effect=AssertionError("task A must retain its coordinate"),
        ):
            again = self.run_mock(
                scheduled=True,
                retry_context={
                    "date": "2026-10-10",
                    "task_ids": {self.account["id"]: "task-a"},
                },
                position_handler=self.persist,
            )[0]
        self.assertEqual(again["status"], "success")
        self.assertEqual(
            (again["preview"]["latitude"], again["preview"]["longitude"]),
            (first["preview"]["latitude"], first["preview"]["longitude"]),
        )
        self.assertEqual(again["preview"]["radius"], 20)

    def test_outside_radius_by_small_amount_is_rejected_without_tolerance(self):
        point = candidate(CENTER, 20, FixedRandom())
        radius = arc_distance(CENTER, point) - 1e-8
        value = {
            "latitude": point[0],
            "longitude": point[1],
            "center_latitude": CENTER[0],
            "center_longitude": CENTER[1],
            "radius": radius,
        }
        with self.assertRaises(ApiError):
            validate_selected(task(), value)

    def test_timeout_then_server_signed_only_queries(self):
        OfflineServer.errors = [ApiError("模拟超时", "network_timeout", True, True)]
        OfflineServer.on_sign = lambda client: OfflineServer.signed.add(client.openid)
        self.run_mock(scheduled=True, position_handler=self.persist)
        result = self.run_mock(
            scheduled=True,
            retry_context={
                "date": "2026-10-10",
                "task_ids": {self.account["id"]: "task-a"},
            },
        )[0]
        self.assertEqual(result["success_kind"], "already_signed")
        self.assertEqual(len(self.posts()), 1)

    def test_distance_rejection_adjusts_once_to_original_center(self):
        OfflineServer.errors = [ApiError("不在签到范围内", "position_rejected")]
        result = self.run_mock(position_handler=self.persist)[0]
        self.assertEqual(result["status"], "success")
        self.assertEqual(len(self.posts()), 2)
        self.assertEqual(result["position_state"]["adjustments"], 1)
        self.assertEqual(result["candidate_count"], 2)
        self.assertEqual(
            (self.posts()[1][2]["signLat"], self.posts()[1][2]["signLng"]), CENTER
        )
        self.assertEqual(result["preview"]["stage"], "中心点")

    def test_repeated_distance_rejection_cannot_loop(self):
        OfflineServer.errors = [ApiError("不在签到范围内", "position_rejected")] * 8
        result = self.run_mock()[0]
        self.assertEqual(result["error_code"], "position_rejected")
        self.assertEqual(len(self.posts()), 2)

    def test_same_center_is_not_resubmitted_after_distance_rejection(self):
        self.config["global_offset"]["radius"] = 0
        OfflineServer.errors = [ApiError("不在签到范围内", "position_rejected")]
        result = self.run_mock()[0]
        self.assertEqual(result["status"], "failure")
        self.assertEqual(len(self.posts()), 1)

    def test_network_failure_after_adjustment_keeps_adjusted_coordinate_and_limit(self):
        OfflineServer.errors = [
            ApiError("不在签到范围内", "position_rejected"),
            ApiError("超时", "network_timeout", True, True),
        ]
        first = self.run_mock(scheduled=True, position_handler=self.persist)[0]
        self.assertEqual(first["position_state"]["adjustments"], 1)
        OfflineServer.errors = [ApiError("不在签到范围内", "position_rejected")]
        second = self.run_mock(
            scheduled=True,
            retry_context={
                "date": "2026-10-10",
                "task_ids": {self.account["id"]: "task-a"},
            },
            position_handler=self.persist,
        )[0]
        self.assertEqual(second["error_code"], "position_rejected")
        self.assertEqual(len(self.posts()), 3)
        self.assertEqual(self.posts()[1][2], self.posts()[2][2])

    def test_auth_photo_unknown_and_uncertain_errors_do_not_switch(self):
        for code, uncertain in (
            ("authentication", False),
            ("photo_required", False),
            ("server_rejected", False),
            ("position_rejected", True),
            ("http_temporary", True),
        ):
            OfflineServer.calls, OfflineServer.signed = [], set()
            OfflineServer.errors = [
                ApiError("模拟业务错误", code, code == "http_temporary", uncertain)
            ]
            result = self.run_mock()[0]
            self.assertEqual(len(self.posts()), 1)
            self.assertEqual(result["position_state"]["adjustments"], 0)

    def test_photo_required_generates_no_candidates_or_submissions(self):
        OfflineServer.sample["openTakePhoto"] = 1
        with patch(
            "position_offset.candidate", side_effect=AssertionError("photo must stop")
        ):
            result = self.run_mock()[0]
        self.assertEqual(result["error_code"], "photo_required")
        self.assertEqual(self.posts(), [])

    def test_distance_rejection_then_task_end_or_change_stops(self):
        for change in (
            {"signStartTime": "00:00:00", "signEndTime": "01:00:00"},
            {"taskId": "changed"},
            {"openTakePhoto": 1},
            {"enabled": False},
            {"locationAccuracy": 0},
        ):
            OfflineServer.calls, OfflineServer.signed = [], set()
            OfflineServer.sample = task()
            OfflineServer.errors = [ApiError("不在签到范围内", "position_rejected")]
            OfflineServer.on_sign = lambda client: OfflineServer.sample.update(change)
            result = self.run_mock()[0]
            self.assertEqual(result["status"], "failure")
            self.assertEqual(len(self.posts()), 1)

    def test_cancellation_after_checkpoint_prevents_submission(self):
        cancel = threading.Event()

        def checkpoint(value):
            cancel.set()
            return True

        result = self.run_mock(cancel=cancel, position_handler=checkpoint)[0]
        self.assertEqual(result["status"], "cancelled")
        self.assertEqual(self.posts(), [])

    def test_cancel_during_distance_rejection_does_not_adjust(self):
        cancel = threading.Event()
        OfflineServer.errors = [ApiError("不在签到范围内", "position_rejected")]
        OfflineServer.on_sign = lambda client: cancel.set()
        result = self.run_mock(cancel=cancel)[0]
        self.assertEqual(result["status"], "cancelled")
        self.assertEqual(len(self.posts()), 1)

    def test_checkpoint_save_failure_prevents_submission(self):
        result = self.run_mock(position_handler=lambda value: False)[0]
        self.assertEqual(result["error_code"], "position_save")
        self.assertEqual(self.posts(), [])

    def test_local_candidate_cancellation(self):
        cancel = threading.Event()
        cancel.set()
        with self.assertRaises(RequestCancelled):
            choose_position(task(), CENTER, 20, cancel=cancel)

    def test_cross_day_and_task_switch_cannot_reuse_retry(self):
        OfflineServer.errors = [ApiError("模拟超时", "network_timeout", True, True)]
        self.run_mock(scheduled=True, position_handler=self.persist)
        count = len(self.posts())
        result = self.run_mock(
            scheduled=True,
            retry_context={
                "date": "2026-10-11",
                "task_ids": {self.account["id"]: "task-a"},
            },
        )[0]
        self.assertEqual(result["status"], "failure")
        result = self.run_mock(
            scheduled=True,
            retry_context={
                "date": "2026-10-10",
                "task_ids": {self.account["id"]: "other"},
            },
        )[0]
        self.assertEqual(result["error_code"], "retry_task_changed")
        self.assertEqual(len(self.posts()), count)

    def test_multiaccount_cache_cannot_cross_id(self):
        OfflineServer.errors = [ApiError("模拟超时", "network_timeout", True, True)]
        self.run_mock(position_handler=self.persist)
        second = new_account("mock-second")
        self.config["accounts"].append(second)
        self.config["position_cache"][second["id"]] = self.config["position_cache"][
            self.account["id"]
        ]
        result = self.run_mock(accounts=[second])[0]
        self.assertEqual(result["status"], "success")
        self.assertEqual(result["submission_count"], 1)

    def test_persisted_submit_limit_stops_even_after_restart(self):
        OfflineServer.errors = [ApiError("模拟超时", "network_timeout", True, True)] * 5
        for _ in range(4):
            result = self.run_mock(scheduled=True, position_handler=self.persist)[0]
        self.assertEqual(len(self.posts()), 4)
        result = self.run_mock(
            scheduled=True,
            retry_context={
                "date": "2026-10-10",
                "task_ids": {self.account["id"]: "task-a"},
            },
            position_handler=self.persist,
        )[0]
        self.assertEqual(result["error_code"], "submission_limit")
        self.assertEqual(len(self.posts()), 4)

    def test_reboot_recovers_frozen_position_before_any_new_sampling(self):
        with tempfile.TemporaryDirectory() as folder:
            store = Store(folder)
            store.data = copy.deepcopy(self.config)
            store.data["schedule"]["enabled"] = True
            request = make_request(store.data, "automatic", moment=NOW)
            begin_attempt(store.data, request, NOW)

            def checkpoint(value):
                checkpoint_position(store.data, value)
                store.save()
                return True

            OfflineServer.errors = [
                ApiError("模拟提交超时", "network_timeout", True, True)
            ]
            result = self.run_mock(
                config=store.data, scheduled=True, position_handler=checkpoint
            )[0]
            # Emulate abrupt exit with an in-flight automation state and saved coordinate.
            restored = Store(folder)
            self.assertTrue(
                recover_interrupted(restored.data, NOW + timedelta(minutes=5))
            )
            self.assertIn(
                self.account["id"], restored.data["automation"]["pending_ids"]
            )
            with patch(
                "position_offset.candidate",
                side_effect=AssertionError("restore must use saved coordinate"),
            ):
                again = self.run_mock(
                    config=restored.data,
                    scheduled=True,
                    retry_context={
                        "date": "2026-10-10",
                        "task_ids": {self.account["id"]: "task-a"},
                    },
                )[0]
            self.assertEqual(again["status"], "success")
            self.assertEqual(
                result["preview"]["latitude"], again["preview"]["latitude"]
            )

    def test_new_config_fields_roundtrip_backup_and_restore(self):
        with tempfile.TemporaryDirectory() as folder:
            store = Store(Path(folder) / "data")
            store.data = copy.deepcopy(self.config)
            store.save()
            target = Path(folder) / "backup.pingan"
            create_backup(store.data, store.directory, target, "test-password")
            payload = read_backup(target, "test-password")
            restored = restore_backup(store, payload)
            self.assertEqual(
                Store(store.directory).data["global_offset"],
                self.config["global_offset"],
            )
            self.assertEqual(Store(store.directory).data["position_cache"], {})

    def test_stale_or_removed_account_cache_is_pruned(self):
        OfflineServer.errors = [ApiError("模拟超时", "network_timeout", True, True)]
        self.run_mock(position_handler=self.persist)
        value = copy.deepcopy(self.config["position_cache"][self.account["id"]])
        self.config["position_cache"]["deleted"] = {**value, "account_id": "deleted"}
        self.config["position_cache"][self.account["id"]]["date"] = "2026-10-09"
        with patch("storage.now_shanghai", return_value=NOW):
            checkpoint_position(self.config, value)
        self.assertEqual(list(self.config["position_cache"]), [self.account["id"]])

    def test_cached_point_outside_new_rule_stops_without_rerandomizing(self):
        OfflineServer.errors = [ApiError("模拟超时", "network_timeout", True, True)]
        self.run_mock(scheduled=True, position_handler=self.persist)
        OfflineServer.sample["locationAccuracy"] = 1
        with patch(
            "position_offset.candidate",
            side_effect=AssertionError("network retry must not repair distance"),
        ):
            result = self.run_mock(
                scheduled=True,
                retry_context={
                    "date": "2026-10-10",
                    "task_ids": {self.account["id"]: "task-a"},
                },
            )[0]
        self.assertEqual(result["error_code"], "offset_local")
        self.assertEqual(len(self.posts()), 1)

    def test_exact_rejection_classification_only_at_submit_endpoint(self):
        self.assertEqual(
            rejection_code({"success": False, "msg": "不在签到范围内"}, "stuSign"),
            "position_rejected",
        )
        self.assertEqual(
            rejection_code(
                {"success": False, "code": "LOCATION_OUT_OF_RANGE"}, "stuSign"
            ),
            "position_rejected",
        )
        for body, endpoint in (
            ({"success": False, "msg": "距离异常，请稍后重试"}, "stuSign"),
            (
                {"success": False, "msg": "不在签到范围内", "error": "invalid_token"},
                "stuSign",
            ),
            ({"success": False, "msg": "不在签到范围内", "code": 401}, "stuSign"),
            ({"success": False, "msg": "不在签到范围内"}, "login"),
            ({"success": True, "msg": "不在签到范围内"}, "stuSign"),
        ):
            self.assertEqual(rejection_code(body, endpoint), "server_rejected")

    def test_real_api_signature_uses_final_candidate_and_server_confirmation(self):
        selected = choose_position(task(), CENTER, 20, FixedRandom())
        session = Session(
            [
                {"success": True, "data": {"signStatus": 3, "signDate": "2026-10-10"}},
                {"success": True},
                {"success": True, "data": {"signStatus": 0, "signDate": "2026-10-10"}},
            ]
        )
        client = SchoolClient("mock", session)
        client.token = "mock-token"
        with patch("api.time.time", return_value=1770000000.123):
            checked = client.sign(task(), selected["latitude"], selected["longitude"])
        method, url, kwargs = session.calls[1]
        self.assertEqual(checked["signStatus"], 0)
        self.assertEqual(kwargs["json"]["signLat"], selected["latitude"])
        self.assertEqual(kwargs["json"]["locationAccuracy"], selected["distance"])
        self.assertEqual(
            kwargs["headers"]["Flysource-Sign"],
            signature(url, "mock-token", 1770000000123),
        )


if __name__ == "__main__":
    unittest.main()
