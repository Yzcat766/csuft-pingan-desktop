"""Versioned display history and DPAPI-protected undo for multi-file saves."""

import base64
import copy
import hashlib
import json
import os
from pathlib import Path
from security import DPAPI

HISTORY_KEYS = ("results", "overview_batches")
HISTORY_MARKER = {"version": 1, "file": "history.json"}
JOURNAL = "history-transaction.dpapi"
TARGETS = ("history.json", "config.json.bak", "config.json")


def dumps(value):
    return json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False)


def ensure_record_ids(rows):
    """Deterministic legacy IDs retain distinct occurrences of identical old rows."""
    seen, occurrences = set(), {}
    for row in rows:
        if not isinstance(row, dict):
            raise ValueError("历史记录格式损坏，请保留原文件并恢复完整备份。")
        identity = row.get("record_id")
        if not isinstance(identity, str) or not identity or identity in seen:
            content = {k: v for k, v in row.items() if k != "record_id"}
            digest = hashlib.sha256(dumps(content).encode("utf-8")).hexdigest()
            occurrence = occurrences.get(digest, 0)
            identity = f"legacy-{digest}-{occurrence}"
            while identity in seen:
                occurrence += 1
                identity = f"legacy-{digest}-{occurrence}"
            occurrences[digest] = occurrence + 1
            row["record_id"] = identity
        seen.add(identity)


def history_value(data):
    value = {k: copy.deepcopy(data.get(k, [])) for k in HISTORY_KEYS}
    if not all(isinstance(v, list) for v in value.values()):
        raise ValueError("历史记录或批次摘要格式无效。")
    ensure_record_ids(value["results"])
    value["results"] = value["results"][:200]
    value["overview_batches"] = value["overview_batches"][:20]
    return validate_history({"format": "csuft-pingan-history", "version": 1, **value})


def validate_history(value):
    if (not isinstance(value, dict)
                or value.get("format") != "csuft-pingan-history"
                or type(value.get("version")) is not int or value["version"] != 1
                or any(not isinstance(value.get(k), list) for k in HISTORY_KEYS)):
        raise ValueError("历史格式或版本无效")
    ids = set()
    for row in value["results"]:
        if (not isinstance(row, dict)
                    or row.get("status") not in ("success", "failure", "cancelled", "skipped")
                    or not isinstance(row.get("account_id"), str)
                    or not isinstance(row.get("date"), str)
                    or not isinstance(row.get("record_id"), str)
                    or not row["record_id"] or row["record_id"] in ids
                    or any(not isinstance(row.get(k, ""), str) for k in ("label", "message", "task"))):
            raise ValueError("历史记录格式或标识无效")
        ids.add(row["record_id"])
    if any(not isinstance(row, dict) or not isinstance(row.get("batch_id"), str)
               or not isinstance(row.get("summary"), dict)
               for row in value["overview_batches"]):
        raise ValueError("历史批次摘要无效")
    for batch in value["overview_batches"]:
        if any(type(batch["summary"].get(s, 0)) is not int or batch["summary"].get(s, 0) < 0
               for s in ("success", "failure", "cancelled", "skipped")):
            raise ValueError("历史批次计数无效")
    if len(value["results"]) > 200 or len(value["overview_batches"]) > 20:
        raise ValueError("历史容量无效")
    return value


def read_history(path):
    try:
        return validate_history(json.loads(Path(path).read_text(encoding="utf-8")))
    except (OSError, ValueError, TypeError, KeyError) as error:
        raise ValueError(f"history.json 缺失、损坏或不兼容：{error}。请保留原文件并显式恢复完整备份；不会自动载入旧历史。") from error


def atomic_bytes(path, content):
    path = Path(path)
    temporary = path.with_name(path.name + ".history-tmp")
    try:
        with temporary.open("wb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


def recover_transaction(directory):
    """An undo journal exists until ALL replacements commit; recovery is repeatable."""
    directory = Path(directory)
    journal = directory / JOURNAL
    if not journal.exists():
        return False
    try:
        envelope = json.loads(journal.read_text(encoding="utf-8"))
        if envelope.get("scheme") != "dpapi-user-v1":
            raise ValueError("事务保护格式无效")
        value = json.loads(DPAPI().unprotect(envelope["value"]))
        if (value.get("version") != 1 or not isinstance(value.get("before"), dict)
                or not value["before"] or set(value["before"]) - set(TARGETS)):
            raise ValueError("事务内容无效")
        # Validate the entire journal before changing even one file.
        before = {name: None if raw is None else base64.b64decode(raw, validate=True)
                  for name, raw in value["before"].items()}
        for name, content in before.items():
            path = directory / name
            if content is None:
                path.unlink(missing_ok=True)
            elif not path.exists() or path.read_bytes() != content:
                atomic_bytes(path, content)
        journal.unlink()
        return True
    except Exception as error:
        raise OSError(f"历史/配置事务恢复失败：{error}。原事务文件已保留，请解决后重新启动，不能继续执行任务。") from error


def replace_files(directory, contents):
    directory = Path(directory)
    if set(contents) - set(TARGETS):
        raise ValueError("事务目标无效")
    recover_transaction(directory)
    # History-only deletion has one atomic commit and never writes config or its backup.
    if set(contents) == {"history.json"}:
        atomic_bytes(directory / "history.json", contents["history.json"])
        return
    before = {name: base64.b64encode((directory / name).read_bytes()).decode("ascii")
              if (directory / name).exists() else None for name in contents}
    envelope = {"scheme": "dpapi-user-v1", "value": DPAPI().protect(
        dumps({"version": 1, "before": before}))}
    journal = directory / JOURNAL
    atomic_bytes(journal, dumps(envelope).encode("utf-8"))
    try:
        # History reaches disk before the legacy fields are removed from config.
        for name in TARGETS:
            if name in contents:
                atomic_bytes(directory / name, contents[name])
        journal.unlink()  # Commit point. A crash earlier rolls the entire save back.
    except Exception as error:
        try:
            recover_transaction(directory)
        except OSError as recovery_error:
            raise OSError(f"保存失败（{error}）；{recovery_error}") from error
        raise


def delete_records(data, identities):
    """Delete a captured ID set, updating only display history and batch summaries."""
    identities = frozenset(identities)
    remaining = [r for r in data["results"] if r["record_id"] not in identities]
    count = len(data["results"]) - len(remaining)
    data["results"] = remaining
    grouped = {}
    for row in remaining:
        identity = row.get("batch_id") or row.get("run_id")
        if identity:
            grouped.setdefault(identity, []).append(row)
    batches = []
    for saved in data.get("overview_batches", []):
        rows = grouped.get(saved.get("batch_id"), [])
        if not rows:
            continue
        batch = copy.deepcopy(saved)
        batch["summary"] = {s: sum(r.get("status") == s for r in rows)
                            for s in ("success", "failure", "cancelled", "skipped")}
        batch["completed_count"] = len({r["account_id"] for r in rows})
        batch["account_count"] = batch["completed_count"]
        batch["history_pruned"] = True
        batches.append(batch)
    data["overview_batches"] = batches
    return count
