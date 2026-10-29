"""Explicit execution scopes, immutable previews, and id-based result lookup."""

import copy
import uuid
from locations import freeze_run
from storage import now_shanghai

SCOPE_NAMES = {
    "all": "全部启用账号（包含搜索结果外的账号）",
    "selected": "本次所选账号",
    "single": "单账号临时执行",
    "today": "全部启用账号：核对今日任务并跳过已签任务",
    "automatic": "自动计划：逐账号匹配今日开放任务",
    "retry": "自动重试：明确可重试且该任务尚未成功的账号",
}


def latest_today(data, identity, day):
    return next(
        (
            r
            for r in data.get("results", [])
            if r.get("account_id") == identity and r.get("date") == day
        ),
        None,
    )


def today_succeeded(data, identity, day):
    return data.get("success_dates", {}).get(identity) == day or any(
        r.get("account_id") == identity
        and r.get("date") == day
        and r.get("status") == "success"
        for r in data.get("results", [])
    )


def make_request(data, scope="all", identities=None, moment=None):
    if scope not in SCOPE_NAMES:
        raise ValueError("未知执行范围，请重新选择操作。")
    snapshot = copy.deepcopy(data)
    day = (moment or now_shanghai()).date().isoformat()
    ids = set(identities or [])
    explicit = scope in ("single", "selected", "retry")
    accounts = [
        a
        for a in snapshot["accounts"]
        if (not explicit or a["id"] in ids)
        and (scope in ("single", "selected") or a.get("enabled", True))
    ]
    if not accounts:
        if explicit and not ids:
            reason = "尚未选择账号。请明确选择本次要执行的账号；不会退回执行全部账号。"
        elif not snapshot["accounts"]:
            reason = "还没有账号。请点击「添加账号」或导入账号文件。"
        elif scope in ("today", "automatic", "retry"):
            reason = "此范围没有参与批量与定时的账号；成功任务不会重复提交。"
        elif scope == "all":
            reason = "没有账号参与批量与定时。请打开账号参与开关，或使用行内「打卡」临时执行。"
        else:
            reason = "所选账号已不存在。请重新选择账号。"
        raise ValueError(reason)
    return {
        "run_id": uuid.uuid4().hex,
        "scope": scope,
        "scope_label": SCOPE_NAMES[scope],
        "config": snapshot,
        "accounts": freeze_run(accounts, snapshot),
        "count": len(accounts),
        "day": day,
    }


def merge_results(data, results):
    """Idempotent checkpoints also retain completed accounts across a process restart."""
    keys = {(r.get("run_id"), r["account_id"]) for r in results if r.get("run_id")}
    old = [
        r
        for r in data["results"]
        if not r.get("run_id") or (r.get("run_id"), r["account_id"]) not in keys
    ]
    incoming = copy.deepcopy(results)
    for result in incoming:
        if result.get("run_id"):
            result["record_id"] = uuid.uuid5(
                uuid.NAMESPACE_URL, result["run_id"] + ":" + result["account_id"]
            ).hex
    from history_storage import ensure_record_ids
    data["results"] = (list(reversed(incoming)) + old)[:200]
    ensure_record_ids(data["results"])
    for result in results:
        if result["status"] == "success":
            data["success_dates"][result["account_id"]] = result["date"]
            if result.get("task_id"):
                data.setdefault("task_success_dates", {}).setdefault(
                    result["account_id"], {}
                )[result["task_id"]] = result["date"]
