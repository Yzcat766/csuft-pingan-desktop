"""Password-authenticated portable backup and transactional local recovery."""

import base64
import copy
import hashlib
import json
import os
import shutil
import tempfile
import uuid
from pathlib import Path, PurePosixPath
from cryptography.fernet import Fernet, InvalidToken
from cryptography.hazmat.primitives.kdf.scrypt import Scrypt
from security import encode_config
from storage import Store, defaults, now_shanghai, validate_schedule
from locations import normalize_locations
from logbook import atomic_json, clean
from history_storage import HISTORY_KEYS, JOURNAL, history_value

MAX_BYTES = 64 * 1024 * 1024
LOG_ROOTS = ("运行日志", "总日志")


def summary(data):
    return f"账号 {len(data.get('accounts', []))} · 位置 {len(data.get('locations', []))} · 计划 {int(bool(data.get('schedule')))} · 历史 {len(data.get('results', []))}"


def safe_relative(name):
    if not isinstance(name, str) or "\\" in name or ":" in name:
        raise ValueError("备份路径无效。")
    path = PurePosixPath(name)
    if (
        path.is_absolute()
        or any(p in ("", ".", "..") for p in name.split("/"))
        or path.parts[0] not in LOG_ROOTS
    ):
        raise ValueError("备份包含不允许的路径。")
    if path.suffix not in (".json", ".log"):
        raise ValueError("备份日志文件类型无效。")
    return path


def validate_data(data):
    if not isinstance(data, dict) or data.get("version") not in (1, 2, 3):
        raise ValueError("备份配置版本不兼容。")
    if not isinstance(data.get("accounts"), list) or not isinstance(
        data.get("results", []), list
    ):
        raise ValueError("备份配置格式无效。")
    ids = set()
    for account in data["accounts"]:
        if (
            not isinstance(account, dict)
            or not isinstance(account.get("id"), str)
            or not account["id"]
            or account["id"] in ids
            or not isinstance(account.get("openid"), str)
            or not account["openid"].strip()
        ):
            raise ValueError(
                "备份账号缺少有效凭据或 ID；不能使用本地 DPAPI 密文冒充可迁移备份。"
            )
        if (
            not isinstance(account.get("label", "新账号"), str)
            or type(account.get("enabled", True)) is not bool
            or any(c.isspace() for c in account["openid"])
        ):
            raise ValueError("备份账号名称、启用状态或凭据格式无效。")
        ids.add(account["id"])
    merged = {**defaults(), **copy.deepcopy(data)}
    for section in ("appearance", "reliability", "notifications", "maintenance"):
        if not isinstance(merged.get(section), dict):
            raise ValueError("备份偏好格式无效。")
        merged[section] = {**defaults()[section], **merged[section]}
    if merged["appearance"]["theme"] not in ("dark", "light"):
        raise ValueError("备份主题无效。")
    for section, keys in (
        ("reliability", ("autostart", "start_hidden", "prevent_sleep")),
        ("notifications", ("enabled", "success")),
    ):
        if any(type(merged[section][key]) is not bool for key in keys):
            raise ValueError("备份开关偏好无效。")
    days = merged["maintenance"]["retention_days"]
    if type(days) is not int or not 7 <= days <= 3650:
        raise ValueError("备份日志保留天数无效。")
    for result in merged["results"]:
        if (
            not isinstance(result, dict)
            or result.get("status")
            not in ("success", "failure", "cancelled", "skipped")
            or not isinstance(result.get("account_id"), str)
            or not isinstance(result.get("date"), str)
        ):
            raise ValueError("备份历史记录无效。")
        for key in ("label", "message", "task", "user_name", "school", "account_no"):
            if not isinstance(result.get(key, ""), str):
                raise ValueError("备份历史文字字段无效。")
    for key in ("success_dates", "task_success_dates"):
        if not isinstance(merged[key], dict):
            raise ValueError("备份成功记录格式无效。")
    merged["schedule"] = validate_schedule(merged["schedule"])
    normalize_locations(merged, migrate=True)
    history = history_value(merged)
    merged.update({k: history[k] for k in HISTORY_KEYS})
    return merged


def password_cipher(password, salt):
    if not isinstance(password, str) or len(password) < 8:
        raise ValueError("备份密码至少 8 个字符；请自行保管，软件不保存密码。")
    # Fixed bounded parameters prevent malicious headers from requesting excessive KDF work.
    key = Scrypt(salt=salt, length=32, n=2**15, r=8, p=1).derive(
        password.encode("utf-8")
    )
    return Fernet(base64.urlsafe_b64encode(key))


def create_backup(data, directory, target, password, include_logs=False):
    ensure_export_path(target, directory)
    value = validate_data(data)
    for account in value["accounts"]:
        account.pop("openid_protected", None)
        account.pop("credential_error", None)
    logs = {}
    total = 0
    if include_logs:
        check_tree(directory)
        for root in LOG_ROOTS:
            for path in sorted((Path(directory) / root).rglob("*")):
                if path.is_symlink() or (
                    hasattr(path, "is_junction") and path.is_junction()
                ):
                    raise ValueError("日志目录含链接，请检查后再备份。")
                if path.is_file() and path.suffix in (".json", ".log"):
                    size = path.stat().st_size
                    total += size
                    if total > MAX_BYTES // 2:
                        raise ValueError(
                            "日志超过备份容量，请不含日志备份或先清理日志。"
                        )
                    content = path.read_bytes()
                    name = path.relative_to(directory).as_posix()
                    safe_relative(name)
                    logs[name] = {
                        "content": base64.b64encode(content).decode("ascii"),
                        "sha256": hashlib.sha256(content).hexdigest(),
                    }
    payload = {
        "format": "csuft-pingan-backup",
        "version": 1,
        "app_version": "3.4.1",
        "created_at": now_shanghai().isoformat(),
        "data": value,
        "logs_included": include_logs,
        "logs": logs,
    }
    raw = json.dumps(payload, ensure_ascii=False, allow_nan=False).encode("utf-8")
    if len(raw) > MAX_BYTES // 2:
        raise ValueError("数据超过备份容量，未写入。")
    salt = os.urandom(16)
    envelope = {
        "format": "csuft-pingan-encrypted",
        "version": 1,
        "kdf": "scrypt-32768-8-1",
        "salt": base64.b64encode(salt).decode("ascii"),
        "token": password_cipher(password, salt).encrypt(raw).decode("ascii"),
    }
    atomic_json(Path(target), envelope)
    return summary(value)


def read_backup(path, password):
    path = Path(path)
    if path.stat().st_size > MAX_BYTES:
        raise ValueError("备份超过允许容量。")
    try:
        envelope = json.loads(path.read_text(encoding="utf-8"))
        if (
            envelope.get("format") != "csuft-pingan-encrypted"
            or envelope.get("version") != 1
            or envelope.get("kdf") != "scrypt-32768-8-1"
        ):
            raise ValueError("备份格式或版本不兼容。")
        salt = base64.b64decode(envelope["salt"], validate=True)
        if len(salt) != 16:
            raise ValueError("备份盐值无效。")
        raw = password_cipher(password, salt).decrypt(envelope["token"].encode("ascii"))
        payload = json.loads(raw)
        if (
            payload.get("format") != "csuft-pingan-backup"
            or payload.get("version") != 1
        ):
            raise ValueError("备份内容版本不兼容。")
        payload["data"] = validate_data(payload["data"])
        if (
            not isinstance(payload.get("logs"), dict)
            or type(payload.get("logs_included")) is not bool
        ):
            raise ValueError("备份日志目录无效。")
        if not payload["logs_included"] and payload["logs"]:
            raise ValueError("备份日志标记不一致。")
        for name, record in payload["logs"].items():
            safe_relative(name)
            content = base64.b64decode(record["content"], validate=True)
            if hashlib.sha256(content).hexdigest() != record["sha256"]:
                raise ValueError("备份日志完整性校验失败。")
        return payload
    except InvalidToken:
        raise ValueError("密码错误或备份已损坏；当前数据未修改。") from None
    except (KeyError, TypeError, AttributeError, UnicodeError, json.JSONDecodeError):
        raise ValueError("备份内容损坏；当前数据未修改。") from None


def check_tree(directory):
    root = Path(directory)
    for current, directories, files in os.walk(root, followlinks=False):
        for path in [
            Path(current),
            *(Path(current) / name for name in directories + files),
        ]:
            if path.is_symlink() or (
                hasattr(path, "is_junction") and path.is_junction()
            ):
                raise ValueError("数据目录含链接或联接，无法安全复制。请先检查目录。")


def copy_local(store, destination):
    """Retains all source files; destination is new and protected for this user."""
    source, destination = store.directory.resolve(), Path(destination).resolve()
    if (
        destination == source
        or source in destination.parents
        or destination in source.parents
    ):
        raise ValueError("来源和目标必须是互不包含的不同目录。")
    if destination.exists():
        raise ValueError("目标已存在，为避免覆盖请选择新目录或主动恢复备份。")
    check_tree(source)
    if any((source / name).exists() for name in (
        "config.tmp", "history.tmp", JOURNAL, JOURNAL + ".history-tmp",
        "config.json.history-tmp", "history.json.history-tmp", "config.json.bak.history-tmp",
    )):
        raise ValueError("来源含未完成配置临时文件，请先检查；迁移未应用。")
    destination.parent.mkdir(parents=True, exist_ok=True)
    stage = Path(tempfile.mkdtemp(prefix=".pingan-migrate-", dir=destination.parent))
    try:
        if source.exists():
            shutil.copytree(source, stage, dirs_exist_ok=True)
        staged = Store(stage, store.protector)
        staged.data = store.snapshot()
        staged.save()
        # Any legacy temp is never copied silently as plaintext.
        for name in ("config.tmp", "history.tmp", JOURNAL, JOURNAL + ".history-tmp",
                     "config.json.history-tmp", "history.json.history-tmp", "config.json.bak.history-tmp"):
            candidate = stage / name
            if candidate.exists():
                raise ValueError("来源含未完成配置临时文件，请先检查；迁移未应用。")
        os.replace(stage, destination)
        return destination
    finally:
        if stage.exists():
            shutil.rmtree(stage)


def restore_backup(store, payload, apply_automatic=False, replace=os.replace):
    data = validate_data(payload["data"])
    if not apply_automatic:
        data["schedule"]["enabled"] = False
    data["reliability"] = {
        **defaults()["reliability"],
        **data.get("reliability", {}),
        "autostart": False,
    }
    data["automation"] = None
    data["position_cache"] = {}  # Restored settings/history do not resume old submissions.
    data["schedule"]["last_trigger"] = None
    directory = store.directory.resolve()
    check_tree(directory)
    # Precompute encryption before touching any live data.
    encode_config(data, store.protector)
    # Retained DPAPI rollback snapshot (same user/PC) is created before restoration.
    rollback = directory.with_name(
        directory.name
        + "-before-restore-"
        + now_shanghai().strftime("%Y%m%d-%H%M%S")
        + "-"
        + uuid.uuid4().hex[:6]
    )
    copy_local(store, rollback)
    stage = Path(tempfile.mkdtemp(prefix=".pingan-restore-", dir=directory.parent))
    displaced = directory.with_name(".pingan-displaced-" + uuid.uuid4().hex)
    committed = False
    try:
        shutil.copytree(rollback, stage, dirs_exist_ok=True)
        staged = Store(stage, store.protector)
        staged.data = data
        staged.save()
        if payload.get("logs_included"):
            for root in LOG_ROOTS:
                if (stage / root).exists():
                    shutil.rmtree(stage / root)
            for name, record in payload.get("logs", {}).items():
                safe_relative(name)
                content = base64.b64decode(record["content"], validate=True)
                if hashlib.sha256(content).hexdigest() != record["sha256"]:
                    raise ValueError("日志校验失败，未恢复。")
                output = stage.joinpath(*PurePosixPath(name).parts)
                output.parent.mkdir(parents=True, exist_ok=True)
                output.write_bytes(content)
        replace(directory, displaced)
        try:
            replace(stage, directory)
        except BaseException:
            os.replace(displaced, directory)
            raise
        try:
            loaded = Store(directory, store.protector)
        except BaseException:
            os.replace(directory, stage)
            os.replace(displaced, directory)
            raise
        store.__dict__.update(loaded.__dict__)
        committed = True
        return rollback
    finally:
        if stage.exists():
            shutil.rmtree(stage)
        if committed and displaced.exists():
            shutil.rmtree(displaced)  # Protected rollback already retains the original.


def ensure_export_path(target, directory):
    resolved = Path(target).resolve()
    root = Path(directory).resolve()
    if resolved == root or root in resolved.parents:
        raise ValueError(
            "导出目标不能位于当前数据目录内，请选择其它目录，避免覆盖配置或日志。"
        )


def diagnostic_export(data, target, directory=None):
    if directory is not None:
        ensure_export_path(target, directory)
    # Deliberately exclude account identities, labels, results, coordinates and credentials.
    value = {
        "version": 3,
        "created_at": now_shanghai().isoformat(),
        "account_count": len(data["accounts"]),
        "location_count": len(data["locations"]),
        "schedule": clean(
            data["schedule"], [a.get("openid", "") for a in data["accounts"]]
        ),
        "notifications": data["notifications"],
        "credential_issues": sum(
            bool(a.get("credential_error")) for a in data["accounts"]
        ),
    }
    value["schedule"].pop("task_rule", None)
    atomic_json(Path(target), value)


def selected_directory(app_dir):
    marker = Path(app_dir) / "data-mode.json"
    if not marker.exists():
        return Path(app_dir) / "data"
    value = json.loads(marker.read_text(encoding="utf-8"))
    if value.get("mode") == "local":
        from windows_integration import local_directory

        return local_directory()
    if value.get("mode") == "portable":
        return Path(app_dir) / "data"
    raise ValueError(
        "数据模式选择无效，请检查程序旁 data-mode.json；没有自动迁移数据。"
    )


def set_mode(app_dir, mode):
    if mode not in ("local", "portable"):
        raise ValueError("无效数据模式。")
    atomic_json(Path(app_dir) / "data-mode.json", {"version": 1, "mode": mode})
