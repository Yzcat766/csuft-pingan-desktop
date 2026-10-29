"""Atomic, credential-free batch journals. Legacy files remain unassociated."""

import copy
import json
import os
import re
import threading
from collections import deque
from pathlib import Path
from api import redact
from storage import now_shanghai

CHUNK_SIZE = 200
PAGE_SIZE = 200
LEVELS = {"info": "信息", "warning": "警告", "error": "错误"}


def clean(value, secrets=()):
    if isinstance(value, str):
        return redact(value, secrets)
    if isinstance(value, list):
        return [clean(v, secrets) for v in value]
    if isinstance(value, dict):
        return {
            k: (
                "[已隐藏]"
                if re.search(
                    r"(?i)openid|token|password|authorization|proxy_pass|client_secret",
                    k,
                )
                else clean(v, secrets)
            )
            for k, v in value.items()
        }
    return value


def atomic_json(path, value):
    if path.exists():
        try:
            json.loads(path.read_text(encoding="utf-8"))
        except (ValueError, OSError):
            raise OSError(
                "已有日志记录损坏或不可读；请保留原文件并检查备份，不能覆盖。"
            ) from None
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    try:
        with temporary.open("w", encoding="utf-8") as stream:
            json.dump(value, stream, ensure_ascii=False, allow_nan=False)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


def matches(event, filters):
    account = filters.get("account_id")
    return (
        (
            account is None
            or event.get("account_id") == (None if account == "" else account)
        )
        and (
            not filters.get("date") or event.get("time", "").startswith(filters["date"])
        )
        and (not filters.get("level") or event.get("level") == filters["level"])
        and (
            not filters.get("keyword")
            or filters["keyword"].casefold()
            in (event.get("message", "") + " " + event.get("step", "")).casefold()
        )
    )


def event_text(event):
    who = event.get("account_label") or ("批次" if event.get("batch_id") else "系统")
    if event.get("account_id"):
        who += " · " + event["account_id"][:6]
    return (
        f"{(event.get('time') or '时间未知').replace('T', ' ')} | {who} | "
        f"{LEVELS.get(event.get('level'), '未知级别')} | {event.get('step', '')}\n"
        f"{event.get('message', '')}"
    )


class LogBook:
    def __init__(self, directory, secrets=()):
        self.directory = Path(directory)
        self.root = self.directory / "运行日志"
        self.legacy_root = self.directory / "总日志"
        self.secrets = list(secrets)
        self.active = None
        self.events = []
        self.dirty = set()
        self.meta_dirty = False
        self.lock = threading.RLock()

    def start(self, request):
        with self.lock:
            if self.dirty or self.meta_dirty:
                self.flush()
            moment = now_shanghai()
            identity = request["run_id"]
            if not re.fullmatch(r"[a-zA-Z0-9_-]{1,100}", identity):
                raise ValueError("日志批次标识无效，请重新启动任务。")
            key = moment.strftime("%Y%m%d%H%M%S%f") + "-" + identity
            self.active = clean(
                {
                    "version": 1,
                    "kind": "batch",
                    "key": key,
                    "batch_id": identity,
                    "started_at": moment.isoformat(),
                    "mode": request["scope_label"],
                    "account_count": request["count"],
                    "status": "running",
                    "summary": {},
                    "accounts": [
                        {"id": a["id"], "label": a["label"]}
                        for a in request["accounts"]
                    ],
                    "event_count": 0,
                },
                self.secrets,
            )
            self.events = []
            self.dirty = set()
            atomic_json(self.root / key / "meta.json", self.active)
            return copy.deepcopy(self.active)

    def append(self, value):
        with self.lock:
            if not self.active:
                raise ValueError("没有正在记录的日志批次。")
            event = clean(
                {
                    "batch_id": self.active["batch_id"],
                    "account_id": value.get("account_id"),
                    "account_label": value.get("account_label", ""),
                    "time": value.get("time") or now_shanghai().isoformat(),
                    "seq": len(self.events) + 1,
                    "level": value.get("level", "info"),
                    "step": value.get("step", "批次"),
                    "message": str(value.get("message", "")),
                },
                self.secrets,
            )
            if event["level"] not in LEVELS:
                event["level"] = "info"
            self.events.append(event)
            self.dirty.add((len(self.events) - 1) // CHUNK_SIZE)
            return copy.deepcopy(event)

    def flush(self):
        with self.lock:
            if not self.active or not (self.dirty or self.meta_dirty):
                return
            folder = self.root / self.active["key"]
            for chunk in sorted(self.dirty):
                atomic_json(
                    folder / f"{chunk:06d}.json",
                    self.events[chunk * CHUNK_SIZE : (chunk + 1) * CHUNK_SIZE],
                )
            self.active["event_count"] = len(self.events)
            atomic_json(folder / "meta.json", self.active)
            self.dirty.clear()
            self.meta_dirty = False

    def finish(self, results, status="finished"):
        with self.lock:
            if not self.active:
                return
            self.meta_dirty = True
            self.active.update(
                status=status,
                finished_at=now_shanghai().isoformat(),
                summary={
                    s: sum(r.get("status") == s for r in results)
                    for s in ("success", "failure", "cancelled", "skipped")
                },
            )
            self.flush()

    def checkpoint_result(self, result):
        with self.lock:
            if not self.active:
                return
            self.active.setdefault("status_by_account", {})[result["account_id"]] = (
                result["status"]
            )
            self.active["summary"] = {
                key: sum(
                    value == key for value in self.active["status_by_account"].values()
                )
                for key in ("success", "failure", "cancelled", "skipped")
            }
            self.meta_dirty = True

    def list_batches(self, offset=0, limit=50):
        candidates = []
        if self.root.exists():
            candidates.extend(
                (p.name, "batch", p) for p in self.root.iterdir() if p.is_dir()
            )
        if self.legacy_root.exists():
            candidates.extend(
                (f"{p.stat().st_mtime:020.6f}", "legacy", p)
                for p in self.legacy_root.rglob("*.log")
                if p.is_file()
            )
        # File modification time is explicitly not represented as a task start time.
        candidates.sort(key=lambda item: item[2].stat().st_mtime, reverse=True)
        records = []
        for _, kind, path in candidates[offset : offset + limit]:
            if kind == "legacy":
                records.append(
                    {
                        "kind": "legacy",
                        "key": "legacy:" + str(path.relative_to(self.legacy_root)),
                        "mode": "旧文本日志（无批次或账号关联）",
                        "filename": path.name,
                        "status": "legacy",
                        "accounts": [],
                        "started_at": "",
                    }
                )
                continue
            try:
                raw = json.loads((path / "meta.json").read_text(encoding="utf-8"))
                if (
                    not isinstance(raw, dict)
                    or raw.get("key") != path.name
                    or not isinstance(raw.get("batch_id"), str)
                    or not isinstance(raw.get("accounts"), list)
                    or raw.get("kind") != "batch"
                    or not isinstance(raw.get("summary"), dict)
                    or any(type(v) is not int or v < 0 for v in raw["summary"].values())
                    or not isinstance(raw.get("started_at"), str)
                    or not isinstance(raw.get("mode"), str)
                    or not isinstance(raw.get("status"), str)
                    or type(raw.get("account_count")) is not int
                    or raw["account_count"] != len(raw["accounts"])
                    or type(raw.get("event_count")) is not int
                    or not 0 <= raw["event_count"] <= 1000000
                    or any(
                        not isinstance(a, dict)
                        or not isinstance(a.get("id"), str)
                        or not isinstance(a.get("label"), str)
                        for a in raw["accounts"]
                    )
                ):
                    raise ValueError()
                records.append(clean(raw, self.secrets))
            except (OSError, ValueError, TypeError):
                records.append(
                    {
                        "kind": "broken",
                        "key": path.name,
                        "status": "broken",
                        "mode": "日志元数据损坏",
                        "accounts": [],
                        "started_at": "",
                        "warning": "记录无法读取。原文件保留，请打开日志目录检查或恢复备份。",
                    }
                )
        return {"batches": records, "total": len(candidates), "offset": offset}

    def iter_filtered(self, record, filters, warnings, cancelled=None):
        if record.get("kind") == "broken":
            warnings.append(record.get("warning", "记录无法读取。"))
            return
        if record.get("kind") == "legacy":
            path = (self.legacy_root / Path(record["key"][7:])).resolve()
            if not path.is_relative_to(self.legacy_root.resolve()):
                raise ValueError("旧日志路径无效。")
            warnings.append(
                "旧文本日志没有结构化批次、账号、时间或级别关联；按原文展示，不推测账号归属。"
            )
            try:
                with path.open("r", encoding="utf-8-sig", errors="replace") as stream:
                    text = stream.read(5_000_001)
                if len(text) > 5_000_000:
                    warnings.append(
                        "文件过大，仅加载前 5 百万字符；完整内容请打开原文件。"
                    )
                    text = text[:5_000_000]
                if "\ufffd" in text:
                    warnings.append(
                        "部分字符无法按 UTF-8 读取，以替代符显示；原文件保留。"
                    )
                for i, line in enumerate(text.splitlines()):
                    if cancelled and cancelled():
                        return
                    event = dict(
                        batch_id=None,
                        account_id=None,
                        account_label="旧文本 · 未关联",
                        time="",
                        seq=i + 1,
                        level="unknown",
                        step="原文",
                        message=line,
                    )
                    event = clean(event, self.secrets)
                    if matches(event, filters):
                        yield event
            except OSError:
                warnings.append("旧日志无法读取。请检查文件权限，原文件未修改。")
            return
        key = record["key"]
        if not re.fullmatch(r"[a-zA-Z0-9_-]+", key):
            raise ValueError("日志目录标识无效。")
        folder = self.root / key
        count = record["event_count"]
        identities = {a["id"] for a in record["accounts"]}
        previous = 0
        for chunk in range((count + CHUNK_SIZE - 1) // CHUNK_SIZE):
            if cancelled and cancelled():
                return
            try:
                path = folder / f"{chunk:06d}.json"
                if path.stat().st_size > 32_000_000:
                    raise ValueError()
                raw = json.loads(path.read_text(encoding="utf-8"))
                if not isinstance(raw, list) or len(raw) > CHUNK_SIZE:
                    raise ValueError()
                for event in raw:
                    if (
                        not isinstance(event, dict)
                        or type(event.get("seq")) is not int
                        or event["seq"] <= previous
                        or event.get("batch_id") != record["batch_id"]
                        or not isinstance(event.get("message"), str)
                        or not isinstance(event.get("time"), str)
                        or not isinstance(event.get("step"), str)
                        or not isinstance(event.get("level"), str)
                        or event["level"] not in LEVELS
                        or not isinstance(event.get("account_label", ""), str)
                        or event.get("account_id") is not None
                        and event["account_id"] not in identities
                    ):
                        raise ValueError()
                    if event["seq"] > count:
                        break
                    previous = event["seq"]
                    sanitized = clean(event, self.secrets)
                    if matches(sanitized, filters):
                        yield sanitized
            except (OSError, ValueError, TypeError):
                warning = "部分结构化记录损坏或暂不可读；已跳过该片段。原文件保留，请检查日志目录。"
                if warning not in warnings:
                    warnings.append(warning)

    def query(self, record, filters=None, offset=-1, limit=PAGE_SIZE, cancelled=None):
        warnings, events, total = [], deque(maxlen=limit), 0
        for event in self.iter_filtered(record, filters or {}, warnings, cancelled):
            if offset < 0 or offset <= total < offset + limit:
                events.append(event)
            total += 1
        actual = max(0, total - limit) if offset < 0 else min(offset, total)
        return {
            "events": list(events),
            "total": total,
            "offset": actual,
            "warning": "\n".join(warnings),
        }

    def export(self, record, filters, destination):
        # Stream through the same redactor and filter as the viewer; retain no full batch in memory.
        path = Path(destination)
        if path.resolve().is_relative_to(
            self.root.resolve()
        ) or path.resolve().is_relative_to(self.legacy_root.resolve()):
            raise ValueError("请导出到日志目录之外，以免覆盖原始记录。")
        temporary = path.with_suffix(path.suffix + ".tmp")
        warnings, count = [], 0
        try:
            with temporary.open("w", encoding="utf-8") as stream:
                for event in self.iter_filtered(record, filters, warnings):
                    stream.write(event_text(event) + "\n\n")
                    count += 1
                if warnings:
                    stream.write("\n".join(warnings) + "\n")
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path)
        finally:
            if temporary.exists():
                temporary.unlink()
        return count
