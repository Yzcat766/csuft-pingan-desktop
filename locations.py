"""Location configuration and selection shared by every execution entry point."""
import copy
import math
import uuid
from position_offset import OFF, INHERIT, validate_offset, resolve_offset, validate_cache


TASK_LOCATION = {"mode": "task", "location_id": None}
INHERIT_LOCATION = {"mode": "inherit", "location_id": None}


def university_location():
    """Changsha campus POI from https://amap.com/place/B02DB05TRY.

    This campus map point is editable and is still subject to task distance gates.
    """
    return {"id": "csuft-changsha-default", "name": "中南林业科技大学", "latitude": 28.131567, "longitude": 112.994905}


def coordinates(latitude, longitude):
    values = []
    for label, raw, limit in (("纬度", latitude, 90), ("经度", longitude, 180)):
        try:
            if isinstance(raw, bool) or raw is None or str(raw).strip() == "":
                raise ValueError()
            value = float(raw)
        except (TypeError, ValueError, OverflowError):
            raise ValueError(f"{label}不能为空，请填写 -{limit}～{limit} 的有限数字。") from None
        if not math.isfinite(value) or not -limit <= value <= limit:
            raise ValueError(f"{label}必须是 -{limit}～{limit} 的有限数字，请检查数值和经纬度顺序。")
        values.append(value)
    return tuple(values)


def saved_location(name, latitude, longitude, identity=None):
    name = str(name).strip() if name is not None else ""
    if not name:
        raise ValueError("位置名称不能为空，请填写便于识别的名称。")
    lat, lng = coordinates(latitude, longitude)
    return {"id": identity or uuid.uuid4().hex, "name": name, "latitude": lat, "longitude": lng}


def validate_choice(choice, locations, allow_inherit=False):
    modes = ("task", "specified", "inherit") if allow_inherit else ("task", "specified")
    if not isinstance(choice, dict) or choice.get("mode") not in modes:
        raise ValueError("定位模式无效，请在定位设置或账号编辑中重新选择。")
    if choice["mode"] == "specified" and not any(item["id"] == choice.get("location_id") for item in locations):
        raise ValueError("指定位置不存在，请保存常用位置后重新选择；不会自动改用任务默认位置。")
    return {"mode": choice["mode"], "location_id": choice.get("location_id") if choice["mode"] == "specified" else None}


def normalize_locations(data, migrate=False):
    data["global_offset"] = validate_offset(data.get("global_offset", OFF))
    for account in data.get("accounts", []):
        account["offset"] = validate_offset(account.get("offset", INHERIT), True)
    raw = data.get("locations", [])
    if not isinstance(raw, list):
        raise ValueError("常用位置配置损坏，请从 config.json.bak 恢复。")
    locations = []
    for item in raw:
        if not isinstance(item, dict) or not isinstance(item.get("id"), str) or not item["id"]:
            raise ValueError("常用位置 ID 无效，请从 config.json.bak 恢复。")
        location = saved_location(item.get("name"), item.get("latitude"), item.get("longitude"), item["id"])
        if any(previous["id"] == location["id"] for previous in locations):
            raise ValueError("常用位置 ID 重复，请从 config.json.bak 恢复。")
        locations.append(location)
    data["locations"] = locations
    data["global_location"] = validate_choice(data.get("global_location", TASK_LOCATION), locations)
    for account in data.get("accounts", []):
        # Old accounts stay on their original behavior even if a global location is later set.
        account["location"] = validate_choice(account.get("location", TASK_LOCATION if migrate else INHERIT_LOCATION), locations, True)
    validate_cache(data)


def resolve_location(account, config):
    locations = config.get("locations", [])
    choice = validate_choice(account.get("location", TASK_LOCATION), locations, True)
    source = "账号设置"
    if choice["mode"] == "inherit":
        choice = validate_choice(config.get("global_location", TASK_LOCATION), locations)
        source = "全局设置"
    if choice["mode"] == "task":
        return {"mode": "task", "name": "任务默认位置", "source": source}
    item = next(item for item in locations if item["id"] == choice["location_id"])
    lat, lng = coordinates(item["latitude"], item["longitude"])
    return {"mode": "specified", "name": item["name"], "source": source, "latitude": lat, "longitude": lng}


def freeze_run(accounts, config):
    """Freeze all accounts before the first network call, including later batch accounts."""
    frozen = copy.deepcopy(accounts)
    config = copy.deepcopy(config or {})
    for account in frozen:
        try:
            if "run_location" not in account:
                account["run_location"] = resolve_location(account, config)
            if "run_offset" not in account:
                account["run_offset"] = resolve_offset(account, config)
        except ValueError as error:
            account["location_error"] = str(error)
    return frozen
