"""Build a Windows x64 EXE using the project's existing PyInstaller spec.

Usage: python build_exe.py
No downloads, account access, release archive updates or existing EXE replacement.
"""

import argparse
import hashlib
import json
import os
import subprocess
import sys
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent


class BuildError(RuntimeError):
    pass


def digest(path):
    value = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            value.update(block)
    return value.hexdigest()


def protected_files():
    """Keep normal source/EXE data and existing release artifacts untouched."""
    return {
        str(path.relative_to(ROOT)): digest(path)
        for name in ("data", "dist", "releases")
        for path in (ROOT / name).rglob("*") if path.is_file()
    }


def environment(job):
    env = os.environ.copy()
    for key in ("PYTHONHOME", "PYTHONPATH", "QT_PLUGIN_PATH",
                "QT_QPA_PLATFORM_PLUGIN_PATH", "QT_SCALE_FACTOR"):
        env.pop(key, None)
    env.update(PYTHONUTF8="1", PYTHONUNBUFFERED="1",
               PYTHONPYCACHEPREFIX=str(job / "pycache"),
               PYINSTALLER_CONFIG_DIR=str(job / "pyinstaller-cache"))
    return env


def run_step(title, arguments, job, env, timeout=120, cwd=ROOT):
    log = job / (title + ".log")
    print(f"开始：{title}（日志：{log}）", flush=True)
    with log.open("w", encoding="utf-8") as stream:
        stream.write(subprocess.list2cmdline([str(v) for v in arguments]) + "\n\n")
        stream.flush()
        try:
            completed = subprocess.run(
                [str(v) for v in arguments], cwd=cwd, env=env,
                stdout=stream, stderr=subprocess.STDOUT, timeout=timeout,
                creationflags=subprocess.CREATE_NO_WINDOW,
            )
        except subprocess.TimeoutExpired as error:
            raise BuildError(f"{title} 超时；检查日志 {log}，未标记构建成功。") from error
    if completed.returncode:
        tail = "\n".join(log.read_text(encoding="utf-8", errors="replace").splitlines()[-25:])
        raise BuildError(f"{title} 失败，退出码 {completed.returncode}。\n{tail}\n完整日志：{log}")
    print(f"通过：{title}", flush=True)
    return log


def main(argv=None):
    parser = argparse.ArgumentParser(description="将平安签到源码打包为独立 Windows EXE。")
    parser.add_argument("--python", type=Path, help="指定已安装依赖的 Python；默认优先本工程 .venv")
    parser.add_argument("--check-only", action="store_true", help="只检查运行环境及冻结文件，不生成 EXE")
    parser.add_argument("--skip-tests", action="store_true", help="跳过完整离线测试；仍运行源码及 EXE 演示启动检查")
    parser.add_argument("--console", action="store_true", help="生成带控制台的调试版本（默认无控制台）")
    args = parser.parse_args(argv)
    if sys.platform != "win32":
        raise BuildError("此脚本需要在 Windows 上运行，不能从其他系统交叉构建 Windows EXE。")
    python = args.python or ROOT / ".venv" / "Scripts" / "python.exe"
    if not python.is_file() and args.python is None:
        python = Path(sys.executable)
    python = python.resolve()
    if not python.is_file():
        raise BuildError(f"Python 不存在：{python}")
    for name in ("app.py", "CSUFT-Pingan.spec", "history_storage.py", "ui_resources.py", "offline_tests.py"):
        if not (ROOT / name).is_file():
            raise BuildError(f"缺少工程文件：{ROOT / name}")
    before = protected_files()
    stamp = datetime.now(timezone(timedelta(hours=8))).strftime("%Y%m%d-%H%M%S")
    job = ROOT / "build" / f"exe-{stamp}-{uuid.uuid4().hex[:6]}"
    job.mkdir(parents=True, exist_ok=False)
    env = environment(job)
    env["PINGAN_BUILD_CONSOLE"] = "1" if args.console else "0"
    print(f"Python：{python}\n构建目录：{job}", flush=True)
    report = {"status": "running", "python": str(python),
              "full_tests": not args.skip_tests and not args.check_only,
              "console": args.console, "steps": []}
    try:
        probe = (
            "import sys,struct; "
            "assert sys.platform=='win32' and sys.version_info>=(3,12) and struct.calcsize('P')==8,"
            "'Windows x64 Python 3.12+ required'; "
            "import PyInstaller,PySide6.QtCore,win32crypt,pywintypes,cryptography; "
            "print('Python',sys.version); print('PyInstaller',PyInstaller.__version__); print('Dependencies OK')"
        )
        steps = [("01-environment", [python, "-c", probe], 120),
                 ("02-pip-check", [python, "-m", "pip", "check"], 120)]
        if not args.check_only:
            if not args.skip_tests:
                steps.append(("03-offline-tests", [python, ROOT / "offline_tests.py"], 900))
            steps.extend([
                ("04-source-smoke", [python, ROOT / "app.py", "--demo", "--smoke-test"], 120),
                ("05-pyinstaller", [python, "-m", "PyInstaller", "--noconfirm", "--clean",
                 "--distpath", job / "dist", "--workpath", job / "work", ROOT / "CSUFT-Pingan.spec"], 1800),
            ])
        for title, arguments, timeout in steps:
            run_step(title, arguments, job, env, timeout)
            report["steps"].append({"name": title, "exit_code": 0})
        if not args.check_only:
            executable = job / "dist" / "CSUFT-Pingan.exe"
            if not executable.is_file():
                raise BuildError("PyInstaller 返回成功，但没有生成预期 EXE。")
            # Verify the EXE also starts without local Python or Conda on PATH.
            clean = env.copy()
            clean.pop("VIRTUAL_ENV", None)
            windows = Path(os.environ["SystemRoot"])
            clean["PATH"] = os.pathsep.join((str(windows / "System32"), str(windows)))
            run_step("06-exe-smoke", [executable, "--demo", "--smoke-test"], job, clean, cwd=executable.parent)
            report["steps"].append({"name": "06-exe-smoke", "exit_code": 0})
            # PyInstaller's archive viewer must find the newly added history module.
            module_log = run_step("07-bundled-modules", [python, "-m", "PyInstaller.utils.cliutils.archive_viewer",
                                  "--list", "--recursive", executable], job, env)
            archive = module_log.read_text(encoding="utf-8", errors="replace")
            missing = [name for name in ("history_storage", "_ctypes.pyd", "_ssl.pyd") if name not in archive]
            if missing:
                raise BuildError("EXE 归档缺少必要模块：" + ", ".join(missing))
            report["steps"].append({"name": "07-bundled-modules", "exit_code": 0})
            report.update(executable=str(executable), bytes=executable.stat().st_size, sha256=digest(executable))
        if before != protected_files():
            raise BuildError("现有数据或发行文件发生变化，请检查；未标记构建成功。")
        report.update(status="passed", protected_files_unchanged=True,
                      mode="check-only" if args.check_only else "build")
        print("环境检查通过。" if args.check_only else f"EXE 构建并验证成功：\n{report['executable']}", flush=True)
        return 0
    except (BuildError, OSError, KeyboardInterrupt) as error:
        report.update(status="failed", error=str(error))
        raise
    finally:
        (job / "build-report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"报告：{job / 'build-report.json'}", flush=True)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (BuildError, OSError) as error:
        print(f"构建失败：{error}\n缺少依赖时，请在本工程运行 install.cmd 或安装 requirements-build.txt。", file=sys.stderr)
        raise SystemExit(1)
    except KeyboardInterrupt:
        print("构建已取消，未标记成功。", file=sys.stderr)
        raise SystemExit(130)
