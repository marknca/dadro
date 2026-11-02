"""One axis readout with local direction, coordinate mode, zero, and display precision."""
import math

from PyQt6.QtCore import QEvent, QRectF, Qt, pyqtSignal
from PyQt6.QtGui import QColor, QFont, QFontMetricsF, QPainter, QPen
from PyQt6.QtWidgets import QFrame, QGridLayout, QHBoxLayout, QLabel, QPushButton, QSizePolicy


def fit_readout_font(label, base_font, *, expand=False):
    """Fit the available bounds; live readouts can grow beyond the base font size."""
    font = QFont(base_font)
    metrics = QFontMetricsF(font)
    bounds = label.contentsRect()
    scale = min(max(1, bounds.width() - 4) / max(1, metrics.horizontalAdvance(label.text())),
                max(1, bounds.height() - 4) / metrics.height())
    if not expand:
        scale = min(1.0, scale)
    font.setPointSizeF(max(1.0, font.pointSizeF() * scale))
    # Font hinting is not linear at small sizes; verify the actual fitted metrics.
    while font.pointSizeF() > 1:
        fitted = QFontMetricsF(font)
        if fitted.horizontalAdvance(label.text()) <= bounds.width() - 4 and fitted.height() <= bounds.height() - 4:
            break
        font.setPointSizeF(max(1.0, font.pointSizeF() - .5))
    if label.font() != font:
        label.setFont(font)


class AxisToggle(QPushButton):
    """A native checkable button with both values inside a segmented control."""
    def __init__(self, off, on, parent):
        super().__init__(off, parent)
        self.values = (off, on)
        self._pointer_choice = None
        self.setCheckable(True)
        self.toggled.connect(lambda checked: self.setText(self.values[checked]))

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton and self.isDown() and self.rect().contains(event.position().toPoint()):
            self._pointer_choice = event.position().x() >= self.width() / 2
        try:
            super().mouseReleaseEvent(event)
        finally:
            self._pointer_choice = None

    def nextCheckState(self):
        self.setChecked(not self.isChecked() if self._pointer_choice is None else self._pointer_choice)

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        track = QRectF(self.rect()).adjusted(1, 1, -1, -1)
        painter.setPen(QPen(QColor('#24566e' if self.hasFocus() else '#708491'), 2 if self.hasFocus() else 1))
        painter.setBrush(QColor('#e1e7ec' if not self.isEnabled() else '#dce9f1' if self.underMouse() else '#e8eef2'))
        painter.drawRoundedRect(track, 7, 7)
        font = QFont(self.font())
        font.setPixelSize(round(min(24, max(12, self.height() * .28)) if len(self.values[0]) > 1
                                else min(32, max(18, self.height() * .42))))
        font.setWeight(QFont.Weight.ExtraBold)
        painter.setFont(font)
        for index, value in enumerate(self.values):
            segment = QRectF(track.left()+3+index*(track.width()-6)/2, track.top()+3,
                             (track.width()-6)/2, track.height()-6)
            selected = index == self.isChecked()
            if selected:
                painter.setPen(Qt.PenStyle.NoPen)
                painter.setBrush(QColor('#174b63' if self.isEnabled() else '#ced8e0'))
                painter.drawRoundedRect(segment, 4, 4)
            painter.setPen(QColor('#4f606d' if not self.isEnabled() else '#ffffff' if selected else '#344b5c'))
            painter.drawText(segment, Qt.AlignmentFlag.AlignCenter, value)


class AxisWidget(QFrame):
    """Presentation only: the owner still computes coordinates and owns software datums."""
    precision_changed = pyqtSignal()

    def __init__(self, axis, parent=None):
        super().__init__(parent)
        self.setObjectName('axisReadout')
        self.setMinimumHeight(148)
        self.setMaximumWidth(1152)
        self.number_font = QFont(self.font())
        self.number_font.setPointSize(128)
        self.reading, self.inches = None, True
        self.decimals = {False: 3, True: 4}
        self.name = QLabel(axis, self)
        self.name.setObjectName('axis')
        self.value = QLabel('—', self)
        self.value.setFont(self.number_font)
        self.value.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        self.value.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Ignored)
        self.value.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.value.installEventFilter(self)
        self.point_number, self.distance = None, None
        self.point_origin = False
        self.point_distance = QFrame(self)
        self.point_distance.setObjectName('pointDistance')
        self.point_distance.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        distance_layout = QHBoxLayout(self.point_distance)
        distance_layout.setContentsMargins(8, 0, 8, 0)
        distance_layout.setSpacing(8)
        self.distance_title = QLabel()
        self.distance_title.setStyleSheet('font-size: 11px; font-weight: 400;')
        self.distance_value = QLabel()
        self.distance_value.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        self.distance_value.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Ignored)
        self.distance_font = QFont(self.number_font)
        self.distance_font.setPointSize(20)
        distance_layout.addWidget(self.distance_title)
        distance_layout.addWidget(self.distance_value)
        self.point_distance.hide()
        self.sign = AxisToggle('+', '-', self)
        self.mode = AxisToggle('ABS', 'REL', self)
        self.zero = QPushButton('0', self)
        self.zero.setEnabled(False)
        self.less_digits = QPushButton('.0\n←', self)
        self.more_digits = QPushButton('.00\n→', self)
        self.less_digits.setObjectName('lessDigits')
        self.more_digits.setObjectName('moreDigits')
        self.less_digits.clicked.connect(lambda: self._change_digits(-1))
        self.more_digits.clicked.connect(lambda: self._change_digits(1))
        self.sign.toggled.connect(self._update_controls)
        self.mode.toggled.connect(self._update_controls)
        row = QHBoxLayout(self)
        row.setContentsMargins(24, 4, 12, 4)
        row.setSpacing(0)
        row.addWidget(self.name)
        row.addSpacing(12)
        row.addWidget(self.value, 1)
        row.addSpacing(32)
        controls = QGridLayout()
        controls.setHorizontalSpacing(0)
        controls.setVerticalSpacing(4)
        for i, button in enumerate((self.sign, self.mode, self.zero, self.less_digits, self.more_digits)):
            button.setAutoDefault(False)
            button.setMinimumHeight(32)
            button.setFixedWidth(104 if i < 3 else 52)
            button.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Expanding)
            controls.addWidget(button, min(i, 3), 1 if i == 4 else 0, 1, 2 if i < 3 else 1)
        row.addLayout(controls)
        self.setStyleSheet('''
            QFrame#axisReadout { background: white; border: 1px solid #718692; border-radius: 12px; }
            QFrame#pointDistance { background: #10212f; border: 1px solid #344b5c; border-radius: 6px; }
            QLabel { color: #10212f; border: none; background: transparent; }
            QFrame#pointDistance QLabel { color: #ffffff; }
            QLabel#axis { color: #24566e; font-size: 28px; font-weight: 800; }
            QPushButton { background: #ffffff; color: #10212f; border: 1px solid #708491;
                          border-radius: 7px; padding: 0; font-size: 14px; }
            QPushButton:hover { background: #dce9f1; }
            QPushButton:focus { border-color: #24566e; }
            QPushButton:disabled { color: #4f606d; background: #e1e7ec; }
            QPushButton#lessDigits, QPushButton#moreDigits { font-size: 12px; font-weight: 800; }
            QPushButton#lessDigits { border-top-right-radius: 0; border-bottom-right-radius: 0; }
            QPushButton#moreDigits { border-top-left-radius: 0; border-bottom-left-radius: 0; border-left: none; }
        ''')
        self.set_axis_name(axis)
        self._render()

    def set_axis_name(self, axis):
        self.name.setText(axis)
        self.value.setAccessibleName(f'{axis} coordinate')
        self._update_controls()

    def _update_controls(self):
        axis = self.name.text()
        direction = '-' if self.sign.isChecked() else '+'
        mode = 'relative' if self.mode.isChecked() else 'absolute'
        self.sign.setAccessibleName(f'{axis} direction: {direction}')
        self.sign.setToolTip(f'Select + or − to change {axis} direction; Space toggles')
        self.mode.setAccessibleName(f'{axis} coordinates: {mode}')
        self.mode.setToolTip(f'Select ABS or REL for {axis}; retain both zeros; Space toggles')
        self.zero.setAccessibleName(f'Zero {axis} in {mode} mode')
        self.zero.setToolTip(f'Set the displayed {axis} position to zero in {mode} mode')
        for button, action in ((self.less_digits, 'Fewer'), (self.more_digits, 'More')):
            button.setAccessibleName(f'{axis}: {action.lower()} decimal places')
            button.setToolTip(f'{action} decimal places for {axis} (1–4); display only')

    def set_reading(self, value, inches):
        self.reading = value if value is not None and math.isfinite(value) else None
        self.inches = inches
        self._render()

    def _change_digits(self, step):
        self.decimals[self.inches] = max(1, min(4, self.decimals[self.inches] + step))
        self._render()
        self.precision_changed.emit()

    def set_distance(self, number, value, *, origin=False):
        """Overlay a signed distance, without replacing or changing the live reading."""
        value = value if value is not None and math.isfinite(value) else None
        if (number, value, origin) == (self.point_number, self.distance, self.point_origin):
            return
        self.point_number = number
        self.point_origin = origin
        self.distance = value
        self.point_distance.setVisible(number is not None)
        self.value.setContentsMargins(0, 0, 0, 40 if number is not None else 0)
        self._render_distance()
        self._position_distance()

    def _render_distance(self):
        if self.point_number is None:
            return
        precision, value = self.decimals[self.inches], self.distance
        if value is not None and round(value, precision) == 0:
            value = 0.0
        text = '—' if value is None else (f'{value:+.{precision}f}' if abs(value) < 1e7 else f'{value:+.{precision}e}')
        name = 'Origin' if self.point_origin else f'Point {self.point_number}'
        self.distance_title.setText(f'{name} · Δ')
        self.distance_value.setText(text)
        self.distance_value.setAccessibleName(f'{self.name.text()} distance from {name}')
        self.distance_value.setAccessibleDescription(f'{text} {"inches" if self.inches else "millimetres"}')

    def _position_distance(self):
        fit_readout_font(self.value, self.number_font, expand=True)
        if self.point_number is not None:
            title_width = self.distance_title.sizeHint().width()
            width = QFontMetricsF(self.distance_font).horizontalAdvance(self.distance_value.text()) + 4
            width = max(1, min(math.ceil(width), self.value.width() - title_width - 26))
            self.distance_value.setFixedWidth(width)
            badge_width = title_width + width + 26  # Label gap, padding, and the 1-pixel outline.
            metrics = QFontMetricsF(self.value.font())
            line = metrics.boundingRect(QRectF(self.value.contentsRect()), int(self.value.alignment()), self.value.text())
            # QLabel's centered baseline excludes half the font's inter-line leading.
            ink_bottom = (line.top() + metrics.ascent() - metrics.leading() / 2
                          + metrics.tightBoundingRect(self.value.text()).bottom())
            top = min(math.ceil(self.value.y() + ink_bottom + 10), self.value.geometry().bottom() - 31)
            self.point_distance.setGeometry(self.value.geometry().right() - badge_width + 1, top, badge_width, 32)
            self.point_distance.raise_()
            self.point_distance.layout().activate()
            fit_readout_font(self.distance_value, self.distance_font)

    def _render(self):
        precision, value = self.decimals[self.inches], self.reading
        if value is None:
            self.value.setText('—')
        else:
            if round(value, precision) == 0:
                value = 0.0
            self.value.setText(f'{value: .{precision}f}' if abs(value) < 1e7 else f'{value: .{precision}e}')
        self.less_digits.setEnabled(precision > 1)
        self.more_digits.setEnabled(precision < 4)
        self._render_distance()
        self._position_distance()

    def eventFilter(self, watched, event):
        if watched is self.value and event.type() == QEvent.Type.Resize:
            self._position_distance()
        return super().eventFilter(watched, event)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        width = round(max(104, min(176, self.width() * .18, self.height() * .52)) / 2) * 2
        for button in (self.sign, self.mode, self.zero, self.less_digits, self.more_digits):
            button.setFixedWidth(width // 2 if button in (self.less_digits, self.more_digits) else width)
        size = round(max(12, min(24, (self.height() - 20) / 4 * .28)))
        for button in (self.zero, self.less_digits, self.more_digits):
            button.setStyleSheet(f'font-size: {size}px;')
