"""Offline regression tests: all accounts, endpoints and OS mutations are simulated."""

import base64
import copy
import hashlib
import json
import os
import sys
import tempfile
import threading
import unittest
from datetime import timedelta
from pathlib import Path
from unittest.mock import patch, Mock, MagicMock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from storage import Store, defaults, new_account, now_shanghai, import_accounts
from security import CredentialError, DPAPI, encode_config, decode_config
from data_management import (
    create_backup,
    read_backup,
    restore_backup,
    copy_local,
    diagnostic_export,
    password_cipher,
    safe_relative,
    selected_directory,
    set_mode,
)
from windows_integration import SleepGuard, Autostart, launch_command
from notifications import batch_notice
from maintenance import log_plan, clear_logs
from logbook import LogBook
from task_selection import select_task, check_accounts
from readiness import check_ready, check_network
from execution import make_request, merge_results
from scheduling import (
    begin_attempt,
    finish_attempt,
    due_action,
    pending_ids,
    fingerprint,
)
from runner import run_accounts
from api import ApiError
from desktop_common import DemoClient


class FakeProtector:
    # Test-only stand-in, intentionally not used by production.
    def protect(self, text):
        return base64.b64encode(text.encode()).decode()

    def unprotect(self, text):
        return base64.b64decode(text).decode()


class MultiClient(DemoClient):
    posted = []
    signed = set()
    task_ids = ["old", "current"]
    days = {}
    windows = {}
    on_status = None

    def __init__(self, openid):
        self.openid = openid

    def list_tasks(self):
        return [{"taskId": t} for t in self.task_ids]

    def get_task(self, identity):
        value = super().get_task(identity)
        value.update(taskId=identity, taskName=identity)
        value.update(self.windows.get(identity, {}))
        return value

    def get_status(self, identity):
        if self.on_status:
            self.on_status(identity)
        return {
            "signDate": self.days.get(identity, now_shanghai().date().isoformat()),
            "signStatus": 0 if (self.openid, identity) in type(self).signed else 3,
        }

    def sign(self, task, lat, lng):
        self.submission_guard(self.get_status(task["taskId"]))
        type(self).posted.append((self.openid, task["taskId"]))
        type(self).signed.add((self.openid, task["taskId"]))
        return self.get_status(task["taskId"])


class SecurityTests(unittest.TestCase):
    def test_dpapi_round_trip_and_wrong_cipher_error_without_secret(self):
        protector = DPAPI()
        protected = protector.protect("mock-private-credential")
        self.assertNotIn("mock-private", protected)
        self.assertEqual(protector.unprotect(protected), "mock-private-credential")
        with self.assertRaises(CredentialError) as error:
            protector.unprotect("invalid-mock-secret")
        self.assertNotIn("invalid-mock-secret", str(error.exception))

    def test_plaintext_migration_retains_encrypted_exact_schema_rollback(self):
        with tempfile.TemporaryDirectory() as folder:
            raw = {
                "version": 1,
                "accounts": [
                    {"id": "legacy", "label": "模拟", "openid": "mock-private"}
                ],
            }
            path = Path(folder) / "config.json"
            path.write_text(json.dumps(raw), encoding="utf-8")
            before = path.read_bytes()
            store = Store(folder)
            self.assertTrue(store.legacy_plaintext)
            self.assertEqual(before, path.read_bytes())
            store.save()
            for output in Path(folder).glob("*"):
                self.assertNotIn(b"mock-private", output.read_bytes())
            envelope = json.loads(
                (Path(folder) / "migration-rollback.dpapi").read_text()
            )
            self.assertEqual(
                json.loads(store.protector.unprotect(envelope["value"])), raw
            )
            self.assertEqual(
                Store(folder).data["accounts"][0]["openid"], "mock-private"
            )
            self.assertFalse(store.legacy_plaintext)

    def test_encryption_failure_preserves_existing_config_and_leaves_no_temp(self):
        with tempfile.TemporaryDirectory() as folder:
            store = Store(folder)
            store.data["accounts"] = [new_account("mock-a")]
            store.save()
            before = store.path.read_bytes()
            store.protector = Mock()
            store.protector.protect.side_effect = CredentialError("模拟加密失败")
            with self.assertRaises(CredentialError):
                store.save()
            self.assertEqual(before, store.path.read_bytes())
            self.assertFalse(store.path.with_suffix(".tmp").exists())

    def test_cross_user_decryption_failure_retains_cipher_and_disables_account(self):
        with tempfile.TemporaryDirectory() as folder:
            store = Store(folder)
            store.data["accounts"] = [new_account("mock-a")]
            store.save()
            encrypted = json.loads(store.path.read_text())["accounts"][0][
                "openid_protected"
            ]
            other = Mock()
            other.unprotect.side_effect = CredentialError("模拟其它用户，重新输入")
            loaded = Store(folder, other)
            account = loaded.data["accounts"][0]
            self.assertFalse(account["enabled"])
            self.assertEqual(account["openid"], "")
            self.assertEqual(account["openid_protected"], encrypted)
            loaded.save()
            self.assertEqual(
                json.loads(loaded.path.read_text())["accounts"][0]["openid_protected"],
                encrypted,
            )

    def test_unknown_protection_scheme_is_not_silently_lost(self):
        data = defaults()
        data["accounts"] = [
            {
                "id": "locked",
                "openid_protected": {"scheme": "future", "value": "abc"},
                "enabled": True,
            }
        ]
        account = decode_config(data)["accounts"][0]
        self.assertIn("不兼容", account["credential_error"])
        self.assertEqual(
            encode_config({**data, "accounts": [account]})["accounts"][0][
                "openid_protected"
            ]["value"],
            "abc",
        )


class BackupTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.root = Path(self.folder.name)
        self.store = Store(self.root / "data")
        self.store.data["accounts"] = [new_account("mock-backup-private", "模拟账号")]
        self.store.data["schedule"]["enabled"] = True
        self.store.data["reliability"]["autostart"] = True
        self.store.save()
        self.backup = self.root / "backup.pingan-backup"

    def tearDown(self):
        self.folder.cleanup()

    def create(self, logs=False):
        create_backup(
            self.store.snapshot(),
            self.store.directory,
            self.backup,
            "mock-password-123",
            logs,
        )
        return read_backup(self.backup, "mock-password-123")

    def malicious(self, edit):
        payload = self.create()
        edit(payload)
        envelope = json.loads(self.backup.read_text())
        salt = base64.b64decode(envelope["salt"])
        envelope["token"] = (
            password_cipher("mock-password-123", salt)
            .encrypt(json.dumps(payload).encode())
            .decode()
        )
        self.backup.write_text(json.dumps(envelope))

    def test_encrypted_backup_contains_complete_data_without_plaintext(self):
        payload = self.create()
        self.assertNotIn("mock-backup-private", self.backup.read_text())
        self.assertNotIn("mock-password-123", self.backup.read_text())
        self.assertEqual(
            payload["data"]["accounts"][0]["openid"], "mock-backup-private"
        )
        self.assertEqual(payload["version"], 1)
        self.assertTrue(payload["created_at"])
        self.assertEqual(payload["data"]["locations"], self.store.data["locations"])

    def test_restore_preserves_existing_data_and_disables_automatic_controls(self):
        payload = self.create()
        self.store.data["accounts"] = [new_account("mock-before-restore")]
        self.store.save()
        rollback = restore_backup(self.store, payload)
        self.assertEqual(
            Store(rollback).data["accounts"][0]["openid"], "mock-before-restore"
        )
        self.assertEqual(
            self.store.data["accounts"][0]["openid"], "mock-backup-private"
        )
        self.assertFalse(self.store.data["schedule"]["enabled"])
        self.assertFalse(self.store.data["reliability"]["autostart"])
        self.assertIsNone(self.store.data["automation"])
        for output in rollback.glob("*"):
            self.assertNotIn(b"mock-before-restore", output.read_bytes())

    def test_logs_optional_and_integrity_verified(self):
        log = self.store.directory / "总日志" / "demo.log"
        log.parent.mkdir()
        log.write_text("模拟日志", encoding="utf-8")
        payload = self.create(True)
        self.assertIn("总日志/demo.log", payload["logs"])
        log.write_text("changed")
        restore_backup(self.store, payload)
        self.assertEqual(log.read_text(encoding="utf-8"), "模拟日志")
        self.malicious(
            lambda value: value["logs"].update(
                {
                    "总日志/demo.log": {
                        "content": base64.b64encode(b"mock").decode(),
                        "sha256": "bad",
                    }
                }
            )
        )
        self.backup.write_text(
            self.backup.read_text().replace(
                '"logs_included": false', '"logs_included": true'
            )
        )
        with self.assertRaises(ValueError):
            read_backup(self.backup, "mock-password-123")

    def test_wrong_password_and_tampering_leave_current_data_unchanged(self):
        self.create()
        before = self.store.path.read_bytes()
        with self.assertRaisesRegex(ValueError, "密码错误"):
            read_backup(self.backup, "wrong-password")
        envelope = json.loads(self.backup.read_text())
        token = envelope["token"]
        envelope["token"] = token[:40] + ("a" if token[40] != "a" else "b") + token[41:]
        self.backup.write_text(json.dumps(envelope))
        with self.assertRaises(ValueError):
            read_backup(self.backup, "mock-password-123")
        self.assertEqual(before, self.store.path.read_bytes())

    def test_path_traversal_rejected_even_with_authenticated_payload(self):
        self.malicious(
            lambda value: value.update(
                logs_included=True,
                logs={
                    "运行日志/../../escape.log": {
                        "content": "",
                        "sha256": hashlib.sha256(b"").hexdigest(),
                    }
                },
            )
        )
        with self.assertRaisesRegex(ValueError, "路径"):
            read_backup(self.backup, "mock-password-123")
        for path in (
            "../config.json",
            "总日志/../config.json",
            "总日志\\x.log",
            "C:/x.log",
            "/总日志/a.log",
            "总日志/x.exe",
        ):
            with self.subTest(path=path), self.assertRaises(ValueError):
                safe_relative(path)

    def test_future_version_and_malformed_backup_rejected(self):
        self.malicious(lambda value: value["data"].update(version=99))
        with self.assertRaisesRegex(ValueError, "版本"):
            read_backup(self.backup, "mock-password-123")

    def test_authenticated_invalid_preferences_rejected_before_restore(self):
        before = self.store.path.read_bytes()
        for section, bad in [
            ("appearance", {"theme": "invalid"}),
            ("notifications", {"enabled": "yes"}),
            ("maintenance", {"retention_days": -1}),
        ]:
            self.malicious(lambda value: value["data"].update({section: bad}))
            with self.assertRaises(ValueError):
                read_backup(self.backup, "mock-password-123")
        self.assertEqual(before, self.store.path.read_bytes())
        self.backup.write_text("{bad")
        with self.assertRaises(ValueError):
            read_backup(self.backup, "mock-password-123")

    def test_failed_commit_restores_original_data(self):
        payload = self.create()
        payload["data"]["accounts"][0]["label"] = "新标签"
        before = self.store.path.read_bytes()
        calls = []

        def replace(source, target):
            calls.append((source, target))
            if len(calls) == 2:
                raise OSError("模拟第二次替换失败")
            os.replace(source, target)

        with self.assertRaises(OSError):
            restore_backup(self.store, payload, replace=replace)
        self.assertEqual(before, self.store.path.read_bytes())
        self.assertEqual(
            Store(self.store.directory).data["accounts"][0]["label"], "模拟账号"
        )

    def test_directory_copy_retains_source_refuses_overwrite_and_nested_paths(self):
        before = self.store.path.read_bytes()
        target = self.root / "新数据 空格"
        copy_local(self.store, target)
        self.assertEqual(before, self.store.path.read_bytes())
        self.assertEqual(Store(target).data["accounts"], self.store.data["accounts"])
        for destination in (
            target,
            self.store.directory,
            self.store.directory / "nested",
            self.root,
        ):
            with self.assertRaises(ValueError):
                copy_local(self.store, destination)

    def test_migration_copy_failure_does_not_change_source_or_target(self):
        before = self.store.path.read_bytes()
        target = self.root / "migration-failed"
        with patch("data_management.shutil.copytree", side_effect=OSError("mock")):
            with self.assertRaises(OSError):
                copy_local(self.store, target)
        self.assertFalse(target.exists())
        self.assertEqual(before, self.store.path.read_bytes())

    def test_mode_selection_does_not_migrate_or_read_external_data(self):
        app = self.root / "exe"
        app.mkdir()
        self.assertEqual(selected_directory(app), app / "data")
        with patch(
            "windows_integration.local_directory", return_value=self.root / "local"
        ):
            set_mode(app, "local")
            self.assertEqual(selected_directory(app), self.root / "local")
            self.assertFalse((self.root / "local").exists())
        set_mode(app, "portable")
        self.assertEqual(selected_directory(app), app / "data")

    def test_diagnostic_and_exports_never_overwrite_data_or_include_credentials(self):
        target = self.root / "diagnostic.json"
        diagnostic_export(self.store.snapshot(), target, self.store.directory)
        value = target.read_text()
        for secret in ("mock-backup-private", "模拟账号", "openid_protected", "openid"):
            self.assertNotIn(secret, value)
        with self.assertRaises(ValueError):
            create_backup(
                self.store.snapshot(),
                self.store.directory,
                self.store.path,
                "mock-password-123",
            )
        with self.assertRaises(ValueError):
            diagnostic_export(
                self.store.snapshot(), self.store.path, self.store.directory
            )

    def test_locked_dpapi_account_cannot_be_exported_as_portable_backup(self):
        self.store.data["accounts"][0].update(
            openid="",
            openid_protected={"scheme": "dpapi-user-v1", "value": "unreadable"},
        )
        with self.assertRaisesRegex(ValueError, "DPAPI"):
            self.create()


class TaskAndAutomationTests(unittest.TestCase):
    def setUp(self):
        MultiClient.posted, MultiClient.signed = [], set()
        MultiClient.task_ids = ["old", "current"]
        MultiClient.days = {
            "old": (now_shanghai().date() - timedelta(days=1)).isoformat()
        }
        MultiClient.windows = {}
        MultiClient.on_status = None
        self.data = defaults()
        self.data["accounts"] = [new_account("mock-a"), new_account("mock-b")]
        self.now = now_shanghai()

    def run_batch(self, **kwargs):
        return run_accounts(
            self.data["accounts"],
            lambda *args: None,
            client_factory=MultiClient,
            delay_scale=0,
            location_config=self.data,
            **kwargs
        )

    def test_server_date_selects_current_task_regardless_of_list_order(self):
        results = self.run_batch(scheduled=True)
        self.assertEqual([r["task_id"] for r in results], ["current", "current"])
        self.assertEqual(
            MultiClient.posted, [("mock-a", "current"), ("mock-b", "current")]
        )

    def test_ambiguous_automatic_task_fails_without_submit_or_dialog(self):
        MultiClient.days = {}
        chooser = Mock(side_effect=AssertionError("automatic must not wait"))
        results = self.run_batch(scheduled=True, task_chooser=chooser)
        self.assertTrue(all(r["error_code"] == "task_ambiguous" for r in results))
        self.assertEqual(MultiClient.posted, [])
        chooser.assert_not_called()

    def test_manual_selection_and_cancel_work_per_account(self):
        MultiClient.days = {}

        def choose(account, candidates):
            self.assertEqual(len(candidates), 2)
            return "old" if account["id"] == self.data["accounts"][0]["id"] else None

        results = self.run_batch(task_chooser=choose)
        self.assertEqual([r["status"] for r in results], ["success", "cancelled"])
        self.assertEqual(MultiClient.posted, [("mock-a", "old")])

    def test_exact_name_strategy_persists_and_still_requires_unique_match(self):
        MultiClient.days = {}
        self.data["schedule"]["task_rule"] = {"mode": "exact_name", "name": "current"}
        results = self.run_batch(scheduled=True)
        self.assertTrue(all(r["task_id"] == "current" for r in results))
        self.assertNotEqual(
            fingerprint(self.data["schedule"]), fingerprint(defaults()["schedule"])
        )

    def test_one_task_success_does_not_skip_another_same_day_task(self):
        MultiClient.task_ids = ["current"]
        first = self.run_batch(scheduled=True)
        merge_results(self.data, first)
        MultiClient.task_ids = ["second"]
        second = self.run_batch(
            scheduled=True, success_dates=self.data["success_dates"]
        )
        merge_results(self.data, second)
        self.assertTrue(all(r["status"] == "success" for r in second))
        self.assertEqual(len(MultiClient.posted), 4)
        for account in self.data["accounts"]:
            self.assertEqual(
                set(self.data["task_success_dates"][account["id"]]),
                {"current", "second"},
            )

    def test_requery_already_signed_avoids_duplicate_submission(self):
        self.run_batch(scheduled=True)
        second = self.run_batch(scheduled=True)
        self.assertTrue(all(r.get("success_kind") == "already_signed" for r in second))
        self.assertEqual(len(MultiClient.posted), 2)

    def test_read_only_account_check_never_signs_and_can_cancel(self):
        cancel = threading.Event()
        results = check_accounts(
            self.data["accounts"],
            self.data,
            lambda _: cancel.set(),
            cancel,
            MultiClient,
        )
        self.assertEqual(len(results), 1)
        self.assertTrue(results[0]["login_ok"])
        self.assertEqual(len(results[0]["tasks"]), 1)
        self.assertEqual(MultiClient.posted, [])

    def test_automatic_excludes_closed_windows_and_future_dates(self):
        MultiClient.windows["current"] = {
            "signStartTime": "00:00:00",
            "signEndTime": "00:00:01",
        }
        chosen = select_task(
            MultiClient("mock-a"), self.now.replace(hour=12), automatic=True
        )
        self.assertIsNone(chosen)
        MultiClient.windows = {}
        MultiClient.days["current"] = (self.now.date() + timedelta(days=1)).isoformat()
        self.assertIsNone(select_task(MultiClient("mock-a"), self.now, automatic=True))

    def test_missing_server_date_is_manual_attention_not_guess(self):
        MultiClient.days["current"] = ""
        results = self.run_batch(scheduled=True)
        self.assertTrue(all(r["error_code"] == "task_date_unknown" for r in results))
        self.assertEqual(MultiClient.posted, [])

    def test_trigger_state_deduplicates_and_retries_only_original_task(self):
        self.data["schedule"].update(enabled=True, time="00:00")
        request = make_request(self.data, "automatic", moment=self.now)
        begin_attempt(self.data, request, self.now)
        result = {
            "account_id": self.data["accounts"][0]["id"],
            "status": "failure",
            "retryable": True,
            "task_id": "current",
            "task_window": {"start": "00:00:00", "end": "23:59:59"},
        }
        finish_attempt(
            self.data, request, [result], self.now.replace(hour=12, minute=0)
        )
        self.assertIsNone(due_action(self.data, self.now.replace(hour=12, minute=1)))
        self.assertEqual(
            due_action(self.data, self.now.replace(hour=12, minute=5)), "retry"
        )
        context = {
            "date": self.now.date().isoformat(),
            "task_ids": {a["id"]: "missing" for a in self.data["accounts"]},
        }
        self.assertTrue(
            all(
                r["error_code"] == "retry_task_changed"
                for r in self.run_batch(scheduled=True, retry_context=context)
            )
        )
        self.assertEqual(MultiClient.posted, [])

    def test_account_specific_task_lists_use_each_account(self):
        class Different(MultiClient):
            def list_tasks(self):
                return [
                    {"taskId": "task-alpha" if self.openid == "mock-a" else "task-beta"}
                ]

        results = run_accounts(
            self.data["accounts"],
            lambda *args: None,
            scheduled=True,
            client_factory=Different,
            delay_scale=0,
            location_config=self.data,
        )
        self.assertEqual([r["task_id"] for r in results], ["task-alpha", "task-beta"])


class IntegrationAndMaintenanceTests(unittest.TestCase):
    def test_all_task_pages_are_queried_and_duplicate_pages_rejected(self):
        client = __import__("api").SchoolClient("mock")
        first = [{"taskId": str(n)} for n in range(100)]
        second = [{"taskId": "extra"}]
        with patch.object(
            client,
            "_request",
            side_effect=[
                {"data": {"records": first, "total": 101}},
                {"data": {"records": second, "total": 101}},
            ],
        ) as query:
            self.assertEqual(len(client.list_tasks()), 101)
            self.assertEqual(query.call_args.kwargs["params"]["current"], 2)
        with patch.object(
            client,
            "_request",
            side_effect=[
                {"data": {"records": first, "total": 101}},
                {"data": {"records": first, "total": 101}},
            ],
        ):
            with self.assertRaisesRegex(ApiError, "重复"):
                client.list_tasks()
        client.close()

    def test_sleep_guard_releases_on_exception_and_never_requests_display(self):
        setter = Mock(return_value=0x80000000)
        with self.assertRaises(RuntimeError):
            with SleepGuard(True, setter):
                raise RuntimeError("mock")
        self.assertEqual(
            [call.args[0] for call in setter.call_args_list], [0x80000001, 0x80000000]
        )
        for enabled in (False,):
            setter.reset_mock()
            with SleepGuard(enabled, setter):
                pass
            setter.assert_not_called()

    def test_sleep_guard_failure_does_not_abort_task(self):
        setter = Mock(return_value=0)
        with SleepGuard(True, setter) as guard:
            self.assertIn("未生效", guard.error)
        self.assertEqual(setter.call_count, 1)

    def test_sleep_release_failure_does_not_lose_completed_result(self):
        setter = Mock(side_effect=[0x80000000, OSError("mock release failure")])
        with SleepGuard(True, setter) as guard:
            result = "completed"
        self.assertEqual(result, "completed")
        self.assertFalse(guard.active)
        self.assertIn("释放失败", guard.error)

    def test_autostart_quoted_paths_status_repair_and_remove_only_mock_registry(self):
        directory = Path("D:/模拟用户 空格/data")
        with patch(
            "windows_integration.sys.executable", "D:/软件 空格/平安签到.exe"
        ), patch("windows_integration.sys.frozen", True, create=True):
            integration = Autostart(directory)
        self.assertIn('"D:\\软件 空格\\平安签到.exe"', integration.command)
        self.assertIn('--data-dir "', integration.command)
        registry = MagicMock()
        handle = registry.CreateKey.return_value.__enter__.return_value
        with patch.dict(sys.modules, winreg=registry):
            integration.set_enabled(True)
            integration.set_enabled(False)
        registry.SetValueEx.assert_called_once()
        registry.DeleteValue.assert_called_once()
        with patch.object(integration, "read", return_value="outdated"):
            self.assertIn("修复", integration.status())
        with patch.object(integration, "read", return_value=integration.command):
            self.assertIn("已匹配", integration.status())

    def test_notifications_merge_final_failures_and_hide_credentials(self):
        data = defaults()
        results = [
            {
                "account_id": "a",
                "status": "failure",
                "retryable": True,
                "message": "mock-secret",
                "label": "mock-person",
            },
            {
                "account_id": "b",
                "status": "failure",
                "retryable": False,
                "message": "mock-secret",
            },
        ]
        value = batch_notice(
            results, data["notifications"], {"pending_ids": ["a"]}, "batch"
        )
        self.assertEqual(value["title"], "需要人工处理")
        self.assertIn("2 个账号失败", value["message"])
        self.assertEqual(value["account_id"], "b")
        self.assertNotIn("mock-secret", str(value))
        self.assertNotIn("mock-person", str(value))
        self.assertIsNone(batch_notice(results, {"enabled": False}))
        self.assertIsNone(
            batch_notice(results[:1], data["notifications"], {"pending_ids": ["a"]})
        )
        self.assertIsNone(batch_notice([{"status": "success"}], data["notifications"]))
        self.assertIsNotNone(
            batch_notice([{"status": "success"}], {"enabled": True, "success": True})
        )

    def test_readiness_lists_concrete_blockers_without_network(self):
        with tempfile.TemporaryDirectory() as folder, patch(
            "requests.Session.request", side_effect=AssertionError("no network")
        ):
            data = defaults()
            issues = check_ready(data, folder, True)
            self.assertEqual({page for page, _ in issues}, {"schedule", "accounts"})
            data["schedule"]["enabled"] = True
            data["accounts"] = [new_account("mock")]
            self.assertEqual(check_ready(data, folder), [])
            data["accounts"][0]["location"] = {
                "mode": "specified",
                "location_id": "gone",
            }
            self.assertIn("location", {page for page, _ in check_ready(data, folder)})

    def test_network_check_uses_head_timeout_without_login(self):
        response = Mock(status_code=403)
        with patch("requests.Session.request", return_value=response) as request:
            value = check_network()
        self.assertIn("403", value)
        self.assertEqual(request.call_args.args[0], "HEAD")
        self.assertEqual(request.call_args.kwargs["timeout"], (3, 5))
        self.assertNotIn("oauth", request.call_args.args[1])

    def test_log_cleanup_retains_running_linked_corrupt_and_unrelated_files(self):
        with tempfile.TemporaryDirectory() as folder:
            data = defaults()
            ids = {}
            for identity, status in [
                ("old", "finished"),
                ("linked", "finished"),
                ("running", "running"),
                ("active", "finished"),
            ]:
                book = LogBook(folder)
                book.start(
                    {
                        "run_id": identity,
                        "scope_label": "mock",
                        "count": 0,
                        "accounts": [],
                    }
                )
                book.finish([], status)
                path = book.root / book.active["key"] / "meta.json"
                meta = json.loads(path.read_text())
                meta["started_at"] = (now_shanghai() - timedelta(days=100)).isoformat()
                path.write_text(json.dumps(meta))
                ids[identity] = path.parent
            data["results"] = [{"batch_id": "linked"}]
            unrelated = Path(folder) / "运行日志" / "unrelated"
            unrelated.mkdir()
            (unrelated / "secret.txt").write_text("mock unrelated")
            damaged = Path(folder) / "运行日志" / "damaged"
            damaged.mkdir()
            (damaged / "meta.json").write_text("{broken")
            plan = log_plan(folder, data, 90, "active")
            self.assertEqual([e["batch_id"] for e in plan["entries"]], ["old"])
            self.assertEqual(clear_logs(folder, plan, data, "active"), 1)
            self.assertFalse(ids["old"].exists())
            for identity in ("linked", "running", "active"):
                self.assertTrue(ids[identity].exists())
            self.assertTrue(unrelated.exists())
            self.assertTrue(damaged.exists())

    def test_log_cleanup_rechecks_changed_record_before_delete(self):
        with tempfile.TemporaryDirectory() as folder:
            book = LogBook(folder)
            book.start(
                {"run_id": "old", "scope_label": "mock", "count": 0, "accounts": []}
            )
            book.finish([], "finished")
            path = book.root / book.active["key"] / "meta.json"
            value = json.loads(path.read_text())
            value["started_at"] = (now_shanghai() - timedelta(days=100)).isoformat()
            path.write_text(json.dumps(value))
            plan = log_plan(folder, defaults())
            value["status"] = "running"
            path.write_text(json.dumps(value))
            self.assertEqual(clear_logs(folder, plan, defaults()), 0)
            self.assertTrue(path.exists())

    def test_openid_tool_formats_import_only_selected_result_and_deduplicate(self):
        with tempfile.TemporaryDirectory() as folder:
            formats = {
                "openid.json": '{"openid":"mock-1","captured_at":"mock","source":"mock"}',
                "openid.txt": "mock-1\nmock-2\nmock-1",
                ".env": 'PORT=3001\nopenid="mock-1,mock-2"\nopenid=mock-2,mock-3\n',
            }
            for name, text in formats.items():
                path = Path(folder) / name
                path.write_text(text, encoding="utf-8-sig")
                result = import_accounts(path)
                self.assertEqual(
                    len(result), {"openid.json": 1, "openid.txt": 2, ".env": 3}[name]
                )
                self.assertFalse(any("PORT" in v or "captured" in v for v in result))


if __name__ == "__main__":
    unittest.main()
