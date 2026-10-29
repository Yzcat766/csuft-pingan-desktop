"""Settings and optional workflows kept separate from the desktop shell."""

import json
import threading
from pathlib import Path
from PySide6.QtCore import QThread, QTimer, Qt
from PySide6.QtWidgets import (
    QDialog,
    QVBoxLayout,
    QDialogButtonBox,
    QLineEdit,
    QFileDialog,
    QComboBox,
    QPlainTextEdit,
    QSpinBox,
)
from ui_style import CheckBox, Switch
from ui_components import SettingForm, WrapToolbar
from data_management import (
    create_backup,
    read_backup,
    restore_backup,
    diagnostic_export,
    copy_local,
    summary,
    set_mode,
)
from windows_integration import Autostart, local_directory
from desktop_common import app_directory, SingleInstance, DemoClient
from api import SchoolClient
from task_selection import check_accounts
from maintenance import log_plan, clear_logs
from readiness import check_ready, check_network
from notifications import batch_notice
from storage import Store
from form_layout import size_field, ActionRow, FormLayout


class BackgroundJob(QThread):
    def __init__(self, action, parent):
        super().__init__(parent)
        self.action = action
        self.result = None
        self.error = None

    def run(self):
        try:
            self.result = self.action()
        except Exception as error:
            self.error = str(error)


class ReliabilityUI:
    def build_reliability_settings(self, layout):
        from qt_ui import card, label, button

        frame, box = card("自动运行与通知")
        form = SettingForm()
        preferences = self.store.data["reliability"]
        for name, title, explanation, checked in (
            (
                "start_hidden",
                "启动后隐藏",
                "托盘不可用时显示窗口；与关闭窗口行为独立。",
                preferences["start_hidden"],
            ),
            (
                "prevent_sleep",
                "任务期间防睡眠",
                "仅执行期间保持系统唤醒，不改变锁屏或永久电源计划。",
                preferences["prevent_sleep"],
            ),
            (
                "notification_enabled",
                "失败与需处理通知",
                "批次失败、认证或配置问题，以及结果/日志保存失败。",
                self.store.data["notifications"]["enabled"],
            ),
            (
                "notification_success",
                "成功通知",
                "默认关闭；临时网络错误等待重试，不反复通知。",
                self.store.data["notifications"]["success"],
            ),
        ):
            control = Switch()
            control.setAccessibleName(title)
            control.setChecked(checked)
            setattr(self, name, control)
            form.addRow(title, control, explanation)
            control.toggled.connect(self.save_reliability_preferences)
        startup_row = form.addRow(
            "登录 Windows 时启动",
            ActionRow(
                button("检查", callback=self.inspect_autostart),
                button("注册 / 修复", callback=lambda: self.change_autostart(True)),
                button("取消注册", callback=lambda: self.change_autostart(False)),
            ),
            "尚未检查；仅当前用户，注册前会显示命令并确认。",
        )
        self.autostart_status = startup_row.description
        form.addRow(
            "睡眠与关机",
            label("未实现唤醒任务", "muted"),
            "此版本未注册计划唤醒。睡眠/关机不能执行，恢复后仅按当日规则处理。",
        )
        form.addRow(
            "最近通知",
            button("查看结果 / 日志", callback=self.open_notification_target),
            "定位最近通知关联的批次、账号或未保存结果。",
        )
        box.addLayout(form)
        layout.addWidget(frame)

        frame, box = card("数据与备份")
        form = SettingForm()
        mode_row = form.addRow(
            "数据模式",
            button("更改模式", callback=self.migrate_dialog),
            self.data_mode_text(),
        )
        self.mode_description = mode_row.description
        self.mode_description.setToolTip(
            "离线演示临时目录" if self.demo else str(self.store.directory)
        )
        self.data_path = QLineEdit(
            "离线演示临时目录" if self.demo else str(self.store.directory)
        )
        self.data_path.setReadOnly(True)
        self.data_path.setToolTip(self.data_path.text())
        self.data_path.setProperty("formSize", "expand")
        path_controls = WrapToolbar(
            self.data_path, button("打开", "folder", self.open_data)
        )
        form.addRow(
            "数据目录",
            path_controls,
            "可选择、滚动或查看提示中的完整路径。",
            expand=True,
        )
        form.addRow(
            "完整备份",
            ActionRow(
                button("创建加密备份", callback=self.backup_dialog),
                button("验证并恢复", callback=self.restore_dialog),
            ),
            "账号、位置、计划与历史使用密码加密；恢复前展示影响并确认。",
        )
        form.addRow(
            "无凭据诊断",
            button("导出诊断", callback=self.export_diagnostics),
            "仅导出计数及配置概要，不含身份、凭据、历史详情或精确坐标。",
        )
        box.addLayout(form)
        layout.addWidget(frame)

        frame, box = card("安全与日志")
        form = SettingForm()
        credential_row = form.addRow(
            "本地凭据保护",
            ActionRow(
                button("检查 / 保护", callback=self.protect_credentials),
                button("回退迁移", callback=self.rollback_credentials),
            ),
            "Windows 当前用户 DPAPI；跨用户/电脑请使用密码备份。",
        )
        self.security_status = credential_row.description
        self.retention_days = QSpinBox()
        self.retention_days.setRange(7, 3650)
        self.retention_days.setValue(self.store.data["maintenance"]["retention_days"])
        self.retention_days.setSuffix(" 天")
        form.addRow(
            "日志保留天数",
            self.retention_days,
            "默认 90 天；只在主动预览并确认后清理，不自动删除。",
        )
        form.addRow(
            "日志维护",
            ActionRow(
                button("预览清理范围", callback=self.cleanup_dialog),
                button("打开日志", "folder", self.open_logs),
            ),
            "保留最近历史/自动任务关联、未结束及损坏批次；旧文本日志保留。",
        )
        box.addLayout(form)
        layout.addWidget(frame)

    def build_readiness(self, layout):
        self.build_readiness_strip(layout)

    def dismiss_onboarding(self):
        if self.persist(lambda: self.store.data.update(onboarding_done=True)):
            self.onboarding_card.hide()

    def save_reliability_preferences(self, *_):
        def save():
            self.store.data["reliability"].update(
                start_hidden=self.start_hidden.isChecked(),
                prevent_sleep=self.prevent_sleep.isChecked(),
            )
            self.store.data["notifications"].update(
                enabled=self.notification_enabled.isChecked(),
                success=self.notification_success.isChecked(),
            )

        if self.persist(save):
            self.notify("自动运行与通知偏好已保存。")

    def inspect_autostart(self):
        self.autostart_status.setText(
            "离线演示：不读取或注册本机启动项。"
            if self.demo
            else Autostart(self.store.directory).status()
        )

    def change_autostart(self, enabled):
        if self.demo:
            self.notify("离线演示不会修改 Windows 启动项。")
            return
        integration = Autostart(self.store.directory)
        if enabled and not self.confirm(
            "注册登录启动项",
            "将为当前 Windows 用户注册以下启动命令：\n"
            + integration.command
            + "\n仅登录后启动；移动程序需主动修复。",
        ):
            return
        try:
            old = integration.read()
            integration.set_enabled(enabled)
            if not self.persist(
                lambda: self.store.data["reliability"].update(autostart=enabled)
            ):
                # Roll back exact prior registry value if config persistence fails.
                import winreg
                from windows_integration import RUN_KEY, RUN_NAME

                with winreg.CreateKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as key:
                    if old is not None:
                        winreg.SetValueEx(key, RUN_NAME, 0, winreg.REG_SZ, old)
                    else:
                        try:
                            winreg.DeleteValue(key, RUN_NAME)
                        except FileNotFoundError:
                            pass
            self.inspect_autostart()
        except Exception:
            self.notify(
                "启动项操作失败，请检查当前 Windows 用户权限；可重新检查并修复。", True
            )

    def inspect_readiness(self):
        if self.data_job or self.busy:
            self.notify("请等待当前任务结束后检查。")
            return
        data = self.store.snapshot()
        unsaved = self.schedule_value() != self.store.data["schedule"]

        def done(issues):
            self.readiness_write_error = any(page == "settings" for page, _ in issues)
            self.update_readiness_projection(force=True)
            self.readiness_toggle.setChecked(True)
            self.refresh_stats()
            self.navigate("overview")

        self.start_job(lambda: check_ready(data, self.store.directory, unsaved), done)

    def refresh_local_readiness(self):
        if hasattr(self, "readiness_label"):
            self.update_readiness_projection()

    def inspect_network(self):
        if self.demo:
            self.notify("离线演示不连接网络。实际使用时可主动执行 HTTPS 检测。")
            return

        def done(value):
            self.network_state.setText("网络：" + value)
            self.notify(value)

        self.start_job(check_network, done)

    def data_mode_text(self):
        if self.demo:
            return "离线演示 · 临时模拟数据\n实际使用可选择 EXE 同目录的便携数据，或 %LOCALAPPDATA% 中的稳定本机目录。"
        current = self.store.directory.resolve()
        mode = (
            "本机使用模式"
            if current == local_directory().resolve()
            else (
                "便携模式"
                if current == (app_directory() / "data").resolve()
                else "指定目录模式"
            )
        )
        return f"{mode} · 完整路径见下行。\n更新 EXE 时保留 data。更改模式先预览，原目录保留。"

    def update_security_status(self):
        if not hasattr(self, "security_status"):
            return
        locked = sum(
            bool(a.get("credential_error")) for a in self.store.data["accounts"]
        )
        self.security_status.setText(
            "检测到旧明文配置；下一次保存将加密凭据，并将旧结构保留为 DPAPI 回退文件。建议现在点击加密。旧版 2.x 不能直接读取新凭据。"
            if self.store.legacy_plaintext
            else f"本地凭据：Windows 用户加密 · 无法解密账号 {locked} 个。"
        )
        if self.store.history_notice:
            self.security_status.setText(self.security_status.text() + "\n" + self.store.history_notice)

    def protect_credentials(self):
        if self.busy or self.data_job:
            self.notify("请等待任务结束后保护凭据。")
            return
        if self.persist(lambda: None):
            self.update_security_status()
            self.notify(
                "配置与回退备份已使用 Windows 当前用户加密；无法解密的原密文保留。"
            )

    def rollback_credentials(self):
        path = self.store.directory / "migration-rollback.dpapi"
        if not path.exists():
            self.notify("没有旧配置迁移回退文件。")
            return
        if not self.confirm(
            "回退配置",
            "恢复加密回退文件中的账号、定位和计划；自动执行关闭，仍以当前版本加密格式保存。当前数据会先保留本地回退副本。",
        ):
            return

        def restore():
            from data_management import validate_data

            envelope = json.loads(path.read_text(encoding="utf-8"))
            data = validate_data(
                json.loads(self.store.protector.unprotect(envelope["value"]))
            )
            # Credential/schema rollback must not resurrect deleted display history.
            for key in ("results", "overview_batches"):
                data[key] = self.store.snapshot()[key]
            return restore_backup(self.store, {"data": data, "logs_included": False})

        self.start_job(restore, self.restored)

    def ask_password(self, title, creating=False):
        from qt_ui import label

        dialog = QDialog(self)
        dialog.setWindowTitle(title)
        box = QVBoxLayout(dialog)
        box.addWidget(label("至少 8 个字符。软件不保存密码；跨电脑恢复需要此密码。"))
        password = QLineEdit()
        password.setAccessibleName("备份密码")
        size_field(password)
        password.setEchoMode(QLineEdit.EchoMode.Password)
        box.addWidget(password)
        repeat = None
        if creating:
            repeat = QLineEdit()
            repeat.setAccessibleName("确认备份密码")
            size_field(repeat)
            repeat.setPlaceholderText("再次输入备份密码")
            repeat.setEchoMode(QLineEdit.EchoMode.Password)
            box.addWidget(repeat)
        include = CheckBox("包含运行日志与旧文本日志（可能含个人信息）")
        if creating:
            box.addWidget(include)
        actions = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        actions.accepted.connect(dialog.accept)
        actions.rejected.connect(dialog.reject)
        box.addStretch(1)
        box.addWidget(actions)
        accepted = dialog.exec() == QDialog.DialogCode.Accepted
        value = password.text()
        mismatch = repeat is not None and repeat.text() != value
        password.clear()
        if repeat:
            repeat.clear()
        dialog.deleteLater()
        if not accepted:
            return None
        if len(value) < 8 or mismatch:
            self.notify("密码至少 8 位，两次输入须一致。请重新操作。", True)
            return None
        return value, include.isChecked()

    def backup_dialog(self):
        if not self.data_available():
            return
        values = self.ask_password("创建完整加密备份", True)
        if values is None:
            return
        path, _ = QFileDialog.getSaveFileName(
            self,
            "保存加密备份",
            "平安签到备份.pingan-backup",
            "加密备份 (*.pingan-backup)",
        )
        if path:
            data = self.store.snapshot()
            self.start_job(
                lambda: create_backup(
                    data, self.store.directory, path, values[0], values[1]
                ),
                lambda value: self.notify("加密备份已保存：" + value),
            )

    def restore_dialog(self):
        if not self.data_available():
            return
        path, _ = QFileDialog.getOpenFileName(
            self, "选择加密备份", "", "加密备份 (*.pingan-backup)"
        )
        if not path:
            return
        values = self.ask_password("验证备份密码")
        if values is None:
            return

        def preview(payload):
            text = f"备份版本 {payload.get('app_version')} · 时间 {payload.get('created_at')}\n{summary(payload['data'])}\n将替换当前配置，{'替换日志' if payload['logs_included'] else '保留当前日志'}。\n先自动备份现有数据；自动计划关闭，启动项不注册。"
            if self.confirm("确认恢复范围", text):
                self.start_job(
                    lambda: restore_backup(self.store, payload), self.restored
                )

        self.start_job(lambda: read_backup(path, values[0]), preview)

    def restored(self, rollback):
        from logbook import LogBook

        self.journal = LogBook(
            self.store.directory, [a["openid"] for a in self.store.data["accounts"]]
        )
        self.log_page.book = self.journal
        self.load_schedule()
        self.global_choice.data = self.store.data
        self.global_choice.refresh(self.store.data["global_location"])
        self.global_offset.refresh(self.store.data["global_offset"])
        self.refresh()
        self.reload_reliability_preferences()
        self.log_page.refresh_batches()
        self.notify("已恢复，自动计划关闭。恢复前的本地加密回退副本：" + str(rollback))

    def reload_reliability_preferences(self):
        for widget, checked in (
            (self.start_hidden, self.store.data["reliability"]["start_hidden"]),
            (self.prevent_sleep, self.store.data["reliability"]["prevent_sleep"]),
            (self.notification_enabled, self.store.data["notifications"]["enabled"]),
            (self.notification_success, self.store.data["notifications"]["success"]),
        ):
            widget.blockSignals(True)
            widget.setChecked(checked)
            widget.blockSignals(False)
        self.retention_days.setValue(self.store.data["maintenance"]["retention_days"])
        self.onboarding_card.setVisible(not self.store.data["onboarding_done"])
        self.theme = self.store.data["appearance"].get("theme", "dark")
        self.apply_theme()
        self.update_security_status()

    def migrate_dialog(self):
        if self.demo:
            self.notify("离线演示不能切换真实数据模式。")
            return
        if not self.data_available():
            return
        mode = (
            "portable"
            if self.store.directory.resolve() == local_directory().resolve()
            else "local"
        )
        destination = (
            app_directory() / "data" if mode == "portable" else local_directory()
        )
        if destination.exists():

            def preview_existing(value):
                text = f"来源：{self.store.directory}\n现有目标：{destination}\n目标内容：{value}\n将只切换下次启动的目录；不迁移、不合并、不覆盖。当前目录保留，可再次切回。启动项需主动修复。"
                if self.confirm("使用现有数据目录 / 回退模式", text):
                    self.start_job(
                        lambda: set_mode(app_directory(), mode),
                        lambda _: self.notify(
                            "模式已选择，下次启动使用现有目录。请退出并重启，再检查启动项。"
                        ),
                    )

            def inspect_existing():
                instance = SingleInstance(destination)
                try:
                    return summary(Store(destination).data)
                finally:
                    instance.close()

            self.start_job(inspect_existing, preview_existing)
            return
        if not self.confirm(
            "迁移预览",
            f"来源：{self.store.directory}\n目标：{destination}\n{summary(self.store.data)}\n复制全部内容，保留原数据。成功后下次启动使用新目录；本次仍使用原目录。启动项需主动修复。",
        ):
            return

        def migrate():
            instance = SingleInstance(destination)
            try:
                copied = copy_local(self.store, destination)
                try:
                    set_mode(app_directory(), mode)
                except Exception:
                    raise ValueError(
                        f"数据已复制到 {copied}，模式文件无法写入。原模式仍可用；可用 --data-dir 明确打开副本。"
                    ) from None
                return copied
            finally:
                instance.close()

        self.start_job(
            migrate,
            lambda value: self.notify(
                "迁移完成，原目录保留。请退出并重新启动；检查/修复启动项。目标："
                + str(value)
            ),
        )

    def export_diagnostics(self):
        path, _ = QFileDialog.getSaveFileName(
            self, "无凭据诊断导出", "平安签到诊断.json", "JSON (*.json)"
        )
        if path:
            data = self.store.snapshot()
            self.start_job(
                lambda: diagnostic_export(data, path, self.store.directory),
                lambda _: self.notify("诊断已导出，不含凭据、个人身份和详细坐标。"),
            )

    def cleanup_dialog(self):
        if not self.data_available():
            return
        days = self.retention_days.value()
        if not self.persist(
            lambda: self.store.data["maintenance"].update(retention_days=days)
        ):
            return
        data = self.store.snapshot()

        def preview(plan):
            dates = sorted(e["date"] for e in plan["entries"])
            text = f"日志总占用 {plan['total_bytes']/1048576:.2f} MB\n保留最近 {days} 天。可清理 {len(dates)} 批，{plan['delete_bytes']/1048576:.2f} MB\n日期范围：{dates[0] + ' 至 ' + dates[-1] if dates else '无'}\n仅删除完成且没有必要历史关联的批次。当前/未结束/损坏批次与旧文本日志保留。"
            if dates and self.confirm("清理范围预览", text):
                self.start_job(
                    lambda: clear_logs(
                        self.store.directory, plan, self.store.snapshot()
                    ),
                    lambda n: (
                        self.log_page.refresh_batches(),
                        self.notify(f"已清理 {n} 个批次。"),
                    ),
                )
            else:
                self.show_text("日志占用与保留范围", text)

        self.start_job(lambda: log_plan(self.store.directory, data, days), preview)

    def data_available(self):
        if (
            self.busy
            or getattr(self, "history_delete_pending", False)
            or self.data_job
            or self.import_worker
            or self.pending_request
            or self.pending_results is not None
            or self.journal_error
            or self.legacy_log_pending is not None
        ):
            self.notify(
                "请结束当前任务、导入或预览，并保存结果/日志后再操作数据。", True
            )
            return False
        return True

    def start_job(self, action, done, cancel_event=None):
        if not self.data_available():
            return False
        if not self.flush_journal(update=False):
            return False
        self.data_job = BackgroundJob(action, self)
        worker = self.data_job
        self.journal_timer.stop()
        self.log_page.debounce.stop()
        self.log_page.follow_timer.stop()
        self.centralWidget().setEnabled(False)
        self.footer_status.setText("正在后台处理，请稍候…")
        self.job_dialog = None
        if cancel_event:
            from qt_ui import label, button

            dialog = QDialog(self)
            dialog.setWindowTitle("正在检查账号 · 不提交打卡")
            box = QVBoxLayout(dialog)
            box.addWidget(label("仅登录及只读查询；取消会等待当前请求超时或返回。"))
            box.addWidget(
                button("取消检查", callback=lambda: (cancel_event.set(), dialog.hide()))
            )
            dialog.rejected.connect(cancel_event.set)
            dialog.show()
            self.job_dialog = dialog

        def finish():
            self.data_job = None
            self.journal_timer.start(1000)
            self.centralWidget().setEnabled(True)
            self.footer_status.setText("后台处理完成")
            if self.job_dialog:
                self.job_dialog.close()
                self.job_dialog.deleteLater()
                self.job_dialog = None
            if worker.error:
                self.notify(self.scrub(worker.error), True)
            else:
                done(worker.result)
            worker.action = None  # Discard password-capturing closure promptly.
            worker.result = None
            worker.deleteLater()
            if self.closing and not self.data_job:
                self.shutdown()

        worker.finished.connect(finish)

        def launch_when_logs_idle():
            if self.log_page.worker is not None or self.log_page.pending:
                QTimer.singleShot(50, launch_when_logs_idle)
            else:
                worker.start()

        launch_when_logs_idle()
        return True

    def check_account_dialog(self):
        if not self.data_available():
            return
        accounts = self.selected_accounts() or self.store.data["accounts"]
        if not accounts:
            self.notify("请先添加或导入账号。")
            return
        data = self.store.snapshot()
        selected = {a["id"] for a in accounts}
        accounts = [a for a in data["accounts"] if a["id"] in selected]
        cancel = threading.Event()

        def finished(results):
            lines = []
            for value in results:
                lines.append(
                    value["label"]
                    + (
                        "：可登录（未提交打卡）"
                        if value["login_ok"]
                        else "：登录/检查失败"
                    )
                )
                lines.append(" / ".join(value.get("identity", {}).values()))
                for item in value["tasks"]:
                    lines.append(
                        f"任务 {item['name']} · {item['date']} · {item['start']}–{item['end']} · {'已签' if item['signed'] else '未签'} · ID {item['task_id']}"
                    )
                lines.extend(value["issues"])
                lines.append("")
            self.show_text(
                "检查账号 · 不代表实际打卡成功",
                "\n".join(lines) or "检查已取消，未提交打卡。",
                next_steps=True,
            )

        self.start_job(
            lambda: check_accounts(
                accounts,
                data,
                lambda _: None,
                cancel,
                DemoClient if self.demo else SchoolClient,
            ),
            finished,
            cancel,
        )

    def show_text(self, title, text, next_steps=False):
        from qt_ui import button, row, label

        dialog = QDialog(self)
        dialog.setWindowTitle(title)
        dialog.resize(620, 460)
        box = QVBoxLayout(dialog)
        box.addWidget(label(title, "title"))
        editor = QPlainTextEdit(self.scrub(text))
        editor.setReadOnly(True)
        box.addWidget(editor)
        if next_steps:
            box.addWidget(
                ActionRow(
                    button(
                        "设置定位",
                        callback=lambda: (dialog.close(), self.navigate("location")),
                    ),
                    button(
                        "设置计划",
                        callback=lambda: (dialog.close(), self.navigate("schedule")),
                    ),
                )
            )
        box.addWidget(
            button("关闭", callback=dialog.close), 0, Qt.AlignmentFlag.AlignRight
        )
        dialog.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        dialog.show()
        return dialog

    def deliver_system_notice(self, notice):
        if not notice or not self.store.data["notifications"]["enabled"]:
            return
        self.notification_target = notice
        # Offline demos/tests exercise the destination without emitting real Windows notifications.
        if self.demo:
            return
        try:
            from tray import TrayIcon

            if self.tray is None:
                self.tray = TrayIcon(self.handle_event, self)
            self.tray.show()
            self.tray.notify(notice["title"], notice["message"])
        except Exception:
            self.footer_status.setText(
                "系统通知不可用；请通过概览「最近通知」查看结果。"
            )

    def batch_system_notice(self, results, request):
        cycle = (
            self.store.data.get("automation")
            if request["scope"] in ("automatic", "retry")
            else None
        )
        self.deliver_system_notice(
            batch_notice(
                results, self.store.data["notifications"], cycle, request["run_id"]
            )
        )

    def storage_system_notice(self):
        key = (self.active_request or {}).get("run_id", "settings")
        if key == self.last_storage_notice:
            return
        self.last_storage_notice = key
        self.deliver_system_notice(
            {
                "title": "数据保存失败，需要处理",
                "message": "请打开平安签到，检查目录权限并重试保存。任务结果以软件内显示为准。",
                "batch_id": (self.active_request or {}).get("run_id"),
                "account_id": None,
            }
        )

    def open_notification_target(self):
        self.restore_window()
        if (
            self.pending_results is not None
            or self.journal_error
            or self.legacy_log_pending is not None
        ):
            self.navigate("overview")
        elif self.notification_target and self.notification_target.get("page"):
            self.navigate(self.notification_target["page"])
        elif self.notification_target:
            self.open_result_logs(self.notification_target)
        else:
            self.navigate("history")

    def choose_worker_task(self, account, candidates, worker):
        from qt_ui import label

        dialog = QDialog(self)
        dialog.setWindowTitle("选择本次学校任务")
        dialog.resize(600, 370)
        box = QVBoxLayout(dialog)
        box.addWidget(
            label(account["label"] + "：存在多项任务，请明确选择。取消则不提交。")
        )
        choices = QComboBox()
        choices.setAccessibleName("本次学校任务")
        choices.setMinimumContentsLength(20)
        choices.setSizeAdjustPolicy(
            QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon
        )
        for item in candidates:
            choices.addItem(
                self.scrub(item["name"]),
                item["task_id"],
            )
        box.addWidget(choices)
        detail = label("", "muted")

        def update_detail():
            item = candidates[choices.currentIndex()]
            full = self.scrub(
                f"{item['name']}\n服务端日期 {item['date']} · 时段 {item['start']}–{item['end']}\n任务 ID {item['task_id']}"
            )
            detail.setText(full)
            choices.setToolTip(full)

        choices.currentIndexChanged.connect(update_detail)
        update_detail()
        box.addWidget(detail)
        box.addStretch(1)
        actions = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        box.addWidget(actions)
        actions.accepted.connect(dialog.accept)
        actions.rejected.connect(dialog.reject)

        def finish(code):
            worker.task_answer = (
                choices.currentData() if code == QDialog.DialogCode.Accepted else None
            )
            worker.task_ready.set()
            self.task_dialog = None
            dialog.deleteLater()

        dialog.finished.connect(finish)
        self.task_dialog = dialog
        dialog.show()
