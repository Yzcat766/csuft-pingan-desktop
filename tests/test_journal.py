import copy
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from logbook import LogBook, PAGE_SIZE, event_text
from execution import make_request
from storage import defaults, new_account
from runner import run_accounts
from test_locations import MockClient


class JournalTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.data = defaults()
        self.data["accounts"] = [
            new_account("mock-private-a", "同名"),
            new_account("mock-private-b", "同名"),
        ]
        self.request = make_request(self.data)
        self.book = LogBook(
            self.folder.name, ["mock-private-a", "mock-private-b", "mock-secret-token"]
        )
        self.book.start(self.request)
        (
            MockClient.submitted,
            MockClient.sample,
            MockClient.on_login,
            MockClient.final_status,
        ) = ([], None, None, 0)

    def tearDown(self):
        self.folder.cleanup()

    def record(self):
        return next(
            r for r in self.book.list_batches()["batches"] if r["kind"] == "batch"
        )

    def run_offline(self):
        results = run_accounts(
            self.request["accounts"],
            lambda kind, value: (
                self.book.append(value) if kind == "log_event" else None
            ),
            client_factory=MockClient,
            delay_scale=0,
            location_config=self.request["config"],
            run_id=self.request["run_id"],
        )
        self.book.finish(results)
        return results

    def test_same_name_accounts_are_associated_by_id_not_text(self):
        with patch(
            "requests.Session.request",
            side_effect=AssertionError("Real network forbidden"),
        ):
            results = self.run_offline()
        record = self.record()
        all_events = self.book.query(record)["events"]
        self.assertEqual(record["account_count"], 2)
        self.assertEqual(record["summary"]["success"], 2)
        for account in self.data["accounts"]:
            events = self.book.query(record, {"account_id": account["id"]})["events"]
            self.assertTrue(events)
            self.assertTrue(all(e["account_id"] == account["id"] for e in events))
            self.assertEqual(
                {e["step"] for e in events},
                {
                    "定位配置",
                    "登录",
                    "任务查询",
                    "服务端查询",
                    "定位校验",
                    "提交",
                    "服务端复查",
                    "结果",
                },
            )
        self.assertEqual(len({e["seq"] for e in all_events}), len(all_events))
        self.assertEqual(
            [e["seq"] for e in all_events], sorted(e["seq"] for e in all_events)
        )
        self.assertEqual({r["batch_id"] for r in results}, {record["batch_id"]})
        self.assertEqual({r["run_id"] for r in results}, {record["batch_id"]})

    def test_rename_delete_and_restart_keep_historical_identity(self):
        self.run_offline()
        identity = self.data["accounts"][0]["id"]
        self.data["accounts"][0]["label"] = "改名"
        self.data["accounts"].clear()
        restored = LogBook(self.folder.name)
        record = restored.list_batches()["batches"][0]
        self.assertEqual(record["accounts"][0], {"id": identity, "label": "同名"})
        events = restored.query(record, {"account_id": identity})["events"]
        self.assertTrue(events)
        self.assertTrue(all(e["account_label"] == "同名" for e in events))

    def test_batch_system_account_date_level_keyword_filters(self):
        identity = self.data["accounts"][0]["id"]
        self.book.append(
            {"message": "通用摘要", "time": "2026-10-10T12:00:00+08:00", "step": "批次"}
        )
        self.book.append(
            {
                "message": "网络\n错误",
                "account_id": identity,
                "account_label": "同名",
                "time": "2026-10-10T12:01:00+08:00",
                "level": "error",
                "step": "登录",
            }
        )
        self.book.append(
            {
                "message": "其它日",
                "account_id": identity,
                "time": "2026-10-09T12:00:00+08:00",
            }
        )
        self.book.flush()
        record = self.record()
        self.assertEqual(self.book.query(record, {"account_id": ""})["total"], 1)
        self.assertEqual(
            self.book.query(
                record,
                {
                    "account_id": identity,
                    "date": "2026-10-10",
                    "level": "error",
                    "keyword": "网络",
                },
            )["total"],
            1,
        )
        self.assertEqual(self.book.query(record, {"keyword": "不存在"})["total"], 0)
        self.assertIn(
            "批次", event_text(self.book.query(record, {"account_id": ""})["events"][0])
        )

    def test_multiple_attempts_are_separate_batches(self):
        first = self.request["run_id"]
        self.run_offline()
        for scope in ("selected", "automatic", "retry"):
            request = make_request(self.data, scope, [self.data["accounts"][0]["id"]])
            self.book.start(request)
            self.book.append({"message": scope, "step": "批次"})
            self.book.finish([])
        records = self.book.list_batches()["batches"]
        self.assertEqual(len(records), 4)
        self.assertEqual(len({r["batch_id"] for r in records}), 4)
        self.assertIn(first, {r["batch_id"] for r in records})

    def test_paged_events_and_batches_have_bounded_load(self):
        for i in range(455):
            self.book.append({"message": str(i)})
        self.book.flush()
        record = self.record()
        tail = self.book.query(record)
        self.assertEqual(tail["total"], 455)
        self.assertEqual(len(tail["events"]), PAGE_SIZE)
        self.assertEqual(tail["events"][0]["seq"], 256)
        self.assertEqual(self.book.query(record, offset=0)["events"][0]["seq"], 1)
        self.assertEqual(self.book.query(record, offset=200)["events"][0]["seq"], 201)
        self.assertEqual(len(self.book.list_batches(limit=1)["batches"]), 1)

    def test_atomic_failure_retains_original_and_pending_events(self):
        self.book.append({"message": "原始"})
        self.book.flush()
        path = self.book.root / self.book.active["key"] / "000000.json"
        before = path.read_bytes()
        self.book.append({"message": "新增"})
        with patch("logbook.os.replace", side_effect=OSError("模拟只读")):
            with self.assertRaises(OSError):
                self.book.flush()
        self.assertEqual(path.read_bytes(), before)
        self.assertTrue(self.book.dirty)
        self.book.flush()
        self.assertEqual(self.book.query(self.record())["total"], 2)

    def test_empty_legacy_and_corruption_do_not_guess_or_modify_files(self):
        empty = LogBook(Path(self.folder.name) / "empty")
        self.assertEqual(empty.list_batches()["total"], 0)
        self.book.legacy_root.mkdir()
        legacy = self.book.legacy_root / "2026-10-10成功.log"
        original = "[同名] 登录成功\nopenid=mock-private-a\n"
        legacy.write_text(original, encoding="utf-8")
        record = next(
            r for r in self.book.list_batches()["batches"] if r["kind"] == "legacy"
        )
        loaded = self.book.query(record)
        self.assertTrue(
            all(
                e["account_id"] is None and e["batch_id"] is None and e["time"] == ""
                for e in loaded["events"]
            )
        )
        self.assertIn("不推测", loaded["warning"])
        self.assertEqual(
            self.book.query(record, {"account_id": self.data["accounts"][0]["id"]})[
                "total"
            ],
            0,
        )
        self.assertEqual(legacy.read_text(encoding="utf-8"), original)
        self.book.append({"message": "正常"})
        self.book.flush()
        path = self.book.root / self.book.active["key"] / "000000.json"
        path.write_text("损坏", encoding="utf-8")
        original_bytes = path.read_bytes()
        self.assertIn("损坏", self.book.query(self.record())["warning"])
        self.assertEqual(path.read_bytes(), original_bytes)
        meta = path.parent / "meta.json"
        meta.write_text("损坏", encoding="utf-8")
        records = self.book.list_batches()["batches"]
        damaged = next(r for r in records if r["kind"] == "broken")
        self.assertIn("保留", self.book.query(damaged)["warning"])
        self.assertEqual(meta.read_text(encoding="utf-8"), "损坏")

    def test_saved_and_exported_credentials_share_ui_redaction(self):
        message = 'mock-private-a mock-secret-token {"access_token":"secret-json"} OpenID：secret-id http://proxy-user:proxy-password@localhost:8000 Bearer secret-bearer'
        self.book.append(
            {
                "message": message,
                "account_id": self.data["accounts"][0]["id"],
                "level": "error",
            }
        )
        self.book.flush()
        record = self.record()
        displayed = self.book.query(record, {"level": "error"})["events"][0]["message"]
        export = Path(self.folder.name) / "导出.txt"
        self.assertEqual(self.book.export(record, {"level": "error"}, export), 1)
        exported = export.read_text(encoding="utf-8")
        stored = (self.book.root / self.book.active["key"] / "000000.json").read_text(
            encoding="utf-8"
        )
        for secret in (
            "mock-private-a",
            "mock-secret-token",
            "secret-json",
            "secret-id",
            "proxy-user",
            "proxy-password",
            "secret-bearer",
        ):
            self.assertNotIn(secret, displayed)
            self.assertNotIn(secret, exported)
            self.assertNotIn(secret, stored)
        self.assertIn(displayed, exported)
        with self.assertRaises(ValueError):
            self.book.export(record, {}, self.book.root / "覆盖.txt")

    def test_running_batch_read_after_checkpoint_has_truthful_unfinished_state(self):
        self.book.append({"message": "尚在运行"})
        self.book.flush()
        restored = LogBook(self.folder.name)
        record = restored.list_batches()["batches"][0]
        self.assertEqual(record["status"], "running")
        self.assertEqual(record["summary"], {})
        self.assertEqual(restored.query(record)["total"], 1)


if __name__ == "__main__":
    unittest.main()
