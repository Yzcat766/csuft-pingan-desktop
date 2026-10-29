"""Neutral Qt styling and one consistent, code-native SVG icon family."""

from PySide6.QtCore import QByteArray, Qt, QRectF, QPointF
from PySide6.QtGui import QColor, QIcon, QPainter, QPen, QPixmap, QPalette
from PySide6.QtSvg import QSvgRenderer
from PySide6.QtWidgets import QCheckBox, QApplication
import ui_resources  # Registers the embedded SVG resources in source and frozen builds.

PALETTES = {
    "dark": dict(
        bg="#202124",
        side="#191A1C",
        card="#27282B",
        field="#292A2D",
        border="#3B3D40",
        text="#ECECEC",
        muted="#A0A0A0",
        hover="#333333",
        pressed="#404040",
        accent="#BBD7CE",
        accent_text="#16221E",
        selected="#303D38",
        success="#98CFB2",
        failure="#EBA7A7",
        cancelled="#D7C097",
        skipped="#A8B4C5",
    ),
    "light": dict(
        bg="#FAFAFA",
        side="#F2F3F3",
        card="#FFFFFF",
        field="#FFFFFF",
        border="#E4E5E7",
        text="#232426",
        muted="#777A80",
        hover="#F4F5F5",
        pressed="#E9EBEB",
        accent="#285E4B",
        accent_text="#FFFFFF",
        selected="#E3ECE7",
        success="#327C53",
        failure="#B74343",
        cancelled="#916C24",
        skipped="#64748B",
    ),
}
PATHS = {
    "overview": '<rect x="3" y="3" width="7" height="7" rx="1.5"/><rect x="14" y="3" width="7" height="7" rx="1.5"/><rect x="3" y="14" width="7" height="7" rx="1.5"/><rect x="14" y="14" width="7" height="7" rx="1.5"/>',
    "accounts": '<circle cx="9" cy="8" r="3"/><path d="M3 21v-3a6 6 0 0 1 12 0v3M17 5a3 3 0 0 1 0 6M18 15a5 5 0 0 1 3 5"/>',
    "schedule": '<rect x="3" y="5" width="18" height="16" rx="2"/><path d="M7 3v4M17 3v4M3 11h18M7 15h3M14 15h3M7 18h3"/>',
    "location": '<path d="M20 10c0 6-8 12-8 12S4 16 4 10a8 8 0 1 1 16 0Z"/><circle cx="12" cy="10" r="2.5"/>',
    "history": '<path d="M3 11a9 9 0 1 1 2 7M3 4v7h7M12 7v5l3 2"/>',
    "settings": '<circle cx="12" cy="12" r="3"/><path d="m9 3-.7 2.2-2 .9-2.1-.5-2 3.4 1.5 1.7v2.5L2.2 15l2 3.4 2.1-.5 2 .9L9 21h4l.7-2.2 2-.9 2.1.5 2-3.4-1.5-1.8v-2.4l1.5-1.7-2-3.4-2.1.5-2-.9L13 3Z"/>',
    "menu": '<path d="M4 6h16M4 12h16M4 18h16"/>',
    "play": '<path d="m8 4 12 8-12 8Z"/>',
    "plus": '<path d="M12 5v14M5 12h14"/>',
    "import": '<path d="M12 3v12m-5-5 5 5 5-5M4 16v5h16v-5"/>',
    "edit": '<path d="m15 4 5 5M4 20l5-1L21 7l-5-5L4 14Z"/>',
    "trash": '<path d="M3 6h18M9 6V3h6v3M5 6l1 15h12l1-15M10 10v7M14 10v7"/>',
    "copy": '<rect x="8" y="8" width="13" height="13" rx="2"/><path d="M16 8V3H3v13h5"/>',
    "eye": '<path d="M2 12s4-7 10-7 10 7 10 7-4 7-10 7S2 12 2 12Z"/><circle cx="12" cy="12" r="3"/>',
    "log": '<path d="M5 3h14v18H5ZM8 7h8M8 11h8M8 15h5"/>',
    "folder": '<path d="M3 6h7l2 2h9v12H3ZM3 8V4h7l2 2"/>',
    "sun": '<circle cx="12" cy="12" r="4"/><path d="M12 2v2M12 20v2M2 12h2M20 12h2M5 5l1.5 1.5M17.5 17.5 19 19M5 19l1.5-1.5M17.5 6.5 19 5"/>',
    "moon": '<path d="M20 14A9 9 0 0 1 10 3a9 9 0 1 0 10 11Z"/>',
    "close": '<path d="m6 6 12 12M6 18 18 6"/>',
    "check": '<path d="m5 12 4 4L19 6"/>',
    "exit": '<path d="M10 3H3v18h7M8 12h13m-5-5 5 5-5 5"/>',
    "save": '<path d="M3 3h15l3 3v15H3ZM7 3v6h10V3M7 21v-7h10v7"/>',
}


def icon(name, color="#A0A0A0", size=24):
    data = f'<svg xmlns="http://www.w3.org/2000/svg" width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="{color}" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round">{PATHS.get(name, PATHS["overview"])}</svg>'
    result = QIcon()
    renderer = QSvgRenderer(QByteArray(data.encode()))
    for scale in (1, 1.25, 1.5, 2, 3):
        pixmap = QPixmap(round(size * scale), round(size * scale))
        pixmap.fill(Qt.GlobalColor.transparent)
        painter = QPainter(pixmap)
        renderer.render(painter)
        painter.end()
        pixmap.setDevicePixelRatio(scale)
        result.addPixmap(pixmap)
    return result


def stylesheet(theme):
    p = PALETTES[theme]
    return f"""
    QWidget {{ color:{p['text']}; font-size:14px; }}
    QMainWindow, QWidget#main {{ background:{p['bg']}; }}
    QFrame#sidebar {{ background:{p['side']}; border-right:1px solid {p['border']}; }}
    QFrame#card, QFrame#runCard {{ background:{p['card']}; border:1px solid {p['border']}; border-radius:14px; }}
    QFrame#settingRow {{ background:transparent; border:0; }}
    QFrame#settingRow[separator="true"] {{ border-bottom:1px solid {p['border']}; }}
    QLabel#rowTitle {{ font-size:14px; font-weight:500; }}
    QLabel#rowDescription {{ font-size:12px; color:{p['muted']}; }}
    QWidget#toolbar {{ background:transparent; }}
    QFrame#notice {{ background:{p['selected']}; border:1px solid {p['border']}; border-radius:7px; }}
    QLabel#feedback {{ background:{p['selected']}; border-top:1px solid {p['border']}; border-bottom:1px solid {p['border']}; }}
    QLabel {{ background:transparent; border:0; }}
    QLabel#muted {{ color:{p['muted']}; }}
    QLabel#caption {{ color:{p['muted']}; font-size:12px; }}
    QLabel#title {{ font-size:25px; font-weight:600; }}
    QLabel#section {{ font-size:15px; font-weight:600; }}
    QLabel#stat {{ font-size:23px; font-weight:600; }}
    QLabel#brand {{ font-size:17px; font-weight:600; }}
    QPushButton, QToolButton {{ background:{p['card']}; border:1px solid {p['border']}; border-radius:8px; padding:6px 11px; min-height:20px; }}
    QPushButton:hover, QToolButton:hover {{ background:{p['hover']}; }}
    QPushButton:pressed, QToolButton:pressed {{ background:{p['pressed']}; }}
    QPushButton:disabled, QToolButton:disabled {{ color:{p['muted']}; background:{p['field']}; border-color:{p['border']}; }}
    QPushButton:focus, QToolButton:focus {{ border:1px solid {p['accent']}; }}
    QPushButton[primary="true"] {{ background:{p['accent']}; color:{p['accent_text']}; border-color:{p['accent']}; font-weight:600; }}
    QPushButton[primary="true"]:hover {{ background:{p['accent']}; border:1px solid {p['text']}; }}
    QPushButton[primary="true"]:pressed {{ background:{p['selected']}; color:{p['text']}; }}
    QPushButton[primary="true"]:disabled {{ background:{p['hover']}; color:{p['muted']}; border-color:{p['border']}; }}
    QPushButton[primary="true"]:focus {{ border-color:{p['text']}; }}
    QPushButton[nav="true"] {{ background:transparent; border:1px solid transparent; text-align:left; padding:8px 11px; }}
    QPushButton[nav="true"]:checked {{ background:{p['selected']}; color:{p['text']}; }}
    QPushButton[nav="true"]:focus {{ border-color:{p['accent']}; }}
    QLineEdit, QComboBox, QTimeEdit, QDateEdit, QSpinBox, QPlainTextEdit {{ background:{p['field']}; border:1px solid {p['border']}; border-radius:8px; padding:6px 9px; selection-background-color:{p['selected']}; selection-color:{p['text']}; }}
    QLineEdit:focus, QComboBox:focus, QTimeEdit:focus, QDateEdit:focus, QSpinBox:focus, QPlainTextEdit:focus {{ border-color:{p['accent']}; }}
    QLineEdit:disabled, QComboBox:disabled, QTimeEdit:disabled, QDateEdit:disabled, QSpinBox:disabled {{ color:{p['muted']}; background:{p['bg']}; }}
    QComboBox {{ min-height:20px; padding-right:26px; }}
    QComboBox::drop-down {{ border:0; width:22px; }}
    QComboBox::down-arrow, QDateEdit::down-arrow {{ image:url(:/icons/chevron-down.svg); width:12px; height:12px; }}
    QTimeEdit::up-arrow, QSpinBox::up-arrow {{ image:url(:/icons/chevron-up.svg); width:10px; height:10px; }}
    QTimeEdit::down-arrow, QSpinBox::down-arrow {{ image:url(:/icons/chevron-down.svg); width:10px; height:10px; }}
    QTimeEdit::up-button {{ subcontrol-origin:border; subcontrol-position:top right; width:22px; border:0; border-left:1px solid {p['border']}; }}
    QTimeEdit::down-button {{ subcontrol-origin:border; subcontrol-position:bottom right; width:22px; border:0; border-left:1px solid {p['border']}; }}
    QSpinBox::up-button {{ subcontrol-origin:border; subcontrol-position:top right; width:22px; border:0; border-left:1px solid {p['border']}; }}
    QSpinBox::down-button {{ subcontrol-origin:border; subcontrol-position:bottom right; width:22px; border:0; border-left:1px solid {p['border']}; }}
    QTimeEdit::up-button:hover, QTimeEdit::down-button:hover {{ background:{p['hover']}; }}
    QTimeEdit::up-button:pressed, QTimeEdit::down-button:pressed {{ background:{p['pressed']}; }}
    QComboBox QAbstractItemView {{ background:{p['card']}; color:{p['text']}; selection-background-color:{p['selected']}; border:1px solid {p['border']}; padding:4px; }}
    QCheckBox {{ spacing:8px; background:transparent; padding:4px 0; }}
    QCheckBox::indicator {{ width:16px; height:16px; border:1px solid {p['muted']}; border-radius:4px; background:{p['field']}; }}
    QCheckBox::indicator:checked {{ background:{p['accent']}; border-color:{p['accent']}; }}
    QCheckBox::indicator:hover, QCheckBox::indicator:focus {{ border-color:{p['text']}; }}
    QCheckBox:disabled {{ color:{p['muted']}; }}
    QTableWidget {{ background:{p['card']}; alternate-background-color:{p['card']}; border:0; gridline-color:{p['border']}; selection-background-color:{p['selected']}; selection-color:{p['text']}; outline:0; }}
    QTableWidget::item {{ padding:7px 9px; border-bottom:1px solid {p['border']}; }}
    QTableWidget::item:hover {{ background:{p['hover']}; }}
    QTableWidget::item:focus {{ border:1px solid {p['accent']}; }}
    QHeaderView::section {{ background:{p['card']}; color:{p['muted']}; padding:10px 9px; border:0; border-bottom:1px solid {p['border']}; font-size:12px; }}
    QTableCornerButton::section {{ background:{p['card']}; border:0; }}
    QScrollArea {{ background:transparent; border:0; }}
    QScrollArea > QWidget > QWidget {{ background:{p['bg']}; }}
    QScrollBar:vertical {{ background:transparent; width:10px; margin:2px; }}
    QScrollBar:horizontal {{ background:transparent; height:10px; margin:2px; }}
    QScrollBar::handle {{ background:{p['border']}; border-radius:4px; min-height:28px; min-width:28px; }}
    QScrollBar::handle:hover {{ background:{p['muted']}; }}
    QScrollBar::add-line, QScrollBar::sub-line {{ width:0; height:0; }}
    QScrollBar::add-page, QScrollBar::sub-page {{ background:transparent; }}
    QProgressBar {{ background:{p['field']}; border:0; border-radius:3px; height:5px; max-height:5px; }}
    QProgressBar::chunk {{ background:{p['accent']}; border-radius:3px; }}
    QWidget#footer, QWidget#logPanel {{ border-top:1px solid {p['border']}; background:{p['bg']}; }}
    QSplitter::handle {{ background:{p['border']}; height:1px; }}
    QDialog, QMessageBox, QMenu {{ background:{p['card']}; }}
    QMenu {{ border:1px solid {p['border']}; border-radius:8px; padding:5px; }}
    QMenu::separator {{ height:1px; background:{p['border']}; margin:5px; }}
    QMenu::item {{ padding:8px 24px; border-radius:4px; }}
    QMenu::item:selected {{ background:{p['selected']}; }}
    QToolTip {{ background:{p['card']}; color:{p['text']}; border:1px solid {p['border']}; padding:5px; }}
    QPlainTextEdit#dialogText {{ font-size:13px; }}
    """


def set_palette(application, theme):
    application.setProperty("pinganTheme", theme)
    p = PALETTES[theme]
    palette = QPalette()
    for role, color in (
        (QPalette.ColorRole.Window, p["bg"]),
        (QPalette.ColorRole.WindowText, p["text"]),
        (QPalette.ColorRole.Base, p["field"]),
        (QPalette.ColorRole.Text, p["text"]),
        (QPalette.ColorRole.Button, p["card"]),
        (QPalette.ColorRole.ButtonText, p["text"]),
        (QPalette.ColorRole.Highlight, p["selected"]),
        (QPalette.ColorRole.HighlightedText, p["text"]),
        (QPalette.ColorRole.PlaceholderText, p["muted"]),
    ):
        palette.setColor(role, QColor(color))
    application.setPalette(palette)


class Switch(QCheckBox):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedSize(42, 26)
        self.setAccessibleName("启用账号")
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setAttribute(Qt.WidgetAttribute.WA_Hover)

    def hitButton(self, position):
        return self.rect().contains(position)

    def paintEvent(self, event):
        p = PALETTES[
            self.property("theme")
            or QApplication.instance().property("pinganTheme")
            or "dark"
        ]
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(
            QColor(
                p["accent"] if self.isChecked() and self.isEnabled() else p["border"]
            )
        )
        painter.drawRoundedRect(QRectF(2, 4, 36, 18), 9, 9)
        if self.underMouse() and self.isEnabled():
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.setPen(QPen(QColor(p["text"] if self.isDown() else p["muted"]), 1))
            painter.drawRoundedRect(QRectF(2, 4, 36, 18), 9, 9)
            painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(p["accent_text"] if self.isChecked() else p["muted"]))
        painter.drawEllipse(QRectF(23 if self.isChecked() else 5, 7, 12, 12))
        if self.hasFocus():
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.setPen(QPen(QColor(p["accent"]), 1))
            painter.drawRoundedRect(QRectF(0.5, 1, 40, 24), 7, 7)
        painter.end()


class CheckBox(QCheckBox):
    """A readable vector checkmark in both themes, retaining Qt keyboard semantics."""

    def __init__(self, text="", parent=None):
        super().__init__(text, parent)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setAttribute(Qt.WidgetAttribute.WA_Hover)

    def paintEvent(self, event):
        p = PALETTES[
            self.property("theme")
            or QApplication.instance().property("pinganTheme")
            or "dark"
        ]
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        y = (self.height() - 16) / 2
        painter.setPen(
            QPen(
                QColor(
                    p["accent"]
                    if self.hasFocus()
                    else p["text"] if self.underMouse() else p["muted"]
                ),
                1,
            )
        )
        painter.setBrush(
            QColor(
                p["pressed"]
                if self.isDown()
                else p["accent"] if self.isChecked() else p["field"]
            )
        )
        painter.drawRoundedRect(QRectF(1, y, 16, 16), 4, 4)
        if self.isChecked():
            painter.setPen(
                QPen(
                    QColor(p["accent_text"]),
                    1.8,
                    Qt.PenStyle.SolidLine,
                    Qt.PenCapStyle.RoundCap,
                )
            )
            painter.drawLine(QPointF(5, y + 8), QPointF(8, y + 11))
            painter.drawLine(QPointF(8, y + 11), QPointF(14, y + 5))
        painter.setPen(QColor(p["text"] if self.isEnabled() else p["muted"]))
        painter.drawText(
            QRectF(26, 0, max(0, self.width() - 26), self.height()),
            Qt.AlignmentFlag.AlignVCenter,
            self.text(),
        )
        painter.end()
