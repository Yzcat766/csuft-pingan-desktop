"""Opt-in integrations. Constructors and status queries never mutate Windows."""

import ctypes
import os
import subprocess
import sys
from pathlib import Path

RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
RUN_NAME = "CSUFT-Pingan"


def launch_command(directory):
    arguments = [str(Path(sys.executable).resolve())]
    if not getattr(sys, "frozen", False):
        arguments.append(str(Path(__file__).with_name("app.py").resolve()))
    arguments += ["--data-dir", str(Path(directory).resolve()), "--startup"]
    return subprocess.list2cmdline(arguments)


class Autostart:
    def __init__(self, directory):
        self.command = launch_command(directory)

    def read(self):
        import winreg

        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as key:
                return winreg.QueryValueEx(key, RUN_NAME)[0]
        except FileNotFoundError:
            return None

    def status(self):
        try:
            current = self.read()
            return (
                "未注册"
                if current is None
                else (
                    "已匹配当前程序及数据目录"
                    if current == self.command
                    else "启动项路径已变化，请主动修复"
                )
            )
        except Exception:
            return "无法读取启动项；请检查当前 Windows 用户权限"

    def set_enabled(self, enabled):
        import winreg

        with winreg.CreateKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as key:
            if enabled:
                winreg.SetValueEx(key, RUN_NAME, 0, winreg.REG_SZ, self.command)
            else:
                try:
                    winreg.DeleteValue(key, RUN_NAME)
                except FileNotFoundError:
                    pass


class SleepGuard:
    """Must enter/exit on the same worker thread; never requests display/away mode."""

    def __init__(self, enabled=False, setter=None):
        self.enabled = enabled
        self.setter = setter
        self.active = False
        self.error = None

    def __enter__(self):
        if self.enabled:
            try:
                if self.setter is None:
                    function = ctypes.WinDLL(
                        "kernel32", use_last_error=True
                    ).SetThreadExecutionState
                    function.argtypes = [ctypes.c_uint32]
                    function.restype = ctypes.c_uint32
                    self.setter = function
                if not self.setter(0x80000001):
                    raise OSError()
                self.active = True
            except Exception:
                self.error = "防自动睡眠未生效，请保持设备唤醒；任务仍继续。"
        return self

    def __exit__(self, *args):
        if self.active:
            try:
                if not self.setter(0x80000000):
                    self.error = (
                        "防睡眠状态释放失败；工作线程退出后 Windows 会释放请求。"
                    )
            except Exception:
                self.error = "防睡眠状态释放失败；工作线程退出后 Windows 会释放请求。"
            finally:
                self.active = False


def local_directory():
    return (
        Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local"))
        / "CSUFT-Pingan"
        / "data"
    )
