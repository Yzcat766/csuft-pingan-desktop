# 开发、验证与发布

## 环境

Windows x64，Python 3.12+，建议 Python 3.13。运行依赖固定在 `requirements.txt`；`requirements-build.txt` 在此基础上增加 PyInstaller。

```powershell
python -m venv .venv
.venv/Scripts/python.exe -m pip install -r requirements-build.txt
.venv/Scripts/python.exe -m pip check
.venv/Scripts/python.exe offline_tests.py
.venv/Scripts/python.exe app.py --demo --smoke-test
```

离线测试冻结模拟服务日期、拦截真实 HTTP，并使用虚构授权头。GUI 测试使用真实 Qt 控件与临时数据。不要将真实凭据设为 CI secret；离线构建无需学校接口授权。

## 目录

| 文件/目录 | 职责 |
| --- | --- |
| `app.py`, `qt_ui.py`, `*_ui.py` | 入口、桌面页面与交互 |
| `api.py`, `runner.py`, `execution.py` | 接口封装、执行与结果合并 |
| `storage.py`, `history_storage.py`, `security.py` | 配置、历史事务与 DPAPI |
| `scheduling.py`, `task_selection.py`, `position_offset.py` | 定时、任务选择与位置规则 |
| `logbook.py`, `log_page.py`, `data_management.py` | 日志与加密备份/恢复 |
| `tests/`, `offline_tests.py`, `capture_*.py` | 离线测试与模拟截图生成 |
| `build_exe.py`, `CSUFT-Pingan.spec` | Windows 单文件 EXE 构建与启动验证 |
| `package_release.py`, `verify_release.py` | 白名单打包、校验与归档验证 |

## 构建 Windows 便携版

```powershell
.venv/Scripts/python.exe build_exe.py
```

脚本在 `build/exe-时间-标识/` 下创建独立构建任务，日志与 `build-report.json` 保留本机。最终程序位于该目录的 `dist/CSUFT-Pingan.exe`。`build.cmd` 调用相同脚本。

构建前检查 Windows x64、Python、依赖和 `pip check`；随后执行完整离线测试和源码 smoke-test，打包后在仅有系统目录的 PATH 下验证 EXE 并核验必要模块。构建脚本不下载依赖，不访问真实学校接口。

`--check-only` 仅检查环境；`--console` 可创建调试版本；已经完整验证同一份源码时可使用 `--skip-tests`，源码及 EXE 启动验证仍会运行。

## 创建发行包

```powershell
.venv/Scripts/python.exe package_release.py --exe 'build/exe-时间-标识/dist/CSUFT-Pingan.exe'
.venv/Scripts/python.exe verify_release.py
```

发行目录 `releases/` 包含独立 EXE、Windows 便携 ZIP、源码 ZIP、`checksums.json` 和 `SHA256SUMS.txt`。校验脚本检查哈希、文件集合、安全路径、源码必要模块、归档内禁用的用户数据，并在临时目录执行归档中 EXE 的离线启动验证。

源码白名单明确包含历史模块、构建脚本、spec、测试、文档、工作流和四张审核过的模拟截图。新增模块时请更新白名单。禁止发布整个工作目录或旧发布包。

## 自动化

每次 push / pull request 运行 Windows 离线测试与源码启动验证。推送 `v*` tag 时构建并验证发行包，然后发布到相同 tag 的 GitHub Release。工作流的写权限仅限发行任务，使用 GitHub 自动提供的 `GITHUB_TOKEN`，不需要长期个人令牌。

## 生成截图

```powershell
.venv/Scripts/python.exe capture_visual.py
```

脚本使用临时模拟数据并禁止真实 HTTP。自动生成的大量截图与报告默认忽略；仓库仅保留四张精选截图。不要使用个人账号页面或真实日志替换文档截图。
