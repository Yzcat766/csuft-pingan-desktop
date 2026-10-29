"""Python port of src/index.ts. This module does not capture OpenID."""

import base64
import hashlib
import json
import math
import os
import re
import time
from decimal import Decimal
from urllib.parse import urlsplit

import requests
from locations import coordinates

BASE_URL = "https://simp.csuft.edu.cn/api/"
AUTH = os.environ.get("PINGAN_API_AUTHORIZATION", "").strip()
REFERER = "https://servicewechat.com/wx0e47c34c9982aa09/7/page-frame.html"
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/132.0.0.0 Safari/537.36 MicroMessenger/7.0.20.1781(0x6700143B) NetType/WIFI MiniProgramEnv/Windows WindowsWechat/WMPF WindowsWechat(0x63090a13) UnifiedPCWindowsWechat(0xf2541739) XWEB/18955"


class ApiError(RuntimeError):
    def __init__(self, message, code="validation", retryable=False, uncertain=False):
        super().__init__(message)
        self.code, self.retryable, self.uncertain = code, retryable, uncertain


def redact(value, secrets=()):
    value = str(value)
    for secret in sorted({s for s in (*secrets, AUTH) if s}, key=len, reverse=True):
        value = value.replace(secret, "[已隐藏]")
    value = re.sub(
        r"(?i)\b(?:Bearer|Basic)\s+[A-Za-z0-9+/=_\-.]+", "[凭据已隐藏]", value
    )
    value = re.sub(r"(?i)(https?://)[^/\s:@]+:[^@\s/]+@", r"\1[凭据已隐藏]@", value)
    value = re.sub(r"(?<![\w-])o[A-Za-z0-9_-]{27}(?![\w-])", "[OpenID已隐藏]", value)
    value = re.sub(
        r"\beyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+", "[令牌已隐藏]", value
    )
    return re.sub(
        r"(?i)((?:access[_-]?token|refresh[_-]?token|token|password|proxy[_-]?(?:pass|password)|client_secret|openid|flysource-auth|代理密码)[\"']?\s*[=:：]\s*[\"']?)[^\"'\s,;，；}&]+",
        r"\1[已隐藏]",
        value,
    )


class RequestCancelled(ApiError):
    pass


def rejection_code(body, endpoint):
    # Conservative exact allowlist; generic errors never cause another submission.
    if endpoint.endswith("stuSign") and body.get("success") is False and not body.get("error"):
        code = body.get("code")
        message = body.get("msg")
        if code in ("POSITION_OUT_OF_RANGE", "LOCATION_OUT_OF_RANGE", "DISTANCE_OUT_OF_RANGE") or (code is None and message in (
            "不在签到范围内", "当前位置不在签到范围内", "签到位置超出允许范围", "签到距离超出允许范围"
        )):
            return "position_rejected"
    return "server_rejected"


def md5(value):
    return hashlib.md5(value.encode("utf-8")).hexdigest()


def signature(url, token, timestamp=None):
    stamp = str(timestamp if timestamp is not None else int(time.time() * 1000))
    pathname = urlsplit(url).path
    return (
        md5(f"{pathname}?sign={md5(stamp + token)}")
        + "1."
        + base64.b64encode(stamp.encode()).decode()
    )


def distance(lat, lng, target_lat, target_lng):
    a, b = math.radians(lat), math.radians(target_lat)
    dlat, dlng = a - b, math.radians(lng - target_lng)
    value = (
        math.sin(dlat / 2) ** 2 + math.cos(a) * math.cos(b) * math.sin(dlng / 2) ** 2
    )
    meters = 2 * math.asin(math.sqrt(min(1, max(0, value)))) * 6378137
    return math.floor(meters * 10 + 0.5) / 10


def js_number(value):
    # JavaScript Number.toString style, preserving the submitted float's precision.
    if value == 0:
        return "0"
    if value == int(value):
        return str(int(value))
    raw = str(float(value))
    if 1e-6 <= abs(value) < 1e21:
        return format(Decimal(raw), "f")
    mantissa, exponent = raw.split("e")
    return (
        mantissa + "e" + ("+" if int(exponent) >= 0 else "-") + str(abs(int(exponent)))
    )


def location_details(task, latitude, longitude):
    try:
        lat, lng = coordinates(latitude, longitude)
    except ValueError as error:
        raise ApiError(str(error)) from None
    dorm = task.get("dormitoryRegisterVO") or {}
    try:
        target_lat, target_lng = coordinates(dorm["locationLat"], dorm["locationLng"])
        if isinstance(task["locationAccuracy"], bool):
            raise ValueError()
        accuracy = float(task["locationAccuracy"])
        room = str(dorm["roomId"])
    except (KeyError, TypeError, ValueError):
        raise ApiError(
            "任务缺少有效宿舍位置、房间或距离要求，请检查学校任务信息后重试。"
        ) from None
    if (
        dorm["roomId"] is None
        or not room
        or not all(map(math.isfinite, (accuracy, target_lat, target_lng)))
        or accuracy <= 0
    ):
        raise ApiError("任务的房间或距离要求无效，请联系学校核对任务配置。")
    measured = distance(lat, lng, target_lat, target_lng)
    return {
        "latitude": lat,
        "longitude": lng,
        "target_latitude": target_lat,
        "target_longitude": target_lng,
        "distance": measured,
        "allowed_distance": accuracy,
        "room_id": room,
    }


def build_sign_payload(task, status, latitude, longitude):
    details = location_details(task, latitude, longitude)
    lat, lng = details["latitude"], details["longitude"]
    measured, accuracy, room = (
        details["distance"],
        details["allowed_distance"],
        details["room_id"],
    )
    if measured >= accuracy:
        raise ApiError(
            f"位置距宿舍 {measured:g} 米，超出允许范围（须小于 {accuracy:g} 米）。请在定位设置中选择范围内的位置或核对任务位置。"
        )
    if task.get("openTakePhoto") in (1, "1"):
        raise ApiError(
            "此任务要求上传照片，当前版本不支持。请使用学校官方入口完成签到。"
        )
    if not status.get("signDate"):
        raise ApiError("服务端缺少签到日期，无法提交。")
    task_id = str(task["taskId"])
    digest_source = {
        "latitude": js_number(lat),
        "longitude": js_number(lng),
        "locationAccuracy": js_number(measured),
        "signDate": status["signDate"],
        "taskId": task_id,
        "fileId": "",
    }
    return {
        "taskId": task_id,
        "scanType": task.get("scanType", "1"),
        "roomId": room,
        "isLateStuTakePhoto": task.get("isLateStuTakePhoto", 0),
        "signLat": lat,
        "signLng": lng,
        "locationAccuracy": measured,
        "stuTaskId": md5(
            json.dumps(digest_source, ensure_ascii=False, separators=(",", ":"))
        ),
        "signType": 0,
        "scanCode": "",
    }


class SchoolClient:
    def __init__(self, openid, session=None):
        self.openid = openid
        self.session = session or requests.Session()
        self.session.trust_env = False  # Match the original axios proxy:false.
        self.token = ""
        self.cancel = None

    def close(self):
        self.session.close()

    def _request(self, method, endpoint, authenticated=True, **kwargs):
        if self.cancel is not None and self.cancel.is_set():
            raise RequestCancelled("执行已取消；下次运行会核对服务端实际状态。")
        if not AUTH:
            raise ApiError(
                "未配置服务授权头。请设置 PINGAN_API_AUTHORIZATION 环境变量后重启程序；也可使用离线演示。",
                "configuration",
            )
        url = BASE_URL + endpoint
        headers = {"User-Agent": UA, "Authorization": AUTH, "Referer": REFERER}
        if authenticated:
            if not self.token:
                raise ApiError("请先登录账号。")
            headers.update(
                {
                    "Flysource-Auth": self.token,
                    "Flysource-Sign": signature(url, self.token),
                }
            )
        headers.update(kwargs.pop("headers", {}))
        try:
            response = self.session.request(
                method, url, headers=headers, timeout=(10, 25), **kwargs
            )
            response.raise_for_status()
            body = response.json()
        except requests.Timeout:
            raise ApiError(
                "连接学校接口超时，结果可能尚未确认；重试前会先查询服务端。",
                "network_timeout",
                True,
                endpoint.endswith("stuSign"),
            ) from None
        except requests.exceptions.SSLError:
            raise ApiError(
                "学校接口证书校验失败，请检查系统时间和网络后手动重试。", "certificate"
            ) from None
        except requests.ConnectionError:
            raise ApiError(
                "学校接口连接中断，请检查网络；重试前会核对服务端状态。",
                "network_connection",
                True,
                endpoint.endswith("stuSign"),
            ) from None
        except requests.HTTPError as error:
            status = getattr(error.response, "status_code", None)
            temporary = status in (408, 429, 500, 502, 503, 504)
            raise ApiError(
                (
                    "学校接口暂时繁忙，请稍后重试。"
                    if temporary
                    else "学校接口拒绝请求，请核对账号或使用官方入口。"
                ),
                "http_temporary" if temporary else "http_rejected",
                temporary,
                temporary and endpoint.endswith("stuSign"),
            ) from None
        except requests.RequestException:
            raise ApiError(
                "学校接口请求无法完成，请检查网络配置后手动重试。", "request_invalid"
            ) from None
        except ValueError:
            raise ApiError("学校接口未返回有效 JSON。") from None
        if not isinstance(body, dict):
            raise ApiError("学校接口响应格式异常。")
        if body.get("success") is False or body.get("error"):
            message = str(
                body.get("msg")
                or body.get("error_description")
                or body.get("error")
                or "接口返回失败。"
            )
            raise ApiError(
                redact(message, (self.openid, self.token))[:300], rejection_code(body, endpoint)
            )
        return body

    def login(self):
        body = self._request(
            "POST",
            "flySource-auth/oauth/token",
            authenticated=False,
            data={
                "tenantId": "000000",
                "grant_type": "wxapp",
                "username": "",
                "password": "",
                "scope": "all",
                "bindState": 0,
                "openid": self.openid,
            },
            headers={
                "Content-Type": "application/x-www-form-urlencoded",
                "Tenant-Id": "000000",
                "Web-Type": "wxapp",
            },
        )
        token = body.get("access_token")
        if not isinstance(token, str) or not token:
            raise ApiError(
                "登录失败：OpenID 无效、已失效或尚未绑定账号。", "authentication"
            )
        self.token = token
        return body

    def list_tasks(self):
        all_records = []
        for page in range(1, 11):
            body = self._request(
                "GET",
                "flySource-yxgl/dormSignTask/getListForApp",
                params={"current": page, "size": 100},
            )
            data = body.get("data")
            records = data.get("records") if isinstance(data, dict) else None
            if not isinstance(records, list):
                raise ApiError("任务列表响应格式异常。")
            all_records.extend(records)
            identities = [
                str(record.get("taskId", ""))
                for record in all_records
                if isinstance(record, dict)
            ]
            if len(identities) != len(all_records) or len(set(identities)) != len(
                identities
            ):
                raise ApiError(
                    "任务列表分页出现重复或无效任务，无法完整核对，未选择任务。",
                    "task_list_invalid",
                )
            total = data.get("total")
            if total is not None:
                try:
                    if int(total) < 0:
                        raise ValueError()
                    if len(all_records) >= int(total):
                        return all_records
                except (ValueError, TypeError):
                    raise ApiError("任务列表总数格式异常，未选择任务。") from None
            elif len(records) < 100:
                return all_records
            if not records:
                raise ApiError("任务列表分页不完整，未选择任务。")
        raise ApiError(
            "候选任务过多，无法完整核对，请使用官方入口。", "task_list_limit"
        )

    def get_task(self, task_id):
        body = self._request(
            "GET",
            "flySource-yxgl/dormSignTask/getTaskByIdForApp",
            params={"taskId": task_id},
        )
        task = body.get("data")
        if not isinstance(task, dict) or str(task.get("taskId")) != str(task_id):
            raise ApiError("任务详情不存在或任务 ID 不匹配。")
        return task

    def get_status(self, task_id):
        body = self._request(
            "GET", "flySource-yxgl/dormSignRecord/getOne", params={"taskId": task_id}
        )
        if not isinstance(body.get("data"), dict):
            raise ApiError("签到状态响应格式异常。")
        return body["data"]

    def sign(self, task, lat, lng):
        # Recheck immediately before submission to handle concurrent manual sign-ins.
        journal = getattr(self, "journal", lambda phase, message: None)
        journal("服务端复查", "提交前重新查询签到状态。")
        current = self.get_status(task["taskId"])
        guard = getattr(self, "submission_guard", None)
        if guard:
            guard(current)
        if current.get("signStatus") in (0, "0"):
            return {**current, "_success_kind": "already_signed"}
        payload = build_sign_payload(task, current, lat, lng)
        journal("提交", "发送本次真实坐标与距离的打卡请求。")
        body = self._request(
            "POST", "flySource-yxgl/dormSignRecord/stuSign", json=payload
        )
        if body.get("success") is not True:
            raise ApiError("服务端没有确认签到提交成功。", "server_unconfirmed", uncertain=True)
        journal("服务端复查", "提交后查询服务端确认结果。")
        try:
            checked = self.get_status(task["taskId"])
        except ApiError as error:
            error.uncertain = True
            raise
        if guard:
            guard(checked)
        if checked.get("signStatus") not in (0, "0"):
            raise ApiError(
                f"提交后服务端仍显示未签到（signStatus={checked.get('signStatus')}），请检查结果后再重试。"
            )
        return {**checked, "_success_kind": "submitted"}
