import random
import threading
import uuid

from api import (
    SchoolClient,
    ApiError,
    RequestCancelled,
    build_sign_payload,
    location_details,
    redact,
)
from locations import coordinates, freeze_run
from position_offset import choose_position, validate_selected, arc_distance
from storage import now_shanghai


class Cancelled(RuntimeError):
    pass


def in_time_window(start, end, moment):
    def seconds(value):
        try:
            parts = [int(item) for item in value.split(":")]
        except (AttributeError, ValueError):
            raise ValueError(
                "任务签到时段格式无效，请联系学校核对 HH:mm:ss 格式的任务时间。"
            ) from None
        if (
            len(parts) != 3
            or not 0 <= parts[0] < 24
            or any(not 0 <= n < 60 for n in parts[1:])
        ):
            raise ValueError(
                "任务签到时段格式无效，请联系学校核对 HH:mm:ss 格式的任务时间。"
            )
        return parts[0] * 3600 + parts[1] * 60 + parts[2]

    first, last = seconds(start), seconds(end)
    current = moment.hour * 3600 + moment.minute * 60 + moment.second
    return (
        first <= current <= last
        if first <= last
        else current >= first or current <= last
    )


def run_accounts(
    accounts,
    emit,
    cancel=None,
    scheduled=False,
    success_dates=None,
    client_factory=SchoolClient,
    delay_scale=1.0,
    location_config=None,
    preview_handler=None,
    run_id=None,
    retry_context=None,
    task_chooser=None,
    position_handler=None,
    random_source=None,
):
    accounts = freeze_run(accounts, location_config)
    cancel = cancel or threading.Event()
    success_dates = success_dates or {}
    run_id = run_id or uuid.uuid4().hex
    sequence = 0
    results = []
    today = now_shanghai().date().isoformat()
    raw_emit = emit
    secrets = [item["openid"] for item in accounts if item.get("openid")]

    def sanitize(value):
        if isinstance(value, str):
            value = redact(value, secrets)
        elif isinstance(value, dict):
            return {key: sanitize(item) for key, item in value.items()}
        elif isinstance(value, list):
            return [sanitize(item) for item in value]
        return value

    def emit(kind, value):
        raw_emit(kind, sanitize(value))

    def log(message, level="info", phase=None):
        nonlocal sequence
        sequence += 1
        event = {
            "batch_id": run_id,
            "account_id": identity,
            "account_label": label,
            "time": now_shanghai().isoformat(),
            "seq": sequence,
            "level": level,
            "step": phase or step,
            "message": message,
        }
        emit("log_event", event)
        emit("log", f"[{label}] {message}")

    def wait(low, high):
        if cancel.wait(random.uniform(low, high) * delay_scale):
            raise Cancelled("本次执行已取消。")

    for account in accounts:
        identity, label = account["id"], account["label"]
        result = {
            "account_id": identity,
            "label": label,
            "status": "failure",
            "message": "",
            "user_name": "",
            "school": "",
            "account_no": "",
            "task": "",
            "date": today,
            "finished_at": None,
        }
        result.update(
            batch_id=run_id,
            run_id=run_id,
            retryable=False,
            uncertain=False,
            error_code=None,
        )
        step = "定位配置"
        client = None
        try:
            if cancel.is_set():
                raise Cancelled("本次执行已取消。")
            if account.get("location_error"):
                raise ApiError(account["location_error"])
            selected_location = account["run_location"]
            result["location"] = selected_location
            log(
                f"定位：{selected_location['source']} / {selected_location['name']}；{'使用指定位置' if selected_location['mode'] == 'specified' else '使用任务默认位置'}。"
            )
            if account.get("credential_error") or not account.get("openid"):
                raise ApiError(
                    "凭据不可用，请编辑账号或恢复加密备份。", "authentication"
                )
            step = "登录"
            emit("progress", {"account_id": identity, "stage": "等待登录"})
            log(f"开始执行。")
            wait(5, 7.5)
            client = client_factory(account["openid"])
            client.cancel = cancel
            client.journal = lambda phase, message: log(message, phase=phase)
            auth = client.login()
            if getattr(client, "token", None):
                secrets.append(client.token)
            if auth.get("access_token"):
                secrets.append(auth["access_token"])
            result.update(
                user_name=auth.get("userName", ""),
                school=auth.get("schoolName", ""),
                account_no=auth.get("accountNo", ""),
            )
            log(f"登录成功：{result['user_name']} / {result['school']}。")
            wait(1, 3)
            step = "任务查询"
            log("查询学校任务列表及详情。")
            from task_selection import select_task

            expected = (retry_context or {}).get("task_ids", {}).get(identity)
            chosen = select_task(
                client,
                now_shanghai(),
                (
                    (location_config or {}).get("schedule", {}).get("task_rule")
                    if scheduled
                    else None
                ),
                scheduled,
                expected,
                task_chooser,
                {"id": identity, "label": label},
            )
            if not chosen:
                if retry_context:
                    raise ApiError(
                        "没有匹配的重试任务，请人工检查任务日期及开放时段。",
                        "retry_task_changed",
                    )
                result.update(status="skipped", message="当前没有可用签到任务。")
            else:
                wait(1, 3)
                task = chosen["task"]
                result["task"] = task.get("taskName", "")
                result.update(
                    task_id=str(task["taskId"]),
                    task_window={
                        "start": task.get("signStartTime"),
                        "end": task.get("signEndTime"),
                    },
                )
                emit(
                    "task",
                    {
                        "account_id": identity,
                        "task_id": str(task["taskId"]),
                        "window": result["task_window"],
                    },
                )
                emit("progress", {"account_id": identity, "stage": "核对签到状态"})
                log(
                    f"任务：{result['task']}；时段 {task.get('signStartTime', '')}–{task.get('signEndTime', '')}。"
                )
                outside_window = not in_time_window(
                    task.get("signStartTime"),
                    task.get("signEndTime"),
                    now_shanghai(),
                )
                if retry_context and (
                    retry_context.get("date") != now_shanghai().date().isoformat()
                    or outside_window
                ):
                    raise ApiError(
                        "本次自动重试已超过允许日期或任务时段，请核对任务后手动执行。",
                        "retry_rule",
                    )
                if outside_window:
                    log(f"当前在常规签到时段外，将由服务端判断是否允许补签。")
                wait(1, 3)
                step = "服务端查询"
                status = client.get_status(task["taskId"])
                if status.get("signStatus") not in (0, "0", 1, "1", 2, "2", 3, "3"):
                    raise ApiError(
                        "服务端签到状态无法识别，停止提交。", "task_status_unknown"
                    )
                if status.get("signDate") != chosen["date"]:
                    raise ApiError(
                        "查询期间服务端任务日期变化，需要人工核对。",
                        "task_date_changed",
                    )
                if retry_context and status.get("signDate") != retry_context.get(
                    "date"
                ):
                    raise ApiError(
                        "服务端任务日期已变化，本次自动重试停止。请核对学校任务日期。",
                        "retry_date_changed",
                    )
                result["date"] = status.get("signDate") or today
                result["server_status"] = {
                    key: status.get(key) for key in ("signStatus", "signDate")
                }
                log(f"服务端查询结果：{result['server_status']}。")
                if status.get("signStatus") in (0, "0"):
                    result.update(
                        status="success",
                        success_kind="already_signed",
                        message="服务端确认已签到，未重复提交。",
                    )
                else:
                    step = "定位校验"
                    offset_rule = account["run_offset"]
                    cached = (
                        (location_config or {}).get("position_cache", {}).get(identity)
                    )
                    if cached and cached.get("task_id") != result["task_id"]:
                        cached = cached.get("task_positions", {}).get(result["task_id"])
                    use_cache = bool(
                        cached
                        and cached.get("task_id") == result["task_id"]
                        and cached.get("date") == result["date"]
                        and cached.get("account_id") == identity
                        and (retry_context or cached.get("unresolved"))
                    )
                    if use_cache:
                        # An in-flight retry uses its saved location and offset snapshot.
                        selected_location = cached["location"]
                        offset_rule = cached["offset_rule"]
                        result["location"] = selected_location
                    if selected_location["mode"] == "specified":
                        lat, lng = (
                            selected_location["latitude"],
                            selected_location["longitude"],
                        )
                    else:
                        dorm = task.get("dormitoryRegisterVO") or {}
                        try:
                            base_lat, base_lng = coordinates(
                                dorm.get("locationLat"), dorm.get("locationLng")
                            )
                            lat, lng = base_lat, base_lng
                            if offset_rule["mode"] != "on" and not use_cache:
                                lat, lng = round(
                                    base_lat + random.random() * 0.001, 6
                                ), round(base_lng + random.random() * 0.001, 6)
                        except ValueError as error:
                            raise ApiError(
                                f"任务宿舍位置无效：{error} 请核对学校任务信息。"
                            ) from None
                    center = (lat, lng)
                    # Check unrelated business gates before generating any candidates.
                    if offset_rule["mode"] == "on" and (
                        task.get("openTakePhoto") in (1, "1")
                        or (
                            outside_window
                            and task.get("isLateStuTakePhoto") in (1, "1")
                        )
                    ):
                        photo_preview = {
                            "account_id": identity,
                            "label": label,
                            "task": result["task"],
                            "task_id": result["task_id"],
                            "date": result["date"],
                            "start": task.get("signStartTime"),
                            "end": task.get("signEndTime"),
                            **selected_location,
                            "latitude": lat,
                            "longitude": lng,
                            "distance": None,
                            "allowed_distance": None,
                            "allowed": False,
                            "reason": "任务要求照片，未生成偏移候选，请使用学校官方入口。",
                        }
                        result["preview"] = photo_preview
                        emit("preview", photo_preview)
                        raise ApiError(
                            "此任务要求上传照片，请使用学校官方入口完成签到。",
                            "photo_required",
                        )

                    def candidate_report(stages):
                        result["candidate_stages"] = stages
                        result["candidate_count"] = sum(s["tried"] for s in stages)
                        log(
                            "本地候选阶段："
                            + "；".join(
                                f"比例 {s['factor']:g}：尝试 {s['tried']}，淘汰 {s['rejected']}（{s['reason']}）"
                                for s in stages
                            ),
                            phase="偏移候选",
                        )

                    position = {}
                    if use_cache:
                        position = dict(cached["position"])
                        if offset_rule["mode"] == "on":
                            position = validate_selected(task, position)
                        lat, lng = coordinates(
                            position["latitude"], position["longitude"]
                        )
                        log(
                            "复用同账号、同任务、同服务端日期的已保存坐标；配置编辑不改变本次重试。",
                            phase="位置恢复",
                        )
                    elif offset_rule["mode"] == "on":
                        result["position_origin"] = {
                            **location_details(task, *center),
                            "center_latitude": center[0],
                            "center_longitude": center[1],
                            "radius": offset_rule["radius"],
                        }
                        position = choose_position(
                            task,
                            center,
                            offset_rule["radius"],
                            random_source,
                            report=candidate_report,
                            cancel=cancel,
                        )
                        lat, lng = position["latitude"], position["longitude"]
                    else:
                        position = {
                            "latitude": lat,
                            "longitude": lng,
                            "center_latitude": lat,
                            "center_longitude": lng,
                            "radius": 0,
                            "stage": "原有定位",
                            "offset_distance": 0,
                            "adjustment_reason": "可选偏移关闭，保持原有定位行为",
                            "candidate_count": 0,
                        }
                    position_state = {
                        "account_id": identity,
                        "task_id": result["task_id"],
                        "date": result["date"],
                        "position": position,
                        "location": selected_location,
                        "offset_rule": offset_rule,
                        "adjustments": cached.get("adjustments", 0) if use_cache else 0,
                        "submission_count": (
                            cached.get("submission_count", 0) if use_cache else 0
                        ),
                        "max_submissions": (
                            cached.get("max_submissions", 2)
                            if use_cache
                            else 2
                            + (
                                (location_config or {})
                                .get("schedule", {})
                                .get("retry", {})
                                .get("max_retries", 2)
                                if scheduled
                                else 0
                            )
                        ),
                        "unresolved": True,
                        "candidate_total": (
                            cached.get(
                                "candidate_total", position.get("candidate_count", 0)
                            )
                            if use_cache
                            else position.get("candidate_count", 0)
                        ),
                    }
                    result["position_state"] = position_state
                    result["candidate_count"] = position_state["candidate_total"]
                    preview = {
                        "account_id": identity,
                        "label": label,
                        "task": result["task"],
                        "task_id": result["task_id"],
                        "date": result["date"],
                        "start": task.get("signStartTime"),
                        "end": task.get("signEndTime"),
                        **selected_location,
                        **position,
                        "latitude": lat,
                        "longitude": lng,
                        "distance": None,
                        "allowed_distance": None,
                        "allowed": False,
                        "reason": "",
                    }
                    result["preview"] = preview
                    preview["candidate_count"] = position_state["candidate_total"]
                    try:
                        preview.update(location_details(task, lat, lng))
                        build_sign_payload(task, status, lat, lng)
                        if outside_window and task.get("isLateStuTakePhoto") in (
                            1,
                            "1",
                        ):
                            raise ApiError(
                                "此任务要求补签上传照片，请使用学校官方入口完成签到。"
                            )
                        preview.update(
                            allowed=True,
                            reason=(
                                "校验通过；常规时段外是否允许补签由服务端判断。"
                                if outside_window
                                else "本地校验通过，提交结果以服务端查询为准。"
                            ),
                        )
                    except ApiError as error:
                        preview["reason"] = str(error)
                    emit("preview", preview)
                    log(
                        f"提交前预览：{preview['name']}；纬度 {lat!r}；经度 {lng!r}；距离 {preview['distance']} 米；须小于 {preview['allowed_distance']} 米；{preview['reason']}"
                    )
                    if not preview["allowed"]:
                        raise ApiError(preview["reason"])
                    if preview_handler is not None and not preview_handler(
                        sanitize(preview)
                    ):
                        raise Cancelled("已取消本账号的提交前预览，未发送签到请求。")
                    wait(1, 3)
                    emit(
                        "progress",
                        {"account_id": identity, "stage": "提交并核对结果"},
                    )

                    def submission_guard(actual_status):
                        if cancel.is_set():
                            raise RequestCancelled("本次执行已取消，停止提交。")
                        if actual_status.get("signStatus") not in (
                            0,
                            "0",
                            1,
                            "1",
                            2,
                            "2",
                            3,
                            "3",
                        ):
                            raise ApiError(
                                "服务端签到状态无法识别，停止提交。",
                                "task_status_unknown",
                            )
                        moment = now_shanghai()
                        if (
                            actual_status.get("signDate") != result["date"]
                            or moment.date().isoformat() != result["date"]
                        ):
                            raise ApiError(
                                "提交前任务日期已变化，停止提交，请重新查询任务。",
                                "task_date_changed",
                            )
                        if scheduled and not in_time_window(
                            task.get("signStartTime"), task.get("signEndTime"), moment
                        ):
                            raise ApiError(
                                "自动任务已超出开放时段，停止提交。", "retry_rule"
                            )
                        if not retry_context:
                            return
                        if (
                            retry_context.get("date") != moment.date().isoformat()
                            or actual_status.get("signDate")
                            != retry_context.get("date")
                            or not in_time_window(
                                task.get("signStartTime"),
                                task.get("signEndTime"),
                                moment,
                            )
                        ):
                            raise ApiError(
                                "重试提交前日期或任务时段已变化，本次停止。请核对任务后手动执行。",
                                "retry_rule",
                            )

                    submission_guard(status)
                    client.submission_guard = submission_guard
                    step = "提交"
                    log("准备提交；提交前后都会查询服务端实际状态。")

                    def save_position():
                        if position_handler is None:
                            emit("position", position_state)
                        if (
                            position_handler is not None
                            and position_handler(sanitize(position_state)) is False
                        ):
                            raise ApiError(
                                "提交前位置检查点保存失败，未发送本次请求。",
                                "position_save",
                            )

                    def submit_position():
                        if (
                            position_state["submission_count"]
                            >= position_state["max_submissions"]
                        ):
                            raise ApiError(
                                "已达到本任务持久化提交次数上限，请查看日志并人工核对。",
                                "submission_limit",
                            )
                        submission_guard(status)
                        build_sign_payload(task, status, lat, lng)
                        if offset_rule["mode"] == "on":
                            validate_selected(task, position)
                        position_state["submission_count"] += 1
                        result["submission_count"] = position_state["submission_count"]
                        save_position()
                        if cancel.is_set():
                            raise RequestCancelled("检查点保存期间已取消，未提交。")
                        return client.sign(task, lat, lng)

                    try:
                        checked = submit_position()
                    except ApiError as error:
                        if (
                            error.code != "position_rejected"
                            or error.uncertain
                            or offset_rule["mode"] != "on"
                            or position_state["adjustments"] >= 1
                        ):
                            raise
                        log(
                            f"服务端明确距离拒绝：{error}；先查询状态，再决定是否进行唯一一次中心点调整。",
                            "warning",
                            "服务端位置拒绝",
                        )
                        status = client.get_status(task["taskId"])
                        submission_guard(status)
                        if status.get("signStatus") in (0, "0"):
                            checked = {**status, "_success_kind": "already_signed"}
                        else:
                            from task_selection import select_task

                            fresh = select_task(
                                client,
                                now_shanghai(),
                                automatic=True,
                                expected=result["task_id"],
                            )
                            if not fresh or fresh["date"] != result["date"]:
                                raise ApiError(
                                    "位置调整前任务或日期已变化，停止。",
                                    "task_date_changed",
                                )
                            task = fresh["task"]
                            result["task_window"] = {
                                "start": task.get("signStartTime"),
                                "end": task.get("signEndTime"),
                            }
                            status = fresh["status"]
                            submission_guard(status)
                            if status.get("signStatus") in (0, "0"):
                                checked = {**status, "_success_kind": "already_signed"}
                            else:
                                position_state["adjustments"] = 1
                                position = choose_position(
                                    task,
                                    (
                                        position["center_latitude"],
                                        position["center_longitude"],
                                    ),
                                    position["radius"],
                                    random_source,
                                    conservative=True,
                                    report=candidate_report,
                                    cancel=cancel,
                                )
                                # Identical coordinates cannot fix a server distance rejection.
                                if (position["latitude"], position["longitude"]) == (
                                    lat,
                                    lng,
                                ):
                                    raise error
                                position_state["position"] = position
                                position_state["candidate_total"] += position[
                                    "candidate_count"
                                ]
                                result["candidate_count"] = position_state[
                                    "candidate_total"
                                ]
                                lat, lng = position["latitude"], position["longitude"]
                                build_sign_payload(task, status, lat, lng)
                                preview.update(
                                    position,
                                    reason="服务端明确拒绝后，重新核对任务并通过中心点校验。",
                                )
                                preview["candidate_count"] = position_state[
                                    "candidate_total"
                                ]
                                preview.update(
                                    start=task.get("signStartTime"),
                                    end=task.get("signEndTime"),
                                )
                                emit("preview", preview)
                                log(
                                    f"唯一一次位置调整：中心点 {lat!r}, {lng!r}；距离 {position['distance']} 米；余量 {position['margin']} 米。",
                                    phase="位置调整",
                                )
                                checked = submit_position()
                    step = "服务端复查"
                    submission_guard(checked)
                    result["server_status"] = {
                        key: checked.get(key) for key in ("signStatus", "signDate")
                    }
                    log(f"提交后服务端查询结果：{result['server_status']}。")
                    if checked.get("signStatus") not in (0, "0"):
                        raise ApiError(
                            "提交后服务端仍显示未签到，请在学校官方入口核对结果。"
                        )
                    result["date"] = checked.get("signDate") or result["date"]
                    result.update(status="success", message="服务端确认签到成功。")
                    result["success_kind"] = checked.get("_success_kind", "submitted")
                    position_state["unresolved"] = False
        except (Cancelled, RequestCancelled) as error:
            result.update(status="cancelled", message=str(error))
        except Exception as error:
            if client and getattr(client, "token", None):
                secrets.append(client.token)
            result["message"] = sanitize(str(error))[:300]
            result.update(
                error_code=getattr(error, "code", "unexpected"),
                retryable=getattr(error, "retryable", False),
                uncertain=getattr(error, "uncertain", False),
            )
            if (
                result.get("position_state")
                and not result["retryable"]
                and not result["uncertain"]
            ):
                result["position_state"]["unresolved"] = False
        finally:
            if result.get("position_state"):
                if result["status"] == "cancelled":
                    result["position_state"]["unresolved"] = False
                emit("position", result["position_state"])
            if client:
                try:
                    client.close()
                except Exception:
                    log(f"网络会话已结束。")
            result["finished_at"] = now_shanghai().isoformat()
            result = sanitize(result)
            results.append(result)
            emit("result", result)
            log(
                result["message"],
                (
                    "error"
                    if result["status"] == "failure"
                    else (
                        "warning"
                        if result["status"] in ("cancelled", "skipped")
                        else "info"
                    )
                ),
                "结果",
            )
    return results
