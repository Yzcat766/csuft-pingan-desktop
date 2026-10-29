"""Compact overview widgets; all actions reuse existing execution and log flows."""

import json
from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QWidget,
    QVBoxLayout,
    QHBoxLayout,
    QGridLayout,
    QDialog,
    QDialogButtonBox,
    QProgressBar,
    QAbstractItemView,
    QTableWidgetItem,
)
from overview import today_metrics, recent_batches, summary_text, time_rank, STATUSES
from storage import now_shanghai
from locations import resolve_location
from position_offset import resolve_offset
from readiness import check_ready


class OverviewUI:
    def build_overview_page(self):
        from qt_ui import PAGE_INDEX, card, label, button, row, table

        layout = self.page_layouts[PAGE_INDEX["overview"]]
        layout.setSpacing(12)
        self.stat_widget = QWidget()
        self.stat_grid = QGridLayout(self.stat_widget)
        self.stat_grid.setContentsMargins(0, 0, 0, 0)
        self.stat_grid.setSpacing(10)
        self.stats, self.stat_values = [], []
        for title in (
            "参与批量与定时",
            "今日有成功记录",
            "今日待核对账号",
            "下次自动执行",
        ):
            frame, box = card()
            box.setContentsMargins(14, 10, 14, 10)
            box.setSpacing(4)
            box.addWidget(label(title, "muted"))
            value = label("—", "section")
            box.addWidget(value)
            self.stats.append(frame)
            self.stat_values.append(value)
        layout.addWidget(self.stat_widget)
        self.today_scope = label("", "muted")
        layout.addWidget(self.today_scope)
        self.overview_empty, box = card("添加第一个账号")
        box.addWidget(
            label(
                "导入已有 OpenID 或添加账号后，即可手动执行。自动计划可稍后设置。",
                "muted",
            )
        )
        box.addLayout(
            row(
                button("添加账号", "plus", self.add_account, True),
                button("导入账号", "import", self.import_file),
            )
        )
        box.addWidget(
            button("查看使用引导", callback=self.open_onboarding),
            0,
            Qt.AlignmentFlag.AlignLeft,
        )
        layout.addWidget(self.overview_empty)
        self.save_alert, box = card()
        box.setContentsMargins(14, 10, 14, 10)
        self.save_alert_text = label("", "section")
        box.addWidget(self.save_alert_text)
        self.retry_save_button = button("重试保存结果", "save", self.retry_results)
        self.overview_retry_logs = button(
            "重试保存日志", "save", self.retry_overview_logs
        )
        box.addLayout(row(self.retry_save_button, self.overview_retry_logs))
        layout.addWidget(self.save_alert)
        self.save_alert.hide()
        self.run_card, running = card()
        running.setContentsMargins(16, 12, 16, 12)
        running.setSpacing(8)
        heading = QHBoxLayout()
        heading.addWidget(label("当前执行", "section"), 1)
        self.cancel_button = button("取消本次任务", "close", self.cancel_run)
        heading.addWidget(self.cancel_button)
        running.addLayout(heading)
        self.run_stage = label("正在准备任务", "muted")
        running.addWidget(self.run_stage)
        self.progress = QProgressBar()
        self.progress.setTextVisible(False)
        self.progress.setFormat("已完成 %v / %m")
        running.addWidget(self.progress)
        self.run_task = label("任务尚未查询", "muted")
        running.addWidget(self.run_task)
        self.batch_summary = label("", "muted")
        running.addWidget(self.batch_summary)
        self.preview_card = QWidget()
        preview = QVBoxLayout(self.preview_card)
        preview.setContentsMargins(0, 0, 0, 0)
        preview.setSpacing(5)
        self.preview_summary = label("", "muted")
        preview.addWidget(self.preview_summary)
        self.preview_toggle = button("展开位置详情")
        self.preview_toggle.setCheckable(True)
        preview.addWidget(self.preview_toggle, 0, Qt.AlignmentFlag.AlignLeft)
        self.preview_text = label("", "muted")
        self.preview_text.hide()
        self.preview_toggle.toggled.connect(self.toggle_position_detail)
        preview.addWidget(self.preview_text)
        running.addWidget(self.preview_card)
        self.preview_card.hide()
        layout.addWidget(self.run_card)
        self.run_card.hide()
        self.build_readiness(layout)
        self.attention_card, box = card("待处理")
        box.setContentsMargins(14, 10, 14, 10)
        self.attention_rows = QVBoxLayout()
        self.attention_rows.setSpacing(6)
        box.addLayout(self.attention_rows)
        layout.addWidget(self.attention_card)
        self.recent_card, box = card("最近执行批次")
        box.setContentsMargins(16, 12, 16, 12)
        self.recent_table = table(["执行时间", "执行方式", "账号数", "结果摘要"])
        self.recent_table.setMinimumHeight(150)
        self.recent_table.setMaximumHeight(275)
        self.recent_table.setSelectionMode(
            QAbstractItemView.SelectionMode.SingleSelection
        )
        for i, width in enumerate((145, 160, 65)):
            self.recent_table.setColumnWidth(i, width)
        self.recent_table.cellClicked.connect(self.recent_detail)
        box.addWidget(self.recent_table)
        self.recent_empty = label("尚无执行记录；可使用右上角主按钮开始。", "muted")
        box.addWidget(self.recent_empty)
        box.addWidget(
            button("查看全部历史", "history", lambda: self.navigate("history")),
            0,
            Qt.AlignmentFlag.AlignLeft,
        )
        layout.addWidget(self.recent_card)
        layout.addStretch()
        self.arrange_stats()

    def toggle_position_detail(self, expanded):
        self.preview_text.setVisible(expanded)
        self.preview_toggle.setText("收起位置详情" if expanded else "展开位置详情")

    def retry_overview_logs(self):
        saved = self.flush_journal()
        if self.legacy_log_pending is not None:
            lines, outcome = self.legacy_log_pending
            try:
                self.store.write_log(lines, outcome)
                self.legacy_log_pending = None
            except OSError:
                saved = False
        self.notify(
            "日志已保存。" if saved else "日志仍未保存，请检查目录权限后重试。",
            not saved,
        )
        self.refresh_stats()
        if saved and self.pending_results is None:
            self.timer.start(1000)
        return saved

    def build_readiness_strip(self, layout):
        from qt_ui import card, label, button, row

        self.readiness_card, box = card()
        box.setContentsMargins(14, 10, 14, 10)
        box.setSpacing(8)
        self.readiness_state = label("", "muted")
        self.readiness_toggle = button("查看详情")
        self.readiness_toggle.setMaximumWidth(100)
        self.readiness_toggle.setCheckable(True)
        self.plan_link = button("设置计划", callback=lambda: self.navigate("schedule"))
        self.plan_link.setMaximumWidth(100)
        box.addLayout(row(self.readiness_state, self.plan_link, self.readiness_toggle))
        self.retry_pending_info = label("", "muted")
        box.addWidget(self.retry_pending_info)
        self.retry_pending_info.hide()
        self.readiness_details = QWidget()
        details = QVBoxLayout(self.readiness_details)
        details.setContentsMargins(0, 0, 0, 0)
        self.readiness_label = label("", "muted")
        self.network_state = label("网络：未检测（未知）", "muted")
        details.addWidget(self.readiness_label)
        self.readiness_fixes = QVBoxLayout()
        details.addLayout(self.readiness_fixes)
        details.addWidget(self.network_state)
        details.addLayout(
            row(
                button("检查本地配置", callback=self.inspect_readiness),
                button("按需检测网络", callback=self.inspect_network),
            )
        )
        self.readiness_details.hide()
        self.readiness_toggle.toggled.connect(self.readiness_details.setVisible)
        box.addWidget(self.readiness_details)
        layout.addWidget(self.readiness_card)
        self.onboarding_card, box = card()
        box.setContentsMargins(14, 8, 14, 8)
        box.addLayout(
            row(
                label("首次使用：可按需完成账号和定位设置", "muted"),
                button("查看引导", callback=self.open_onboarding),
                button("跳过", callback=self.dismiss_onboarding),
            )
        )
        layout.addWidget(self.onboarding_card)
        self._readiness_signature = None

    def open_onboarding(self):
        from qt_ui import label, button

        dialog = QDialog(self)
        dialog.setWindowTitle("首次使用引导")
        dialog.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        dialog.resize(560, 380)
        box = QVBoxLayout(dialog)
        box.setContentsMargins(22, 20, 22, 20)
        accounts = self.store.data["accounts"]
        usable = [
            a for a in accounts if a.get("openid") and not a.get("credential_error")
        ]
        located = []
        for account in usable:
            try:
                resolve_location(account, self.store.data)
                resolve_offset(account, self.store.data)
                located.append(account)
            except ValueError:
                pass
        box.addWidget(
            label(
                f"账号：已保存 {len(usable)} / {len(accounts)} 个可读取凭据；尚未验证登录。"
            )
        )
        box.addWidget(
            button(
                "添加 / 导入账号",
                callback=lambda: (dialog.close(), self.navigate("accounts")),
            )
        )
        box.addWidget(
            label(
                f"定位：{len(located)} / {len(usable)} 个账号配置可解析；任务距离须执行时查询。"
            )
        )
        box.addWidget(
            button(
                "设置定位", callback=lambda: (dialog.close(), self.navigate("location"))
            )
        )
        box.addWidget(
            label(
                "自动计划："
                + (
                    "已启用"
                    if self.store.data["schedule"]["enabled"]
                    else "已关闭，可跳过并只使用手动执行"
                )
            )
        )
        box.addWidget(
            button(
                "按需设置计划",
                callback=lambda: (dialog.close(), self.navigate("schedule")),
            )
        )
        box.addWidget(
            label(
                "引导完成只表示已保存基础配置；不代表已登录、所有学校任务已完成。",
                "muted",
            )
        )
        finish = button(
            "完成引导", callback=lambda: (self.dismiss_onboarding(), dialog.close())
        )
        finish.setEnabled(bool(usable) and len(located) == len(usable))
        box.addWidget(finish)
        box.addWidget(
            button(
                "跳过引导", callback=lambda: (self.dismiss_onboarding(), dialog.close())
            )
        )
        dialog.show()

    @staticmethod
    def clear_rows(layout):
        while layout.count():
            item = layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()

    def update_readiness_projection(self, force=False):
        from qt_ui import label, button, row

        data = self.store.data
        signature = json.dumps(
            {
                "accounts": [
                    {
                        "id": a["id"],
                        "enabled": a.get("enabled", True),
                        "credential": bool(a.get("openid")),
                        "error": bool(a.get("credential_error")),
                        "location": a.get("location"),
                        "offset": a.get("offset"),
                    }
                    for a in data["accounts"]
                ],
                "locations": data["locations"],
                "global_location": data["global_location"],
                "global_offset": data["global_offset"],
                "schedule": data["schedule"],
                "unsaved": self.schedule_value() != data["schedule"],
                "write_error": bool(getattr(self, "readiness_write_error", False)),
                "pending": self.pending_results is not None,
                "journal": bool(self.journal_error)
                or self.legacy_log_pending is not None,
            },
            sort_keys=True,
        )
        if signature == self._readiness_signature and not force:
            return
        self._readiness_signature = signature
        issues = check_ready(
            data,
            self.store.directory,
            self.schedule_value() != data["schedule"],
            probe=False,
        )
        if getattr(self, "readiness_write_error", False):
            issues.append(("settings", "数据目录不可写，请调整权限并重新检查"))
        if (
            self.pending_results is not None
            or self.journal_error
            or self.legacy_log_pending is not None
        ):
            issues.append(("overview", "结果或日志未保存，自动执行暂停"))
        self.readiness_issues = list(dict.fromkeys(issues))
        enabled = data["schedule"]["enabled"]
        self.readiness_state.setText(
            f"自动计划有 {len(self.readiness_issues)} 项待修正"
            if enabled and self.readiness_issues
            else (
                "自动计划已就绪（本地配置）"
                if enabled
                else "自动打卡已关闭 · 手动执行不受计划开关影响"
            )
        )
        self.readiness_label.setText(
            (
                "自动计划已启用。"
                if enabled
                else "自动打卡已关闭（计划未启用）；仍可手动执行。"
            )
            + "\n"
            + (
                "\n".join("• " + text for _, text in self.readiness_issues)
                or "已保存的本地配置可用；服务端任务结果需执行后确认。"
            )
        )
        self.clear_rows(self.readiness_fixes)
        names = {
            "accounts": "修正账号",
            "location": "修正定位",
            "schedule": "修正计划",
            "settings": "检查数据设置",
            "overview": "查看未保存内容",
        }
        for page, text in self.readiness_issues:
            container = QWidget()
            container.setLayout(
                row(
                    label(text, "muted"),
                    button(
                        names.get(page, "修正"),
                        callback=lambda page=page: self.navigate(page),
                    ),
                )
            )
            self.readiness_fixes.addWidget(container)

    def refresh_overview_state(self, moment=None):
        from qt_ui import label, button, row

        moment = moment or now_shanghai()
        data = self.store.data
        metrics = today_metrics(data, moment.date().isoformat())
        empty = not data["accounts"]
        self.stat_widget.setVisible(not empty)
        self.today_scope.setVisible(not empty)
        self.overview_empty.setVisible(empty)
        self.readiness_card.setVisible(not empty and not self.busy)
        self.onboarding_card.setVisible(
            not empty and not self.busy and not data["onboarding_done"]
        )
        self.recent_card.setVisible(
            not empty or bool(data["results"]) or bool(data.get("overview_batches"))
        )
        self.today_scope.setText(
            f"{moment:%Y-%m-%d} 北京时间 · 成功记录含停用账号的临时执行，不代表全部必需任务完成"
        )
        self.stat_values[0].setText(str(metrics["enabled"]))
        self.stat_values[1].setText(str(metrics["success"]))
        self.stat_values[2].setText(f"{metrics['review']} · 失败 {metrics['failure']}")
        self.stat_values[2].setToolTip(
            f"当前账号中：今日最近任务失败 {metrics['failure']} 个；无今日成功记录 {metrics['unknown']} 个。两者去重，未查询不等于学校端未签到。"
        )
        self.save_alert.setVisible(
            self.pending_results is not None
            or bool(self.journal_error)
            or self.legacy_log_pending is not None
        )
        self.retry_save_button.setVisible(self.pending_results is not None)
        self.overview_retry_logs.setVisible(
            bool(self.journal_error) or self.legacy_log_pending is not None
        )
        self.save_alert_text.setText("结果 / 日志尚未保存 · 自动执行暂停，请先重试保存")
        if self.busy:
            counts = {
                s: sum(r.get("status") == s for r in self.run_results) for s in STATUSES
            }
            self.batch_summary.setText("本批：" + summary_text(counts))
        elif (
            hasattr(self, "recent_batches")
            and self.recent_batches
            and self.recent_batches[0].get("date") == moment.date().isoformat()
        ):
            self.batch_summary.setText(
                "最近批次：" + summary_text(self.recent_batches[0]["summary"])
            )
        else:
            self.batch_summary.setText("")
        token = (
            signature_key(data),
            moment.date().isoformat(),
            self.pending_results is not None,
            bool(self.journal_error),
            self.legacy_log_pending is not None,
            tuple(getattr(self, "readiness_issues", [])),
            self.busy,
        )
        if getattr(self, "_attention_token", None) == token:
            return
        self._attention_token = token
        self.clear_rows(self.attention_rows)
        attention = []
        failures = metrics["failures"]
        if failures:
            target = max(failures, key=lambda r: time_rank(r.get("finished_at")))
            attention.append(
                (
                    f"今日有 {metrics['failure']} 个账号的最近任务失败 / 待核对",
                    "查看失败",
                    lambda target=target: self.open_result_logs(target),
                )
            )
        for page, text in getattr(self, "readiness_issues", []):
            if page in ("accounts", "location", "settings"):
                attention.append((text, "修正", lambda page=page: self.navigate(page)))
        for text, caption, action in attention[:3]:
            container = QWidget()
            container.setLayout(
                row(label(self.scrub(text), "muted"), button(caption, callback=action))
            )
            self.attention_rows.addWidget(container)
        self.attention_card.setVisible(bool(attention) and not empty and not self.busy)

    def refresh_overview_recent(self):
        from qt_ui import put

        sorting = self.recent_table.isSortingEnabled()
        self.recent_table.setSortingEnabled(False)
        self.recent_batches = recent_batches(self.store.data)
        self.recent_results = [
            r for batch in self.recent_batches for r in batch["results"]
        ]
        names = {
            "all": "批量打卡",
            "selected": "所选账号",
            "single": "单账号",
            "today": "核对今日任务",
            "automatic": "自动计划",
            "retry": "自动重试",
        }
        put(
            self.recent_table,
            [
                (
                    (
                        time_rank(b["finished_at"]).strftime("%m-%d %H:%M")
                        if time_rank(b["finished_at"]).year > 1
                        else "时间未知"
                    ),
                    self.scrub(names.get(b.get("scope"), b["mode"])),
                    str(b["account_count"])
                    + ("（保留）" if b["state"] == "unknown" else ""),
                    summary_text(b["summary"])
                    + (" · 未完整结束" if b["state"] == "interrupted" else ""),
                )
                for b in self.recent_batches
            ],
        )
        for index, batch in enumerate(self.recent_batches):
            for col in range(self.recent_table.columnCount()):
                self.recent_table.item(index, col).setData(
                    Qt.ItemDataRole.UserRole, batch
                )
        self.recent_table.setSortingEnabled(sorting)
        self.recent_table.setVisible(bool(self.recent_batches))
        self.recent_empty.setVisible(not self.recent_batches)
        self.recent_table.setFixedHeight(min(285, 38 + 52 * len(self.recent_batches)))

    def show_batch_detail(self, batch):
        from qt_ui import label, button, row, table, put

        dialog = QDialog(self)
        dialog.setWindowTitle("执行批次详情")
        dialog.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        dialog.resize(820, 470)
        box = QVBoxLayout(dialog)
        box.addWidget(
            label(
                self.scrub(
                    f"{batch['mode']} · 账号 {batch['account_count']} · {summary_text(batch['summary'])}"
                ),
                "section",
            )
        )
        box.addWidget(
            label(
                f"已保留 {len(batch['results'])} 条账号结果；更早结果请查看关联日志。",
                "muted",
            )
        )
        listing = table(["账号", "任务", "结果", "说明"])
        put(
            listing,
            [
                (
                    self.scrub(r.get("label", "")),
                    self.scrub(r.get("task", "")),
                    STATUSES.get(r["status"], "未知"),
                    self.scrub(r.get("message", "")),
                )
                for r in batch["results"]
            ],
        )
        for index, result in enumerate(batch["results"]):
            for col in range(listing.columnCount()):
                listing.item(index, col).setData(Qt.ItemDataRole.UserRole, result)
        listing.cellClicked.connect(
            lambda r, c: self.show_detail(
                listing.item(r, c).data(Qt.ItemDataRole.UserRole)
            )
        )
        box.addWidget(listing)
        logs = button(
            "查看关联日志",
            "log",
            lambda: (dialog.close(), self.open_overview_batch_logs(batch["batch_id"])),
        )
        logs.setEnabled(bool(batch["batch_id"]))
        box.addLayout(row(logs, button("关闭", callback=dialog.close)))
        dialog.show()

    def open_overview_batch_logs(self, identity):
        self.navigate("log")
        self.log_page.clear_filters()
        self.log_page.refresh_batches(identity)


def signature_key(data):
    return json.dumps(
        {
            "accounts": [
                (a["id"], a.get("credential_error")) for a in data["accounts"]
            ],
            "results": [
                (
                    r.get("run_id"),
                    r.get("account_id"),
                    r.get("task_id"),
                    r.get("date"),
                    r.get("status"),
                    r.get("finished_at"),
                )
                for r in data["results"]
            ],
        },
        sort_keys=True,
    )
