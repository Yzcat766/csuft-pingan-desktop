"""Paged journal viewer. File reads and exports never run in the UI thread."""

from PySide6.QtCore import Qt, QThread, Signal, QTimer, QDate
from PySide6.QtGui import QColor, QTextCursor
from PySide6.QtWidgets import (
    QWidget,
    QVBoxLayout,
    QHBoxLayout,
    QComboBox,
    QLineEdit,
    QDateEdit,
    QLabel,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QHeaderView,
    QAbstractItemView,
    QPlainTextEdit,
    QApplication,
    QFileDialog,
)
from logbook import LEVELS, event_text, PAGE_SIZE
from ui_style import PALETTES, icon
from desktop_common import copy_text, CLIPBOARD_ERROR
from ui_components import WrapToolbar
from form_layout import size_field


def append_quick(editor, line):
    """Insert with an independent cursor; retain selection and reading position."""
    bar = editor.verticalScrollBar()
    old = bar.value()
    following = old >= bar.maximum() - 2 and not editor.textCursor().hasSelection()
    cursor = QTextCursor(editor.document())
    cursor.movePosition(QTextCursor.MoveOperation.End)
    if not editor.document().isEmpty():
        cursor.insertBlock()
    cursor.insertText(line)
    bar.setValue(bar.maximum() if following else old)


class JournalWorker(QThread):
    ready = Signal(object)

    def __init__(self, book, job, parent):
        super().__init__(parent)
        self.book, self.job = book, job

    def run(self):
        result = {"job": self.job}
        try:
            if self.job["kind"] == "list":
                result.update(self.book.list_batches(self.job["offset"]))
            elif self.job["kind"] == "export":
                result["exported"] = self.book.export(
                    self.job["record"], self.job["filters"], self.job["destination"]
                )
            else:
                result.update(
                    self.book.query(
                        self.job["record"],
                        self.job["filters"],
                        self.job["offset"],
                        cancelled=self.isInterruptionRequested,
                    )
                )
        except Exception:
            result["error"] = (
                "日志无法读取或写入，请检查日志文件和目录权限；原文件不会被覆盖。"
            )
        self.ready.emit(result)


class LogPage(QWidget):
    def __init__(self, host, book):
        super().__init__(host)
        self.host, self.book = host, book
        self.worker = None
        self.stopping = False
        self.pending = []
        self.revision = 0
        self.list_revision = 0
        self.record = None
        self.list_offset = 0
        self.offset = -1
        self.following = True
        self.rendering = False
        self.render_signature = None
        self.display_events = []
        self.unlinked = False
        self.batch_target = None
        self.account_target = None
        self.last_total = 0
        self.layout = QVBoxLayout(self)
        self.layout.setContentsMargins(0, 0, 0, 0)
        self.layout.setSpacing(12)
        self.batch_choice = QComboBox()
        self.batch_choice.setAccessibleName("执行批次或旧日志文件")
        self.batch_choice.currentIndexChanged.connect(self.select_batch)
        self.list_prev = self.make_button("较新批次", lambda: self.page_batches(-50))
        self.list_next = self.make_button("较早批次", lambda: self.page_batches(50))
        self.add_row(self.batch_choice, self.list_prev, self.list_next)
        self.batch_info = self.text("尚无运行日志。执行任务后会按批次保存。")
        self.layout.addWidget(self.batch_info)
        self.account_filter = QComboBox()
        self.account_filter.setAccessibleName("日志账号筛选")
        self.date_mode = QComboBox()
        self.date_mode.addItems(["全部日期", "指定日期"])
        self.date_filter = QDateEdit(QDate.currentDate())
        self.date_filter.setCalendarPopup(True)
        self.date_filter.setDisplayFormat("yyyy-MM-dd")
        self.date_filter.setAccessibleName("日志日期筛选")
        size_field(self.date_filter)
        self.level_filter = QComboBox()
        self.level_filter.addItem("全部级别", None)
        for key, title in LEVELS.items():
            self.level_filter.addItem(title, key)
        self.keyword = QLineEdit()
        self.keyword.setPlaceholderText("搜索消息或步骤")
        self.keyword.setClearButtonEnabled(True)
        self.clear_button = self.make_button("清除筛选", self.clear_filters)
        self.add_row(
            self.account_filter,
            self.date_mode,
            self.date_filter,
            self.level_filter,
            self.keyword,
        )
        self.conditions = self.text("筛选：全部账号 · 全部日期 · 全部级别")
        self.add_row(self.conditions, self.clear_button)
        self.warning = self.text("")
        self.warning.hide()
        self.layout.addWidget(self.warning)
        self.table = QTableWidget(0, 5)
        self.table.setHorizontalHeaderLabels(["时间", "账号", "级别", "步骤", "消息"])
        self.table.verticalHeader().hide()
        self.table.verticalHeader().setDefaultSectionSize(46)
        self.table.setShowGrid(False)
        self.table.setWordWrap(True)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.table.horizontalHeader().setStretchLastSection(True)
        self.table.horizontalHeader().setSectionResizeMode(
            QHeaderView.ResizeMode.Interactive
        )
        for col, width in enumerate((155, 170, 65, 105, 380)):
            self.table.setColumnWidth(col, width)
        self.table.setMinimumHeight(220)
        self.table.itemSelectionChanged.connect(self.selection_changed)
        self.table.verticalScrollBar().valueChanged.connect(self.scrolled)
        self.layout.addWidget(self.table, 1)
        self.empty = self.text("尚无运行日志。")
        self.layout.addWidget(self.empty)
        self.detail = QPlainTextEdit()
        self.detail.setReadOnly(True)
        self.detail.setPlaceholderText(
            "选择日志行可阅读和复制完整消息；Ctrl/Shift 支持多选。"
        )
        self.detail.setMaximumHeight(125)
        self.detail.setMinimumHeight(70)
        self.detail.hide()
        self.layout.addWidget(self.detail)
        self.older = self.make_button("上一页", lambda: self.page_events(-PAGE_SIZE))
        self.newer = self.make_button("下一页", lambda: self.page_events(PAGE_SIZE))
        self.latest = self.make_button("回到最新", self.go_latest)
        self.page_info = self.text("每页最多 200 条")
        self.add_row(self.older, self.newer, self.latest, self.page_info)
        self.add_row(
            self.make_button("复制选中内容", self.copy_selected, "copy"),
            self.make_button("导出筛选结果", self.export_dialog, "save"),
            self.make_button(
                "打开日志目录", lambda: host.open_directory(book.root), "folder"
            ),
            self.make_button("旧文本目录", host.open_legacy_logs, "folder"),
            self.make_button("重试保存日志", host.flush_journal, "save"),
        )
        self.debounce = QTimer(self)
        self.debounce.setSingleShot(True)
        self.debounce.timeout.connect(self.reload_events)
        self.follow_timer = QTimer(self)
        self.follow_timer.setSingleShot(True)
        self.follow_timer.timeout.connect(self.settle_scroll)
        self.table.verticalScrollBar().rangeChanged.connect(self.range_changed)
        self.account_filter.currentIndexChanged.connect(self.changed)
        self.date_mode.currentIndexChanged.connect(self.changed)
        self.date_filter.dateChanged.connect(self.changed)
        self.level_filter.currentIndexChanged.connect(self.changed)
        self.keyword.textChanged.connect(self.changed)
        self.date_filter.hide()
        self.refresh_batches()

    def text(self, value):
        item = QLabel(value)
        item.setWordWrap(True)
        item.setTextFormat(Qt.TextFormat.PlainText)
        return item

    def make_button(self, text, callback, glyph=None):
        item = QPushButton(text)
        if glyph:
            item.setIcon(icon(glyph))
            item.setProperty("glyph", glyph)
        item.clicked.connect(lambda checked=False: callback())
        return item

    def add_row(self, *widgets):
        self.layout.addWidget(WrapToolbar(*widgets))

    def launch(self, job):
        if self.stopping and job["kind"] != "export":
            return
        if self.worker:
            if job["kind"] != "export":
                self.pending = [p for p in self.pending if p["kind"] != job["kind"]]
            self.pending.append(job)
            return
        worker = JournalWorker(self.book, job, self)
        self.worker = worker
        worker.ready.connect(self.loaded)
        worker.finished.connect(self.completed)
        worker.start()

    def completed(self):
        worker = self.worker
        self.worker = None
        worker.deleteLater()
        if self.pending:
            self.launch(self.pending.pop(0))
        if (
            (self.host.closing or self.stopping)
            and not self.host.busy
            and not self.host.import_worker
        ):
            self.host.shutdown()

    def refresh_batches(self, batch_id=None, account_id=None):
        if batch_id:
            self.list_revision += 1
            self.unlinked = False
            self.batch_target, self.account_target = batch_id, account_id
            self.list_offset = 0
        self.launch(
            {"kind": "list", "offset": self.list_offset, "revision": self.list_revision}
        )

    def loaded(self, result):
        if self.stopping:
            return
        job = result["job"]
        if result.get("error"):
            self.warning.setText(result["error"])
            self.warning.show()
            return
        if job["kind"] == "export":
            self.host.notify(f"已导出 {result['exported']} 条筛选日志。")
            return
        if job["kind"] == "list":
            if job["revision"] != self.list_revision:
                return
            previous = self.record["key"] if self.record else None
            previous_account = self.account_filter.currentData()
            records = result["batches"]
            target = (
                next(
                    (r for r in records if r.get("batch_id") == self.batch_target), None
                )
                if self.batch_target
                else None
            )
            if self.batch_target and not target:
                if result["offset"] + 50 < result["total"]:
                    self.list_offset += 50
                    self.refresh_batches()
                    return
                self.warning.setText(
                    "该记录没有可用的结构化日志。旧文本不能精确关联批次或账号，请查看旧文本文件。"
                )
                self.warning.show()
                self.batch_target = self.account_target = None
                self.show_unlinked()
                return
            self.batch_choice.blockSignals(True)
            self.batch_choice.clear()
            for record in records:
                text = (
                    record.get("started_at", "").replace("T", " ")[:19]
                    + " · "
                    + record.get("mode", "")
                    + " · "
                    + record.get("filename", "")
                )
                self.batch_choice.addItem(text.strip(" ·"), record)
            selected = (
                target
                or next((r for r in records if r["key"] == previous), None)
                or next((r for r in records if r.get("kind") == "batch"), None)
                or (records[0] if records else None)
            )
            if selected:
                self.batch_choice.setCurrentIndex(
                    next(
                        i for i, r in enumerate(records) if r["key"] == selected["key"]
                    )
                )
            self.list_prev.setEnabled(result["offset"] > 0)
            self.list_next.setEnabled(result["offset"] + 50 < result["total"])
            changed = (
                not self.record or not selected or self.record["key"] != selected["key"]
            )
            if self.unlinked and not target:
                self.show_unlinked()
            elif changed or target:
                self.select_batch()
            elif selected:
                self.record = selected
                self.update_info()
                self.account_filter.setCurrentIndex(
                    self.account_filter.findData(previous_account)
                )
                self.reload_events()
            if target:
                identity = self.account_target
                self.batch_target = self.account_target = None
                if identity:
                    self.account_filter.setCurrentIndex(
                        self.account_filter.findData(identity)
                    )
            self.batch_choice.blockSignals(False)
            if not selected:
                self.record = None
                self.table.setRowCount(0)
                self.empty.setText(
                    "尚无运行日志。执行任务后会自动保存，旧文本文件也可在这里查看。"
                )
                self.empty.show()
            return
        if (
            job["revision"] != self.revision
            or not self.record
            or job["record"]["key"] != self.record["key"]
        ):
            return
        self.render(result, job)

    def update_info(self):
        record = self.record
        if record.get("kind") != "batch":
            self.batch_info.setText(
                "旧文本文件："
                + record.get("filename", "")
                + "。无可靠批次开始时间、账号数或结果摘要，按原文查看。"
                if record.get("kind") == "legacy"
                else "日志元数据损坏，无法获取可靠批次信息；原文件保留。"
            )
            return
        names = {
            "running": (
                "执行中"
                if self.host.busy
                and self.host.active_request
                and self.host.active_request["run_id"] == record.get("batch_id")
                else "未记录结束（可能中断）"
            ),
            "finished": "已结束",
            "cancelled": "已取消",
            "interrupted": "未完整结束",
            "legacy": "旧文本",
            "broken": "记录损坏",
            "skipped": "已跳过",
            "not_started": "未启动",
        }
        summary = " · ".join(
            f"{title} {record.get('summary', {}).get(key, 0)}"
            for key, title in (
                ("success", "成功"),
                ("failure", "失败"),
                ("cancelled", "取消"),
                ("skipped", "跳过"),
            )
        )
        if not record.get("summary"):
            summary = "尚无完成结果摘要"
        self.batch_info.setText(
            f"开始：{record.get('started_at', '').replace('T', ' ')[:19] or '旧文件无可靠开始时间'} · {record.get('mode', '')}\n"
            f"账号数：{record.get('account_count', '未知')} · {names.get(record.get('status'), '未知状态')} · {summary}"
        )

    def select_batch(self, *_):
        if _:
            self.list_revision += 1
            self.batch_target = self.account_target = None
        self.unlinked = False
        self.record = self.batch_choice.currentData()
        if not self.record:
            return
        self.update_info()
        self.account_filter.blockSignals(True)
        self.account_filter.clear()
        self.account_filter.addItem("全部账号与批次消息", None)
        self.account_filter.addItem("仅批次 / 系统消息", "")
        for account in self.record.get("accounts", []):
            self.account_filter.addItem(
                f"{account['label']} · {account['id'][:6]}", account["id"]
            )
        self.account_filter.blockSignals(False)
        self.changed()

    def filters(self):
        return {
            "account_id": self.account_filter.currentData(),
            "date": (
                self.date_filter.date().toString("yyyy-MM-dd")
                if self.date_mode.currentIndex()
                else None
            ),
            "level": self.level_filter.currentData(),
            "keyword": self.keyword.text().strip(),
        }

    def changed(self, *_):
        self.date_filter.setVisible(self.date_mode.currentIndex() == 1)
        self.offset = -1
        self.following = True
        self.revision += 1
        f = self.filters()
        self.conditions.setText(
            f"筛选：{self.account_filter.currentText()} · {f['date'] or '全部日期'} · "
            f"{self.level_filter.currentText()} · 关键词：{f['keyword'] or '无'}"
        )
        self.debounce.start(120)

    def clear_filters(self):
        for control in (self.account_filter, self.date_mode, self.level_filter):
            control.blockSignals(True)
            control.setCurrentIndex(0)
            control.blockSignals(False)
        self.keyword.blockSignals(True)
        self.keyword.clear()
        self.keyword.blockSignals(False)
        self.changed()

    def reload_events(self):
        if self.record:
            self.launch(
                {
                    "kind": "query",
                    "record": self.record,
                    "filters": self.filters(),
                    "offset": -1 if self.following else max(0, self.offset),
                    "revision": self.revision,
                }
            )

    def render(self, result, job):
        events = result["events"]
        signature = (job["record"]["key"], self.revision)
        previous = self.display_events
        prefix = events[: len(previous)] == previous
        selected = (
            bool(self.table.selectedItems()) or self.detail.textCursor().hasSelection()
        )
        same = self.render_signature == signature
        # A moving tail must never destroy the user's selection or reading page.
        if same and not prefix and (selected or not self.following):
            self.latest.setText(f"回到最新（共 {result['total']} 条）")
            return
        self.rendering = True
        bar = self.table.verticalScrollBar()
        old_scroll = bar.value()
        self.table.blockSignals(True)
        start = len(previous) if same and prefix else 0
        if start == 0:
            self.table.setRowCount(0)
            self.detail.clear()
            self.detail.hide()
        self.table.setRowCount(len(events))
        for row in range(start, len(events)):
            event = events[row]
            who = event.get("account_label") or (
                "批次" if event.get("batch_id") else "系统"
            )
            if event.get("account_id"):
                who += " · " + event["account_id"][:6]
            values = [
                event.get("time", "").replace("T", " ")[:19] or "未知",
                who,
                LEVELS.get(event.get("level"), "未知"),
                event.get("step", ""),
                event["message"][:2000]
                + ("…（选中查看完整消息）" if len(event["message"]) > 2000 else ""),
            ]
            for col, text in enumerate(values):
                item = QTableWidgetItem(text)
                item.setToolTip(text)
                item.setData(Qt.ItemDataRole.UserRole, event["seq"])
                if event.get("level") in ("error", "warning"):
                    item.setForeground(
                        QColor(
                            PALETTES[self.host.theme][
                                "failure" if event["level"] == "error" else "cancelled"
                            ]
                        )
                    )
                self.table.setItem(row, col, item)
        self.table.resizeRowsToContents()
        for row in range(len(events)):
            self.table.setRowHeight(row, min(200, max(58, self.table.rowHeight(row))))
        self.display_events = events
        self.render_signature = signature
        self.offset, self.last_total = result["offset"], result["total"]
        self.table.blockSignals(False)
        bar.setValue(bar.maximum() if self.following and not selected else old_scroll)
        self.rendering = False
        if self.following and not selected:
            self.follow_timer.start(30)
        self.older.setEnabled(self.offset > 0)
        self.newer.setEnabled(self.offset + len(events) < result["total"])
        self.latest.setText("回到最新")
        self.page_info.setText(
            f"显示 {self.offset+1 if events else 0}～{self.offset+len(events)} / {result['total']} 条"
        )
        self.warning.setText(result.get("warning", ""))
        self.warning.setVisible(bool(result.get("warning")))
        self.empty.setText(
            "此批次暂无日志。"
            if not self.filters()["keyword"] and result["total"] == 0
            else "没有匹配结果。请调整或清除筛选条件。"
        )
        self.empty.setVisible(not events)

    def selection_changed(self):
        rows = sorted({item.row() for item in self.table.selectedItems()})
        if rows:
            self.detail.show()
            self.following = False
            self.detail.setPlainText(
                "\n\n".join(event_text(self.display_events[r]) for r in rows)
            )

    def scrolled(self, value):
        if not self.rendering:
            self.following = (
                value >= self.table.verticalScrollBar().maximum() - 2
                and not self.table.selectedItems()
            )

    def range_changed(self, minimum, maximum):
        if self.following and not self.table.selectedItems() and not self.stopping:
            self.follow_timer.start(30)

    def settle_scroll(self):
        if self.following and not self.table.selectedItems() and not self.stopping:
            self.rendering = True
            bar = self.table.verticalScrollBar()
            bar.setValue(bar.maximum())
            self.rendering = False

    def go_latest(self):
        self.table.clearSelection()
        self.detail.clear()
        self.detail.hide()
        self.following = True
        self.offset = -1
        self.render_signature = None
        self.reload_events()

    def page_events(self, amount):
        self.following = False
        self.offset = max(0, self.offset + amount)
        self.render_signature = None
        self.reload_events()

    def page_batches(self, amount):
        self.list_revision += 1
        self.list_offset = max(0, self.list_offset + amount)
        self.record = None
        self.refresh_batches()

    def on_updated(self):
        if self.isVisible():
            self.refresh_batches()

    def copy_selected(self):
        if self.detail.textCursor().hasSelection():
            text = self.detail.textCursor().selectedText().replace("\u2029", "\n")
        else:
            rows = sorted({item.row() for item in self.table.selectedItems()})
            text = "\n\n".join(event_text(self.display_events[r]) for r in rows)
        if not text:
            self.host.notify("请先选择日志行或消息文字。", True)
            return
        if copy_text(self.host.scrub(text)):
            self.host.notify("选中日志已复制。")
        else:
            self.host.notify(CLIPBOARD_ERROR, True)

    def export_dialog(self):
        if not self.record:
            self.host.notify("请先选择一个批次或旧日志文件。", True)
            return
        path, _ = QFileDialog.getSaveFileName(
            self, "导出当前筛选日志", "筛选日志.txt", "文本文件 (*.txt)"
        )
        if path:
            self.launch(
                {
                    "kind": "export",
                    "record": self.record,
                    "filters": self.filters(),
                    "destination": path,
                }
            )

    def stop(self):
        self.stopping = True
        self.debounce.stop()
        self.follow_timer.stop()
        self.pending = [job for job in self.pending if job["kind"] == "export"]
        if self.worker:
            self.worker.requestInterruption()
            return False
        return True

    def show_unlinked(self):
        self.unlinked = True
        self.record = None
        self.revision += 1
        self.display_events = []
        self.table.setRowCount(0)
        self.detail.clear()
        self.detail.hide()
        self.empty.setText(
            "没有可精确关联的结构化日志。请从上方选择旧文本文件查看可用信息。"
        )
        self.empty.show()
        self.warning.setText(
            "此历史记录没有结构化日志关联；不推测旧文本的批次或账号归属。"
        )
        self.warning.show()

    def retheme(self):
        for row, event in enumerate(self.display_events):
            if event.get("level") in ("warning", "error"):
                color = QColor(
                    PALETTES[self.host.theme][
                        "failure" if event["level"] == "error" else "cancelled"
                    ]
                )
                for col in range(5):
                    item = self.table.item(row, col)
                    if item:
                        item.setForeground(color)
