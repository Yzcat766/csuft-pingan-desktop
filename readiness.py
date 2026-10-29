"""Local checks never log in or submit; network check is explicitly requested."""

import tempfile
from pathlib import Path
from locations import resolve_location
from position_offset import resolve_offset
from storage import validate_schedule


def check_ready(data, directory, unsaved=False, probe=True):
    issues = []
    try:
        validate_schedule(data["schedule"])
    except ValueError as error:
        issues.append(("schedule", str(error)))
    if unsaved:
        issues.append(("schedule", "计划有未保存修改；自动执行仅使用已保存版本"))
    accounts = [a for a in data["accounts"] if a.get("enabled", True)]
    if not accounts:
        issues.append(("accounts", "没有启用账号"))
    for account in accounts:
        if not account.get("openid") or account.get("credential_error"):
            issues.append(("accounts", "账号凭据不可用，请编辑账号或恢复加密备份"))
        try:
            resolve_location(account, data)
            resolve_offset(account, data)
        except ValueError as error:
            issues.append(("location", str(error)))
    if any(a.get("credential_error") for a in data["accounts"]):
        issues.append(
            ("accounts", "有账号因凭据无法解密而停用；原密文保留，请编辑修复")
        )
    try:
        if not probe:
            return issues
        Path(directory).mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryFile(dir=directory) as stream:
            stream.write(b"write-check")
            stream.flush()
    except OSError:
        issues.append(("settings", "数据目录不可写，请调整目录或权限"))
    return issues


def check_network():
    import requests
    from api import BASE_URL

    # HTTPS connectivity only; no authentication or school task endpoint.
    with requests.Session() as session:
        session.trust_env = False
        try:
            response = session.head(
                BASE_URL.split("/api")[0], timeout=(3, 5), allow_redirects=False
            )
            return f"HTTPS 连接可达（HTTP {response.status_code}）。不代表账号可登录或打卡成功。"
        except requests.RequestException:
            return "HTTPS 连接未通过，请核对网络、证书及系统时间；没有登录或提交打卡。"
