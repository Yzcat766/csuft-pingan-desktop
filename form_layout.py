"""Content-sized form fields in Qt logical pixels; tables and paths stay expansive."""

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QWidget,
    QFormLayout,
    QGridLayout,
    QVBoxLayout,
    QLabel,
    QSizePolicy,
    QComboBox,
    QLineEdit,
    QAbstractSpinBox,
    QSpinBox,
    QDateTimeEdit,
    QCheckBox,
    QPushButton,
    QPlainTextEdit,
)

GAP = 12
TEXT_COLUMNS = 24
LONG_COLUMNS = 38


def size_field(widget, kind=None):
    """Keep native editing/step/calendar buttons and use the styled sizeHint."""
    if widget.property("formSize") == "expand":
        return widget
    kind = kind or widget.property("formKind")
    widget.ensurePolished()
    fm = widget.fontMetrics()
    if isinstance(widget, (QLineEdit, QComboBox, QAbstractSpinBox)):
        widget.setMinimumHeight(fm.height() + 20)
    samples = []
    if isinstance(widget, QComboBox):
        samples = [widget.itemText(i) for i in range(widget.count())]
        widget.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToContents)
        widget.setToolTip(widget.currentText())
        if not widget.property("formTooltipConnected"):
            widget.currentTextChanged.connect(widget.setToolTip)
            widget.setProperty("formTooltipConnected", True)
    elif isinstance(widget, QSpinBox):
        samples = [
            widget.prefix() + str(v) + widget.suffix()
            for v in (widget.minimum(), widget.maximum())
        ]
    elif isinstance(widget, QDateTimeEdit):
        samples = ["23:59" if widget.displayFormat() == "HH:mm" else "2099-12-31"]
    elif isinstance(widget, QLineEdit):
        columns = (
            18 if kind == "coordinate" else (10 if kind == "number" else TEXT_COLUMNS)
        )
        samples = ["0" * columns]
    elif isinstance(widget, QPlainTextEdit):
        samples = ["0" * LONG_COLUMNS]
    elif isinstance(widget, (QCheckBox, QPushButton)):
        widget.setSizePolicy(QSizePolicy.Policy.Maximum, QSizePolicy.Policy.Fixed)
        return widget
    else:
        return widget
    # Frame/padding and the widest native button area are included by sizeHint;
    # the extra font-based allowance protects stylesheet spin/calendar controls.
    text_width = max((fm.horizontalAdvance(s) for s in samples), default=0)
    allowance = fm.horizontalAdvance("000000")
    width = max(
        widget.sizeHint().width() if not isinstance(widget, QLineEdit) else 0,
        text_width + allowance,
    )
    if isinstance(widget, QComboBox) and widget.accessibleName() == "常用位置":
        width = min(width, fm.horizontalAdvance("汉" * 20) + allowance)
        widget.setSizeAdjustPolicy(
            QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon
        )
        widget.setMinimumContentsLength(16)
    widget.setMaximumWidth(width)
    # A measured native editor minimum makes QFormLayout move the label above
    # the field in narrow dialogs instead of clipping an option or a step button.
    # Free text (including arbitrary position names) may scroll/elide normally.
    protected = isinstance(widget, QAbstractSpinBox) or (
        isinstance(widget, QComboBox) and widget.accessibleName() != "常用位置"
    )
    widget.setMinimumWidth(width if protected else 0)
    widget.setSizePolicy(
        QSizePolicy.Policy.Preferred,
        (
            QSizePolicy.Policy.Preferred
            if isinstance(widget, QPlainTextEdit)
            else QSizePolicy.Policy.Fixed
        ),
    )
    widget.setProperty("formTargetWidth", width)
    widget.setProperty("formKind", kind or "auto")
    return widget


class FormLayout(QFormLayout):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setHorizontalSpacing(GAP + 4)
        self.setVerticalSpacing(GAP)
        self.setLabelAlignment(
            Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter
        )
        self.setFormAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop)
        self.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
        self.setRowWrapPolicy(QFormLayout.RowWrapPolicy.WrapLongRows)

    def addRow(self, *args):
        field = args[-1]
        if isinstance(field, QWidget):
            size_field(field)
        super().addRow(*args)
        if len(args) == 2 and isinstance(field, QWidget):
            caption = self.labelForField(field)
            if caption and not field.accessibleName():
                field.setAccessibleName(caption.text())


class AdaptiveFields(QWidget):
    """Two small fields share a row when they fit, otherwise stack without loss."""

    def __init__(self, entries, parent=None):
        super().__init__(parent)
        self.grid = QGridLayout(self)
        self.grid.setContentsMargins(0, 0, 0, 0)
        self.grid.setSpacing(GAP)
        self.groups = []
        for title, field in entries:
            size_field(field)
            group = QWidget()
            box = QVBoxLayout(group)
            box.setContentsMargins(0, 0, 0, 0)
            box.setSpacing(6)
            if title:
                caption = QLabel(title)
                caption.setBuddy(field)
                field.setAccessibleName(title)
                box.addWidget(caption)
            box.addWidget(field)
            self.groups.append(group)
        self.columns = None
        self.reflow()

    def reflow(self):
        needed = sum(g.sizeHint().width() for g in self.groups) + GAP * (
            len(self.groups) - 1
        )
        columns = len(self.groups) if self.width() >= needed else 1
        if self.columns == columns:
            return
        self.columns = columns
        for group in self.groups:
            self.grid.removeWidget(group)
        for i, group in enumerate(self.groups):
            self.grid.addWidget(
                group, i // columns, i % columns, Qt.AlignmentFlag.AlignLeft
            )
        for i in range(len(self.groups) + 1):
            self.grid.setColumnStretch(i, 1 if i == columns else 0)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self.reflow()


def compact_form_tree(root):
    """Recalculate after stylesheet/font changes, without constraining long fields."""
    for field in root.findChildren(QWidget):
        if field.property("formTargetWidth") is not None:
            size_field(field, field.property("formKind"))


class ActionRow(AdaptiveFields):
    def __init__(self, *buttons):
        super().__init__([(None, item) for item in buttons])
