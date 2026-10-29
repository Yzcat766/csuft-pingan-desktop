import copy
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from storage import Store, defaults, new_account, now_shanghai
from history_storage import JOURNAL, delete_records, ensure_record_ids
from execution import merge_results, make_request
from overview import record_batch, today_metrics, recent_batches
from data_management import create_backup, read_backup, restore_backup, copy_local, set_mode, selected_directory
from position_offset import checkpoint_position


def result(number=0, status="success", batch="batch-a", account="account-a"):
    return {"account_id": account, "label": "模拟账号", "task_id": f"task-{number}",
            "task": "模拟任务", "date": "2026-10-10", "finished_at": f"2026-10-10T12:{number % 60:02d}:00+08:00",
            "run_id": batch, "batch_id": batch, "status": status, "message": "模拟结果"}


def seed_position(data):
    checkpoint_position(data, {
        "account_id": "account-a", "task_id": "task-0",
        "date": now_shanghai().date().isoformat(), "unresolved": True,
        "adjustments": 0, "submission_count": 2, "max_submissions": 4,
        "offset_rule": {"mode": "off", "radius": 0},
        "location": {"mode": "task"},
        "position": {"latitude": 28.1, "longitude": 112.9,
                     "center_latitude": 28.1, "center_longitude": 112.9, "radius": 0},
    })


class HistoryStorageTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.root = Path(self.folder.name)
        self.store = Store(self.root / "data")

    def tearDown(self):
        self.folder.cleanup()

    def seed(self):
        s = self.store
        s.data["accounts"] = [new_account("mock-history-private", "模拟账号")]
        s.data["accounts"][0]["id"] = "account-a"
        s.data["schedule"]["enabled"] = True
        merge_results(s.data, [result(0), result(1, "failure", account="account-b")])
        s.data["automation"] = {"state": "waiting_retry", "next_retry": "2026-10-10T12:05:00+08:00", "retry_count": 1}
        seed_position(s.data)
        record_batch(s.data, {"run_id": "batch-a", "scope_label": "模拟批次", "count": 2}, s.data["results"], now_shanghai())
        s.save()
        return s

    def legacy(self):
        data = defaults()
        data["accounts"] = [new_account("mock-history-legacy-private")]
        data["results"] = [result(), result(1, "failure", "batch-b")]
        self.store.directory.mkdir()
        self.store.path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        self.store = Store(self.store.directory)
        return data

    def bytes(self):
        return {p.name: p.read_bytes() for p in self.store.directory.iterdir() if p.is_file() and p.name != "migration-rollback.dpapi"}

    def test_legacy_read_then_first_save_and_repeated_migration(self):
        old = self.legacy()
        original = self.store.path.read_bytes()
        self.assertFalse(self.store.history_path.exists())
        self.assertEqual(self.store.path.read_bytes(), original)
        self.store.save()
        config = json.loads(self.store.path.read_text(encoding="utf-8"))
        self.assertNotIn("results", config)
        self.assertNotIn("overview_batches", config)
        self.assertEqual(config["history_store"]["version"], 1)
        self.assertEqual(len(Store(self.store.directory).data["results"]), 2)
        protected = self.store.directory / "migration-rollback.dpapi"
        envelope = json.loads(protected.read_text(encoding="utf-8"))
        self.assertEqual(json.loads(self.store.protector.unprotect(envelope["value"])), old)
        for p in self.store.directory.iterdir():
            self.assertNotIn(b"mock-history-legacy-private", p.read_bytes())
        before = self.bytes()
        self.store = Store(self.store.directory)
        self.store.save()
        self.assertEqual(before, self.bytes())

    def test_mixed_files_use_authoritative_history_without_merging_old_rows(self):
        s = self.seed()
        s.delete_history([r["record_id"] for r in s.data["results"]])
        config = json.loads(s.path.read_text(encoding="utf-8"))
        config.pop("history_store")
        config["results"] = [result(50)]
        config["overview_batches"] = []
        s.path.write_text(json.dumps(config), encoding="utf-8")
        loaded = Store(s.directory)
        self.assertEqual(loaded.data["results"], [])
        self.assertIn("不会自动合并或复活", loaded.history_notice)
        loaded.save()
        self.assertNotIn("results", json.loads(loaded.path.read_text(encoding="utf-8")))
        self.assertEqual(Store(s.directory).data["results"], [])

    def test_migration_history_replace_failure_keeps_old_then_can_retry(self):
        self.legacy()
        before = self.bytes()
        original_replace = os.replace
        fired = []

        def fail_once(source, destination):
            if Path(destination).name == "history.json" and not fired:
                fired.append(True)
                raise OSError("INJECTED history disk replacement failure")
            return original_replace(source, destination)

        with patch("history_storage.os.replace", side_effect=fail_once):
            with self.assertRaisesRegex(OSError, "INJECTED") as error:
                self.store.save()
        print(f"FAULT migration: {error.exception}; original config retained; restart reads 2 records")
        self.assertEqual(before, self.bytes())
        self.assertEqual(len(Store(self.store.directory).data["results"]), 2)
        self.store.save()
        self.assertEqual(len(Store(self.store.directory).data["results"]), 2)
        print("RECOVERY migration: retry saved separate history.json; PASS")

    def test_second_file_failure_undoes_history_and_business_state(self):
        s = self.seed()
        before = self.bytes()
        snapshot = s.snapshot()
        merge_results(s.data, [result(2, batch="batch-new")])
        s.data["schedule"]["time"] = "22:00"
        original_replace = os.replace
        fired = []

        def fail_once(source, destination):
            if Path(destination).name == "config.json" and not fired:
                fired.append(True)
                raise OSError("INJECTED second-file replacement failure")
            return original_replace(source, destination)

        with patch("history_storage.os.replace", side_effect=fail_once):
            with self.assertRaisesRegex(OSError, "INJECTED"):
                s.save()
        self.assertEqual(before, self.bytes())
        self.assertEqual(Store(s.directory).snapshot(), snapshot)
        s.save()
        self.assertEqual(Store(s.directory).data["schedule"]["time"], "22:00")
        self.assertEqual(len(Store(s.directory).data["results"]), 3)

    def test_crash_between_files_is_undone_on_restart(self):
        s = self.seed()
        snapshot, before = s.snapshot(), self.bytes()
        merge_results(s.data, [result(3, batch="crash-batch")])
        s.data["schedule"]["time"] = "22:00"
        original_replace = os.replace

        class SimulatedCrash(BaseException):
            pass

        def crash(source, destination):
            if Path(destination).name == "config.json":
                raise SimulatedCrash()
            return original_replace(source, destination)

        with patch("history_storage.os.replace", side_effect=crash):
            with self.assertRaises(SimulatedCrash):
                s.save()
        journal = s.directory / JOURNAL
        self.assertTrue(journal.exists())
        self.assertNotIn(b"mock-history-private", journal.read_bytes())
        recovered = Store(s.directory)
        self.assertTrue(recovered.recovered_transaction)
        self.assertEqual(recovered.snapshot(), snapshot)
        self.assertEqual(before, self.bytes())
        recovered.data["appearance"]["theme"] = "light"
        recovered.save()
        self.assertEqual(Store(s.directory).data["appearance"]["theme"], "light")

    def test_commit_marker_removal_failure_rolls_back(self):
        s = self.seed()
        before = self.bytes()
        s.data["schedule"]["time"] = "22:00"
        original_unlink = Path.unlink
        fired = []

        def fail_once(path, *args, **kwargs):
            if path.name == JOURNAL and not fired:
                fired.append(True)
                raise OSError("INJECTED commit removal failure")
            return original_unlink(path, *args, **kwargs)

        with patch.object(Path, "unlink", new=fail_once):
            with self.assertRaisesRegex(OSError, "INJECTED"):
                s.save()
        self.assertEqual(before, self.bytes())
        s.save()
        self.assertEqual(Store(s.directory).data["schedule"]["time"], "22:00")

    def test_undo_write_failure_retains_transaction_and_recovers_after_repair(self):
        s = self.seed()
        original = s.snapshot()
        merge_results(s.data, [result(8, batch="pending-new")])
        s.data["schedule"]["time"] = "22:00"
        real_replace, writes = os.replace, []

        def fail_save_and_undo(source, destination):
            name = Path(destination).name
            if name == "history.json":
                writes.append(name)
                if len(writes) > 1:
                    raise OSError("INJECTED undo disk failure")
            if name == "config.json":
                raise OSError("INJECTED configuration replacement failure")
            return real_replace(source, destination)

        with patch("history_storage.os.replace", side_effect=fail_save_and_undo):
            with self.assertRaisesRegex(OSError, "事务恢复失败"):
                s.save()
            self.assertTrue((s.directory / JOURNAL).exists())
            with self.assertRaisesRegex(OSError, "事务恢复失败"):
                Store(s.directory)
        recovered = Store(s.directory)
        self.assertTrue(recovered.recovered_transaction)
        self.assertEqual(recovered.snapshot(), original)
        self.assertIn("未完成保存", recovered.history_notice)
        s.save()  # Still-pending memory can be committed once the disk is writable again.
        self.assertFalse((s.directory / JOURNAL).exists())
        self.assertEqual(Store(s.directory).data["schedule"]["time"], "22:00")
        self.assertEqual(len(Store(s.directory).data["results"]), 3)

    def test_corrupt_transaction_is_preserved_and_blocks_loading(self):
        s = self.seed()
        path = s.directory / JOURNAL
        path.write_bytes(b"broken journal")
        before = self.bytes()
        with self.assertRaisesRegex(OSError, "事务恢复失败"):
            Store(s.directory)
        self.assertEqual(before, self.bytes())

    def test_missing_migrated_history_refuses_old_backup_or_temporary(self):
        s = self.seed()
        s.history_path.unlink()
        (s.directory / "history.tmp").write_text(json.dumps({"results": [result()]}), encoding="utf-8")
        before = self.bytes()
        with self.assertRaisesRegex(ValueError, "history.json"):
            Store(s.directory)
        with self.assertRaisesRegex(ValueError, "history.json"):
            s.save()
        self.assertEqual(before, self.bytes())

    def test_corrupt_history_is_never_overwritten_or_treated_as_empty(self):
        s = self.seed()
        s.history_path.write_bytes(b"{broken history")
        before = self.bytes()
        with self.assertRaisesRegex(ValueError, "history.json"):
            Store(s.directory)
        with self.assertRaisesRegex(ValueError, "history.json"):
            s.delete_history([r["record_id"] for r in s.data["results"]])
        self.assertEqual(len(s.data["results"]), 2)
        self.assertEqual(before, self.bytes())

    def test_future_history_version_and_duplicate_ids_are_rejected(self):
        s = self.seed()
        original = json.loads(s.history_path.read_text(encoding="utf-8"))
        for kind in ("version", "duplicate"):
            value = copy.deepcopy(original)
            if kind == "version":
                value["version"] = 2
            else:
                value["results"][1]["record_id"] = value["results"][0]["record_id"]
            s.history_path.write_text(json.dumps(value), encoding="utf-8")
            with self.subTest(kind=kind), self.assertRaisesRegex(ValueError, "history.json"):
                Store(s.directory)

    def test_delete_preserves_accounts_plan_success_retry_and_position_state(self):
        s = self.seed()
        before = s.snapshot()
        config_bytes = s.path.read_bytes()
        ids = [r["record_id"] for r in s.data["results"]]
        self.assertEqual(s.delete_history(ids), 2)
        after = Store(s.directory).data
        for key in before:
            if key not in ("results", "overview_batches"):
                self.assertEqual(after[key], before[key], key)
        self.assertEqual(after["results"], [])
        self.assertEqual(after["overview_batches"], [])
        self.assertEqual(s.path.read_bytes(), config_bytes)
        self.assertEqual(today_metrics(after, "2026-10-10")["success"], 1)

    def test_partial_delete_recounts_batch_and_removes_empty_batches(self):
        s = self.seed()
        failure = next(r for r in s.data["results"] if r["status"] == "failure")
        s.delete_history([failure["record_id"]])
        batch = recent_batches(Store(s.directory).data)[0]
        self.assertEqual(batch["summary"], {"success": 1, "failure": 0, "cancelled": 0, "skipped": 0})
        self.assertEqual(batch["account_count"], 1)
        self.assertEqual(batch["completed_count"], 1)
        s.delete_history([s.data["results"][0]["record_id"]])
        self.assertEqual(recent_batches(Store(s.directory).data), [])

    def test_history_delete_does_not_expire_position_cache_at_midnight(self):
        from datetime import timedelta
        s = self.seed()
        cache = copy.deepcopy(s.data["position_cache"])
        config = s.path.read_bytes()
        with patch("storage.now_shanghai", return_value=now_shanghai() + timedelta(days=1)):
            s.delete_history([r["record_id"] for r in s.data["results"]])
        self.assertEqual(s.data["position_cache"], cache)
        self.assertEqual(s.path.read_bytes(), config)

    def test_history_only_and_settings_only_do_not_rewrite_other_file(self):
        s = self.seed()
        config_stat = s.path.stat().st_mtime_ns
        config = s.path.read_bytes()
        backup = (s.directory / "config.json.bak").read_bytes() if (s.directory / "config.json.bak").exists() else None
        s.delete_history([s.data["results"][0]["record_id"]])
        self.assertEqual(s.path.stat().st_mtime_ns, config_stat)
        self.assertEqual(s.path.read_bytes(), config)
        self.assertEqual((s.directory / "config.json.bak").read_bytes() if backup is not None else None, backup)
        history, stamp = s.history_path.read_bytes(), s.history_path.stat().st_mtime_ns
        s.data["appearance"]["theme"] = "light"
        s.save()
        self.assertEqual(s.history_path.read_bytes(), history)
        self.assertEqual(s.history_path.stat().st_mtime_ns, stamp)

    def test_stable_checkpoint_ids_and_identical_legacy_occurrences(self):
        s = self.seed()
        row = result(2, batch="checkpoint")
        merge_results(s.data, [row])
        s.save()
        identity = s.data["results"][0]["record_id"]
        row["message"] = "完成结果"
        merge_results(s.data, [row])
        s.save()
        self.assertEqual(s.data["results"][0]["record_id"], identity)
        self.assertEqual(sum(r.get("run_id") == "checkpoint" for r in s.data["results"]), 1)
        rows = [result(), result()]
        ensure_record_ids(rows)
        self.assertNotEqual(rows[0]["record_id"], rows[1]["record_id"])
        ids = [r["record_id"] for r in rows]
        ensure_record_ids(rows)
        self.assertEqual(ids, [r["record_id"] for r in rows])

    def test_password_backup_restore_contains_history_and_disables_automation(self):
        s = self.seed()
        history = copy.deepcopy(s.data["results"])
        target = self.root / "new.backup"
        create_backup(s.snapshot(), s.directory, target, "mock-history-password")
        payload = read_backup(target, "mock-history-password")
        self.assertEqual(payload["data"]["results"], history)
        s.delete_history([r["record_id"] for r in history])
        rollback = restore_backup(s, payload)
        self.assertEqual(Store(rollback).data["results"], [])
        self.assertEqual(s.data["results"], history)
        self.assertFalse(s.data["schedule"]["enabled"])
        self.assertIsNone(s.data["automation"])
        self.assertEqual(s.data["position_cache"], {})
        self.assertEqual(s.snapshot(), Store(s.directory).snapshot())
        s.data["appearance"]["theme"] = "light"
        s.save()
        self.assertEqual(Store(s.directory).data["results"], history)

    def test_old_unified_backup_restores_into_two_files(self):
        s = self.seed()
        data = defaults()
        data["accounts"] = [new_account("mock-old-backup")]
        data["results"] = [result(5)]
        target = self.root / "old.backup"
        create_backup(data, s.directory, target, "mock-history-password")
        restore_backup(s, read_backup(target, "mock-history-password"))
        self.assertEqual(len(Store(s.directory).data["results"]), 1)
        self.assertNotIn("results", json.loads(s.path.read_text(encoding="utf-8")))
        self.assertTrue(s.history_path.exists())

    def test_directory_copy_and_mode_switch_preserve_split_data(self):
        s = self.seed()
        destination = self.root / "portable" / "data"
        source = self.bytes()
        copy_local(s, destination)
        copied = Store(destination)
        self.assertEqual(copied.snapshot(), s.snapshot())
        self.assertEqual(source, self.bytes())
        set_mode(destination.parent, "portable")
        self.assertEqual(selected_directory(destination.parent), destination)
        copied.delete_history([r["record_id"] for r in copied.data["results"]])
        self.assertEqual(Store(destination).data["results"], [])
        self.assertEqual(len(Store(s.directory).data["results"]), 2)

    def test_directory_copy_rejects_unfinished_history_without_changes(self):
        s = self.seed()
        (s.directory / "history.json.history-tmp").write_text("incomplete", encoding="utf-8")
        before = self.bytes()
        target = self.root / "copy"
        with self.assertRaisesRegex(ValueError, "临时"):
            copy_local(s, target)
        self.assertFalse(target.exists())
        self.assertEqual(before, self.bytes())

    def test_retains_capacity_of_200_results_and_20_batches(self):
        s = self.seed()
        for number in range(230):
            rows = [result(number, batch=f"batch-{number}")]
            merge_results(s.data, rows)
            record_batch(s.data, {"run_id": f"batch-{number}", "scope_label": "模拟", "count": 1}, rows, now_shanghai())
        s.save()
        self.assertEqual(len(Store(s.directory).data["results"]), 200)
        self.assertEqual(len(Store(s.directory).data["overview_batches"]), 20)
