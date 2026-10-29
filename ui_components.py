"""Shared setting rows, wrapping toolbars and dialog presentation, with no IO."""

from PySide6.QtCore import Qt, QObject, QEvent, QTimer
from PySide6.QtWidgets import (
    QWidget,
    QFrame,
    QLabel,
    QVBoxLayout,
    QGridLayout,
    QLayout,
    QSizePolicy,
    QDialog,
    QDialogButtonBox,
    QPushButton,
    QPlainTextEdit,
    QComboBox,
    QLineEdit,
)
from form_layout import size_field, compact_form_tree
from shiboken6 import isValid

ROW_GAP = 16


def plain_label(text, description=False):
    item = QLabel(str(text))
    item.setWordWrap(True)
    item.setTextFormat(Qt.TextFormat.PlainText)
    item.setObjectName("rowDescription" if description else "rowTitle")
    item.setMinimumWidth(0)
    item.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Preferred)
    return item


class SettingRow(QFrame):
    def __init__(self, title, control, description="", expand=False):
        super().__init__()
        self.setObjectName("settingRow")
        self.grid = QGridLayout(self)
        self.grid.setContentsMargins(0, 12, 0, 12)
        self.grid.setHorizontalSpacing(ROW_GAP)
        self.grid.setVerticalSpacing(8)
        self.copy = QWidget()
        texts = QVBoxLayout(self.copy)
        texts.setContentsMargins(0, 0, 0, 0)
        texts.setSpacing(4)
        self.title = plain_label(title)
        self.description = plain_label(description, True)
        texts.addWidget(self.title)
        texts.addWidget(self.description)
        self.description.setVisible(bool(description))
        if isinstance(control, QLayout):
            holder = QWidget()
            holder.setLayout(control)
            control.setContentsMargins(0, 0, 0, 0)
            control = holder
        self.control = control
        control.installEventFilter(self)
        self.expand = expand
        if not expand:
            size_field(control)
        if not control.accessibleName():
            control.setAccessibleName(title)
        self.title.setBuddy(control)
        control.setToolTip(control.toolTip() or description)
        self.stacked = None
        self.reflow_timer = QTimer(self)
        self.reflow_timer.setSingleShot(True)
        self.reflow_timer.timeout.connect(self.reflow)
        self.reflow()

    def reflow(self):
        if not isValid(self) or not isValid(self.control):
            return
        desired = min(self.control.sizeHint().width(), self.control.maximumWidth())
        stacked = self.width() < desired + ROW_GAP + 190
        if stacked == self.stacked:
            return
        self.stacked = stacked
        self.grid.removeWidget(self.copy)
        self.grid.removeWidget(self.control)
        self.grid.addWidget(self.copy, 0, 0)
        if stacked:
            self.grid.addWidget(
                self.control,
                1,
                0,
                Qt.AlignmentFlag.AlignLeft if not self.expand else Qt.AlignmentFlag(0),
            )
            self.grid.setColumnStretch(0, 1)
            self.grid.setColumnStretch(1, 0)
        else:
            self.grid.addWidget(
                self.control,
                0,
                1,
                Qt.AlignmentFlag.AlignRight if not self.expand else Qt.AlignmentFlag(0),
            )
            self.grid.setColumnStretch(0, 1)
            self.grid.setColumnStretch(1, 1 if self.expand else 0)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self.reflow()

    def eventFilter(self, watched, event):
        if event.type() in (QEvent.Type.LayoutRequest, QEvent.Type.StyleChange):
            self.reflow_timer.start(0)
        return False


class SettingForm(QVBoxLayout):
    """A row group exposing the small QFormLayout API used by existing workflows."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setContentsMargins(0, 0, 0, 0)
        self.setSpacing(0)
        self.rows = {}
        self.originals = []

    def addRow(self, title, field, description="", expand=False):
        row = SettingRow(title, field, description, expand)
        self.rows[field] = row
        self.originals.append(field)
        self.addWidget(row)
        self.separators()
        return row

    def separators(self):
        visible = [
            self.rows[field]
            for field in self.originals
            if self.rows[field].property("rowVisible") is not False
        ]
        for row in self.rows.values():
            value = row in visible[:-1]
            if row.property("separator") != value:
                row.setProperty("separator", value)
                row.style().unpolish(row)
                row.style().polish(row)

    def setRowVisible(self, field, visible):
        row = self.rows[field]
        row.setProperty("rowVisible", visible)
        row.setVisible(visible)
        field.setVisible(visible)
        self.separators()

    def labelForField(self, field):
        return self.rows[field].title


class WrapToolbar(QWidget):
    """Reflow existing controls without rebuilding them or resetting their values."""

    def __init__(self, *controls, parent=None):
        super().__init__(parent)
        self.setObjectName("toolbar")
        self.controls = controls
        self.grid = QGridLayout(self)
        self.grid.setContentsMargins(0, 0, 0, 0)
        self.grid.setSpacing(8)
        self.signature = None
        self.reflow_timer = QTimer(self)
        self.reflow_timer.setSingleShot(True)
        self.reflow_timer.timeout.connect(self.reflow)
        for control in controls:
            control.setParent(self)
            control.show()
            control.installEventFilter(self)
            if isinstance(control, QPushButton):
                size_field(control)
            elif (
                isinstance(control, QComboBox)
                and control.property("formTargetWidth") is None
            ):
                control.setMinimumWidth(0)
                control.setSizePolicy(
                    QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Fixed
                )
                control.currentTextChanged.connect(control.setToolTip)
        self.reflow()

    def reflow(self):
        if not isValid(self):
            return
        available = max(1, self.width())
        positions, row, col, used = [], 0, 0, 0
        for control in self.controls:
            if not isValid(control):
                continue
            if control.isHidden():
                continue
            width = min(control.sizeHint().width(), control.maximumWidth(), available)
            if col and used + width + 8 > available:
                row, col, used = row + 1, 0, 0
            positions.append((control, row, col))
            used += width + 8
            col += 1
        signature = tuple((id(c), r, col) for c, r, col in positions)
        if signature == self.signature:
            return
        self.signature = signature
        for control in self.controls:
            self.grid.removeWidget(control)
        for control, row, col in positions:
            self.grid.addWidget(control, row, col)
        for col in range(self.grid.columnCount()):
            items = [control for control, _, column in positions if column == col]
            weight = (
                3 if any(isinstance(c, (QLineEdit, QComboBox)) for c in items) else 0
            )
            self.grid.setColumnStretch(col, weight)
        self.updateGeometry()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self.reflow()

    def eventFilter(self, watched, event):
        if event.type() in (
            QEvent.Type.Show,
            QEvent.Type.Hide,
            QEvent.Type.LayoutRequest,
        ):
            self.reflow_timer.start(0)
        return False


class DialogPresentation(QObject):
    def eventFilter(self, watched, event):
        if isinstance(watched, QDialog) and event.type() == QEvent.Type.Show:
            layout = watched.layout()
            if layout and not watched.property("visualPolished"):
                watched.setProperty("visualPolished", True)
                layout.setContentsMargins(22, 20, 22, 20)
                layout.setSpacing(12)
                for box in watched.findChildren(QDialogButtonBox):
                    for button in box.buttons():
                        if box.standardButton(button) in (
                            QDialogButtonBox.StandardButton.Save,
                            QDialogButtonBox.StandardButton.Ok,
                        ):
                            button.setProperty("primary", True)
                        button.setMinimumHeight(button.fontMetrics().height() + 14)
                        button.style().unpolish(button)
                        button.style().polish(button)
                for editor in watched.findChildren(QPlainTextEdit):
                    editor.setObjectName("dialogText")
                timer = QTimer(watched)
                timer.setSingleShot(True)
                timer.timeout.connect(
                    lambda: compact_form_tree(watched) if isValid(watched) else None
                )
                timer.start(0)
        return False


def install_dialog_presentation(application):
    if not hasattr(application, "dialog_presentation"):
        application.dialog_presentation = DialogPresentation(application)
        application.installEventFilter(application.dialog_presentation)
