"""Overview projections from persisted records; no IO, login or submission."""

import copy
from datetime import datetime
from storage import SHANGHAI

STATUSES = {
    "success": "成功",
    "failure": "失败",
    "cancelled": "取消",
    "skipped": "跳过",
}


def time_rank(value):
    try:
        parsed = datetime.fromisoformat(value or "")
        return (
            parsed.replace(tzinfo=SHANGHAI)
            if parsed.tzinfo is None
            else parsed.astimezone(SHANGHAI)
        )
    except (ValueError, TypeError):
        return datetime.min.replace(tzinfo=SHANGHAI)


def today_metrics(data, day):
    ids = {a["id"] for a in data["accounts"]}
    rows = [
        r
        for r in data.get("results", [])
        if r.get("account_id") in ids and r.get("date") == day
    ]
    successes = {r["account_id"] for r in rows if r.get("status") == "success"}
    successes.update(
        identity
        for identity, recorded_day in data.get("success_dates", {}).items()
        if identity in ids and recorded_day == day
    )
    successes.update(
        identity
        for identity, tasks in data.get("task_success_dates", {}).items()
        if identity in ids and day in tasks.values()
    )
    latest = {}
    for result in sorted(
        rows, key=lambda r: time_rank(r.get("finished_at")), reverse=True
    ):
        latest.setdefault((result["account_id"], result.get("task_id")), result)
    failures = [r for r in latest.values() if r.get("status") == "failure"]
    failing_ids = {r["account_id"] for r in failures}
    return {
        "enabled": sum(a.get("enabled", True) for a in data["accounts"]),
        "success": len(successes),
        "failure": len(failing_ids),
        "review": len((ids - successes) | failing_ids),
        "unknown": len(ids - successes),
        "failures": failures,
        "all_tasks_confirmed": False,  # No required-task inventory exists in this app.
    }


def summary_text(counts):
    return " · ".join(
        f"{text} {counts.get(status, 0)}" for status, text in STATUSES.items()
    )


def record_batch(data, request, results, moment, state="finished"):
    row = {
        "batch_id": request["run_id"],
        "finished_at": moment.isoformat(),
        "date": moment.date().isoformat(),
        "mode": request["scope_label"],
        "scope": request.get("scope"),
        "account_count": request["count"],
        "state": state,
        "completed_count": len({r["account_id"] for r in results}),
        "summary": {
            status: sum(r.get("status") == status for r in results)
            for status in STATUSES
        },
    }
    previous = [
        v
        for v in data.get("overview_batches", [])
        if v.get("batch_id") != row["batch_id"]
    ]
    data["overview_batches"] = [row, *previous][:20]


def recent_batches(data, limit=5):
    grouped = {}
    legacy = []
    for index, result in enumerate(data.get("results", [])):
        identity = result.get("batch_id") or result.get("run_id")
        if identity:
            grouped.setdefault(identity, []).append(copy.deepcopy(result))
        else:
            legacy.append(
                {
                    "batch_id": None,
                    "key": f"legacy-{index}",
                    "finished_at": result.get("finished_at"),
                    "date": result.get("date"),
                    "mode": "旧记录（无批次标识）",
                    "account_count": 1,
                    "state": "unknown",
                    "summary": {s: int(result.get("status") == s) for s in STATUSES},
                    "results": [copy.deepcopy(result)],
                }
            )
    output, seen = [], set()
    for saved in data.get("overview_batches", []):
        identity = saved.get("batch_id")
        if not identity or identity in seen:
            continue
        seen.add(identity)
        output.append(
            {
                **copy.deepcopy(saved),
                "key": identity,
                "results": grouped.get(identity, []),
            }
        )
    for identity, results in grouped.items():
        if identity in seen:
            continue
        latest = max(results, key=lambda r: time_rank(r.get("finished_at")))
        output.append(
            {
                "batch_id": identity,
                "key": identity,
                "finished_at": latest.get("finished_at"),
                "date": latest.get("date"),
                "mode": "历史批次（方式未记录）",
                "account_count": len({r["account_id"] for r in results}),
                "state": "unknown",
                "summary": {
                    s: sum(r.get("status") == s for r in results) for s in STATUSES
                },
                "results": results,
            }
        )
    return sorted(
        [*output, *legacy], key=lambda r: time_rank(r.get("finished_at")), reverse=True
    )[:limit]
