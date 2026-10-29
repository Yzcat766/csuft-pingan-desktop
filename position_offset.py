"""Bounded, local geodesic candidate selection; never contacts the school."""

import copy
import math
import random

EARTH_RADIUS = 6378137
MAX_RADIUS = 500.0
STAGE_FACTORS = (1.0, 0.5, 0.25, 0.125)
CANDIDATES_PER_STAGE = 8
MAX_CANDIDATES = 33
OFF = {"mode": "off", "radius": 0.0}
INHERIT = {"mode": "inherit", "radius": 0.0}


def validate_offset(value, inherit=False):
    if not isinstance(value, dict) or value.get("mode") not in (
        ("off", "on", "inherit") if inherit else ("off", "on")
    ):
        raise ValueError("请选择有效的位置偏移规则。")
    raw = value.get("radius", 0)
    try:
        if isinstance(raw, bool) or raw is None or str(raw).strip() == "":
            raise ValueError()
        radius = float(raw)
    except (ValueError, TypeError, OverflowError):
        raise ValueError("偏移半径应为 0～500 米的有限数字。") from None
    if not math.isfinite(radius) or not 0 <= radius <= MAX_RADIUS:
        raise ValueError("偏移半径应为 0～500 米的有限数字。")
    return {"mode": value["mode"], "radius": radius}


def resolve_offset(account, config):
    value = validate_offset(account.get("offset", INHERIT), True)
    source = "账号设置"
    if value["mode"] == "inherit":
        value = validate_offset(config.get("global_offset", OFF))
        source = "全局设置"
    return {**value, "source": source}


def arc_distance(a, b):
    lat1, lat2 = math.radians(a[0]), math.radians(b[0])
    angle = (
        math.sin((lat2 - lat1) / 2) ** 2
        + math.cos(lat1) * math.cos(lat2) * math.sin(math.radians(b[1] - a[1]) / 2) ** 2
    )
    return 2 * EARTH_RADIUS * math.asin(math.sqrt(min(1, max(0, angle))))


def candidate(center, radius, rng):
    # Uniform area on a spherical cap, with a uniformly distributed bearing.
    u, v = rng.random(), rng.random()
    if not (0 <= u < 1 and 0 <= v < 1):
        raise ValueError("随机源返回无效数值。")
    angular = 2 * math.asin(math.sqrt(u) * math.sin(radius / EARTH_RADIUS / 2))
    bearing = v * math.tau
    lat, lng = map(math.radians, center)
    # 3D tangent basis remains defined at both poles.
    p = (math.cos(lat) * math.cos(lng), math.cos(lat) * math.sin(lng), math.sin(lat))
    north = (
        -math.sin(lat) * math.cos(lng),
        -math.sin(lat) * math.sin(lng),
        math.cos(lat),
    )
    east = (-math.sin(lng), math.cos(lng), 0.0)
    q = [
        math.cos(angular) * p[i]
        + math.sin(angular)
        * (math.cos(bearing) * north[i] + math.sin(bearing) * east[i])
        for i in range(3)
    ]
    return math.degrees(math.atan2(q[2], math.hypot(q[0], q[1]))), math.degrees(
        math.atan2(q[1], q[0])
    )


def validate_selected(task, value):
    from api import ApiError, location_details
    from locations import coordinates

    center = coordinates(value["center_latitude"], value["center_longitude"])
    point = coordinates(value["latitude"], value["longitude"])
    radius = validate_offset({"mode": "on", "radius": value["radius"]})["radius"]
    offset = arc_distance(center, point)
    if offset > radius:
        raise ApiError("最终坐标超出配置偏移半径，停止提交。", "offset_local")
    details = location_details(task, *point)
    margin = min(1.0, details["allowed_distance"] * 0.05)
    if details["distance"] + margin >= details["allowed_distance"]:
        raise ApiError("最终坐标不满足任务距离与安全余量，停止提交。", "offset_local")
    return {**value, **details, "offset_distance": offset, "margin": margin}


def choose_position(
    task, center, radius, rng=None, conservative=False, report=None, cancel=None
):
    from api import ApiError, RequestCancelled, location_details
    from locations import coordinates

    center = coordinates(*center)
    radius = validate_offset({"mode": "on", "radius": radius})["radius"]
    reference = location_details(
        task, *center
    )  # Reject invalid school rules before sampling.
    margin = min(1.0, reference["allowed_distance"] * 0.05)
    rng = rng or random.SystemRandom()
    stages = []
    for index, factor in enumerate(
        STAGE_FACTORS if radius and not conservative else ()
    ):
        tried = 0
        for _ in range(CANDIDATES_PER_STAGE):
            if cancel is not None and cancel.is_set():
                raise RequestCancelled("候选生成已取消，未提交。")
            tried += 1
            point = candidate(center, radius * factor, rng)
            value = {
                "latitude": point[0],
                "longitude": point[1],
                "center_latitude": center[0],
                "center_longitude": center[1],
                "radius": radius,
                "stage": "原半径" if index == 0 else "缩小范围",
                "factor": factor,
                "adjustment_reason": (
                    "服务端明确距离拒绝后的中心点调整"
                    if conservative
                    else "本地候选选择"
                ),
            }
            try:
                selected = validate_selected(task, value)
            except ApiError as error:
                if error.code != "offset_local":
                    raise
                continue
            stages.append(
                {
                    "factor": factor,
                    "tried": tried,
                    "rejected": tried - 1,
                    "reason": "距离/余量或半径校验",
                }
            )
            if report:
                report(stages)
            return {
                **selected,
                "candidate_stages": stages,
                "candidate_count": sum(s["tried"] for s in stages),
            }
        stages.append(
            {
                "factor": factor,
                "tried": tried,
                "rejected": tried,
                "reason": "距离/余量或半径校验",
            }
        )
    if cancel is not None and cancel.is_set():
        raise RequestCancelled("中心点校验已取消，未提交。")
    stages.append({"factor": 0, "tried": 1, "rejected": 0, "reason": "中心点兜底"})
    value = {
        "latitude": center[0],
        "longitude": center[1],
        "center_latitude": center[0],
        "center_longitude": center[1],
        "radius": radius,
        "stage": "中心点",
        "factor": 0,
        "adjustment_reason": (
            "服务端明确距离拒绝，改用原中心点"
            if conservative
            else "半径为零或有限候选耗尽"
        ),
    }
    try:
        selected = validate_selected(task, value)
    except ApiError:
        stages[-1]["rejected"] = 1
        if report:
            report(stages)
        raise ApiError(
            f"本地候选全部不合格（{sum(s['tried'] for s in stages)} 次）；中心距任务位置 {reference['distance']:g} 米，须小于 {reference['allowed_distance']:g} 米，余量 {margin:g} 米。请调整中心坐标或半径后重试。",
            "offset_local",
        ) from None
    if report:
        report(stages)
    return {
        **selected,
        "candidate_stages": stages,
        "candidate_count": sum(s["tried"] for s in stages),
    }


def checkpoint_position(data, value):
    """Keep current-date task snapshots for each existing account, plus its latest entry."""
    from storage import now_shanghai

    today = now_shanghai().date().isoformat()
    ids = {a["id"] for a in data.get("accounts", [])}
    cache = data.setdefault("position_cache", {})
    for identity in list(cache):
        if identity not in ids or cache[identity].get("date") != today:
            del cache[identity]
    if value["account_id"] in ids and value["date"] == today:
        previous = cache.get(value["account_id"], {})
        tasks = copy.deepcopy(previous.get("task_positions", {}))
        if previous.get("task_id") and previous.get("date") == today:
            tasks[previous["task_id"]] = {
                k: copy.deepcopy(v)
                for k, v in previous.items()
                if k != "task_positions"
            }
        snapshot = {
            k: copy.deepcopy(v) for k, v in value.items() if k != "task_positions"
        }
        tasks[value["task_id"]] = snapshot
        if len(tasks) > 100:
            raise ValueError("当天位置任务检查点超过 100 项，请人工核对，未提交。")
        cache[value["account_id"]] = {
            **copy.deepcopy(snapshot),
            "task_positions": tasks,
        }


def validate_cache(data):
    from datetime import date
    from locations import coordinates
    from storage import now_shanghai

    cache = data.get("position_cache", {})
    if not isinstance(cache, dict):
        raise ValueError("位置重试检查点损坏，请恢复兼容备份。")
    ids = {a["id"] for a in data.get("accounts", [])}
    checked = {}
    for identity, latest in cache.items():
        if identity not in ids:
            continue
        if (
            not isinstance(latest, dict)
            or not isinstance(latest.get("task_positions", {}), dict)
            or len(latest.get("task_positions", {})) > 100
        ):
            raise ValueError("位置重试检查点损坏，请恢复兼容备份。")
        entries = [(None, latest), *latest.get("task_positions", {}).items()]
        for task_key, value in entries:
            _validate_entry(identity, task_key, value, coordinates, date, now_shanghai)
        if latest["date"] == now_shanghai().date().isoformat():
            checked[identity] = latest
    data["position_cache"] = checked


def _validate_entry(identity, task_key, value, coordinates, date, now_shanghai):
    try:
        if (
            not isinstance(value, dict)
            or value["account_id"] != identity
            or not isinstance(value["task_id"], str)
            or not value["task_id"]
        ):
            raise ValueError()
        if task_key is not None and task_key != value["task_id"]:
            raise ValueError()
        if date.fromisoformat(value["date"]).isoformat() != value["date"]:
            raise ValueError()
        if value["date"] != now_shanghai().date().isoformat():
            return
        if (
            type(value["unresolved"]) is not bool
            or type(value["adjustments"]) is not int
            or not 0 <= value["adjustments"] <= 1
        ):
            raise ValueError()
        if (
            any(
                type(value[k]) is not int
                for k in ("submission_count", "max_submissions")
            )
            or not 0 <= value["submission_count"] <= value["max_submissions"] <= 7
        ):
            raise ValueError()
        rule = validate_offset(value["offset_rule"])
        if value["location"]["mode"] not in ("task", "specified"):
            raise ValueError()
        position = value["position"]
        center = coordinates(position["center_latitude"], position["center_longitude"])
        point = coordinates(position["latitude"], position["longitude"])
        if rule["mode"] == "on" and (
            position["radius"] != rule["radius"]
            or arc_distance(center, point) > rule["radius"]
        ):
            raise ValueError()
    except (KeyError, TypeError, ValueError, OverflowError):
        raise ValueError("位置重试检查点损坏，请恢复兼容备份。") from None


def offset_description(value):
    if value.get("mode") == "on":
        return f"{value.get('source', '')} · 偏移半径 {value['radius']:g} 米 · 有限候选 → 缩小 → 中心点"
    return f"{value.get('source', '')} · 可选偏移关闭"
