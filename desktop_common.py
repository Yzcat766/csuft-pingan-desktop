import ctypes
import hashlib
import os
import sys
from pathlib import Path
from api import build_sign_payload
from locations import university_location
from storage import now_shanghai

CLIPBOARD_ERROR = "剪贴板暂不可用，请解锁 Windows 或关闭占用剪贴板的程序后重试。"


def copy_text(value):
    from PySide6.QtWidgets import QApplication

    clipboard = QApplication.clipboard()
    clipboard.setText(value)
    return clipboard.text() == value


def app_directory():
    return (
        Path(sys.executable).resolve().parent
        if getattr(sys, "frozen", False)
        else Path(__file__).resolve().parent
    )


class SingleInstance:
    def __init__(self, directory):
        self.handle = None
        if os.name == "nt":
            api = ctypes.WinDLL("kernel32", use_last_error=True)
            api.CreateMutexW.argtypes = (
                ctypes.c_void_p,
                ctypes.c_int,
                ctypes.c_wchar_p,
            )
            api.CreateMutexW.restype = ctypes.c_void_p
            api.CloseHandle.argtypes = (ctypes.c_void_p,)
            key = hashlib.sha256(
                str(Path(directory).resolve()).casefold().encode()
            ).hexdigest()[:20]
            self.handle = api.CreateMutexW(None, True, "Local\\CSUFTPythonApp-" + key)
            error = ctypes.get_last_error()
            if not self.handle:
                raise ctypes.WinError(error)
            self.api = api
            if error == 183:
                self.close()
                raise RuntimeError("这个数据目录的软件已经运行，请切换到已打开的窗口。")

    def close(self):
        if self.handle:
            self.api.CloseHandle(self.handle)
            self.handle = None


class DemoClient:
    """Offline GUI preview; never calls any school API."""

    def __init__(self, openid):
        self.signed = False

    def login(self):
        return {
            "userName": "演示用户",
            "schoolName": "中南林业科技大学",
            "accountNo": "演示账号",
        }

    def list_tasks(self):
        return [{"taskId": "demo"}]

    def get_task(self, task_id):
        campus = university_location()
        return {
            "taskId": "demo",
            "taskName": "晚间平安签到 · 演示",
            "signStartTime": "00:00:00",
            "signEndTime": "23:59:59",
            "locationAccuracy": 200,
            "dormitoryRegisterVO": {
                "roomId": "demo-room",
                "locationLat": str(campus["latitude"]),
                "locationLng": str(campus["longitude"]),
            },
        }

    def get_status(self, task_id):
        return {
            "signStatus": 0 if self.signed else 3,
            "signDate": now_shanghai().date().isoformat(),
        }

    def sign(self, task, lat, lng):
        build_sign_payload(task, self.get_status(task["taskId"]), lat, lng)
        self.signed = True
        return self.get_status(task["taskId"])

    def close(self):
        pass
