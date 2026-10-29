"""Persistent, bounded automatic attempts. A trigger is never treated as success."""

import copy
import json
from datetime import datetime, timedelta
from storage import SHANGHAI, matches_day, trigger_key, schedule_due, next_run
from execution import make_request, today_succeeded


def fingerprint(schedule):
    return json.dumps(
        {
            k: schedule.get(k)
            for k in ("enabled", "mode", "time", "dates", "retry", "task_rule")
        },
        sort_keys=True,
    )


def parse_time(value):
    try:
        value = datetime.fromisoformat(value)
        return value.astimezone(SHANGHAI) if value.tzinfo else None
    except (ValueError, TypeError):
        return None


def valid_cycle(data, moment):
    cycle = data.get("automation") or {}
    schedule = data["schedule"]
    return bool(
        schedule["enabled"]
        and cycle.get("date") == moment.date().isoformat()
        and cycle.get("schedule_fingerprint") == fingerprint(schedule)
        and matches_day(schedule, moment.date())
    )


def pending_ids(data, moment):
    if not valid_cycle(data, moment):
        return []
    cycle = data.get("automation") or {}
    selected = set(cycle.get("pending_ids", []))

    def within_rules(identity):
        result = cycle.get("account_results", {}).get(identity, {})
        window = result.get("task_window")
        if not window:
            return True
        from runner import in_time_window

        try:
            return in_time_window(window["start"], window["end"], moment)
        except (ValueError, KeyError):
            return False

    return [
        a["id"]
        for a in data["accounts"]
        if a["id"] in selected
        and a.get("enabled", True)
        and data.get("task_success_dates", {})
        .get(a["id"], {})
        .get(cycle.get("task_ids", {}).get(a["id"]))
        != moment.date().isoformat()
        and within_rules(a["id"])
    ]


def next_automatic(data, moment):
    schedule = data["schedule"]
    if not schedule["enabled"]:
        return None
    cycle = data.get("automation") or {}
    due = parse_time(cycle.get("next_retry"))
    retry = schedule["retry"]
    if (
        valid_cycle(data, moment)
        and due
        and pending_ids(data, moment)
        and retry["enabled"]
        and cycle.get("retry_count", 0) < retry["max_retries"]
    ):
        if due.date() == moment.date():
            return {"kind": "retry", "at": max(due, moment)}
    initial = next_run(schedule, moment)
    return {"kind": "initial", "at": initial} if initial else None


def due_action(data, moment):
    cycle = data.get("automation") or {}
    due = parse_time(cycle.get("next_retry"))
    if (
        due
        and due <= moment
        and valid_cycle(data, moment)
        and pending_ids(data, moment)
    ):
        retry = data["schedule"]["retry"]
        if retry["enabled"] and cycle.get("retry_count", 0) < retry["max_retries"]:
            return "retry"
    return "initial" if schedule_due(data["schedule"], moment) else None


def prepare_schedule(previous, proposed, moment, choice=None):
    """Past-time save is explicit: cancel leaves the caller's data untouched."""
    changed = any(
        proposed.get(k) != previous.get(k) for k in ("enabled", "mode", "time", "dates")
    )
    needs_choice = (
        changed
        and proposed["enabled"]
        and matches_day(proposed, moment.date())
        and moment.strftime("%H:%M") >= proposed["time"]
    )
    result = copy.deepcopy(proposed)
    if needs_choice:
        if choice not in ("now", "next"):
            return None
        result["last_trigger"] = (
            None if choice == "now" else trigger_key(result, moment)
        )
    elif changed:
        result["last_trigger"] = None
    return result


def begin_attempt(data, request, moment, retry=False):
    if retry:
        cycle = data["automation"]
        if not valid_cycle(data, moment) or not pending_ids(data, moment):
            raise ValueError("本次自动重试已失效，请核对计划和账号。")
        if cycle.get("retry_count", 0) >= data["schedule"]["retry"]["max_retries"]:
            raise ValueError("已达到自动重试上限。")
        cycle["retry_count"] += 1
    else:
        cycle = {
            "date": moment.date().isoformat(),
            "triggered_at": moment.isoformat(),
            "schedule_fingerprint": fingerprint(data["schedule"]),
            "retry_count": 0,
            "account_results": {},
            "task_ids": {},
            "summary": {},
            "next_retry": None,
        }
        data["automation"] = cycle
        data["schedule"]["last_trigger"] = trigger_key(data["schedule"], moment)
    cycle.update(
        attempt_id=request["run_id"],
        attempt_ids=[a["id"] for a in request["accounts"]],
        pending_ids=[],
        state="running",
        last_attempt_at=moment.isoformat(),
        next_retry=None,
        reason="",
    )


def checkpoint_task(data, run_id, value):
    cycle = data.get("automation") or {}
    if cycle.get("attempt_id") == run_id:
        cycle.setdefault("task_ids", {})[value["account_id"]] = value["task_id"]


def checkpoint_result(data, run_id, result):
    cycle = data.get("automation") or {}
    if cycle.get("attempt_id") == run_id:
        if result.get("task_id"):
            cycle.setdefault("task_ids", {})[result["account_id"]] = result["task_id"]
        fields = (
            "status",
            "retryable",
            "uncertain",
            "error_code",
            "task_id",
            "task_window",
            "finished_at",
        )
        cycle.setdefault("account_results", {})[result["account_id"]] = {
            k: result.get(k) for k in fields
        }


def can_retry_result(result, due):
    if result.get("status") != "failure" or result.get("retryable") is not True:
        return False
    window = result.get("task_window")
    if window:
        from runner import in_time_window

        try:
            return in_time_window(window["start"], window["end"], due)
        except ValueError:
            return False
    return True


def finish_attempt(data, request, results, moment, cancelled=False):
    cycle = data.get("automation") or {}
    if cycle.get("attempt_id") != request["run_id"]:
        return
    for result in results:
        checkpoint_result(data, request["run_id"], result)
    cycle["last_finished_at"] = moment.isoformat()
    cycle["summary"] = {
        s: sum(r.get("status") == s for r in cycle["account_results"].values())
        for s in ("success", "failure", "skipped", "cancelled")
    }
    cycle.update(
        state="cancelled" if cancelled else "finished", next_retry=None, pending_ids=[]
    )
    if cancelled:
        cycle["reason"] = "主动取消，不自动重试。"
        return
    retry = data["schedule"]["retry"]
    due = moment + timedelta(minutes=retry["interval_minutes"])
    if (
        not valid_cycle(data, moment)
        or not retry["enabled"]
        or cycle["retry_count"] >= retry["max_retries"]
    ):
        cycle["reason"] = "计划已关闭/修改、自动重试已关闭或已达到次数上限。"
        return
    if due.date() != moment.date() or not matches_day(data["schedule"], due.date()):
        cycle["reason"] = "重试将跨越本次允许日期，不再自动重试。"
        return
    ids = [
        identity
        for identity, result in cycle["account_results"].items()
        if can_retry_result(result, due)
    ]
    cycle["pending_ids"] = ids
    cycle["pending_ids"] = pending_ids(data, moment)
    if cycle["pending_ids"]:
        cycle.update(
            state="waiting_retry",
            next_retry=due.isoformat(),
            reason="仅重试明确可重试失败；提交前会重新查询服务端。",
        )
    else:
        cycle["reason"] = "没有可自动重试的失败账号；需要修正的错误请查看原因。"


def recover_interrupted(data, moment):
    cycle = data.get("automation") or {}
    if cycle.get("state") != "running":
        return False
    cycle.update(
        state="interrupted",
        next_retry=None,
        reason="上次执行未完整结束，未将触发视作成功。",
    )
    retry = data["schedule"]["retry"]
    if (
        not valid_cycle(data, moment)
        or not retry["enabled"]
        or cycle.get("retry_count", 0) >= retry["max_retries"]
    ):
        return True
    at = parse_time(cycle.get("last_attempt_at"))
    due = max(moment, (at or moment) + timedelta(minutes=retry["interval_minutes"]))
    if due.date() != moment.date():
        return True
    checkpoint = cycle.get("account_results", {})
    cycle["pending_ids"] = [
        identity
        for identity in cycle.get("attempt_ids", [])
        if identity not in checkpoint or can_retry_result(checkpoint[identity], due)
    ]
    cycle["pending_ids"] = pending_ids(data, moment)
    if cycle["pending_ids"]:
        cycle.update(
            next_retry=due.isoformat(),
            reason="上次结果不确定；到时先查询服务端，再决定是否提交。",
        )
    return True


def invalidate_pending(data, reason="计划已关闭或修改，已取消尚未开始的自动重试。"):
    cycle = data.get("automation")
    if cycle:
        cycle.update(next_retry=None, pending_ids=[], reason=reason)
        # A running attempt still finishes its current snapshot, but cannot queue more retries.
        cycle["schedule_fingerprint"] = ""
