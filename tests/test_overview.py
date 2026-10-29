import copy
import sys
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from overview import today_metrics, recent_batches, record_batch
from storage import Store, defaults, new_account, SHANGHAI
from readiness import check_ready
from execution import make_request
from data_management import create_backup, read_backup

NOW = datetime(2026, 10, 10, 12, tzinfo=SHANGHAI)


class OverviewTests(unittest.TestCase):
    def setUp(self):
        self.data = defaults()
        self.a = new_account("mock-a", "同名")
        self.b = new_account("mock-b", "同名")
        self.data["accounts"] = [self.a, self.b]
        self.network = patch(
            "requests.Session.request", side_effect=AssertionError("No real network")
        )
        self.network.start()
        self.addCleanup(self.network.stop)

    def result(
        self,
        account,
        status="success",
        task="task-a",
        batch="batch-a",
        at=NOW,
        day=None,
    ):
        return {
            "account_id": account["id"],
            "label": account["label"],
            "task_id": task,
            "task": task,
            "status": status,
            "date": day or at.date().isoformat(),
            "finished_at": at.isoformat(),
            "run_id": batch,
            "batch_id": batch,
            "message": "模拟",
        }

    def test_no_record_is_review_not_claim_of_missing_school_signin(self):
        values = today_metrics(self.data, "2026-10-10")
        self.assertEqual(values["success"], 0)
        self.assertEqual(values["review"], 2)
        self.assertEqual(values["failure"], 0)
        self.assertFalse(values["all_tasks_confirmed"])

    def test_multiple_successes_and_same_name_accounts_deduplicate_by_stable_id(self):
        self.data["results"] = [
            self.result(self.a),
            self.result(self.a, task="task-b"),
            self.result(self.b),
        ]
        values = today_metrics(self.data, "2026-10-10")
        self.assertEqual(values["success"], 2)
        self.assertEqual(values["review"], 0)
        self.assertFalse(values["all_tasks_confirmed"])

    def test_disabled_temporary_success_counts_without_changing_enabled_count(self):
        self.b["enabled"] = False
        self.data["results"] = [self.result(self.b)]
        values = today_metrics(self.data, "2026-10-10")
        self.assertEqual(values["enabled"], 1)
        self.assertEqual(values["success"], 1)
        self.assertEqual(values["review"], 1)

    def test_later_failure_after_success_keeps_success_record_and_needs_review(self):
        self.data["results"] = [
            self.result(self.a, "failure", at=NOW + timedelta(minutes=1)),
            self.result(self.a),
        ]
        values = today_metrics(self.data, "2026-10-10")
        self.assertEqual(values["success"], 1)
        self.assertEqual(values["failure"], 1)
        self.assertEqual(values["review"], 2)

    def test_later_success_resolves_same_task_failure_but_not_other_task_failure(self):
        self.data["results"] = [
            self.result(self.a, at=NOW + timedelta(minutes=1)),
            self.result(self.a, "failure"),
            self.result(self.a, "failure", task="task-b"),
        ]
        values = today_metrics(self.data, "2026-10-10")
        self.assertEqual(values["failure"], 1)
        self.assertEqual([r["task_id"] for r in values["failures"]], ["task-b"])

    def test_server_date_defines_today_instead_of_later_execution_timestamp(self):
        self.data["results"] = [
            self.result(self.a, at=NOW + timedelta(days=1), day="2026-10-10")
        ]
        self.assertEqual(today_metrics(self.data, "2026-10-10")["success"], 1)
        self.assertEqual(today_metrics(self.data, "2026-10-11")["success"], 0)

    def test_retained_success_indexes_count_but_do_not_prove_all_tasks(self):
        self.data["success_dates"][self.a["id"]] = "2026-10-10"
        self.data["task_success_dates"][self.b["id"]] = {
            "task-a": "2026-10-10",
            "task-b": "2026-10-09",
        }
        self.assertEqual(today_metrics(self.data, "2026-10-10")["success"], 2)
        self.assertFalse(today_metrics(self.data, "2026-10-10")["all_tasks_confirmed"])

    def test_deleted_account_is_excluded_from_current_account_statistics(self):
        self.data["results"] = [self.result(self.b)]
        self.data["accounts"].remove(self.b)
        self.assertEqual(today_metrics(self.data, "2026-10-10")["success"], 0)

    def test_cancelled_skipped_are_not_server_success_or_failure(self):
        self.data["results"] = [
            self.result(self.a, "cancelled"),
            self.result(self.b, "skipped"),
        ]
        values = today_metrics(self.data, "2026-10-10")
        self.assertEqual(
            (values["success"], values["failure"], values["review"]), (0, 0, 2)
        )

    def test_saved_batch_keeps_mode_count_and_summary_after_result_retention(self):
        request = make_request(self.data, moment=NOW)
        results = [
            self.result(self.a, batch=request["run_id"]),
            self.result(self.b, "failure", batch=request["run_id"]),
        ]
        record_batch(self.data, request, results, NOW)
        self.data["results"] = [results[0]]
        batch = recent_batches(self.data)[0]
        self.assertEqual(batch["account_count"], 2)
        self.assertEqual(batch["summary"]["failure"], 1)
        self.assertEqual(batch["mode"], request["scope_label"])
        self.assertEqual(len(batch["results"]), 1)

    def test_old_results_group_only_explicit_batch_ids_and_do_not_guess_mode(self):
        legacy = self.result(self.a)
        legacy.pop("run_id")
        legacy.pop("batch_id")
        self.data["results"] = [self.result(self.a), self.result(self.b), legacy]
        batches = recent_batches(self.data)
        self.assertEqual(len(batches), 2)
        explicit = next(v for v in batches if v["batch_id"])
        self.assertEqual(len(explicit["results"]), 2)
        self.assertIn("未记录", explicit["mode"])
        self.assertIsNone(next(v for v in batches if not v["batch_id"])["batch_id"])

    def test_batch_checkpoint_is_idempotent_bounded_and_restart_persistent(self):
        with tempfile.TemporaryDirectory() as folder:
            store = Store(folder)
            store.data = self.data
            for index in range(25):
                request = {
                    "run_id": f"batch-{index}",
                    "scope_label": "单账号临时执行",
                    "count": 1,
                }
                record_batch(
                    store.data,
                    request,
                    [self.result(self.a, batch=request["run_id"])],
                    NOW + timedelta(minutes=index),
                )
                record_batch(
                    store.data,
                    request,
                    [self.result(self.a, batch=request["run_id"])],
                    NOW + timedelta(minutes=index),
                )
            store.save()
            self.assertEqual(len(Store(folder).data["overview_batches"]), 20)
            self.assertEqual(
                recent_batches(Store(folder).data)[0]["batch_id"], "batch-24"
            )

    def test_closed_plan_is_normal_and_only_real_config_errors_are_issues(self):
        self.assertFalse(self.data["schedule"]["enabled"])
        self.assertEqual(check_ready(self.data, "unused", probe=False), [])
        self.a["openid"] = ""
        self.assertIn(
            "accounts",
            {page for page, _ in check_ready(self.data, "unused", probe=False)},
        )

    def test_new_batch_metadata_survives_encrypted_backup(self):
        request = make_request(self.data, moment=NOW)
        record_batch(
            self.data, request, [self.result(self.a, batch=request["run_id"])], NOW
        )
        with tempfile.TemporaryDirectory() as folder:
            store = Store(Path(folder) / "data")
            store.data = self.data
            store.save()
            target = Path(folder) / "mock.pingan"
            create_backup(store.data, store.directory, target, "mock-password")
            saved = read_backup(target, "mock-password")["data"]
            self.assertEqual(saved["overview_batches"], self.data["overview_batches"])


if __name__ == "__main__":
    unittest.main()
