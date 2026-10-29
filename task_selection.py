"""Resolve each account's tasks from server dates, windows and sign status."""

from datetime import date
from api import ApiError, RequestCancelled, build_sign_payload, redact
from locations import resolve_location
from storage import now_shanghai


def candidates(client, moment, rule=None, allow_late=False):
    from runner import in_time_window

    rule = rule or {"mode": "unique_current"}
    records = client.list_tasks()
    found, seen = [], set()
    for record in records:
        if getattr(client, "cancel", None) is not None and client.cancel.is_set():
            raise RequestCancelled("账号检查或任务查询已取消。")
        identity = str(record.get("taskId", ""))
        if not identity or identity in seen:
            if not identity:
                raise ApiError("任务列表缺少 task ID，不能安全选择。", "task_invalid")
            continue
        seen.add(identity)
        task = client.get_task(identity)
        status = client.get_status(identity)
        try:
            server_day = date.fromisoformat(str(status.get("signDate", "")))
        except ValueError:
            raise ApiError(
                "服务端未提供明确任务日期，需要人工核对。", "task_date_unknown"
            ) from None
        if server_day != moment.date():
            continue
        if status.get("signStatus") not in (0, 1, 2, 3, "0", "1", "2", "3"):
            raise ApiError(
                "服务端任务状态无法识别，需要人工核对。", "task_status_unknown"
            )
        if task.get("enabled") is False or task.get("deleted") is True:
            continue
        if rule.get("mode") == "exact_name" and task.get("taskName") != rule.get(
            "name"
        ):
            continue
        opened = in_time_window(
            task.get("signStartTime"), task.get("signEndTime"), moment
        )
        if not opened and not allow_late:
            continue
        found.append(
            {
                "task": task,
                "status": status,
                "task_id": identity,
                "name": task.get("taskName", identity),
                "date": server_day.isoformat(),
                "start": task.get("signStartTime"),
                "end": task.get("signEndTime"),
                "open": opened,
                "signed": status.get("signStatus") in (0, "0"),
            }
        )
    return found


def select_task(
    client,
    moment,
    rule=None,
    automatic=False,
    expected=None,
    chooser=None,
    account=None,
):
    found = candidates(client, moment, rule, allow_late=not automatic)
    if expected:
        found = [item for item in found if item["task_id"] == str(expected)]
        if not found:
            raise ApiError(
                "学校任务已变化或超出日期/时段，本次自动重试停止。",
                "retry_task_changed",
            )
    if not found:
        return None
    if len(found) == 1:
        return found[0]
    if automatic or chooser is None:
        raise ApiError(
            f"有 {len(found)} 项有效任务，无法唯一匹配。请检查账号并手动选择任务，或保存精确名称规则。",
            "task_ambiguous",
        )
    selected = chooser(
        account,
        [
            {k: v for k, v in item.items() if k not in ("task", "status")}
            for item in found
        ],
    )
    if selected is None:
        raise RequestCancelled("已取消任务选择，未提交打卡。")
    for item in found:
        if item["task_id"] == selected:
            return item
    raise ApiError("选择的任务不在本次候选列表中，未提交。", "task_invalid")


def check_accounts(accounts, config, emit, cancel, client_factory):
    """Read-only check: never calls sign or submits any school payload."""
    results = []
    for account in accounts:
        if cancel.is_set():
            break
        client = None
        value = {
            "account_id": account["id"],
            "label": account["label"],
            "login_ok": False,
            "tasks": [],
            "issues": [],
        }
        secrets = [account.get("openid", "")]
        try:
            if account.get("credential_error") or not account.get("openid"):
                raise ApiError("凭据不可用，请编辑账号或恢复密码备份。")
            client = client_factory(account["openid"])
            client.cancel = cancel
            auth = client.login()
            secrets += [getattr(client, "token", ""), auth.get("access_token", "")]
            value.update(
                login_ok=True,
                identity={
                    k: auth.get(k, "") for k in ("userName", "schoolName", "accountNo")
                },
            )
            items = candidates(client, now_shanghai(), allow_late=True)
            value["tasks"] = [
                {k: v for k, v in item.items() if k not in ("task", "status")}
                for item in items
            ]
            location = resolve_location(account, config)
            for item in items:
                if location["mode"] == "specified":
                    try:
                        build_sign_payload(
                            item["task"],
                            item["status"],
                            location["latitude"],
                            location["longitude"],
                        )
                    except ApiError as error:
                        value["issues"].append(f"{item['name']}：{error}")
                else:
                    value["issues"].append(
                        "任务默认位置将在执行时确定；检查不能确认实际提交位置。"
                    )
            if not items:
                value["issues"].append("没有今日可确认的任务；不代表实际打卡成功。")
        except RequestCancelled:
            break
        except Exception as error:
            value["issues"].append(redact(str(error), secrets)[:300])
        finally:
            if client:
                client.close()
        value["issues"] = [redact(s, secrets) for s in value["issues"]]
        value["identity"] = {
            k: redact(str(v), secrets) for k, v in value.get("identity", {}).items()
        }
        results.append(value)
        emit(value)
    return results
