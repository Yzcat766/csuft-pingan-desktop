import copy
import json
import os
import re
import uuid
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from security import DPAPI, encode_config, decode_config
from history_storage import (
    HISTORY_KEYS, HISTORY_MARKER, dumps, history_value, read_history,
    recover_transaction, replace_files, ensure_record_ids,
    delete_records,
)

from locations import (
    INHERIT_LOCATION,
    TASK_LOCATION,
    normalize_locations,
    university_location,
)

SHANGHAI = timezone(timedelta(hours=8), "Asia/Shanghai")


def now_shanghai():
    return datetime.now(SHANGHAI)


def defaults():
    campus = university_location()
    return {
        "version": 3,
        "accounts": [],
        "locations": [campus],
        "global_location": {"mode": "specified", "location_id": campus["id"]},
        "global_offset": {"mode": "off", "radius": 0.0},
        "position_cache": {},
        "schedule": {
            "enabled": False,
            "mode": "daily",
            "time": "21:05",
            "dates": [],
            "last_trigger": None,
            "retry": {"enabled": True, "max_retries": 2, "interval_minutes": 5},
            "task_rule": {"mode": "unique_current", "name": ""},
        },
        "automation": None,
        "success_dates": {},
        "task_success_dates": {},
        "reliability": {
            "autostart": False,
            "start_hidden": False,
            "prevent_sleep": False,
        },
        "notifications": {"enabled": True, "success": False},
        "maintenance": {"retention_days": 90},
        "onboarding_done": False,
        "appearance": {
            "theme": "dark",
            "accent": "teal",
            "close_behavior": "tray",
            "tray_notice_shown": False,
        },
        "results": [],
        "overview_batches": [],
    }


def validate_schedule(schedule):
    if (
        not isinstance(schedule, dict)
        or type(schedule.get("enabled", False)) is not bool
        or not isinstance(schedule.get("time", ""), str)
        or not isinstance(schedule.get("dates", []), list)
        or any(not isinstance(v, str) for v in schedule.get("dates", []))
        or not isinstance(schedule.get("retry", {}), dict)
    ):
        raise ValueError("计划格式无效，请重新配置计划。")
    if schedule.get("mode") not in ("daily", "weekdays", "dates"):
        raise ValueError("请选择有效的定时规则。")
    clock = schedule.get("time", "")
    if not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", clock):
        raise ValueError("时间格式应为 HH:mm，例如 21:05。")
    days = []
    for value in schedule.get("dates", []):
        value = value.strip()
        if value and date.fromisoformat(value).isoformat() != value:
            raise ValueError("日期格式应为 YYYY-MM-DD。")
        if value:
            days.append(value)
    if schedule.get("enabled") and schedule["mode"] == "dates" and not days:
        raise ValueError("启用指定日期计划前，请填写至少一个日期。")
    retry = {
        "enabled": True,
        "max_retries": 2,
        "interval_minutes": 5,
        **schedule.get("retry", {}),
    }
    if (
        not isinstance(retry["enabled"], bool)
        or type(retry["max_retries"]) is not int
        or not 0 <= retry["max_retries"] <= 5
        or type(retry["interval_minutes"]) is not int
        or not 1 <= retry["interval_minutes"] <= 1440
    ):
        raise ValueError("自动重试次数应为 0～5，间隔应为 1～1440 分钟，请修正计划。")
    rule = schedule.get("task_rule", {"mode": "unique_current", "name": ""})
    if (
        not isinstance(rule, dict)
        or rule.get("mode") not in ("unique_current", "exact_name")
        or (rule["mode"] == "exact_name" and not str(rule.get("name", "")).strip())
    ):
        raise ValueError("请选择唯一当前任务，或填写精确匹配的任务名称。")
    return {**schedule, "dates": sorted(set(days)), "retry": retry, "task_rule": rule}


def matches_day(schedule, day):
    return (
        schedule["mode"] == "daily"
        or (schedule["mode"] == "weekdays" and day.weekday() < 5)
        or (
            schedule["mode"] == "dates" and day.isoformat() in schedule.get("dates", [])
        )
    )


def trigger_key(schedule, moment):
    return moment.date().isoformat() + "@" + schedule["time"]


def schedule_due(schedule, moment):
    return bool(
        schedule.get("enabled")
        and matches_day(schedule, moment.date())
        and moment.strftime("%H:%M") >= schedule["time"]
        and schedule.get("last_trigger") != trigger_key(schedule, moment)
    )


def next_run(schedule, moment):
    if not schedule.get("enabled"):
        return None
    hour, minute = map(int, schedule["time"].split(":"))
    for offset in range(401):
        day = moment.date() + timedelta(days=offset)
        candidate = datetime.combine(day, datetime.min.time(), SHANGHAI).replace(
            hour=hour, minute=minute
        )
        if matches_day(schedule, day) and schedule.get("last_trigger") != trigger_key(
            schedule, candidate
        ):
            if offset == 0 and candidate <= moment:
                return moment
            if candidate > moment:
                return candidate
    return None


def normalize_openids(text):
    values = re.split(r"[,，\s]+", text.strip())
    return list(dict.fromkeys(value for value in values if value))


def import_accounts(path):
    text = Path(path).read_text(encoding="utf-8-sig")
    if Path(path).suffix.lower() == ".json":
        value = json.loads(text)
        if isinstance(value, dict) and isinstance(value.get("openid"), str):
            return normalize_openids(value["openid"])
        raise ValueError("JSON 文件需要包含字符串 openid 字段。")
    env = re.findall(r"^\s*openid\s*=\s*(.*)$", text, re.MULTILINE | re.IGNORECASE)
    return (
        list(
            dict.fromkeys(
                v for line in env for v in normalize_openids(line.strip().strip("\"'"))
            )
        )
        if env
        else normalize_openids(text)
    )


def new_account(openid, label=None):
    return {
        "id": uuid.uuid4().hex,
        "label": label or "新账号",
        "openid": openid.strip(),
        "enabled": True,
        "location": dict(INHERIT_LOCATION),
        "offset": {"mode": "inherit", "radius": 0.0},
    }


class Store:
    def __init__(self, directory, protector=None):
        self.protector = protector or DPAPI()
        self.directory = Path(directory)
        self.path = self.directory / "config.json"
        self.history_path = self.directory / "history.json"
        self.history_notice = ""
        self.recovered_transaction = recover_transaction(self.directory)
        self.data = defaults()
        self.legacy_plaintext = False
        self.existing = self.path.exists()
        loaded = {}
        if self.path.exists():
            loaded = json.loads(self.path.read_text(encoding="utf-8"))
            if not isinstance(loaded, dict) or not isinstance(
                loaded.get("accounts"), list
            ):
                raise ValueError("本地配置格式损坏，请从 config.json.bak 恢复。")
            if loaded.get("version", 2) not in (1, 2, 3):
                raise ValueError("配置版本不兼容，请使用对应版本软件或恢复兼容备份。")
            self.legacy_plaintext = any(a.get("openid") for a in loaded["accounts"])
            loaded = decode_config(loaded, self.protector)
            self.data.update(loaded)
            # New-install defaults must not override an existing user's saved choice.
            self.data["locations"] = loaded.get("locations", [])
            self.data["global_location"] = loaded.get(
                "global_location", dict(TASK_LOCATION)
            )
            self.data["schedule"] = validate_schedule(
                {**defaults()["schedule"], **loaded.get("schedule", {})}
            )
            self.data["appearance"] = {
                **defaults()["appearance"],
                **loaded.get("appearance", {}),
            }
            normalize_locations(self.data, migrate=True)
            self.data["onboarding_done"] = loaded.get("onboarding_done", True)
            for section in ("reliability", "notifications", "maintenance"):
                self.data[section] = {**defaults()[section], **loaded.get(section, {})}
            self.data["version"] = 3

        self._history_migrated = loaded.get("history_store") == HISTORY_MARKER
        if "history_store" in loaded and not self._history_migrated:
            raise ValueError("历史存储版本不兼容，请保留原文件。")
        if self.history_path.exists() or self._history_migrated:
            value = read_history(self.history_path)
            self.data.update({k: value[k] for k in HISTORY_KEYS})
            if any(k in loaded for k in HISTORY_KEYS):
                self.history_notice = "检测到旧配置内嵌历史；已采用有效 history.json，旧历史不会自动合并或复活。"
        else:
            value = history_value(self.data)
            self.data.update({k: value[k] for k in HISTORY_KEYS})
            if self.existing:
                self.history_notice = "旧历史将在首次成功保存时迁移到 history.json；保存前原配置不变。"
        self._saved_history = copy.deepcopy(value) if self.history_path.exists() else None
        self._saved_config = self._config_view()
        if self.recovered_transaction:
            self.history_notice = "检测到未完成保存，已恢复保存前的配置和历史；请核对相关运行日志及服务端结果。\n" + self.history_notice

    def _config_view(self):
        return {k: copy.deepcopy(v) for k, v in self.data.items()
                if k not in HISTORY_KEYS and k != "history_store"}

    def reload(self):
        loaded = Store(self.directory, self.protector)
        self.__dict__.update(loaded.__dict__)

    def save(self):
        # Display-history changes must not expire or otherwise mutate runtime state.
        if self._config_view() != self._saved_config:
            normalize_locations(self.data)
        self.directory.mkdir(parents=True, exist_ok=True)
        recover_transaction(self.directory)
        # A missing/corrupt committed history is never overwritten by stale memory.
        if self._history_migrated or self._saved_history is not None or self.history_path.exists():
            read_history(self.history_path)
        config = self._config_view()
        protected = encode_config({**config, "history_store": HISTORY_MARKER}, self.protector)
        history = history_value(self.data)
        config_changed = (not self.path.exists() or not self._history_migrated
                          or config != self._saved_config or self.legacy_plaintext)
        history_changed = history != self._saved_history
        from logbook import atomic_json

        # Migration keeps the exact old schema in a DPAPI envelope, never a plaintext backup.
        if (
            self.legacy_plaintext
            and not (self.directory / "migration-rollback.dpapi").exists()
        ):
            atomic_json(
                self.directory / "migration-rollback.dpapi",
                {
                    "scheme": "dpapi-user-v1",
                    "value": self.protector.protect(
                        self.path.read_text(encoding="utf-8")
                    ),
                },
            )
        contents = {}
        if history_changed:
            contents["history.json"] = dumps(history).encode("utf-8")
        if config_changed:
            if self.path.exists():
                previous = decode_config(json.loads(self.path.read_text(encoding="utf-8")), self.protector)
                previous = {k: v for k, v in previous.items() if k not in HISTORY_KEYS}
                previous["history_store"] = HISTORY_MARKER
                contents["config.json.bak"] = dumps(encode_config(previous, self.protector)).encode("utf-8")
            else:
                contents["config.json.bak"] = dumps(protected).encode("utf-8")
            contents["config.json"] = dumps(protected).encode("utf-8")
        if contents:
            replace_files(self.directory, contents)
        # Publish generated IDs / retention only after persistence succeeds.
        self.data.update({k: copy.deepcopy(history[k]) for k in HISTORY_KEYS})
        self.data["history_store"] = dict(HISTORY_MARKER)
        self._saved_history = copy.deepcopy(history)
        self._saved_config = config
        self._history_migrated = True
        self.legacy_plaintext = False
        if self.history_notice.startswith("旧历史将在"):
            self.history_notice = "旧历史已成功迁移到 history.json。"

    def snapshot(self):
        return copy.deepcopy(self.data)

    def delete_history(self, identities):
        before = self.snapshot()
        try:
            count = delete_records(self.data, identities)
            self.save()
            return count
        except Exception:
            self.data.clear()
            self.data.update(before)
            raise

    def write_log(self, lines, outcome, moment=None):
        moment = moment or now_shanghai()
        directory = (
            self.directory
            / "总日志"
            / f"{moment.year}年日志"
            / f"{moment.month}月日志"
            / f"第{(moment.day - 1) // 7 + 1}周日志"
        )
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / f"{moment:%Y-%m-%d-%H-%M-%S-%f}{outcome}.log"
        from api import redact

        path.write_text(
            redact(
                "\n".join(lines) + "\n",
                [a.get("openid", "") for a in self.data["accounts"]],
            ),
            encoding="utf-8",
        )
        return path
