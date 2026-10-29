"""Qt desktop shell. Network workers never access widgets."""

import threading
from datetime import date
from PySide6.QtCore import Qt, QThread, Signal, QTimer, QTime, QDate, QUrl, QItemSelectionModel
from PySide6.QtGui import QColor, QDesktopServices, QKeySequence, QShortcut
from PySide6.QtWidgets import (
    QApplication,
    QMainWindow,
    QWidget,
    QFrame,
    QLabel,
    QPushButton,
    QVBoxLayout,
    QHBoxLayout,
    QGridLayout,
    QFormLayout,
    QStackedWidget,
    QScrollArea,
    QLineEdit,
    QComboBox,
    QCheckBox,
    QTimeEdit,
    QDateEdit,
    QPlainTextEdit,
    QTableWidget,
    QTableWidgetItem,
    QHeaderView,
    QAbstractItemView,
    QProgressBar,
    QDialog,
    QDialogButtonBox,
    QMessageBox,
    QFileDialog,
    QSplitter,
    QSpinBox,
    QMenu,
)
from api import SchoolClient, redact
from logbook import LogBook, event_text
from log_page import LogPage, append_quick
from runner import run_accounts
from position_offset import (
    validate_offset,
    resolve_offset,
    offset_description,
    checkpoint_position,
)
from locations import (
    resolve_location,
    saved_location,
    validate_choice,
    TASK_LOCATION,
    INHERIT_LOCATION,
)
from storage import (
    new_account,
    import_accounts,
    validate_schedule,
    now_shanghai,
)
from execution import make_request, merge_results, latest_today, today_succeeded
from history_storage import ensure_record_ids, delete_records
from scheduling import (
    fingerprint,
    prepare_schedule,
    next_automatic,
    due_action,
    pending_ids,
    begin_attempt,
    checkpoint_task,
    checkpoint_result,
    finish_attempt,
    recover_interrupted,
    invalidate_pending,
)
from desktop_common import DemoClient, copy_text, CLIPBOARD_ERROR
from tray import TrayIcon
from ui_style import icon, stylesheet, set_palette, PALETTES, Switch, CheckBox
from reliability_ui import ReliabilityUI
from overview_ui import OverviewUI
from overview import today_metrics, record_batch
from windows_integration import SleepGuard
from form_layout import (
    FormLayout,
    size_field,
    AdaptiveFields,
    ActionRow,
    compact_form_tree,
)
from ui_components import SettingForm, WrapToolbar

VERSION = "3.4.1"
STATUS = {"success": "成功", "failure": "失败", "cancelled": "取消", "skipped": "跳过"}
PAGES = [
    ("概览", "overview", "管理账号、查看进度，随时了解签到结果。"),
    ("账号", "accounts", "管理账号与定位偏好，选择账号执行批量操作。"),
    ("定时任务", "schedule", "按北京时间自动执行；已签任务经服务端核对后跳过。"),
    (
        "定位设置",
        "location",
        "指定位置用于设置打卡请求中的经纬度，不修改设备系统定位。",
    ),
    ("历史记录", "history", "查看服务端确认的结果，以及失败、取消和跳过原因。"),
    ("运行日志", "log", "按执行批次、账号与步骤查看日志；历史文本保留原有信息。"),
    ("设置", "settings", "调整外观、提交确认和数据存储。"),
]
PAGE_INDEX = {glyph: index for index, (_, glyph, _) in enumerate(PAGES)}


def label(text, role=None):
    item = QLabel(str(text))
    item.setWordWrap(True)
    item.setTextFormat(Qt.TextFormat.PlainText)
    if role:
        item.setObjectName(role)
    return item


def button(text, glyph=None, callback=None, primary=False):
    item = QPushButton(text)
    if glyph:
        item.setIcon(icon(glyph))
        item.setProperty("glyph", glyph)
    item.setProperty("primary", primary)
    item.setCursor(Qt.CursorShape.PointingHandCursor)
    if callback:
        item.clicked.connect(lambda checked=False: callback())
    return item


def card(title=None):
    item = QFrame()
    item.setObjectName("card")
    layout = QVBoxLayout(item)
    layout.setContentsMargins(18, 16, 18, 16)
    layout.setSpacing(12)
    if title:
        layout.addWidget(label(title, "section"))
    return item, layout


def row(*widgets):
    layout = QHBoxLayout()
    layout.setSpacing(10)
    for widget in widgets:
        layout.addWidget(widget)
    return layout


def table(headers):
    item = QTableWidget(0, len(headers))
    item.setHorizontalHeaderLabels(headers)
    item.verticalHeader().hide()
    item.setShowGrid(False)
    item.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
    item.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
    item.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
    item.verticalHeader().setDefaultSectionSize(46)
    item.setWordWrap(False)
    item.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
    item.horizontalHeader().setStretchLastSection(True)
    item.setMinimumHeight(190)
    return item


def put(widget, rows, state_column=None, theme="dark"):
    widget.setRowCount(len(rows))
    for r, values in enumerate(rows):
        for c, value in enumerate(values):
            item = QTableWidgetItem(str(value))
            item.setToolTip(str(value))
            widget.setItem(r, c, item)
            if c == state_column:
                key = next((k for k, v in STATUS.items() if v == value), None)
                if key:
                    item.setForeground(QColor(PALETTES[theme][key]))


class OffsetChoice(QWidget):
    def __init__(self, value=None, inherit=False, global_rule=None):
        super().__init__()
        self.inherit = inherit
        self.global_rule = global_rule or {"mode": "off", "radius": 0}
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)
        self.mode = QComboBox()
        if inherit:
            self.mode.addItem("跟随全局偏移设置", "inherit")
        self.mode.addItem("关闭可选偏移", "off")
        self.mode.addItem("启用圆形随机偏移", "on")
        self.radius = QLineEdit()
        self.radius.setAccessibleName("偏移半径（米）")
        self.radius.setPlaceholderText("0～500")
        size_field(self.mode)
        size_field(self.radius, "number")
        self.form = SettingForm()
        self.form.addRow(
            "偏移规则", self.mode, "账号规则可覆盖全局；关闭时保留已配置半径。"
        )
        self.radius_section = QWidget()
        radius_layout = QHBoxLayout(self.radius_section)
        radius_layout.setContentsMargins(0, 0, 0, 0)
        radius_layout.addWidget(self.radius)
        radius_layout.addWidget(label("米", "muted"))
        radius_layout.addStretch()
        self.form.addRow(
            "偏移半径",
            self.radius_section,
            "0～500 米；0 使用中心点。先本地校验，再缩小范围与检查中心。",
        )
        layout.addLayout(self.form)
        self.inherited_note = label("", "muted")
        layout.addWidget(self.inherited_note)
        self.rules_help = label(
            "以保存位置或任务原坐标为中心；最多 33 个本地候选，逐步缩小半径并检查中心。不合格则停止；网络重试复用已保存坐标。账号偏移优先于全局，与定位继承分别设置。",
            "muted",
        )
        self.rules_help.hide()
        self.rules_button = button(
            "查看偏移规则",
            callback=lambda: self.rules_help.setVisible(
                not self.rules_help.isVisible()
            ),
        )
        layout.addWidget(self.rules_button, 0, Qt.AlignmentFlag.AlignLeft)
        layout.addWidget(self.rules_help)
        self.mode.currentIndexChanged.connect(self.update_enabled)
        self.refresh(value or {"mode": "inherit" if inherit else "off", "radius": 0})

    def refresh(self, value):
        self.saved_radius = value.get("radius", 0)
        self.mode.setCurrentIndex(max(0, self.mode.findData(value.get("mode"))))
        self.radius.setText(str(value.get("radius", 0)))
        self.update_enabled()

    def update_enabled(self, *_):
        self.radius.setEnabled(self.mode.currentData() == "on")
        self.radius_section.setVisible(self.mode.currentData() == "on")
        self.form.setRowVisible(self.radius_section, self.mode.currentData() == "on")
        inherited = self.mode.currentData() == "inherit"
        self.inherited_note.setVisible(inherited)
        self.inherited_note.setText(
            "当前全局：" + offset_description(self.global_rule) if inherited else ""
        )

    def value(self):
        # Keep text in the draft so invalid input remains visible after page refresh.
        return {"mode": self.mode.currentData(), "radius": self.radius.text()}

    def checked_value(self):
        value = self.value()
        if value["mode"] != "on":
            try:
                return validate_offset(value, self.inherit)
            except ValueError:
                # An inactive invalid draft must neither block saving nor enter
                # persistent configuration. Keep its editor text for re-enabling.
                value["radius"] = self.saved_radius
        return validate_offset(value, self.inherit)


class LocationChoice(QWidget):
    def __init__(self, data, choice=None, inherit=False):
        super().__init__()
        self.data = data
        self.inherit = inherit
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)
        self.mode = QComboBox()
        if inherit:
            self.mode.addItem("跟随全局设置", "inherit")
        self.mode.addItem("使用任务默认位置", "task")
        self.mode.addItem("使用指定位置", "specified")
        self.position = QComboBox()
        self.position.setAccessibleName("常用位置")
        self.detail = label("", "muted")
        size_field(self.mode)
        size_field(self.position)
        self.form = SettingForm()
        self.form.addRow(
            "定位模式", self.mode, "指定位置用于请求坐标；任务默认在查询任务后确定。"
        )
        self.form.addRow(
            "常用位置", self.position, "选择已保存的位置；新建位置不会自动应用。"
        )
        layout.addLayout(self.form)
        layout.addWidget(self.detail)
        self.mode.currentIndexChanged.connect(self.update_detail)
        self.position.currentIndexChanged.connect(self.update_detail)
        self.refresh(choice or (INHERIT_LOCATION if inherit else TASK_LOCATION))

    def refresh(self, choice):
        self.position.clear()
        for item in self.data["locations"]:
            self.position.addItem(item["name"], item["id"])
        size_field(self.position)
        self.mode.setCurrentIndex(max(0, self.mode.findData(choice.get("mode"))))
        self.position.setCurrentIndex(
            max(0, self.position.findData(choice.get("location_id")))
        )
        self.update_detail()

    def value(self):
        mode = self.mode.currentData()
        return {
            "mode": mode,
            "location_id": self.position.currentData() if mode == "specified" else None,
        }

    def checked_value(self):
        value = self.value()
        validate_choice(value, self.data["locations"], allow_inherit=self.inherit)
        return value

    def update_detail(self, *args):
        specified = self.mode.currentData() == "specified"
        self.position.setVisible(specified)
        self.form.setRowVisible(self.position, specified)
        item = next(
            (
                p
                for p in self.data["locations"]
                if p["id"] == self.position.currentData()
            ),
            None,
        )
        if specified:
            self.detail.setText(
                f"纬度 {item['latitude']}  ·  经度 {item['longitude']}"
                if item
                else "请先保存一个常用位置。"
            )
        elif self.mode.currentData() == "inherit":
            self.detail.setText("账号跟随全局设置；每次启动任务时固定本次配置。")
        else:
            self.detail.setText("获取任务后使用原有任务定位规则。")


class AccountDialog(QDialog):
    def __init__(self, parent, account=None):
        super().__init__(parent)
        self.account = account
        self.values = None
        self.setWindowTitle("编辑账号" if account else "添加账号")
        self.setMinimumSize(360, 380)
        self.resize(560, 660)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(26, 24, 26, 24)
        layout.setSpacing(18)
        layout.addWidget(label(self.windowTitle(), "title"))
        layout.addWidget(label("账号凭据仅存储在本机，请勿分享给他人。", "muted"))
        self.name = QLineEdit(account.get("label", "") if account else "")
        self.name.setPlaceholderText("例如：我的账号")
        self.openid = QLineEdit(account.get("openid", "") if account else "")
        self.openid.setEchoMode(QLineEdit.EchoMode.Password)
        self.openid.setPlaceholderText("粘贴 OpenID")
        self.reveal_button = button("显示", "eye", self.reveal)
        self.copy_button = button("复制", "copy", self.copy_identifier)
        size_field(self.reveal_button)
        size_field(self.copy_button)
        form = FormLayout()
        form.addRow("账号名称", self.name)
        form.addRow("OpenID", row(self.openid, self.reveal_button, self.copy_button))
        self.enabled = CheckBox("参与批量与定时（关闭仍可临时打卡）")
        self.enabled.setChecked(account.get("enabled", True) if account else True)
        form.addRow("", self.enabled)
        self.location_choice = LocationChoice(
            parent.store.data,
            account.get("location") if account else INHERIT_LOCATION,
            True,
        )
        form.addRow("账号定位", self.location_choice)
        self.offset_choice = OffsetChoice(
            account.get("offset") if account else None,
            True,
            parent.store.data.get("global_offset"),
        )
        form.addRow("账号偏移", self.offset_choice)
        form_widget = QWidget()
        form_widget.setLayout(form)
        self.form_scroll = QScrollArea()
        self.form_scroll.setWidgetResizable(True)
        self.form_scroll.setFrameShape(QFrame.Shape.NoFrame)
        self.form_scroll.setWidget(form_widget)
        layout.addWidget(self.form_scroll, 1)
        self.notice = label("", "muted")
        layout.addWidget(self.notice)
        box = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Cancel
            | QDialogButtonBox.StandardButton.Save
        )
        box.button(QDialogButtonBox.StandardButton.Cancel).setText("取消")
        box.button(QDialogButtonBox.StandardButton.Save).setText("保存账号")
        box.button(QDialogButtonBox.StandardButton.Save).setProperty("primary", True)
        box.rejected.connect(self.reject)
        box.accepted.connect(self.save)
        layout.addWidget(box)

    def reveal(self):
        hidden = self.openid.echoMode() == QLineEdit.EchoMode.Password
        self.openid.setEchoMode(
            QLineEdit.EchoMode.Normal if hidden else QLineEdit.EchoMode.Password
        )
        self.reveal_button.setText("隐藏" if hidden else "显示")

    def copy_identifier(self):
        copied = copy_text(self.openid.text())
        self.notice.setText(
            "已复制 OpenID，请妥善保管。" if copied else CLIPBOARD_ERROR
        )

    def save(self):
        try:
            identifier = self.openid.text().strip()
            if not identifier or any(c.isspace() for c in identifier):
                raise ValueError("请输入单个完整的 OpenID，不要包含空格或换行。")
            others = [
                a
                for a in self.parent().store.data["accounts"]
                if not self.account or a["id"] != self.account["id"]
            ]
            if any(a["openid"] == identifier for a in others):
                raise ValueError("此账号已存在，请编辑已有账号。")
            self.values = {
                "label": self.name.text().strip() or "新账号",
                "openid": identifier,
                "enabled": self.enabled.isChecked(),
                "location": self.location_choice.checked_value(),
                "offset": self.offset_choice.checked_value(),
            }
            self.accept()
        except ValueError as error:
            self.notice.setText(str(error))


class LocationDialog(QDialog):
    """Each dialog owns its immutable location ID and unsaved field values."""

    def __init__(self, parent, location=None):
        super().__init__(parent)
        self.host = parent
        self.identity = location["id"] if location else None
        self.setWindowTitle("编辑位置" if location else "新建位置")
        self.resize(560, 490)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(26, 24, 26, 24)
        layout.setSpacing(16)
        layout.addWidget(label(self.windowTitle(), "title"))
        layout.addWidget(
            label(
                "本次提交位置由任务启动时的有效定位配置决定，不读取设备 GPS。", "muted"
            )
        )
        self.name = QLineEdit(location["name"] if location else "")
        self.latitude = QLineEdit(str(location["latitude"]) if location else "")
        self.longitude = QLineEdit(str(location["longitude"]) if location else "")
        self.latitude.setPlaceholderText("−90～90，例如 28.131567")
        self.longitude.setPlaceholderText("−180～180，例如 112.994905")
        self.errors = {}
        form = FormLayout()
        for title, key, field in (
            ("位置名称", "name", self.name),
            ("纬度（南北）", "latitude", self.latitude),
            ("经度（东西）", "longitude", self.longitude),
        ):
            size_field(field, "text" if key == "name" else "coordinate")
            container = QWidget()
            fields = QVBoxLayout(container)
            fields.setContentsMargins(0, 0, 0, 0)
            fields.setSpacing(5)
            fields.addWidget(field)
            error = label("")
            error.setStyleSheet(f"color:{PALETTES[parent.theme]['failure']};")
            error.hide()
            fields.addWidget(error)
            self.errors[key] = error
            field.setAccessibleName(title)
            form.addRow(title, container)
        layout.addLayout(form)
        uses = parent.location_references(self.identity) if self.identity else []
        layout.addWidget(
            label(
                (
                    "此位置正在被 "
                    + "、".join(uses)
                    + " 使用。修改会影响后续任务；已启动任务继续使用固定快照。"
                    if uses
                    else "创建位置只加入常用列表；不会切换全局默认或账号定位。"
                ),
                "muted",
            )
        )
        self.notice = label("")
        self.notice.hide()
        layout.addWidget(self.notice)
        layout.addStretch(1)
        controls = QDialogButtonBox(QDialogButtonBox.StandardButton.Cancel)
        controls.button(QDialogButtonBox.StandardButton.Cancel).setText("取消")
        controls.rejected.connect(self.reject)
        self.save_button = controls.addButton(
            "保存修改" if location else "创建位置",
            QDialogButtonBox.ButtonRole.ActionRole,
        )
        self.save_button.setProperty("primary", True)
        self.save_button.setDefault(True)
        self.save_button.clicked.connect(self.save)
        layout.addWidget(controls)
        self.setTabOrder(self.name, self.latitude)
        self.setTabOrder(self.latitude, self.longitude)
        self.setTabOrder(self.longitude, self.save_button)
        self.name.setFocus()

    def save(self):
        for message in self.errors.values():
            message.hide()
        self.notice.hide()
        try:
            position = saved_location(
                self.name.text(),
                self.latitude.text(),
                self.longitude.text(),
                self.identity,
            )
        except ValueError as error:
            key = (
                "latitude"
                if "纬度" in str(error)
                else "longitude" if "经度" in str(error) else "name"
            )
            self.errors[key].setText(str(error))
            self.errors[key].show()
            getattr(self, key).setFocus()
            return

        def change():
            if self.identity:
                old = next(
                    (
                        p
                        for p in self.host.store.data["locations"]
                        if p["id"] == self.identity
                    ),
                    None,
                )
                if not old:
                    raise ValueError("位置已被删除，请取消后重新选择。")
                old.update(position)
            else:
                self.host.store.data["locations"].append(position)

        if not self.host.persist(change):
            self.notice.setText(
                "位置未保存。请检查数据目录写入权限后重试；填写内容已保留。"
            )
            self.notice.show()
            return
        self.host.refresh_locations(position["id"])
        self.host.refresh_accounts()
        self.host.notify("修改已保存。" if self.identity else "位置已创建。")
        self.accept()


class BatchPreviewDialog(QDialog):
    def __init__(self, parent, request):
        super().__init__(parent)
        self.request = request
        self.setWindowTitle("确认本次打卡范围")
        self.resize(780, 530)
        self.setMinimumSize(640, 400)
        contents = QVBoxLayout(self)
        contents.setContentsMargins(24, 24, 24, 24)
        contents.setSpacing(16)
        contents.addWidget(label("确认整批后开始", "title"))
        contents.addWidget(
            label(f"执行范围：{request['scope_label']} · 共 {request['count']} 个账号")
        )
        listing = table(["账号", "参与方式", "有效位置", "坐标"])
        listing.setColumnWidth(0, 130)
        listing.setColumnWidth(1, 165)
        listing.setColumnWidth(2, 190)
        listing.setMinimumHeight(200)
        rows = []
        for account in request["accounts"]:
            position = account.get("run_location", {})
            coords = (
                f"纬度 {position['latitude']}\n经度 {position['longitude']}"
                if position.get("mode") == "specified"
                else "待任务查询"
            )
            coords += "\n" + offset_description(account.get("run_offset", {}))
            mode = (
                "参与批量与定时"
                if account.get("enabled", True)
                else "本次临时执行，仍不参与自动计划"
            )
            rows.append(
                [
                    parent.scrub(account["label"]),
                    mode,
                    position.get("name", account.get("location_error", "需修正位置")),
                    coords,
                ]
            )
        put(listing, rows)
        for i in range(len(rows)):
            listing.setRowHeight(i, 110)
        contents.addWidget(listing, 1)
        temporary = [
            parent.scrub(a["label"])
            for a in request["accounts"]
            if not a.get("enabled", True)
        ]
        if temporary:
            contents.addWidget(
                label(
                    "临时执行："
                    + "、".join(temporary)
                    + "。本次临时执行，仍不参与自动计划。"
                )
            )
        contents.addWidget(
            label(
                "本批账号和定位已固定；后续仍逐账号校验任务距离、时间、照片并查询服务端。整批只确认一次。",
                "muted",
            )
        )
        box = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Cancel | QDialogButtonBox.StandardButton.Ok
        )
        box.button(QDialogButtonBox.StandardButton.Cancel).setText("取消整批")
        box.button(QDialogButtonBox.StandardButton.Ok).setText(
            f"开始本批打卡（{request['count']}）"
        )
        box.button(QDialogButtonBox.StandardButton.Ok).setProperty("primary", True)
        box.accepted.connect(self.accept)
        box.rejected.connect(self.reject)
        contents.addWidget(box)


class RunWorker(QThread):
    event = Signal(str, object)
    task_selection = Signal(object, object)
    position_checkpoint = Signal(object)

    def __init__(self, request, cancel, demo, parent=None):
        super().__init__(parent)
        self.request = request
        self.accounts = request["accounts"]
        self.snapshot = request["config"]
        self.cancel = cancel
        self.demo = demo
        self.results = []
        self.error = None
        self.task_ready = threading.Event()
        self.task_answer = None
        self.position_ready = threading.Event()
        self.position_saved = False

    def save_position(self, value):
        self.position_ready.clear()
        self.position_saved = False
        self.position_checkpoint.emit(value)
        while not self.position_ready.wait(0.1):
            if self.cancel.is_set():
                return False
        return self.position_saved and not self.cancel.is_set()

    def choose_task(self, account, candidates):
        self.task_ready.clear()
        self.task_selection.emit(account, candidates)
        while not self.task_ready.wait(0.1):
            if self.cancel.is_set():
                return None
        return None if self.cancel.is_set() else self.task_answer

    def run(self):
        try:
            with SleepGuard(
                self.snapshot["reliability"].get("prevent_sleep", False)
                and not self.demo
            ) as guard:
                if guard.error:
                    self.event.emit("log", guard.error)
                self.results = run_accounts(
                    self.accounts,
                    lambda kind, value: (
                        self.event.emit(kind, value) if kind != "log" else None
                    ),
                    cancel=self.cancel,
                    scheduled=self.request["scope"] in ("automatic", "retry"),
                    success_dates=self.snapshot["success_dates"],
                    location_config=self.snapshot,
                    client_factory=DemoClient if self.demo else SchoolClient,
                    delay_scale=0.02 if self.demo else 1,
                    preview_handler=None,
                    run_id=self.request["run_id"],
                    retry_context=self.request.get("retry_context"),
                    task_chooser=self.choose_task,
                    position_handler=self.save_position,
                )
        except Exception as error:
            self.error = str(error)


class ImportWorker(QThread):
    def __init__(self, path, parent):
        super().__init__(parent)
        self.path = path
        self.values = []
        self.error = None

    def run(self):
        try:
            self.values = import_accounts(self.path)
        except Exception:
            self.error = "文件无法读取或格式不正确。请使用包含 OpenID 的 TXT、.env 或 JSON 文件。"


class App(OverviewUI, ReliabilityUI, QMainWindow):
    def __init__(self, store, demo=False, quit_on_shutdown=False):
        super().__init__()
        self.store = store
        self.demo = demo
        self.quit_on_shutdown = quit_on_shutdown
        self.busy = False
        self.cancel = threading.Event()
        self.closing = False
        self._destroyed = False
        self.tray = None
        self.worker = None
        self.data_job = None
        self.job_dialog = None
        self.task_dialog = None
        self.notification_target = None
        self.last_storage_notice = None
        self.import_worker = None
        self.preview_dialog = None
        self.run_results = []
        self.run_lines = []
        self.previews = {}
        self.collapsed = False
        self.pending_results = None
        self.pending_request = None
        self.active_request = None
        self.pending_context = None
        self.running_ids = set()
        self.theme = store.data["appearance"].get("theme", "dark")
        self.theme = self.theme if self.theme in PALETTES else "dark"
        self.setWindowTitle("平安签到" + (" · 离线演示" if demo else ""))
        self.setWindowIcon(icon("check"))
        available = QApplication.primaryScreen().availableGeometry()
        self.resize(
            min(1240, max(740, available.width() - 40)),
            min(840, max(540, available.height() - 80)),
        )
        self.setMinimumSize(740, 540)
        self.journal = LogBook(
            store.directory, [a["openid"] for a in store.data["accounts"]]
        )
        self.journal_error = None
        self.legacy_log_pending = None
        self.build_shell()
        self.refresh()
        self.apply_theme()
        self.timer = QTimer(self)
        self.timer.timeout.connect(self.tick)
        self.timer.start(1000)
        self.notification_timer = QTimer(self)
        self.notification_timer.setSingleShot(True)
        self.notification_timer.timeout.connect(self.notice.hide)
        self.journal_timer = QTimer(self)
        self.journal_timer.timeout.connect(self.flush_journal)
        self.journal_timer.start(1000)
        for i in range(len(PAGES)):
            shortcut = QShortcut(QKeySequence(f"Ctrl+{i + 1}"), self)
            shortcut.activated.connect(lambda i=i: self.navigate(i))
        QShortcut(QKeySequence("Ctrl+N"), self).activated.connect(self.add_account)
        QShortcut(QKeySequence("Ctrl+Return"), self).activated.connect(self.start_run)
        QShortcut(QKeySequence("Ctrl+L"), self).activated.connect(
            lambda: self.log_toggle.click()
        )
        deletion = QShortcut(QKeySequence("Delete"), self.account_table)
        deletion.setContext(Qt.ShortcutContext.WidgetWithChildrenShortcut)
        deletion.activated.connect(self.delete_accounts)
        self.navigate("overview")
        self.update_security_status()
        from readiness import check_ready

        issues = check_ready(self.store.data, self.store.directory)
        self.readiness_write_error = any(page == "settings" for page, _ in issues)
        self.refresh_local_readiness()
        if any(a.get("credential_error") for a in self.store.data["accounts"]):
            self.deliver_system_notice(
                {
                    "title": "账号凭据需要修复",
                    "message": "有账号无法解密并已停用。请在软件账号页重新输入凭据，或恢复密码备份。",
                    "page": "accounts",
                    "batch_id": None,
                    "account_id": None,
                }
            )
        if (self.store.data.get("automation") or {}).get("state") == "running":
            if self.persist(
                lambda: recover_interrupted(self.store.data, now_shanghai())
            ):
                self.notify(
                    "上次自动任务未完整结束；符合规则的账号会按间隔先查询服务端再重试。"
                )

    def build_shell(self):
        base = QWidget()
        base.setObjectName("main")
        self.setCentralWidget(base)
        shell = QHBoxLayout(base)
        shell.setContentsMargins(0, 0, 0, 0)
        shell.setSpacing(0)
        self.sidebar = QFrame()
        self.sidebar.setObjectName("sidebar")
        self.sidebar.setFixedWidth(220)
        navlayout = QVBoxLayout(self.sidebar)
        navlayout.setContentsMargins(14, 22, 14, 18)
        navlayout.setSpacing(7)
        self.brand = label("平安签到", "brand")
        self.collapse_button = button("", "menu", self.toggle_sidebar)
        self.collapse_button.setToolTip("折叠 / 展开导航栏")
        self.collapse_button.setFixedWidth(36)
        navlayout.addLayout(row(self.brand, self.collapse_button))
        navlayout.addSpacing(24)
        self.nav_buttons = []
        for i, (title, glyph, _) in enumerate(PAGES):
            btn = button(title, glyph, lambda i=i: self.navigate(i))
            btn.setProperty("nav", True)
            btn.setCheckable(True)
            btn.setMinimumHeight(43)
            btn.setToolTip(f"{title} · Ctrl+{i + 1}")
            navlayout.addWidget(btn)
            self.nav_buttons.append(btn)
        navlayout.addStretch()
        self.sidebar_theme = button("切换浅色主题", "sun", self.toggle_theme)
        navlayout.addWidget(self.sidebar_theme)
        self.version_label = label(
            f"版本 {VERSION}" + (" · 模拟接口" if self.demo else ""), "muted"
        )
        navlayout.addWidget(self.version_label)
        shell.addWidget(self.sidebar)
        body = QWidget()
        body_layout = QVBoxLayout(body)
        body_layout.setContentsMargins(0, 0, 0, 0)
        body_layout.setSpacing(0)
        header = QWidget()
        header_layout = QHBoxLayout(header)
        header_layout.setContentsMargins(26, 22, 26, 18)
        titles = QVBoxLayout()
        titles.setSpacing(7)
        self.page_title = label("", "title")
        self.page_description = label("", "muted")
        titles.addWidget(self.page_title)
        titles.addWidget(self.page_description)
        header_layout.addLayout(titles, 1)
        self.primary_action = button("立即打卡", "play", self.header_action, True)
        self.primary_action.setMinimumHeight(40)
        self.page_action = button("", None, self.secondary_action)
        header_actions = QVBoxLayout()
        header_actions.addWidget(self.primary_action)
        header_actions.addWidget(self.page_action)
        header_layout.addLayout(header_actions)
        body_layout.addWidget(header)
        self.notice = label("")
        self.notice.setObjectName("feedback")
        self.notice.setContentsMargins(26, 8, 26, 8)
        self.notice.hide()
        body_layout.addWidget(self.notice)
        self.splitter = QSplitter(Qt.Orientation.Vertical)
        self.stack = QStackedWidget()
        self.splitter.addWidget(self.stack)
        self.pages = []
        self.page_layouts = []
        for _ in PAGES:
            scroll = QScrollArea()
            scroll.setWidgetResizable(True)
            scroll.setFrameShape(QFrame.Shape.NoFrame)
            page = QWidget()
            page.setMinimumWidth(0)
            layout = QVBoxLayout(page)
            layout.setContentsMargins(26, 4, 26, 22)
            layout.setSpacing(14)
            scroll.setWidget(page)
            self.stack.addWidget(scroll)
            self.pages.append(page)
            self.page_layouts.append(layout)
        self.build_overview()
        self.build_accounts()
        self.build_schedule()
        self.build_locations()
        self.build_history()
        self.build_settings()
        self.log_page = LogPage(self, self.journal)
        self.page_layouts[PAGE_INDEX["log"]].addWidget(self.log_page, 1)
        self.log_panel = QWidget()
        self.log_panel.setObjectName("logPanel")
        logs = QVBoxLayout(self.log_panel)
        logs.setContentsMargins(22, 12, 22, 12)
        logs.addWidget(
            WrapToolbar(
                label("快速日志", "section"),
                button("打开完整日志", "log", self.open_full_logs),
                button(
                    "回到最新",
                    None,
                    lambda: self.log_text.verticalScrollBar().setValue(
                        self.log_text.verticalScrollBar().maximum()
                    ),
                ),
                button("收起", "close", lambda: self.log_toggle.setChecked(False)),
            )
        )
        self.log_text = QPlainTextEdit()
        self.log_text.setReadOnly(True)
        self.log_text.setMaximumBlockCount(3000)
        self.log_text.setMinimumHeight(80)
        logs.addWidget(self.log_text)
        self.splitter.addWidget(self.log_panel)
        self.splitter.setStretchFactor(0, 1)
        self.log_panel.hide()
        body_layout.addWidget(self.splitter, 1)
        footer = QWidget()
        footer.setObjectName("footer")
        foot = QHBoxLayout(footer)
        foot.setContentsMargins(30, 8, 24, 8)
        self.footer_status = label(
            (
                "离线演示 · 所有账号和接口均为模拟数据"
                if self.demo
                else "准备就绪 · 点击 X 后在系统托盘继续运行"
            ),
            "muted",
        )
        foot.addWidget(self.footer_status, 1)
        self.log_toggle = button("查看日志", "log")
        self.log_toggle.setCheckable(True)
        self.log_toggle.toggled.connect(self.show_logs)
        foot.addWidget(self.log_toggle)
        body_layout.addWidget(footer)
        shell.addWidget(body, 1)

    def build_overview(self):
        self.build_overview_page()

    def build_accounts(self):
        layout = self.page_layouts[PAGE_INDEX["accounts"]]
        self.account_search = QLineEdit()
        self.account_search.setPlaceholderText(
            "搜索账号名称（只筛选显示，不改变全部打卡范围）"
        )
        self.account_search.setClearButtonEnabled(True)
        self.account_search.textChanged.connect(self.refresh_accounts)
        layout.addWidget(
            WrapToolbar(
                self.account_search,
                button("导入账号", "import", self.import_file),
                button("检查账号（只读）", callback=self.check_account_dialog),
                button("导入 OpenID 工具结果", callback=self.import_file),
            )
        )
        self.account_counts = label("", "muted")
        layout.addWidget(self.account_counts)
        layout.addWidget(
            label(
                "参与开关决定长期批量与定时；行内打卡和所选打卡仅决定本次范围，可临时执行不参与的账号。",
                "muted",
            )
        )
        self.bulk_bar = QWidget()
        bar = QHBoxLayout(self.bulk_bar)
        bar.setContentsMargins(0, 0, 0, 0)
        self.selection_label = label("", "muted")
        self.bulk_run = button(
            "打卡所选账号（0）", "play", lambda: self.start_run(selected=True)
        )
        more = button("更多操作")
        menu = QMenu(more)
        self.bulk_enable = menu.addAction("参与批量与定时")
        self.bulk_enable.triggered.connect(lambda: self.set_accounts_enabled(True))
        self.bulk_disable = menu.addAction("不参与批量与定时")
        self.bulk_disable.triggered.connect(lambda: self.set_accounts_enabled(False))
        self.edit_account_button = menu.addAction("编辑所选账号")
        self.edit_account_button.triggered.connect(self.edit_account)
        menu.addSeparator()
        self.bulk_delete = menu.addAction("删除所选账号")
        self.bulk_delete.triggered.connect(self.delete_accounts)
        more.setMenu(menu)
        bar.addWidget(self.selection_label, 1)
        bar.addWidget(self.bulk_run)
        bar.addWidget(more)
        layout.addWidget(self.bulk_bar)
        self.account_table = table(
            ["参与批量与定时", "账号", "有效定位配置", "今日状态", "最近执行", "操作"]
        )
        for i, width in enumerate((110, 125, 245, 100, 145, 230)):
            self.account_table.setColumnWidth(i, width)
        self.account_table.setMinimumHeight(300)
        self.account_table.setSortingEnabled(True)
        self.account_table.itemSelectionChanged.connect(self.update_selection)
        self.account_table.cellDoubleClicked.connect(
            lambda r, c: self.edit_account_by_id(
                self.account_table.item(r, 1).data(Qt.ItemDataRole.UserRole)
            )
        )
        layout.addWidget(self.account_table, 1)
        self.account_empty, empty = card("还没有账号")
        empty.addWidget(label("添加账号，或导入已有 OpenID 文件。", "muted"))
        empty.addWidget(
            button("添加第一个账号", "plus", self.add_account, True),
            0,
            Qt.AlignmentFlag.AlignLeft,
        )
        layout.addWidget(self.account_empty)
        self.copy_account_button = button("复制 OpenID", "copy", self.copy_account)
        self.show_identifier = CheckBox("显示选中账号的 OpenID")
        self.show_identifier.toggled.connect(self.show_account_identifier)
        self.identifier_text = QLineEdit()
        self.identifier_text.setReadOnly(True)
        self.identifier_text.setEchoMode(QLineEdit.EchoMode.Password)
        self.identifier_text.setPlaceholderText("选择一个账号后可显示或复制 OpenID")
        layout.addWidget(
            WrapToolbar(
                self.show_identifier, self.identifier_text, self.copy_account_button
            )
        )
        layout.addWidget(
            label(
                "Ctrl/Shift 多选用于本次所选操作；不必选中账号即可打卡全部启用账号。",
                "muted",
            )
        )

    def build_schedule(self):
        layout = self.page_layouts[PAGE_INDEX["schedule"]]
        frame, contents = card("自动执行计划")
        self.schedule_enabled = Switch()
        self.schedule_enabled.setAccessibleName("启用定时任务")
        self.schedule_time = QTimeEdit()
        self.schedule_time.setDisplayFormat("HH:mm")
        self.schedule_time.setAccessibleName("执行时间，北京时间")
        self.schedule_mode = QComboBox()
        for title, value in (
            ("每天", "daily"),
            ("工作日 · 周一至周五", "weekdays"),
            ("指定日期", "dates"),
        ):
            self.schedule_mode.addItem(title, value)
        self.schedule_dates = QPlainTextEdit()
        self.schedule_dates.setPlaceholderText("每行一个日期，例如 2026-10-10")
        self.schedule_dates.setMaximumHeight(135)
        form = SettingForm()
        self.schedule_form = form
        form.addRow(
            "启用计划", self.schedule_enabled, "关闭后仍可手动打卡；保存计划后才生效。"
        )
        form.addRow(
            "执行时间", self.schedule_time, "使用北京时间，设备需唤醒且软件保持运行。"
        )
        form.addRow("日期规则", self.schedule_mode, "每天、周一至周五，或指定日期。")
        self.schedule_dates.setParent(self)
        self.schedule_dates.hide()
        self.schedule_date_control = QWidget()
        date_controls = QHBoxLayout(self.schedule_date_control)
        date_controls.setContentsMargins(0, 0, 0, 0)
        self.schedule_dates_summary = label("未选择日期", "muted")
        self.edit_dates_button = button("编辑日期", callback=self.edit_schedule_dates)
        date_controls.addWidget(self.schedule_dates_summary)
        date_controls.addWidget(self.edit_dates_button)
        form.addRow(
            "指定日期",
            self.schedule_date_control,
            "使用日历添加日期，或按 YYYY-MM-DD 每行编辑一个日期。",
        )
        self.schedule_retry_enabled = Switch()
        self.schedule_retry_enabled.setAccessibleName("启用有限自动重试")
        self.schedule_retry_enabled.setChecked(True)
        self.retry_count = QSpinBox()
        self.retry_count.setRange(0, 5)
        self.retry_count.setSuffix(" 次")
        self.retry_interval = QSpinBox()
        self.retry_interval.setRange(1, 1440)
        self.retry_interval.setSuffix(" 分钟")
        form.addRow(
            "自动重试",
            self.schedule_retry_enabled,
            "仅重试临时网络错误；认证、照片、位置与任务歧义需人工处理。",
        )
        self.retry_fields = QWidget()
        self.retry_form = SettingForm(self.retry_fields)
        self.retry_form.addRow(
            "次数上限", self.retry_count, "额外重试 0～5 次；不跨日或扩大任务范围。"
        )
        self.retry_form.addRow(
            "重试间隔", self.retry_interval, "1～1440 分钟；关闭重试时仍保留设置。"
        )
        contents.addLayout(form)
        contents.addWidget(self.retry_fields)
        self.task_form = SettingForm()
        self.task_rule_mode = QComboBox()
        self.task_rule_mode.addItem("按日期、开放时段和状态唯一匹配", "unique_current")
        self.task_rule_mode.addItem("日期与时段内精确匹配任务名称", "exact_name")
        self.task_rule_name = QLineEdit()
        self.task_rule_name.setPlaceholderText("填写完整任务名称")
        self.task_form.addRow(
            "任务策略",
            self.task_rule_mode,
            "只选择服务端日期与开放时段内的任务；多项匹配时停止。",
        )
        self.task_form.addRow(
            "任务名称", self.task_rule_name, "精确匹配完整名称；同名歧义仍需人工选择。"
        )
        contents.addLayout(self.task_form)
        self.schedule_auto_status = label("", "muted")
        contents.addWidget(self.schedule_auto_status)
        self.schedule_badge = label("已保存", "muted")
        self.schedule_next = label("", "muted")
        contents.addWidget(self.schedule_badge)
        contents.addWidget(self.schedule_next)
        self.schedule_retry = button(
            "核对今日任务", "play", lambda: self.start_run(scope="today")
        )
        contents.addWidget(
            ActionRow(
                button("保存计划", "save", self.save_schedule, True),
                button("还原已保存设置", None, self.load_schedule),
                self.schedule_retry,
            )
        )
        layout.addWidget(frame)
        layout.addWidget(
            label(
                "软件需保持运行，可关闭到托盘。已有启用计划启动时仍按当天规则补跑；新计划保存时会单独选择今天补跑或从下次开始。自动重试只处理网络等临时失败，不跨日期或任务规则。",
                "muted",
            )
        )
        layout.addStretch()
        self.schedule_enabled.toggled.connect(self.schedule_dirty)
        self.schedule_time.timeChanged.connect(self.schedule_dirty)
        self.schedule_mode.currentIndexChanged.connect(self.schedule_dirty)
        self.schedule_dates.textChanged.connect(self.schedule_dirty)
        self.schedule_retry_enabled.toggled.connect(self.schedule_dirty)
        self.retry_count.valueChanged.connect(self.schedule_dirty)
        self.retry_interval.valueChanged.connect(self.schedule_dirty)
        self.task_rule_mode.currentIndexChanged.connect(self.schedule_dirty)
        self.task_rule_name.textChanged.connect(self.schedule_dirty)
        self.load_schedule()

    def build_locations(self):
        layout = self.page_layouts[PAGE_INDEX["location"]]
        frame, contents = card("全局默认定位")
        self.global_choice = LocationChoice(
            self.store.data, self.store.data["global_location"]
        )
        contents.addWidget(self.global_choice)
        self.global_offset = OffsetChoice(self.store.data.get("global_offset"))
        contents.addWidget(label("全局可选位置偏移", "section"))
        contents.addWidget(self.global_offset)
        self.global_dirty = label("已保存", "muted")
        contents.addWidget(self.global_dirty)
        self.global_choice.mode.currentIndexChanged.connect(self.global_location_dirty)
        self.global_offset.mode.currentIndexChanged.connect(self.global_location_dirty)
        self.global_offset.radius.textChanged.connect(self.global_location_dirty)
        self.global_choice.position.currentIndexChanged.connect(
            self.global_location_dirty
        )
        contents.addWidget(
            button("保存全局定位", "save", self.save_global_location, True),
            0,
            Qt.AlignmentFlag.AlignLeft,
        )
        layout.addWidget(frame)
        frame, contents = card("常用位置")
        self.location_table = table(
            ["位置名称", "纬度（−90～90）", "经度（−180～180）", "操作"]
        )
        self.location_table.setSelectionMode(
            QAbstractItemView.SelectionMode.SingleSelection
        )
        for i, width in enumerate((230, 190, 190, 225)):
            self.location_table.setColumnWidth(i, width)
        self.location_table.setSortingEnabled(True)
        self.location_table.cellDoubleClicked.connect(
            lambda r, c: self.edit_location(
                self.location_table.item(r, 0).data(Qt.ItemDataRole.UserRole)
            )
        )
        contents.addWidget(self.location_table)
        contents.addWidget(
            button("新建位置", "plus", self.new_location, True),
            0,
            Qt.AlignmentFlag.AlignLeft,
        )
        layout.addWidget(frame)
        layout.addWidget(
            label(
                "可选偏移默认关闭，半径 0～500 米。开启后最多校验 33 个本地候选，安全余量为允许距离的 5% 且最多 1 米。偏移只改变请求坐标，不代表设备 GPS，不保证学校接受。常用位置保持原坐标。",
                "muted",
            )
        )
        layout.addStretch()

    def build_history(self):
        layout = self.page_layouts[PAGE_INDEX["history"]]
        self.history_account = QComboBox()
        self.history_account.setAccessibleName("筛选账号")
        self.history_date_mode = QComboBox()
        self.history_date_mode.addItems(["全部日期", "今天", "指定日期"])
        self.history_date = QDateEdit(QDate.currentDate())
        self.history_date.setCalendarPopup(True)
        self.history_date.setDisplayFormat("yyyy-MM-dd")
        self.history_date.setAccessibleName("筛选日期")
        size_field(self.history_date)
        self.history_result = QComboBox()
        self.history_result.addItem("全部结果", None)
        for value, title in STATUS.items():
            self.history_result.addItem(title, value)
        layout.addWidget(
            WrapToolbar(
                self.history_account,
                self.history_date_mode,
                self.history_date,
                self.history_result,
            )
        )
        for widget in (
            self.history_account,
            self.history_date_mode,
            self.history_result,
        ):
            widget.currentIndexChanged.connect(self.refresh_history)
        self.history_date.dateChanged.connect(self.refresh_history)
        self.history_table = table(["执行时间", "账号", "任务", "结果", "说明"])
        self.history_table.setMinimumHeight(360)
        self.history_table.setSelectionMode(
            QAbstractItemView.SelectionMode.ExtendedSelection
        )
        for i, width in enumerate((155, 130, 230, 70)):
            self.history_table.setColumnWidth(i, width)
        self.history_table.cellDoubleClicked.connect(self.history_detail)
        self.history_table.itemSelectionChanged.connect(self.update_history_actions)
        self.history_delete_selected = button("删除所选", callback=lambda: self.delete_history("selected"))
        self.history_delete_filtered = button("删除当前筛选结果", callback=lambda: self.delete_history("filtered"))
        self.history_clear = button("清空历史", callback=lambda: self.delete_history("all"))
        self.history_detail_button = button("查看详情", callback=lambda: self.history_detail(self.history_table.currentRow()))
        layout.addWidget(WrapToolbar(
            self.history_detail_button, self.history_delete_selected,
            self.history_delete_filtered, self.history_clear,
        ))
        layout.addWidget(self.history_table, 1)
        self.history_empty = label("暂无符合条件的记录。", "muted")
        layout.addWidget(self.history_empty)
        layout.addWidget(
            label(
                "双击查看详情；Ctrl/Shift 可多选。最近保留 200 条记录和 20 个批次摘要。删除只清理本地历史，运行日志请从日志入口管理。", "muted"
            )
        )

    def build_settings(self):
        layout = self.page_layouts[PAGE_INDEX["settings"]]
        frame, contents = card("外观与交互")
        form = SettingForm()
        self.theme_combo = QComboBox()
        self.theme_combo.addItem("深色", "dark")
        self.theme_combo.addItem("浅色", "light")
        self.theme_combo.setCurrentIndex(self.theme_combo.findData(self.theme))
        self.theme_combo.currentIndexChanged.connect(self.select_theme)
        form.addRow(
            "界面主题",
            self.theme_combo,
            "保留已保存偏好；浅色与深色共享布局与控件规范。",
        )
        self.close_behavior = QComboBox()
        self.close_behavior.addItem("隐藏到托盘，继续运行", "tray")
        self.close_behavior.addItem("退出软件", "exit")
        self.close_behavior.setCurrentIndex(
            max(
                0,
                self.close_behavior.findData(
                    self.store.data["appearance"].get("close_behavior", "tray")
                ),
            )
        )
        self.close_behavior.currentIndexChanged.connect(self.save_close_behavior)
        form.addRow(
            "关闭窗口时",
            self.close_behavior,
            "真正退出会取消并等待任务结束，保存结果与日志。",
        )
        self.confirm_preview = Switch()
        self.confirm_preview.setAccessibleName("手动提交前确认整批")
        self.confirm_preview.setChecked(
            self.store.data["appearance"].get("confirm_preview", True)
        )
        self.confirm_preview.toggled.connect(self.save_preview_setting)
        form.addRow(
            "手动提交确认",
            self.confirm_preview,
            "开始前核对本批账号与位置；自动任务不等待确认弹窗。",
        )
        form.addRow(
            "首次使用引导",
            button("查看引导", callback=self.open_onboarding),
            "可跳过，支持纯手动配置；不会自动开启计划。",
        )
        contents.addLayout(form)
        layout.addWidget(frame)
        self.build_reliability_settings(layout)
        frame, contents = card("关于平安签到")
        form = SettingForm()
        form.addRow(
            "Python 桌面版",
            label(f"版本 {VERSION}", "muted"),
            "中南林业科技大学平安签到 · 独立桌面客户端",
        )
        form.addRow(
            "退出软件",
            button("真正退出", "exit", self.request_exit),
            "保存失败时保留内存与处理入口；不会丢弃未保存结果。",
        )
        contents.addLayout(form)
        layout.addWidget(frame)
        layout.addStretch()

    def header_action(self):
        self.start_run()

    def navigate(self, index):
        index = PAGE_INDEX[index] if isinstance(index, str) else index
        self.stack.setCurrentIndex(index)
        title, _, description = PAGES[index]
        self.page_title.setText(title)
        self.page_description.setText(description)
        count = sum(a.get("enabled", True) for a in self.store.data["accounts"])
        self.primary_action.setText(f"打卡全部启用账号（{count}）")
        self.primary_action.setToolTip(
            "执行全部参与批量与定时的账号，包含搜索结果外的账号，不要求选中。"
        )
        self.primary_action.setIcon(icon("play", PALETTES[self.theme]["accent_text"]))
        self.primary_action.setEnabled(
            not self.closing and not self.busy and self.pending_request is None
        )
        key = PAGES[index][1]
        self.page_action.setText(
            {
                "overview": "",
                "accounts": "添加账号",
                "schedule": "保存计划",
                "location": "保存全局定位",
                "history": "重置筛选",
                "log": "清除日志筛选",
                "settings": "打开数据目录",
            }[key]
        )
        self.page_action.setVisible(key != "overview")
        if key == "log":
            self.log_page.refresh_batches()
        for i, btn in enumerate(self.nav_buttons):
            btn.setChecked(i == index)
        self.update_history_actions()

    def toggle_sidebar(self):
        self.collapsed = not self.collapsed
        self.sidebar.setFixedWidth(68 if self.collapsed else 220)
        self.brand.setVisible(not self.collapsed)
        self.version_label.setVisible(not self.collapsed)
        for btn, (title, _, _) in zip(self.nav_buttons, PAGES):
            btn.setText("" if self.collapsed else title)
        self.sidebar_theme.setText(
            ""
            if self.collapsed
            else ("切换浅色主题" if self.theme == "dark" else "切换深色主题")
        )
        self.arrange_stats()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if hasattr(self, "stats"):
            self.arrange_stats()

    def arrange_stats(self):
        width = self.width() - (68 if self.collapsed else 220) - 70
        columns = 4 if width >= 800 else 2
        for i, widget in enumerate(self.stats):
            self.stat_grid.addWidget(widget, i // columns, i % columns)
        for i in range(4):
            self.stat_grid.setColumnStretch(i, 1 if i < columns else 0)

    def apply_theme(self, save=False):
        application = QApplication.instance()
        set_palette(application, self.theme)
        application.setStyleSheet(stylesheet(self.theme))
        compact_form_tree(self)
        for widget in self.findChildren(QPushButton):
            if widget.property("glyph"):
                widget.setIcon(
                    icon(
                        widget.property("glyph"),
                        (
                            PALETTES[self.theme]["accent_text"]
                            if widget.property("primary")
                            else PALETTES[self.theme]["muted"]
                        ),
                    )
                )
        for widget in self.findChildren(QCheckBox):
            widget.setProperty("theme", self.theme)
            widget.update()
        for btn, (_, glyph, _) in zip(self.nav_buttons, PAGES):
            btn.setIcon(icon(glyph, PALETTES[self.theme]["text"]))
        self.sidebar_theme.setIcon(
            icon(
                "sun" if self.theme == "dark" else "moon", PALETTES[self.theme]["muted"]
            )
        )
        self.sidebar_theme.setText(
            ""
            if self.collapsed
            else ("切换浅色主题" if self.theme == "dark" else "切换深色主题")
        )
        self.sidebar_theme.setToolTip("切换界面主题")
        self.theme_combo.blockSignals(True)
        self.theme_combo.setCurrentIndex(self.theme_combo.findData(self.theme))
        self.theme_combo.blockSignals(False)
        self.navigate(self.stack.currentIndex())
        self.refresh_history()
        self.refresh_recent()
        self.refresh_accounts()
        self.log_page.retheme()
        if save:
            if self.persist(
                lambda: self.store.data["appearance"].update(theme=self.theme)
            ):
                self.notify("主题已保存，重启后仍有效。")

    def toggle_theme(self):
        self.theme = "light" if self.theme == "dark" else "dark"
        self.apply_theme(True)

    def select_theme(self):
        self.theme = self.theme_combo.currentData()
        self.apply_theme(True)

    def save_preview_setting(self):
        if self.persist(
            lambda: self.store.data["appearance"].update(
                confirm_preview=self.confirm_preview.isChecked()
            )
        ):
            self.notify("提交确认设置已保存。")

    def notify(self, text, error=False):
        self.notice.setText(self.scrub(text))
        self.notice.setStyleSheet(
            f"color: {PALETTES[self.theme]['failure' if error else 'success']};"
        )
        self.notice.show()
        self.notification_timer.start(10000 if error else 5000)

    def scrub(self, value):
        text = str(value)
        # Keep secrets from the current batch even if an account is edited/deleted during execution.
        accounts = self.store.data["accounts"] + (
            self.worker.accounts if self.worker else []
        )
        for account in sorted(
            accounts, key=lambda a: len(a.get("openid", "")), reverse=True
        ):
            if account.get("openid"):
                text = text.replace(account["openid"], "[已隐藏]")
        return redact(text)

    def persist(self, mutation):
        before = self.store.snapshot()
        try:
            mutation()
            self.store.save()
            self.update_security_status()
            self.readiness_write_error = False
            self.last_storage_notice = None
            return True
        except Exception as error:
            self.store.data.clear()
            self.store.data.update(before)
            self.notify(f"保存失败：{error}。请检查数据目录是否可写后重试。", True)
            self.storage_system_notice()
            return False

    def refresh(self):
        self.refresh_accounts()
        self.refresh_locations()
        self.refresh_history_accounts()
        self.refresh_history()
        self.refresh_recent()
        self.refresh_stats()

    def refresh_stats(self):
        self.update_history_actions()
        self.refresh_local_readiness()
        moment = now_shanghai()
        day = moment.date().isoformat()
        upcoming = next_automatic(self.store.data, moment)
        self.stat_values[3].setText(
            ("重试 " if upcoming["kind"] == "retry" else "计划 ")
            + upcoming["at"].strftime("%m-%d %H:%M")
            if upcoming
            else (
                "自动打卡已关闭"
                if not self.store.data["schedule"]["enabled"]
                else "无后续执行日期"
            )
        )
        cycle = self.store.data.get("automation") or {}
        remaining = max(
            0,
            self.store.data["schedule"]["retry"]["max_retries"]
            - cycle.get("retry_count", 0),
        )
        self.stat_values[3].setToolTip(
            (
                f"自动重试剩余 {remaining} 次；{cycle.get('reason', '')}"
                if upcoming and upcoming["kind"] == "retry"
                else "仅展示已保存的自动计划；手动执行不受计划开关影响。"
            )
            + "\n北京时间"
        )
        self.retry_pending_info.setVisible(
            bool(upcoming and upcoming["kind"] == "retry" and not self.busy)
        )
        self.retry_pending_info.setText(
            f"下次重试 {upcoming['at']:%m-%d %H:%M} · 剩余 {remaining} 次\n{cycle.get('reason', '')}"
            if upcoming and upcoming["kind"] == "retry"
            else ""
        )
        self.plan_link.setVisible(not self.store.data["schedule"]["enabled"])
        self.schedule_next.setText(
            (
                "下次自动重试："
                if upcoming and upcoming["kind"] == "retry"
                else "下次自动执行："
            )
            + (
                upcoming["at"].strftime("%Y-%m-%d %H:%M，北京时间")
                if upcoming
                else (
                    "自动打卡已关闭"
                    if not self.store.data["schedule"]["enabled"]
                    else "无后续执行日期"
                )
            )
        )
        cycle = self.store.data.get("automation") or {}
        names = {
            "running": "执行中",
            "waiting_retry": "等待重试",
            "finished": "已结束",
            "cancelled": "已取消",
            "interrupted": "上次未完整结束",
        }
        counts = cycle.get("summary", {})
        summary = " · ".join(f"{STATUS[s]} {counts.get(s, 0)}" for s in STATUS)
        self.schedule_auto_status.setText(
            "尚未触发自动计划。"
            if not cycle
            else f"触发时间：{cycle.get('triggered_at', '—')}\n状态：{names.get(cycle.get('state'), '待核对')} · 额外重试 {cycle.get('retry_count', 0)} 次\n{summary}\n{cycle.get('reason', '')}"
        )
        self.refresh_overview_state(moment)
        try:
            count = make_request(self.store.data, "today", moment=moment)["count"]
        except ValueError:
            count = 0
        self.schedule_retry.setText(f"核对今日任务（{count}）")
        self.schedule_retry.setEnabled(
            not self.busy and not self.closing and self.pending_request is None
        )
        if hasattr(self, "primary_action"):
            self.primary_action.setText(
                f"打卡全部启用账号（{sum(a.get('enabled', True) for a in self.store.data['accounts'])}）"
            )

    def location_description(self, account):
        try:
            location = resolve_location(account, self.store.data)
            coords = (
                f"纬度 {location['latitude']} · 经度 {location['longitude']}"
                if location["mode"] == "specified"
                else "待任务查询；未读取设备 GPS"
            )
            source = "跟随全局" if location["source"] == "全局设置" else "账号单独设置"
            return (
                source,
                location["name"],
                coords
                + " · "
                + offset_description(resolve_offset(account, self.store.data)),
            )
        except ValueError as error:
            return "需修正定位", str(error), "请重新选择位置"

    def selected_accounts(self):
        ids = {
            item.data(Qt.ItemDataRole.UserRole)
            for item in self.account_table.selectedItems()
        }
        return [a for a in self.store.data["accounts"] if a["id"] in ids]

    def refresh_accounts(self, *_):
        scroll_position = self.account_table.verticalScrollBar().value()
        selected = {a["id"] for a in self.selected_accounts()}
        search = self.account_search.text().strip().casefold()
        self.display_accounts = [
            a for a in self.store.data["accounts"] if search in a["label"].casefold()
        ]
        day = now_shanghai().date().isoformat()
        rows = []
        for account in self.display_accounts:
            source, name, coords = self.location_description(account)
            recent = next(
                (
                    r
                    for r in self.store.data["results"]
                    if r["account_id"] == account["id"]
                ),
                None,
            )
            current = latest_today(self.store.data, account["id"], day)
            state = (
                "执行中"
                if account["id"] in self.running_ids
                else (
                    "成功"
                    if today_succeeded(self.store.data, account["id"], day)
                    else (
                        STATUS.get(current["status"], "今日未执行\n未查询")
                        if current
                        else "今日未执行\n未查询"
                    )
                )
            )
            rows.append(
                [
                    "",
                    self.scrub(account["label"]),
                    f"{source} · {name}\n{coords}",
                    state,
                    self.format_time(recent) if recent else "—",
                    "",
                ]
            )
        self.account_table.blockSignals(True)
        self.account_table.setSortingEnabled(False)
        put(self.account_table, rows, 3, self.theme)
        for i, account in enumerate(self.display_accounts):
            identity = account["id"]
            for c in range(self.account_table.columnCount()):
                self.account_table.item(i, c).setData(
                    Qt.ItemDataRole.UserRole, identity
                )
            toggle = Switch()
            toggle.setProperty("theme", self.theme)
            toggle.setChecked(account.get("enabled", True))
            toggle.setAccessibleName("参与批量与定时：" + self.scrub(account["label"]))
            toggle.setToolTip(
                "开启后加入全部账号批量与自动计划；关闭仍可行内临时打卡。"
            )
            toggle.toggled.connect(
                lambda enabled, identity=identity: self.toggle_account_by_id(
                    identity, enabled
                )
            )
            container = QWidget()
            contents = QHBoxLayout(container)
            contents.setContentsMargins(18, 0, 18, 0)
            contents.addWidget(toggle)
            self.account_table.setCellWidget(i, 0, container)
            self.account_table.item(i, 2).setText("")
            position = label(rows[i][2])
            position.setStyleSheet("font-size:12px;")
            position.setContentsMargins(10, 6, 10, 6)
            position.setToolTip(rows[i][2])
            position.setMaximumHeight(64)
            self.account_table.setCellWidget(i, 2, position)
            operations = QWidget()
            controls = QHBoxLayout(operations)
            controls.setContentsMargins(5, 0, 5, 0)
            controls.setSpacing(6)
            run = button(
                "重试" if rows[i][3] == "失败" else "打卡",
                "play",
                lambda identity=identity: self.start_run(
                    scope="single", identities=[identity]
                ),
            )
            run.setProperty("account_id", identity)
            run.setEnabled(
                not self.busy and not self.closing and self.pending_request is None
            )
            edit = button(
                "编辑",
                "edit",
                lambda identity=identity: self.edit_account_by_id(identity),
            )
            edit.setProperty("account_id", identity)
            controls.addWidget(run)
            controls.addWidget(edit)
            current = latest_today(self.store.data, identity, day)
            if current and current["status"] == "failure":
                details = button(
                    "原因",
                    None,
                    lambda identity=identity: self.show_account_reason(identity),
                )
                controls.addWidget(details)
            self.account_table.setCellWidget(i, 5, operations)
            self.account_table.setRowHeight(i, 60)
            if identity in selected:
                for c in range(self.account_table.columnCount()):
                    self.account_table.item(i, c).setSelected(True)
        self.account_table.setSortingEnabled(True)
        self.account_table.blockSignals(False)
        self.account_table.verticalScrollBar().setValue(scroll_position)
        self.account_empty.setVisible(not self.store.data["accounts"])
        self.account_table.setVisible(bool(self.store.data["accounts"]))
        enabled = sum(a.get("enabled", True) for a in self.store.data["accounts"])
        self.account_counts.setText(
            f"显示 {len(self.display_accounts)} / 共 {len(self.store.data['accounts'])} 个账号 · 全部启用 {enabled} 个（主按钮包含搜索结果外的账号）"
        )
        self.update_selection()

    def update_selection(self):
        count = len(self.selected_accounts())
        self.bulk_bar.setVisible(count > 0)
        self.selection_label.setText(f"本次选中 {count} 个账号")
        self.bulk_run.setText(f"打卡所选账号（{count}）")
        self.bulk_run.setEnabled(
            count > 0
            and not self.busy
            and not self.closing
            and self.pending_request is None
        )
        for action in (self.bulk_enable, self.bulk_disable, self.bulk_delete):
            action.setEnabled(count > 0 and not self.closing)
        self.edit_account_button.setEnabled(count == 1)
        self.copy_account_button.setEnabled(count == 1)
        self.show_identifier.blockSignals(True)
        self.show_identifier.setChecked(False)
        self.show_identifier.blockSignals(False)
        self.identifier_text.clear()
        self.identifier_text.setEchoMode(QLineEdit.EchoMode.Password)

    def show_account_identifier(self, show):
        accounts = self.selected_accounts()
        self.identifier_text.setEchoMode(
            QLineEdit.EchoMode.Normal if show else QLineEdit.EchoMode.Password
        )
        self.identifier_text.setText(
            accounts[0]["openid"] if show and len(accounts) == 1 else ""
        )

    def copy_account(self):
        accounts = self.selected_accounts()
        if len(accounts) == 1:
            copied = copy_text(accounts[0]["openid"])
            self.notify(
                "已复制 OpenID，请妥善保管。" if copied else CLIPBOARD_ERROR, not copied
            )

    def toggle_account(self, account, enabled):
        self.toggle_account_by_id(account["id"], enabled)

    def set_accounts_enabled(self, enabled):
        ids = {a["id"] for a in self.selected_accounts()}
        if self.persist(
            lambda: [
                a.update(enabled=enabled)
                for a in self.store.data["accounts"]
                if a["id"] in ids
            ]
        ):
            self.notify(
                f"已将 {len(ids)} 个账号设为{'参与' if enabled else '不参与'}批量与定时；临时打卡不受此开关限制。"
            )
        self.refresh()
        self.navigate(self.stack.currentIndex())

    def add_account(self):
        self.account_dialog()

    def edit_account(self):
        accounts = self.selected_accounts()
        if len(accounts) == 1:
            self.account_dialog(accounts[0])

    def account_dialog(self, account=None):
        dialog = AccountDialog(self, account)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return

        def change():
            if account:
                if account["openid"] != dialog.values["openid"]:
                    self.store.data["success_dates"].pop(account["id"], None)
                    self.store.data["task_success_dates"].pop(account["id"], None)
                account.update(dialog.values)
                account.pop("credential_error", None)
                account.pop("openid_protected", None)
            else:
                self.store.data["accounts"].append(
                    {**new_account(dialog.values["openid"]), **dialog.values}
                )

        if self.persist(change):
            self.refresh()
            self.notify("账号已保存。")

    def confirm(self, title, text):
        dialog = QMessageBox(self)
        dialog.setWindowTitle(title)
        dialog.setIcon(QMessageBox.Icon.Question)
        dialog.setTextFormat(Qt.TextFormat.PlainText)
        dialog.setText(self.scrub(text))
        accept = dialog.addButton("确认", QMessageBox.ButtonRole.AcceptRole)
        reject = dialog.addButton("取消", QMessageBox.ButtonRole.RejectRole)
        dialog.setDefaultButton(reject)
        dialog.setEscapeButton(reject)
        dialog.exec()
        return dialog.clickedButton() is accept

    def delete_accounts(self):
        accounts = self.selected_accounts()
        if not accounts or not self.confirm(
            "删除账号",
            f"删除选中的 {len(accounts)} 个账号？历史记录会保留。正在执行的任务使用启动时的账号配置。",
        ):
            return
        ids = {a["id"] for a in accounts}
        if self.persist(
            lambda: self.store.data.update(
                accounts=[a for a in self.store.data["accounts"] if a["id"] not in ids]
            )
        ):
            self.refresh()
            self.notify("所选账号已删除。")

    def import_file(self):
        if self.import_worker:
            self.notify("正在导入，请等待当前文件处理完成。", True)
            return
        path, _ = QFileDialog.getOpenFileName(
            self, "导入账号", "", "账号文件 (*.txt *.env *.json);;所有文件 (*)"
        )
        if path:
            self.start_import(path)

    def start_import(self, path):
        if self.import_worker or self.closing or self.data_job:
            return
        worker = ImportWorker(path, self)
        self.import_worker = worker
        worker.finished.connect(self.finish_import)
        self.footer_status.setText("正在读取账号文件…")
        worker.start()

    def finish_import(self):
        worker = self.import_worker
        self.import_worker = None
        if worker.error:
            self.notify(worker.error, True)
        else:
            existing = {a["openid"] for a in self.store.data["accounts"]}
            values = list(dict.fromkeys(v for v in worker.values if v not in existing))
            if values and (
                self.closing
                or not self.confirm(
                    "导入预览",
                    f"读取结果文件：{worker.path}\n解析 {len(worker.values)} 项，去重后新增 {len(values)} 个账号。\n仅导入 OpenID，不读取捕获、恢复状态或 CA 密钥。导入后请设置定位与计划。",
                )
            ):
                worker.deleteLater()
                self.notify("已取消导入，账号未改变。")
                if self.closing and not self.busy:
                    self.shutdown()
                return
            if self.persist(
                lambda: self.store.data["accounts"].extend(
                    new_account(v, f"导入账号 {len(existing) + i + 1}")
                    for i, v in enumerate(values)
                )
            ):
                self.refresh()
                self.notify(
                    f"导入完成：新增 {len(values)} 个账号，已忽略重复项。下一步：左侧「定位设置」→「定时任务」。"
                )
        worker.deleteLater()
        self.footer_status.setText("导入完成")
        if self.closing and not self.busy:
            self.shutdown()

    def schedule_value(self):
        return {
            **self.store.data["schedule"],
            "enabled": self.schedule_enabled.isChecked(),
            "time": self.schedule_time.time().toString("HH:mm"),
            "mode": self.schedule_mode.currentData(),
            "dates": self.schedule_dates.toPlainText()
            .replace("，", "\n")
            .replace(",", "\n")
            .splitlines(),
            "retry": {
                "enabled": self.schedule_retry_enabled.isChecked(),
                "max_retries": self.retry_count.value(),
                "interval_minutes": self.retry_interval.value(),
            },
            "task_rule": {
                "mode": self.task_rule_mode.currentData(),
                "name": self.task_rule_name.text().strip(),
            },
        }

    def edit_schedule_dates(self):
        dialog = QDialog(self)
        dialog.setWindowTitle("编辑指定日期")
        dialog.resize(540, 470)
        box = QVBoxLayout(dialog)
        box.addWidget(label("选择执行日期", "title"))
        box.addWidget(
            label("日历添加，或每行输入 YYYY-MM-DD。取消保留原草稿。", "muted")
        )
        picker = QDateEdit(QDate.currentDate())
        picker.setCalendarPopup(True)
        picker.setDisplayFormat("yyyy-MM-dd")
        picker.setAccessibleName("添加执行日期")
        size_field(picker)
        editor = QPlainTextEdit(self.schedule_dates.toPlainText())
        editor.setAccessibleName("指定日期列表草稿")

        def add_day():
            value = picker.date().toString("yyyy-MM-dd")
            lines = editor.toPlainText().splitlines()
            if value not in [line.strip() for line in lines]:
                editor.setPlainText("\n".join([*lines, value]))

        box.addWidget(WrapToolbar(picker, button("添加日期", "plus", add_day)))
        box.addWidget(editor, 1)
        error = label("", "muted")
        error.hide()
        box.addWidget(error)
        actions = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Cancel
            | QDialogButtonBox.StandardButton.Save
        )
        actions.button(QDialogButtonBox.StandardButton.Save).setText("保留日期草稿")
        actions.button(QDialogButtonBox.StandardButton.Cancel).setText("取消")

        def accept():
            try:
                for line in editor.toPlainText().splitlines():
                    value = line.strip()
                    if value and date.fromisoformat(value).isoformat() != value:
                        raise ValueError()
            except ValueError:
                error.setText("日期格式应为 YYYY-MM-DD，请修正后再保留。")
                error.setStyleSheet(f"color:{PALETTES[self.theme]['failure']};")
                error.show()
                return
            self.schedule_dates.setPlainText(editor.toPlainText())
            dialog.accept()

        actions.accepted.connect(accept)
        actions.rejected.connect(dialog.reject)
        box.addWidget(actions)
        dialog.exec()
        dialog.deleteLater()

    def schedule_dirty(self, *_):
        self.schedule_form.setRowVisible(
            self.schedule_date_control, self.schedule_mode.currentData() == "dates"
        )
        self.retry_fields.setVisible(self.schedule_retry_enabled.isChecked())
        self.task_form.setRowVisible(
            self.task_rule_name, self.task_rule_mode.currentData() == "exact_name"
        )
        days = [
            line
            for line in self.schedule_dates.toPlainText().splitlines()
            if line.strip()
        ]
        self.schedule_dates_summary.setText(
            f"{len(days)} 项日期草稿" if days else "未选择日期"
        )
        self.schedule_dates_summary.setToolTip(self.schedule_dates.toPlainText())
        self.schedule_dates.setEnabled(self.schedule_mode.currentData() == "dates")
        self.retry_count.setEnabled(self.schedule_retry_enabled.isChecked())
        self.retry_interval.setEnabled(self.schedule_retry_enabled.isChecked())
        self.task_rule_name.setEnabled(
            self.task_rule_mode.currentData() == "exact_name"
        )
        dirty = self.schedule_value() != self.store.data["schedule"]
        self.schedule_badge.setText(
            "有未保存的修改 · 保存后才会生效" if dirty else "已保存 · 当前计划已生效"
        )

    def load_schedule(self):
        schedule = self.store.data["schedule"]
        widgets = [
            self.schedule_enabled,
            self.schedule_time,
            self.schedule_mode,
            self.schedule_dates,
            self.schedule_retry_enabled,
            self.retry_count,
            self.retry_interval,
            self.task_rule_mode,
            self.task_rule_name,
        ]
        for w in widgets:
            w.blockSignals(True)
        self.schedule_enabled.setChecked(schedule["enabled"])
        self.schedule_time.setTime(QTime.fromString(schedule["time"], "HH:mm"))
        self.schedule_mode.setCurrentIndex(
            self.schedule_mode.findData(schedule["mode"])
        )
        self.schedule_dates.setPlainText("\n".join(schedule["dates"]))
        self.schedule_retry_enabled.setChecked(schedule["retry"]["enabled"])
        self.retry_count.setValue(schedule["retry"]["max_retries"])
        self.retry_interval.setValue(schedule["retry"]["interval_minutes"])
        rule = schedule.get("task_rule", {"mode": "unique_current", "name": ""})
        self.task_rule_mode.setCurrentIndex(self.task_rule_mode.findData(rule["mode"]))
        self.task_rule_name.setText(rule.get("name", ""))
        for w in widgets:
            w.blockSignals(False)
        self.schedule_dirty()

    def save_schedule(self):
        draft_dates = self.schedule_dates.toPlainText()
        candidate = self.schedule_value()
        inactive_invalid_dates = False
        if candidate["mode"] != "dates":
            try:
                for value in candidate["dates"]:
                    value = value.strip()
                    if value and date.fromisoformat(value).isoformat() != value:
                        raise ValueError("日期格式应为 YYYY-MM-DD。")
            except ValueError:
                inactive_invalid_dates = True
                candidate["dates"] = list(self.store.data["schedule"]["dates"])
        try:
            proposed = validate_schedule(candidate)
        except ValueError as error:
            self.notify(f"计划未保存：{error} 请修正计划。", True)
            return
        previous = self.store.data["schedule"]
        moment = now_shanghai()
        prepared = prepare_schedule(previous, proposed, moment)
        choice = None
        if prepared is None:
            choice = self.choose_past_plan(proposed)
            prepared = prepare_schedule(previous, proposed, moment, choice)
            if prepared is None:
                self.notify("已取消保存，原计划未改变，也未触发任务。")
                return
        changed = fingerprint(previous) != fingerprint(prepared)

        def save():
            self.store.data["schedule"] = prepared
            if changed or not prepared["enabled"]:
                invalidate_pending(self.store.data)

        if self.persist(save):
            self.load_schedule()
            if inactive_invalid_dates:
                self.schedule_dates.setPlainText(draft_dates)
                self.schedule_dirty()
            self.refresh_stats()
            self.notify(
                "定时计划已保存；从下次符合规则的时间开始。"
                if choice == "next"
                else "定时计划已保存。"
            )
            if choice == "now":
                self.start_run(scheduled=True)

    def tick(self):
        self.refresh_stats()
        if (
            self.closing
            or getattr(self, "history_delete_pending", False)
            or self._destroyed
            or self.pending_results is not None
            or self.data_job
            or self.journal_error
            or self.legacy_log_pending is not None
        ):
            return
        moment = now_shanghai()
        cycle = self.store.data.get("automation") or {}
        if cycle.get("next_retry") and not pending_ids(self.store.data, moment):
            saved = self.persist(
                lambda: invalidate_pending(
                    self.store.data,
                    "待重试账号已成功、停止参与或日期/计划已变化，已取消待开始重试。",
                )
            )
            if saved:
                final_failures = [
                    {**result, "account_id": identity}
                    for identity, result in cycle.get("account_results", {}).items()
                    if result.get("status") == "failure"
                    and self.store.data.get("task_success_dates", {})
                    .get(identity, {})
                    .get(result.get("task_id"))
                    != cycle.get("date")
                ]
                self.batch_system_notice(
                    final_failures,
                    {"scope": "retry", "run_id": cycle.get("attempt_id")},
                )
        if self.busy:
            return
        action = due_action(self.store.data, moment)
        if action == "retry":
            self.start_run(
                scope="retry", identities=pending_ids(self.store.data, moment)
            )
        elif action == "initial":
            self.start_run(scheduled=True)

    def save_global_location(self):
        try:
            choice = self.global_choice.checked_value()
            offset = self.global_offset.checked_value()
        except ValueError as error:
            self.notify(str(error), True)
            return
        if self.persist(
            lambda: self.store.data.update(global_location=choice, global_offset=offset)
        ):
            self.global_offset.saved_radius = offset["radius"]
            self.global_location_dirty()
            self.refresh_accounts()
            self.notify("全局定位已保存。已启动的任务继续使用原快照。")

    def refresh_locations(self, select_id=None):
        # Preserve an unsaved global choice even when another page or task refreshes.
        draft = self.global_choice.value()
        old = self.selected_location()
        selected = select_id or (old["id"] if old else None)
        self.display_locations = list(self.store.data["locations"])
        self.location_table.blockSignals(True)
        self.location_table.setSortingEnabled(False)
        put(
            self.location_table,
            [
                (p["name"], p["latitude"], p["longitude"], "")
                for p in self.display_locations
            ],
        )
        for row_index, position in enumerate(self.display_locations):
            identity = position["id"]
            for col in range(4):
                self.location_table.item(row_index, col).setData(
                    Qt.ItemDataRole.UserRole, identity
                )
            actions = QWidget()
            controls = QHBoxLayout(actions)
            controls.setContentsMargins(6, 0, 6, 0)
            controls.setSpacing(8)
            edit = button(
                "编辑", "edit", lambda identity=identity: self.edit_location(identity)
            )
            more = button("更多")
            menu = QMenu(more)
            menu.addAction(
                "设为全局默认",
                lambda checked=False, identity=identity: self.apply_global_location(
                    identity
                ),
            )
            menu.addAction(
                "删除位置",
                lambda checked=False, identity=identity: self.delete_location(identity),
            )
            more.setMenu(menu)
            controls.addWidget(edit)
            controls.addWidget(more)
            self.location_table.setCellWidget(row_index, 3, actions)
            self.location_table.setRowHeight(row_index, 50)
        self.location_table.setSortingEnabled(True)
        if selected:
            for r in range(self.location_table.rowCount()):
                if (
                    self.location_table.item(r, 0).data(Qt.ItemDataRole.UserRole)
                    == selected
                ):
                    self.location_table.selectRow(r)
                    break
        self.location_table.blockSignals(False)
        self.global_choice.data = self.store.data
        self.global_choice.mode.blockSignals(True)
        self.global_choice.position.blockSignals(True)
        self.global_choice.refresh(draft)
        self.global_choice.mode.blockSignals(False)
        self.global_choice.position.blockSignals(False)
        self.global_location_dirty()

    def new_location(self):
        return self.location_dialog()

    def selected_location(self):
        index = self.location_table.currentRow()
        item = self.location_table.item(index, 0) if index >= 0 else None
        identity = item.data(Qt.ItemDataRole.UserRole) if item else None
        return next(
            (p for p in self.store.data["locations"] if p["id"] == identity), None
        )

    def edit_location(self, identity=None):
        position = (
            next((p for p in self.store.data["locations"] if p["id"] == identity), None)
            if identity
            else self.selected_location()
        )
        if not position:
            self.notify("请先选择一个仍存在的常用位置。", True)
            return
        return self.location_dialog(position)

    def location_dialog(self, position=None):
        dialog = LocationDialog(self, position)
        dialog.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        dialog.setWindowModality(Qt.WindowModality.WindowModal)
        dialog.show()
        return dialog

    def location_references(self, identity):
        uses = []
        if self.store.data["global_location"].get("location_id") == identity:
            uses.append("全局默认定位")
        uses.extend(
            "账号「" + self.scrub(a["label"]) + "」"
            for a in self.store.data["accounts"]
            if a.get("location", {}).get("location_id") == identity
        )
        return uses

    def delete_location(self, identity=None):
        position = (
            next((p for p in self.store.data["locations"] if p["id"] == identity), None)
            if identity
            else self.selected_location()
        )
        if not position:
            self.notify("请先选择一个常用位置。", True)
            return
        uses = self.location_references(position["id"])
        if uses:
            self.notify(
                "无法删除：" + "、".join(uses) + "正在使用该位置。请先更换其定位选择。",
                True,
            )
            return
        if self.global_choice.value().get("location_id") == position["id"]:
            self.notify(
                "尚未保存的全局定位选择使用了该位置。请先更换或保存全局选择后删除。",
                True,
            )
            return
        if self.confirm(
            "删除位置", f"删除常用位置「{position['name']}」？"
        ) and self.persist(
            lambda: self.store.data.update(
                locations=[
                    p for p in self.store.data["locations"] if p["id"] != position["id"]
                ]
            )
        ):
            self.refresh_locations()
            self.refresh_accounts()
            self.notify("常用位置已删除。")

    def global_location_dirty(self, *_):
        try:
            offset_dirty = validate_offset(
                self.global_offset.value()
            ) != self.store.data.get("global_offset", {"mode": "off", "radius": 0})
        except ValueError:
            offset_dirty = True
        self.global_dirty.setText(
            "有未保存的全局定位选择"
            if self.global_choice.value() != self.store.data["global_location"]
            or offset_dirty
            else "已保存 · 当前全局定位已生效"
        )

    def apply_global_location(self, identity):
        if not any(p["id"] == identity for p in self.store.data["locations"]):
            self.notify("位置已不存在，请刷新后重试。", True)
            return
        if self.persist(
            lambda: self.store.data.update(
                global_location={"mode": "specified", "location_id": identity}
            )
        ):
            self.global_choice.refresh(self.store.data["global_location"])
            self.global_location_dirty()
            self.refresh_accounts()
            self.notify("已设为全局默认；已启动任务继续使用固定快照。")

    def refresh_history_accounts(self):
        selected = self.history_account.currentData()
        self.history_account.blockSignals(True)
        self.history_account.clear()
        self.history_account.addItem("全部账号", None)
        names = {
            r["account_id"]: r["label"] for r in reversed(self.store.data["results"])
        }
        names.update({a["id"]: a["label"] for a in self.store.data["accounts"]})
        for identity, name in names.items():
            self.history_account.addItem(self.scrub(name), identity)
        self.history_account.setCurrentIndex(
            max(0, self.history_account.findData(selected))
        )
        self.history_account.blockSignals(False)

    def refresh_history(self, *_):
        if not hasattr(self, "history_table"):
            return
        selected_ids = self.selected_history_ids()
        ensure_record_ids(self.store.data["results"])
        old_scroll = self.history_table.verticalScrollBar().value()
        account = self.history_account.currentData()
        status = self.history_result.currentData()
        mode = self.history_date_mode.currentIndex()
        date = (
            now_shanghai().date().isoformat()
            if mode == 1
            else self.history_date.date().toString("yyyy-MM-dd")
        )
        self.history_date.setVisible(mode == 2)
        self.display_results = [
            r
            for r in self.store.data["results"]
            if (not account or r["account_id"] == account)
            and (not status or r["status"] == status)
            and (not mode or r["date"] == date)
        ]
        self.history_table.blockSignals(True)
        sorting = self.history_table.isSortingEnabled()
        self.history_table.setSortingEnabled(False)
        put(
            self.history_table,
            [
                (
                    self.format_time(r),
                    self.scrub(r["label"]),
                    self.scrub(r.get("task", "")),
                    STATUS.get(r["status"], r["status"]),
                    self.scrub(r["message"]),
                )
                for r in self.display_results
            ],
            3,
            self.theme,
        )
        self.history_empty.setVisible(not self.display_results)
        self.history_table.clearSelection()
        for row, result in enumerate(self.display_results):
            for column in range(self.history_table.columnCount()):
                item = self.history_table.item(row, column)
                item.setData(Qt.ItemDataRole.UserRole, result["record_id"])
                item.setSelected(result["record_id"] in selected_ids)
            if result["record_id"] in selected_ids:
                self.history_table.setCurrentCell(row, 0, QItemSelectionModel.SelectionFlag.NoUpdate)
        self.history_table.setSortingEnabled(sorting)
        self.history_table.blockSignals(False)
        self.history_table.verticalScrollBar().setValue(old_scroll)
        self.update_history_actions()

    def selected_history_ids(self):
        if not hasattr(self, "history_table"):
            return frozenset()
        return frozenset(
            self.history_table.item(index.row(), 0).data(Qt.ItemDataRole.UserRole)
            for index in self.history_table.selectionModel().selectedRows()
            if self.history_table.item(index.row(), 0) is not None
        )

    def history_delete_available(self):
        return not (
            self.busy or self.closing or self.data_job or self.import_worker
            or self.pending_request or self.pending_results is not None
            or self.journal_error or self.legacy_log_pending is not None
            or getattr(self, "history_delete_pending", False)
        )

    def update_history_actions(self):
        if not hasattr(self, "history_delete_selected"):
            return
        available = self.history_delete_available()
        self.history_delete_selected.setEnabled(available and bool(self.selected_history_ids()))
        self.history_delete_filtered.setEnabled(available and bool(getattr(self, "display_results", [])))
        self.history_clear.setEnabled(available and bool(
            self.store.data["results"] or self.store.data["overview_batches"]))
        self.history_detail_button.setEnabled(len(self.selected_history_ids()) == 1)

    def delete_history(self, scope):
        if not self.history_delete_available():
            self.notify("请结束当前任务、预览或数据操作，并保存结果/日志后再删除历史。", True)
            return
        if scope == "selected":
            identities = self.selected_history_ids()
        elif scope == "filtered":
            identities = frozenset(r["record_id"] for r in self.display_results)
        elif scope == "all":
            identities = frozenset(r["record_id"] for r in self.store.data["results"])
        else:
            raise ValueError("未知历史删除范围")
        if not identities and not (scope == "all" and self.store.data["overview_batches"]):
            return
        names = {"selected": "删除所选", "filtered": "删除当前筛选结果", "all": "清空历史"}
        preview = self.store.snapshot()
        delete_records(preview, identities)
        removed_batches = len(self.store.data["overview_batches"]) - len(preview["overview_batches"])
        original_batches = {b["batch_id"]: b for b in self.store.data["overview_batches"]}
        changed_batches = sum(b != original_batches[b["batch_id"]] for b in preview["overview_batches"])
        timer_active = self.timer.isActive()
        self.timer.stop()
        self.history_delete_pending = True
        self.update_history_actions()
        try:
            if not self.confirm(names[scope], f"将删除 {len(identities)} 条本地执行记录，移除 {removed_batches} 个批次摘要、更新 {changed_batches} 个批次摘要。账号、计划、成功标记及重试状态保留；运行日志由日志入口管理。确认后不可撤销。"):
                return
            # Recheck safety after the modal event loop; the scope remains the captured ID set.
            self.history_delete_pending = False
            if not self.history_delete_available():
                self.notify("状态已变化，请结束任务并保存结果后重试删除。", True)
                return
            try:
                count = self.store.delete_history(identities)
            except Exception as error:
                self.notify(f"删除历史失败：{error}。原历史已保留，请检查后重试。", True)
                return
            self.refresh()
            self.notify(f"已删除 {count} 条本地历史记录，相关批次摘要已更新。")
        finally:
            self.history_delete_pending = False
            if timer_active:
                self.timer.start(1000)
            self.update_history_actions()

    def reset_history_filters(self):
        self.history_account.setCurrentIndex(0)
        self.history_date_mode.setCurrentIndex(0)
        self.history_result.setCurrentIndex(0)
        self.refresh_history()

    def format_time(self, result):
        return (result.get("finished_at") or result.get("date", "")).replace("T", " ")[
            :16
        ]

    def refresh_recent(self):
        self.refresh_overview_recent()

    def history_detail(self, index, *_):
        item = self.history_table.item(index, 0)
        if item is not None:
            identity = item.data(Qt.ItemDataRole.UserRole)
            result = next((r for r in self.store.data["results"] if r["record_id"] == identity), None)
            if result:
                self.show_detail(result)

    def recent_detail(self, index, column=0):
        item = self.recent_table.item(index, column)
        if item is not None:
            batch = item.data(Qt.ItemDataRole.UserRole)
            if batch:
                self.show_batch_detail(batch)

    def show_detail(self, result):
        dialog = QDialog(self)
        dialog.setWindowTitle("任务详情")
        dialog.resize(600, 520)
        layout = QVBoxLayout(dialog)
        layout.setContentsMargins(24, 24, 24, 24)
        layout.setSpacing(16)
        layout.addWidget(label("任务详情", "title"))
        summary_card, summary_box = card()
        summary_form = SettingForm()
        outcome = label(STATUS.get(result["status"], result["status"]), "section")
        outcome.setStyleSheet(
            f"color:{PALETTES[self.theme].get(result['status'], PALETTES[self.theme]['text'])};"
        )
        summary_form.addRow(
            self.scrub(result["label"]),
            outcome,
            self.scrub(result.get("task", "任务未确认"))
            + " · "
            + self.format_time(result),
        )
        summary_box.addLayout(summary_form)
        layout.addWidget(summary_card)
        layout.addWidget(label("完整详情 · 定位与服务端查询", "section"))
        text = QPlainTextEdit()
        text.setReadOnly(True)
        lines = [
            f"账号：{result['label']}",
            f"用户：{result.get('user_name', '')}",
            f"学校：{result.get('school', '')}",
            f"学号：{result.get('account_no', '')}",
            f"执行时间：{self.format_time(result)}",
            f"任务：{result.get('task', '')}",
            f"结果：{STATUS.get(result['status'], result['status'])}",
            f"说明：{result['message']}",
        ]
        location = result.get("location", {})
        preview = result.get("preview", {})
        if location:
            lines.extend(
                [
                    "",
                    f"定位规则：{location.get('source', '')}",
                    f"位置名称：{location.get('name', '')}",
                ]
            )
        if preview:
            lines.extend(
                [
                    f"实际纬度：{preview.get('latitude')}",
                    f"实际经度：{preview.get('longitude')}",
                    f"距离任务位置：{preview.get('distance')} 米",
                    f"允许距离：小于 {preview.get('allowed_distance')} 米",
                    f"本地校验：{preview.get('reason', '')}",
                ]
            )
            lines.extend(
                [
                    f"中心坐标：{preview.get('center_latitude')}，{preview.get('center_longitude')}",
                    f"配置半径：{preview.get('radius', 0)} 米",
                    f"实际偏移：{preview.get('offset_distance', 0):.3f} 米",
                    f"选用阶段：{preview.get('stage', '')} · 余量 {preview.get('margin', 0)} 米",
                    f"候选次数：{preview.get('candidate_count', 0)} · 提交尝试 {result.get('submission_count', 0)}",
                    f"调整原因：{preview.get('adjustment_reason', '')}",
                ]
            )
        if result.get("server_status"):
            lines.extend(["", f"服务端查询：{result['server_status']}"])
        if result.get("position_origin") and not preview:
            origin = result["position_origin"]
            lines.extend(
                [
                    "",
                    f"中心坐标：{origin['center_latitude']}，{origin['center_longitude']}",
                    f"配置半径：{origin['radius']} 米",
                    f"中心距任务位置：{origin['distance']} 米；允许小于 {origin['allowed_distance']} 米",
                    f"本地候选阶段：{result.get('candidate_stages', [])}",
                ]
            )
        text.setPlainText(self.scrub("\n".join(lines)))
        layout.addWidget(text, 1)
        layout.addWidget(
            ActionRow(
                button(
                    "查看原因 / 关联日志",
                    "log",
                    lambda: (dialog.accept(), self.open_result_logs(result)),
                ),
                button("关闭", None, dialog.accept),
            )
        )
        dialog.exec()

    def open_data(self):
        self.open_directory(self.store.directory)

    def open_logs(self):
        self.open_directory(self.journal.root)

    def open_legacy_logs(self):
        self.open_directory(self.store.directory / "总日志")

    def open_directory(self, directory):
        try:
            directory.mkdir(parents=True, exist_ok=True)
            if not QDesktopServices.openUrl(
                QUrl.fromLocalFile(str(directory.resolve()))
            ):
                raise OSError()
        except OSError:
            self.notify(
                "无法打开目录。请复制设置页面的路径，在文件资源管理器中打开。", True
            )

    def show_logs(self, visible):
        self.log_panel.setVisible(visible)
        self.log_toggle.setText("收起日志" if visible else "查看日志")
        if visible:
            self.splitter.setSizes([max(200, self.height() - 430), 210])

    def start_run(self, selected=False, scheduled=False, scope=None, identities=None):
        if self.busy or self.closing or self.data_job or self.import_worker or getattr(self, "history_delete_pending", False):
            return
        scope = scope or (
            "automatic" if scheduled else "selected" if selected else "all"
        )
        automatic = scope in ("automatic", "retry")
        if self.legacy_log_pending is not None:
            self.notify("上次日志尚未保存，请先重试保存日志。", True)
            return
        if self.pending_results is not None:
            self.notify("上次结果尚未保存，请先重试保存结果。", True)
            return
        if self.pending_request is not None:
            if not automatic:
                return
            if self.preview_dialog:
                self.preview_dialog.reject()
        if selected and identities is None:
            identities = [a["id"] for a in self.selected_accounts()]
        try:
            request = make_request(self.store.data, scope, identities)
        except ValueError as error:
            if scope == "automatic":
                empty = {
                    "run_id": __import__("uuid").uuid4().hex,
                    "accounts": [],
                    "count": 0,
                    "scope_label": "自动计划：无待执行账号",
                }

                def skip():
                    begin_attempt(self.store.data, empty, now_shanghai())
                    finish_attempt(self.store.data, empty, [], now_shanghai())
                    self.store.data["automation"]["reason"] = str(error)

                if self.persist(skip):
                    try:
                        self.journal.start(empty)
                        self.run_lines = []
                        self.record_event(
                            {
                                "message": str(error),
                                "step": "批次结果",
                                "level": "warning",
                            }
                        )
                        self.journal.finish([], "skipped")
                        self.log_page.on_updated()
                    except Exception:
                        self.notify(
                            "自动计划已跳过，但日志未保存。请检查日志目录权限后重试保存日志。",
                            True,
                        )
            else:
                self.navigate("accounts")
                self.notify(str(error), True)
            self.refresh_stats()
            return
        if scope == "retry":
            cycle = self.store.data.get("automation") or {}
            request["retry_context"] = {
                "date": cycle.get("date"),
                "task_ids": dict(cycle.get("task_ids", {})),
            }
        if not automatic and self.confirm_preview.isChecked():
            self.pending_request = request
            self.preview_dialog = BatchPreviewDialog(self, request)
            self.preview_dialog.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
            self.preview_dialog.finished.connect(self.batch_preview_finished)
            self.preview_dialog.setWindowModality(Qt.WindowModality.NonModal)
            self.preview_dialog.show()
            self.navigate(self.stack.currentIndex())
            self.update_selection()
            return
        self.begin_run(request)

    def save_worker_position(self, value, worker):
        try:
            worker.position_saved = self.persist(
                lambda: checkpoint_position(self.store.data, value)
            )
            if not worker.position_saved:
                self.cancel.set()
                self.timer.stop()
        finally:
            worker.position_ready.set()

    def handle_event(self, kind, value):
        if kind == "log_event":
            self.record_event(value)
        elif kind == "log":
            self.record_event(
                {"message": self.scrub(value), "step": "批次", "account_id": None}
            )
        elif kind == "progress":
            account = (
                next(
                    (a for a in self.worker.accounts if a["id"] == value["account_id"]),
                    {},
                )
                if self.worker
                else {}
            )
            self.run_stage.setText(
                f"{self.scrub(account.get('label', '账号'))} · {value['stage']} · 已完成 {len(self.run_results)}/{self.progress.maximum()}"
            )
        elif kind == "task":
            self.run_task.setText(
                f"当前任务 ID：{value['task_id']} · {value.get('window', {}).get('start', '')}–{value.get('window', {}).get('end', '')}"
            )
            if self.active_request and self.active_request["scope"] in (
                "automatic",
                "retry",
            ):
                saved = self.persist(
                    lambda: checkpoint_task(
                        self.store.data, self.active_request["run_id"], value
                    )
                )
                if not saved:
                    self.cancel.set()
                    self.timer.stop()
        elif kind == "preview":
            self.previews[value["account_id"]] = value
            self.preview_card.show()
            distance = (
                f"{value['distance']:.2f} 米"
                if value.get("distance") is not None
                else "无法计算"
            )
            self.preview_text.setText(
                self.scrub(
                    f"{value['label']} · {value['name']}\n任务 {value.get('task', '')} · {value.get('date', '')} · {value.get('start', '')}–{value.get('end', '')}\n纬度 {value['latitude']} · 经度 {value['longitude']}\n距离任务位置 {distance}，允许小于 {value.get('allowed_distance')} 米\n{value['reason']}"
                )
            )
            if value.get("stage"):
                self.preview_text.setText(
                    self.preview_text.text()
                    + f"\n中心 {value.get('center_latitude')}, {value.get('center_longitude')} · 半径 {value.get('radius', 0)} 米\n实际偏移 {value.get('offset_distance', 0):.3f} 米 · {value['stage']} · {value.get('adjustment_reason', '')}"
                )
            self.preview_summary.setText(
                self.scrub(
                    f"{value['name']} · 距任务位置 {distance} · {value.get('stage', '')}"
                )
            )
            self.run_task.setText(
                self.scrub(
                    f"当前任务：{value.get('task', '')} · {value.get('date', '')}"
                )
            )
        elif kind == "position":
            if not self.persist(lambda: checkpoint_position(self.store.data, value)):
                self.cancel.set()
                self.timer.stop()
        elif kind == "result":
            self.journal.checkpoint_result(value)
            self.run_results.append(value)
            # One immediate alert per batch for errors that cannot be retried.
            if (
                self.active_request
                and value.get("status") == "failure"
                and not value.get("retryable")
                and not getattr(self, "urgent_notified", False)
            ):
                self.urgent_notified = True
                self.batch_system_notice(self.run_results, self.active_request)
            self.progress.setValue(len(self.run_results))
            self.running_ids.discard(value["account_id"])
            if self.active_request and self.active_request["scope"] in (
                "automatic",
                "retry",
            ):

                def checkpoint():
                    merge_results(self.store.data, [value])
                    checkpoint_result(
                        self.store.data, self.active_request["run_id"], value
                    )

                if not self.persist(checkpoint):
                    self.cancel.set()
                    self.timer.stop()
            self.flush_journal()
            self.refresh_accounts()
            self.refresh_stats()
        elif kind == "tray_action":
            if value == "restore":
                self.restore_window()
            elif value == "exit":
                self.request_exit()
            elif value == "notification":
                self.open_notification_target()

    def preview_finished(self):
        self.preview_dialog = None

    def cancel_run(self):
        if self.pending_request is not None and self.preview_dialog:
            self.preview_dialog.reject()
        if not self.busy:
            return
        self.cancel.set()
        if self.task_dialog:
            self.task_dialog.reject()
        self.cancel_button.setEnabled(False)
        self.run_stage.setText("正在取消，等待当前请求返回并保存结果…")
        if (self.store.data.get("automation") or {}).get("next_retry"):
            self.persist(
                lambda: invalidate_pending(
                    self.store.data, "用户主动取消，不自动重试。"
                )
            )

    def finish_run(self):
        worker = self.worker
        request = worker.request
        results = worker.results

        def save_results():
            merge_results(self.store.data, results)
            record_batch(
                self.store.data,
                request,
                results,
                now_shanghai(),
                (
                    "interrupted"
                    if worker.error
                    else "cancelled" if self.cancel.is_set() else "finished"
                ),
            )
            if request["scope"] in ("automatic", "retry"):
                finish_attempt(
                    self.store.data,
                    request,
                    results,
                    now_shanghai(),
                    self.cancel.is_set(),
                )

        saved = self.persist(save_results)
        if not saved:
            self.pending_results = results
            self.pending_context = (request, self.cancel.is_set())
            self.retry_save_button.show()
            self.timer.stop()
        counts = {key: sum(r["status"] == key for r in results) for key in STATUS}
        summary = " · ".join(f"{STATUS[k]} {counts[k]}" for k in STATUS)
        self.last_batch_summary = "本批：" + summary
        outcome = (
            "失败"
            if counts["failure"] or worker.error
            else (
                "成功"
                if counts["success"]
                else "取消" if counts["cancelled"] else "跳过"
            )
        )
        log_error = None
        if worker.error:
            self.record_event(
                {
                    "message": "任务中断：" + self.scrub(worker.error),
                    "level": "error",
                    "step": "批次结果",
                }
            )
        self.record_event(
            {
                "message": "任务结束：" + summary,
                "step": "批次结果",
                "level": "error" if counts["failure"] or worker.error else "info",
            }
        )
        try:
            self.journal.finish(
                results,
                (
                    "cancelled"
                    if self.cancel.is_set()
                    else "interrupted" if worker.error else "finished"
                ),
            )
        except Exception:
            log_error = "结构化日志保存失败，请检查目录权限并重试保存日志；本次内容仍保留在内存。"
            self.journal_error = log_error
        self.log_page.on_updated()
        try:
            self.store.write_log(self.run_lines, outcome)
        except OSError:
            log_error = "日志保存失败，请检查日志目录的写入权限；当前日志仍可复制。"
            self.legacy_log_pending = (list(self.run_lines), outcome)
        self.busy = False
        self.running_ids.clear()
        self.readiness_card.show()
        self.onboarding_card.setVisible(not self.store.data["onboarding_done"])
        self.refresh()
        self.navigate(self.stack.currentIndex())
        self.update_selection()
        self.footer_status.setText("任务已结束 · 账号结果与关联日志见概览")
        self.run_card.hide()
        if worker.error:
            self.notify(
                "任务中断：" + self.scrub(worker.error) + "。请查看日志后重试。", True
            )
        elif log_error:
            self.notify("任务完成：" + summary + "；" + log_error, True)
        elif saved:
            self.notify(
                (
                    "执行已结束；失败账号请查看概览待处理项。"
                    if counts["failure"]
                    else "执行结果已保存。"
                ),
                bool(counts["failure"]),
            )
        self.worker = None
        if self.task_dialog:
            self.task_dialog.reject()
        self.batch_system_notice(results, request)
        if log_error or not saved:
            self.storage_system_notice()
        self.active_request = None
        worker.deleteLater()
        if self.closing and self.pending_results is not None:
            self.closing = False
            self.restore_window()
            self.notify(
                "结果尚未保存，请检查目录权限并点击「重试保存结果」后退出。", True
            )
        elif self.closing and not self.import_worker:
            self.shutdown()

    def retry_results(self):
        if self.pending_results is None:
            return

        def save():
            merge_results(self.store.data, self.pending_results)
            if self.pending_context:
                request, cancelled = self.pending_context
                record_batch(
                    self.store.data,
                    request,
                    self.pending_results,
                    now_shanghai(),
                    "cancelled" if cancelled else "finished",
                )
                if request["scope"] in ("automatic", "retry"):
                    finish_attempt(
                        self.store.data,
                        request,
                        self.pending_results,
                        now_shanghai(),
                        cancelled,
                    )

        if self.persist(save):
            self.pending_results = None
            self.pending_context = None
            self.retry_save_button.hide()
            self.refresh()
            self.notify("任务结果已保存。")
            self.timer.start(1000)

    def on_close(self, force_tray=False):
        if self.closing or self._destroyed:
            return
        if (
            not force_tray
            and self.store.data["appearance"].get("close_behavior", "tray") == "exit"
        ):
            self.request_exit()
            return
        try:
            if self.tray is None:
                self.tray = TrayIcon(self.handle_event, self)
            self.tray.show()
            if not self.store.data["appearance"].get("tray_notice_shown", False):
                message = "软件仍在运行，自动计划继续。单击托盘恢复，右键可退出软件。"
                if (
                    hasattr(self.tray, "notify")
                    and not self.demo
                    and self.store.data["notifications"]["enabled"]
                ):
                    self.tray.notify("平安签到已隐藏到托盘", message)
                self.log_text.appendPlainText(message)
                self.persist(
                    lambda: self.store.data["appearance"].update(tray_notice_shown=True)
                )
            if self.preview_dialog:
                self.preview_dialog.hide()
            self.hide()
        except Exception as error:
            if self.tray:
                self.tray.close()
                self.tray = None
            self.notify(str(error), True)
            self.show()

    def restore_window(self):
        if self._destroyed:
            return
        self.showNormal()
        self.raise_()
        self.activateWindow()
        if self.tray:
            self.tray.hide()
        if self.preview_dialog:
            self.preview_dialog.show()

    def request_exit(self):
        if self.closing or self._destroyed:
            return
        if self.pending_results is not None:
            self.restore_window()
            self.notify("请先点击「重试保存结果」，保存完成后再退出。", True)
            return
        if not self.isVisible():
            self.restore_window()
        if self.busy and not self.confirm(
            "退出软件", "当前任务仍在运行。取消任务并等待清理后退出？"
        ):
            return
        self.closing = True
        self.timer.stop()
        self.cancel_run()
        self.navigate(self.stack.currentIndex())
        if not self.busy and not self.import_worker and not self.data_job:
            self.shutdown()

    def shutdown(self):
        if self.busy or self.import_worker or self.data_job or self._destroyed:
            return
        self.timer.stop()
        if self.legacy_log_pending is not None and not self.retry_overview_logs():
            self.closing = False
            self.restore_window()
            return
        if not self.flush_journal(update=False):
            self.closing = False
            self.restore_window()
            return
        self.journal_timer.stop()
        if not self.log_page.stop():
            return
        if self.tray:
            self.tray.close()
            self.tray = None
        self._destroyed = True
        self.close()
        if self.quit_on_shutdown:
            QApplication.instance().quit()

    def closeEvent(self, event):
        if self._destroyed:
            event.accept()
        else:
            event.ignore()
            self.on_close()

    def account_by_id(self, identity):
        return next(
            (a for a in self.store.data["accounts"] if a["id"] == identity), None
        )

    def edit_account_by_id(self, identity):
        account = self.account_by_id(identity)
        if account:
            self.account_dialog(account)
        else:
            self.notify("账号已不存在，请刷新列表后重试。", True)

    def toggle_account_by_id(self, identity, enabled):
        account = self.account_by_id(identity)
        if account and self.persist(lambda: account.update(enabled=enabled)):
            self.notify(
                "已参与批量与定时。"
                if enabled
                else "已停止参与批量与定时；仍可临时打卡。"
            )
        self.refresh()
        self.navigate(self.stack.currentIndex())

    def show_account_reason(self, identity):
        result = latest_today(
            self.store.data, identity, now_shanghai().date().isoformat()
        )
        if result:
            self.open_result_logs(result)

    def batch_preview_finished(self, result):
        request = self.pending_request
        self.pending_request = None
        self.preview_dialog = None
        if (
            request
            and result == QDialog.DialogCode.Accepted
            and not self.closing
            and not self.busy
        ):
            self.begin_run(request)
        else:
            self.navigate(self.stack.currentIndex())
            self.update_selection()
            if not self.closing:
                self.notify("已取消整批确认，未启动本批任务。")

    def begin_run(self, request):
        if self.busy or self.closing or self.data_job or getattr(self, "history_delete_pending", False):
            return
        self.journal.secrets.extend(a["openid"] for a in request["accounts"])
        try:
            self.journal.start(request)
        except Exception:
            self.notify(
                "无法创建本批日志，请检查数据目录写入权限后重试；本批尚未开始。", True
            )
            return
        if request["scope"] in ("automatic", "retry"):
            try:
                if not self.persist(
                    lambda: begin_attempt(
                        self.store.data,
                        request,
                        now_shanghai(),
                        request["scope"] == "retry",
                    )
                ):
                    self.record_event(
                        {
                            "message": "计划状态保存失败，本批未启动。",
                            "level": "error",
                            "step": "批次结果",
                        }
                    )
                    self.journal.finish([], "not_started")
                    return
            except ValueError as error:
                self.record_event(
                    {"message": str(error), "level": "error", "step": "批次结果"}
                )
                self.journal.finish([], "not_started")
                self.notify(str(error), True)
                return
        self.active_request = request
        self.busy = True
        self.onboarding_card.hide()
        self.readiness_card.hide()
        self.cancel = threading.Event()
        self.run_results = []
        self.urgent_notified = False
        self.run_lines = []
        self.previews = {}
        self.running_ids = {a["id"] for a in request["accounts"]}
        self.run_card.show()
        self.preview_toggle.setChecked(False)
        self.run_task.setText("任务尚未查询")
        self.batch_summary.setText("本批：等待账号结果")
        self.attention_card.hide()
        self.preview_card.hide()
        self.progress.setRange(0, request["count"])
        self.progress.setValue(0)
        self.run_stage.setText(f"{request['scope_label']} · {request['count']} 个账号")
        self.cancel_button.setEnabled(True)
        self.navigate("overview")
        self.refresh_accounts()
        self.update_selection()
        self.footer_status.setText(
            f"执行范围：{request['scope_label']} · 共 {request['count']} 个账号"
        )
        self.schedule_retry.setEnabled(False)
        self.worker = RunWorker(request, self.cancel, self.demo, self)
        self.worker.event.connect(self.handle_event)
        self.worker.position_checkpoint.connect(
            lambda value: self.save_worker_position(value, self.worker)
        )
        self.worker.task_selection.connect(
            lambda account, candidates: self.choose_worker_task(
                account, candidates, self.worker
            )
        )
        self.worker.finished.connect(self.finish_run)
        line = f"执行范围：{request['scope_label']}；账号数量 {request['count']}。"
        self.handle_event("log", line)
        for account in request["accounts"]:
            if not account.get("enabled", True):
                self.record_event(
                    {
                        "account_id": account["id"],
                        "account_label": account["label"],
                        "step": "执行范围",
                        "message": "本次临时执行，仍不参与自动计划。",
                    }
                )
        self.log_page.refresh_batches()
        self.worker.start()

    def choose_past_plan(self, schedule):
        dialog = QDialog(self)
        dialog.setWindowTitle("今天的计划时间已过")
        dialog.setMinimumWidth(480)
        contents = QVBoxLayout(dialog)
        contents.setContentsMargins(24, 24, 24, 24)
        contents.setSpacing(16)
        contents.addWidget(label("保存后从何时开始？", "title"))
        contents.addWidget(
            label(
                f"今天符合日期规则，但 {schedule['time']} 已经过。保存计划不会默认立即打卡。",
                "muted",
            )
        )
        choice = QComboBox()
        choice.addItem("从下次符合规则的时间开始（默认）", "next")
        choice.addItem("今天立即补跑", "now")
        contents.addWidget(choice)
        box = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Cancel
            | QDialogButtonBox.StandardButton.Save
        )
        box.button(QDialogButtonBox.StandardButton.Cancel).setText("取消保存")
        box.button(QDialogButtonBox.StandardButton.Save).setText("按此选择保存")
        box.accepted.connect(dialog.accept)
        box.rejected.connect(dialog.reject)
        contents.addWidget(box)
        accepted = dialog.exec() == QDialog.DialogCode.Accepted
        value = choice.currentData() if accepted else None
        dialog.deleteLater()
        return value

    def save_close_behavior(self):
        if self.persist(
            lambda: self.store.data["appearance"].update(
                close_behavior=self.close_behavior.currentData()
            )
        ):
            self.notify("关闭窗口偏好已保存。")

    def secondary_action(self):
        key = PAGES[self.stack.currentIndex()][1]
        {
            "overview": self.start_run,
            "accounts": self.add_account,
            "schedule": self.save_schedule,
            "location": self.save_global_location,
            "history": self.reset_history_filters,
            "log": self.log_page.clear_filters,
            "settings": self.open_data,
        }[key]()

    def open_full_logs(self):
        self.navigate("log")
        if self.active_request:
            self.log_page.refresh_batches(self.active_request["run_id"])

    def open_result_logs(self, result):
        self.navigate("log")
        identity = result.get("batch_id") or result.get("run_id")
        self.log_page.clear_filters()
        if identity:
            self.log_page.refresh_batches(identity, result.get("account_id"))
        else:
            self.log_page.show_unlinked()

    def record_event(self, value):
        if not self.journal.active:
            append_quick(self.log_text, "系统：" + self.scrub(value.get("message", "")))
            return
        event = self.journal.append(value)
        line = event_text(event)
        self.run_lines.append(line)
        append_quick(self.log_text, line)

    def flush_journal(self, update=True):
        dirty = bool(self.journal.dirty or self.journal.meta_dirty)
        try:
            if dirty or self.journal_error:
                self.journal.flush()
            self.journal_error = None
        except Exception:
            self.journal_error = "日志保存失败，请检查日志目录写入权限后点击「重试保存日志」；本次记录仍在内存。"
            if not self.notice.isVisible() or self.notice.text() != self.scrub(
                self.journal_error
            ):
                self.notify(self.journal_error, True)
            if hasattr(self, "save_alert"):
                self.save_alert.show()
                self.overview_retry_logs.show()
            self.storage_system_notice()
            return False
        if dirty and update:
            self.log_page.on_updated()
        return True
