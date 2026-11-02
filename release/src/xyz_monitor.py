#!/usr/bin/env python3
r"""A responsive Python 3 / PyQt6 monitor for a YADRO DRO:int4 controller.

Install on Raspberry Pi OS (Bookworm or newer), using system Python:
    sudo apt update
    sudo apt install python3-pyqt6 python3-serial qt6-wayland
    /usr/bin/python3 src/xyz_monitor.py
    See docs/raspberry-pi.html for Qt startup troubleshooting.
Install in a virtual environment on other platforms:
    python3 -m pip install PyQt6 pyserial
Run without hardware:
    python3 xyz_monitor.py --demo
    Or choose Try XYZ demo in machine selection for simulated travel between 0 and 30 mm.
Run with hardware:
    python3 xyz_monitor.py --port /dev/ttyUSB0
    python3 xyz_monitor.py --port COM3
Run with the separate manual simulator:
    python3 src/machine_simulator.py
    python3 src/app.py --port socket://127.0.0.1:8765
    See docs/machine-simulator.html for mill/lathe setup and channel mapping.
List available ports:
    python3 -m serial.tools.list_ports -v

Protocol: DRO:int4, 19200 baud, 8N1, no flow control. First launch configures the
machine profile and maps two to four inputs to channels 0–3 before opening the readout.
Confirmed settings persist across launches; the top-right Settings cog edits them.
Values stay in mm and display in inches by default; use Units for mm. Connecting enters passive
mode, checks the version, detects the configured channels, polls each supported
scale, then enables active updates. See docs/machine-setup.html for profiles.
Fast mode is indicated by measured cadence, with an enable prompt for slow readings;
see docs/yadro-protocol.html#fast-mode for sequencing and datum precautions.
See docs/yadro-protocol.html for the supported measurement packing convention.

The worker owns the port and parses all complete records it receives. A tiny,
locked mailbox retains only the most recent valid sample. The main-thread
timer redraws at about 30 Hz; there is no unbounded queue of display updates.
This is a live monitor, not a recording system: intermediate samples are
intentionally superseded in the mailbox. Software zero controls affect the display
only; no commands are transmitted to the device. Each axis has direction,
software zero, and absolute/relative controls. See docs/axis-controls.html.
Lay out holes (F3) implements MILL3 HCXY stepping; see docs/hole-circle.html.
Remember point saves positions and overlays live distances; see docs/remember-point.html.

Qt threading: https://doc.qt.io/qt-6/threads-qobject.html
Qt QThread: https://doc.qt.io/qt-6/qthread.html
pySerial: https://pyserial.readthedocs.io/en/latest/pyserial_api.html
"""

from __future__ import annotations

import argparse
import json
import math
import sys
import threading
import time
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Tuple

import serial
from serial.tools import list_ports
from PyQt6.QtCore import QEasingCurve, QEvent, QPointF, QPropertyAnimation, QRectF, QSettings, QSignalBlocker, QThread, QTimer, Qt, QVariantAnimation, pyqtSignal, pyqtSlot
from PyQt6.QtGui import QColor, QCloseEvent, QFont, QFontDatabase, QIcon, QKeySequence, QPainter, QPen, QShortcut
from PyQt6.QtWidgets import (
    QApplication,
    QButtonGroup,
    QCheckBox,
    QComboBox,
    QDialog,
    QDoubleSpinBox,
    QFormLayout,
    QFrame,
    QGraphicsOpacityEffect,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QSpinBox,
    QStackedWidget,
    QStatusBar,
    QStyle,
    QStyleOption,
    QVBoxLayout,
    QWidget,
)
from yadro_protocol import SERIAL_SETTINGS, ResponseParser, decode_reading_mm, encode_command
from machine_setup import PROFILES, ProfileCard, coordinates, validate_setup, validate_toolhead
from axis_widget import AxisToggle, AxisWidget, fit_readout_font
from remember_plot import RememberPointPlot
from keyboard_navigation import KeyboardNavigation


XYZ = Tuple[Optional[float], Optional[float], Optional[float]]
READ_TIMEOUT = 0.05       # Seconds; lets an idle read notice a stop request.
WRITE_TIMEOUT = 0.5
COMMAND_TIMEOUT = 3.0
DETECT_TIMEOUT = 5.0      # Detection can take two seconds per channel, or longer.
BURST_TIMEOUT = 5.0      # Ten samples take ~3.3 seconds in normal mode.
FAST_BURST_MAX = 0.75    # Fast: ~0.2 s plus serial/USB overhead, one channel at a time.
SLOW_BURST_MIN = 2.0     # Ambiguous timing must never authorize state-changing pulses.
READ_CHUNK_BYTES = 4096   # Bound the work done in one read/parse iteration.
UI_INTERVAL_MS = 33      # About 30 redraws/second, independent of input rate.


def configure_fonts():
    """Register bundled faces once, before constructing any interface widgets."""
    app = QApplication.instance()
    if app.property('dadroFontsLoaded'):
        return
    for style in ('extrabold', 'regular'):
        path = Path(__file__).resolve().parents[1] / 'assets' / f'noto-sans-mono-condensed-{style}-custom-zero.ttf'
        if QFontDatabase.addApplicationFont(str(path)) == -1:
            print(f'Could not load interface font: {path}; Qt will use an available fallback.', file=sys.stderr)
    font = app.font()
    font.setFamily('Noto Sans Mono Condensed')
    font.setWeight(QFont.Weight.ExtraBold)
    app.setFont(font)
    app.setProperty('dadroFontsLoaded', True)


def simulated_xyz(elapsed: float) -> XYZ:
    """Travel 0–30 mm and back at X=1, Y=0.6, Z=0.25 mm/second."""
    # ponytail: ideal constant feeds; elapsed time avoids accumulating timer drift.
    return tuple(30 - abs((elapsed * rate) % 60 - 30) for rate in (1.0, 0.6, 0.25))


def hole_circle_target(center_x: float, center_y: float, diameter: float,
                       holes: int, angle: float, index: int) -> tuple[float, float]:
    """MILL3 HCXY target in mm: 0° is -Y, 90° is -X. Index starts at zero."""
    if not all(math.isfinite(value) for value in (center_x, center_y, diameter, angle)):
        raise ValueError("Circle dimensions and angle must be finite")
    if diameter <= 0 or max(abs(center_x), abs(center_y), diameter) > 1e7:
        raise ValueError("Use a positive diameter; dimensions must be within 10,000,000 mm")
    if type(holes) is not int or not 1 <= holes <= 10000:
        raise ValueError("Number of holes must be a whole number from 1 to 10,000")
    if type(index) is not int or not 0 <= index < holes:
        raise ValueError("Hole index is outside the circle")
    radians = math.radians(math.remainder(angle, 360) + index * 360 / holes)
    # MILL3 adds these terms to its offsets, so zero is reached at center minus them.
    return center_x - math.sin(radians) * diameter / 2, center_y - math.cos(radians) * diameter / 2


class AlignedStatusBar(QStatusBar):
    def paintEvent(self, event):
        # QStatusBar paints messages at a fixed inset, ignoring stylesheet padding.
        painter = QPainter(self)
        option = QStyleOption(); option.initFrom(self)
        self.style().drawPrimitive(QStyle.PrimitiveElement.PE_PanelStatusBar, option, painter, self)
        rect = self.rect().adjusted(16, 0, -16, 0)
        for widget in self.findChildren(QWidget, options=Qt.FindChildOption.FindDirectChildrenOnly):
            if widget.isVisible():
                rect.setRight(min(rect.right(), widget.x() - 8))
        painter.setPen(self.palette().windowText().color())
        painter.drawText(rect, Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, self.currentMessage())


class CompactDoubleSpinBox(QDoubleSpinBox):
    def textFromValue(self, value: float) -> str:
        # Keep input precision without filling the large fields with trailing zeros.
        return super().textFromValue(value).rstrip(self.locale().zeroDigit()).removesuffix(self.locale().decimalPoint())


class ToolheadWidthSpinBox(QDoubleSpinBox):
    """Keep at least three decimal places, with extra precision for converted widths."""
    def __init__(self):
        super().__init__()
        self.offset = QLabel(self.lineEdit())
        self.offset.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self.offset.setStyleSheet('background: #10212f; color: white; border: 1px solid #344b5c; '
                                 'border-radius: 6px; padding: 4px 6px; font-size: 11px; font-weight: 400;')
        self.offset.setAccessibleName('Half-width offset')
        self.setToolTip('The offset preview is half the toolhead width.')
        self.lineEdit().installEventFilter(self)

    def set_offset(self, text, unit):
        self.offset.setText(f'½  {text} {unit}')
        self.offset.setAccessibleDescription(f'{text} {unit}, half the toolhead width')
        self._position_offset()

    def _position_offset(self):
        self.offset.adjustSize()
        edit = self.lineEdit()
        self.offset.move(edit.width() - self.offset.width() - 4, (edit.height() - self.offset.height()) // 2)
        edit.setTextMargins(0, 0, self.offset.width() + 12, 0)
        self.offset.raise_()

    def eventFilter(self, watched, event):
        if watched is self.lineEdit() and event.type() == QEvent.Type.Resize and hasattr(self, 'offset'):
            self._position_offset()
        return super().eventFilter(watched, event)

    def textFromValue(self, value):
        text = super().textFromValue(value)
        decimal, zero = self.locale().decimalPoint(), self.locale().zeroDigit()
        if decimal in text:
            whole, fraction = text.rsplit(decimal, 1)
            return whole + decimal + fraction.rstrip(zero).ljust(3, zero)
        return text


class HoleCirclePlot(QWidget):
    """Read-only, center-fitted XY pattern using the same MILL3 geometry as guidance."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.params = None
        self.current_index = 0
        self.visited = set()
        self.points = {}
        self.setMinimumHeight(160)
        self.setAccessibleName("XY hole-circle diagram")

    def set_circle(self, params, index=0, visited=()) -> None:
        self.params = dict(params) if params is not None else None
        self.current_index = index
        self.visited = set(visited)
        self.points = {}
        if params is not None:
            count = params["holes"]
            # ponytail: at most 361 markers; add zoom before depicting every dense-pattern hole.
            indices = set(range(0, count, math.ceil(count / 360))) | {index}
            self.points = {i: hole_circle_target(0, 0, 2, count, params["angle"], i) for i in indices}
        self.update()

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        center = QPointF(self.width() / 2, self.height() / 2)
        radius = max(1.0, min(self.width(), self.height()) / 2 - 36)
        painter.setPen(QPen(QColor("#708491"), 1))
        painter.drawLine(QPointF(12, center.y()), QPointF(self.width() - 12, center.y()))
        painter.drawLine(QPointF(center.x(), 12), QPointF(center.x(), self.height() - 12))
        painter.setPen(QColor("#344b5c"))
        painter.drawText(QRectF(self.width() - 28, center.y() + 4, 28, 20), Qt.AlignmentFlag.AlignCenter, "+X")
        painter.drawText(QRectF(center.x() + 5, 0, 28, 20), Qt.AlignmentFlag.AlignCenter, "+Y")
        if self.params is None:
            painter.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter, "Enter a valid circle to preview")
            return
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.setPen(QPen(QColor("#718692"), 1.5))
        painter.drawEllipse(center, radius, radius)
        first = self.points[0]
        painter.setPen(QPen(QColor("#718692"), 1, Qt.PenStyle.DashLine))
        painter.drawLine(center, QPointF(center.x() + first[0] * radius, center.y() - first[1] * radius))
        painter.setPen(QPen(QColor("#344b5c"), 2))
        painter.drawLine(center + QPointF(-4, 0), center + QPointF(4, 0))
        painter.drawLine(center + QPointF(0, -4), center + QPointF(0, 4))
        dense = self.params["holes"] > 80
        for index in sorted(self.points, key=lambda i: i == self.current_index):
            x, y = self.points[index]
            point = QPointF(center.x() + x * radius, center.y() - y * radius)
            current = index == self.current_index
            color = "#174b63" if current else "#206356" if index in self.visited else "#556e7e"
            painter.setPen(QPen(QColor(color), 2 if current else 1.5))
            painter.setBrush(QColor(color if current or index in self.visited else "white"))
            dot_radius = 6 if current else 2 if dense else 4
            painter.drawEllipse(point, dot_radius, dot_radius)
            if current:
                painter.setBrush(Qt.BrushStyle.NoBrush)
                painter.drawEllipse(point, 9, 9)
            if self.params["holes"] <= 24 or current:
                label = QPointF(center.x() + x * (radius + 21), center.y() - y * (radius + 21))
                painter.drawText(QRectF(label.x() - 28, label.y() - 10, 56, 20),
                                 Qt.AlignmentFlag.AlignCenter, str(index + 1))


class ReadingCadence:
    """Per-input delivery pace; silence in YADRO active mode is not evidence of slowness."""

    def __init__(self, channels, baud):
        self.samples = {channel: deque(maxlen=256) for channel in channels}
        self.states = {channel: ('unknown', 0.) for channel in channels}
        # ponytail: a conservative L-record budget; calibrate per-scale thresholds if hardware rates differ.
        self.fast_threshold = .55 * min(50., baud / (230 * max(1, len(self.samples))))

    def observe(self, counts, now):
        for channel, count in counts.items():
            samples = self.samples[channel]
            if samples and now - samples[-1][0] > 2.:
                samples.clear()
            samples.append((now, count))
            while samples and now - samples[0][0] > 5.:
                samples.popleft()
            if len(samples) >= 4 and now - samples[0][0] >= 2.:
                rate = sum(n for _, n in list(samples)[1:]) / (now - samples[0][0])
                self.states[channel] = ('fast' if rate >= self.fast_threshold else 'slow', rate)

    def snapshot(self):
        return tuple((channel, state, rate) for channel, (state, rate) in self.states.items())


@dataclass(frozen=True)
class Snapshot:
    values: Optional[tuple[Optional[float], ...]] = None
    received_at: float = 0.0
    valid: int = 0
    invalid: int = 0
    warning: str = ""
    cadence: tuple = ()


class LatestReading:
    """One immutable snapshot shared safely by the worker and GUI."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._snapshot = Snapshot()

    def publish(self, values: Optional[tuple[Optional[float], ...]], valid: int, invalid: int,
                warning: str = "", cadence=None) -> None:
        # Parsing, serial I/O, and widget updates never happen under this lock.
        received_at = time.monotonic()
        with self._lock:
            previous = self._snapshot
            self._snapshot = Snapshot(
                values if values is not None else previous.values,
                received_at if values is not None else previous.received_at,
                valid,
                invalid,
                warning,
                previous.cadence if cadence is None else cadence,
            )

    def snapshot(self) -> Snapshot:
        with self._lock:
            return self._snapshot


class SerialWorker(QThread):
    """A blocking I/O loop; intentionally does not run a Qt event loop."""

    connected = pyqtSignal()
    failed = pyqtSignal(str)
    progress = pyqtSignal(str)
    fast_mode_changing = pyqtSignal()
    fast_mode_finished = pyqtSignal()

    def __init__(self, port: str, baud: int, latest: LatestReading,
                 demo: bool = False, parent=None, channels=(0, 1, 2), axes=('X', 'Y', 'Z')) -> None:
        super().__init__(parent)
        self.port = port
        self.baud = baud
        self.latest = latest
        self.demo = demo
        self._parser = ResponseParser()
        if len(channels) != len(axes) or len(set(channels)) != len(channels) or any(type(c) is not int or not 0 <= c <= 3 for c in channels):
            raise ValueError('Invalid scale mapping')
        self.channels, self.axes = tuple(channels), tuple(axes)
        self._values = [None] * len(channels)
        self._types = {}
        self._valid = self._invalid = 0
        self._warning = ""
        self._identified = False
        self._streaming = False
        self._cadence = ReadingCadence((), baud)
        self.fast_requested = threading.Event()

    def run(self) -> None:
        try:
            if self.demo:
                self._run_demo()
                return

            if not all(callable(getattr(serial, name, None)) for name in ('Serial', 'serial_for_url')):
                raise RuntimeError('pyserial installation is incomplete. Reinstall pyserial and restart Dadro. '
                                   f'Command: "{sys.executable}" -m pip install --force-reinstall pyserial')

            # Open, read, and close exclusively in the worker thread.
            options = {**SERIAL_SETTINGS, "baudrate": self.baud,
                       "timeout": READ_TIMEOUT, "write_timeout": WRITE_TIMEOUT}
            connection = (serial.serial_for_url(self.port, **options)
                          if self.port.startswith('socket://') else serial.Serial(port=self.port, **options))
            with connection:
                if self.isInterruptionRequested():
                    return
                connection.reset_input_buffer()
                self.progress.emit("Entering passive mode…")
                self._command(connection, "p", lambda reply: reply.kind == "ack")
                self.progress.emit("Checking controller version…")
                self._command(connection, "v", lambda reply: reply.kind == "version")
                self._identified = True
                for channel, axis in zip(self.channels, self.axes):
                    self.progress.emit(f"Detecting {axis} (channel {channel})…")
                    self._detect(connection, channel)
                if not self._types:
                    raise ValueError("No supported scales detected on the configured channels")
                self._cadence = ReadingCadence(self._types, self.baud)
                for channel in self._types:
                    self.progress.emit(f"Reading {self.axes[self.channels.index(channel)]}…")
                    self._command(connection, f"r{channel}",
                                  lambda reply: reply.kind == "reading" and reply.channel == channel)
                self.progress.emit("Starting live readings…")
                self._command(connection, "a", lambda reply: reply.kind == "ack")
                self._streaming = True
                self.connected.emit()
                fast_started = False
                while not self.isInterruptionRequested():
                    # ponytail: one explicit operation per connection; no command queue or retries.
                    if self.fast_requested.is_set() and not fast_started:
                        fast_started = True
                        self._enable_fast_mode(connection)
                    self._receive(connection)
        except Exception as exc:
            if not self.isInterruptionRequested():
                self.failed.emit(str(exc))

    def _enable_fast_mode(self, connection):
        self._streaming = False
        self.progress.emit("Fast mode: stopping live readings…")
        self._command(connection, "p", lambda reply: reply.kind == "ack")
        # Check ALL inputs before changing any: a prompt can reflect USB lag or mixed modes.
        measured = {}
        for channel in self._types:
            measured[channel] = self._probe_cadence(connection, channel)
        changing = False
        for channel, (state, _) in measured.items():
            if state == 'fast':
                continue
            if not changing:
                self.fast_mode_changing.emit()
                changing = True
            for line, name in (("D", "data"), ("C", "clock")):
                self.progress.emit(f"Fast mode: {self.axes[self.channels.index(channel)]} {name} pulse…")
                self._command(connection, f"c{channel}{line}", lambda reply: reply.kind == "ack")
            measured[channel] = self._probe_cadence(connection, channel)
            if measured[channel][0] != 'fast':
                raise ValueError(f"Channel {channel} did not reach fast cadence; setup stopped")
        self._values = [None] * len(self.channels)
        for channel in self._types:
            self.progress.emit(f"Fast mode: refreshing {self.axes[self.channels.index(channel)]}…")
            self._command(connection, f"r{channel}",
                          lambda reply: reply.kind == "reading" and reply.channel == channel)
        self.progress.emit("Fast mode: resuming live readings…")
        self._command(connection, "a", lambda reply: reply.kind == "ack")
        self._cadence = ReadingCadence(self._types, self.baud)
        self._cadence.states.update(measured)
        self.latest.publish(None, self._valid, self._invalid, self._warning, self._cadence.snapshot())
        self._streaming = True
        self.fast_mode_finished.emit()

    def _probe_cadence(self, connection, channel):
        self.progress.emit(f"Fast mode: checking {self.axes[self.channels.index(channel)]} — keep still…")
        arrivals = []
        def complete(reply):
            if reply.kind == 'reading' and reply.channel == channel:
                arrivals.append(self._received_at)
            return len(arrivals) == 10
        started, valid, invalid = time.monotonic(), self._valid, self._invalid
        self._command(connection, f"s{channel}", complete, BURST_TIMEOUT)
        elapsed = time.monotonic() - started
        if self._invalid == invalid and self._valid - valid == 10:
            if elapsed <= FAST_BURST_MAX:
                return 'fast', 10 / elapsed
            # A late USB batch is not proof of normal mode: require paced deliveries.
            spaced = sum(b - a >= SLOW_BURST_MIN / 20 for a, b in zip(arrivals, arrivals[1:]))
            if elapsed >= SLOW_BURST_MIN and spaced >= 6:
                return 'slow', 10 / elapsed
        raise ValueError(f"Channel {channel} timing inconclusive; no further pulses sent")

    def _receive(self, connection):
        chunk = connection.read(min(max(connection.in_waiting, 1), READ_CHUNK_BYTES))
        self._received_at = time.monotonic()
        counts = {}
        replies, changed = [], False
        for reply in self._parser.feed(chunk):
            if reply.kind == "welcome" and self._identified:
                raise ConnectionError("Controller restarted; reconnect to initialize the scales")
            try:
                if reply.kind == "invalid":
                    raise ValueError(reply.error)
                if reply.kind == "text":
                    raise ValueError(reply.text)
                if reply.kind == "reading":
                    if reply.channel not in self._types or reply.channel not in self.channels:
                        continue  # Unconfigured and unavailable channels never contribute coordinates.
                    if reply.scale_type != self._types[reply.channel]:
                        raise ValueError(f"Scale type changed on channel {reply.channel}; reconnect")
                    self._values[self.channels.index(reply.channel)] = decode_reading_mm(reply)
                    self._valid += 1
                    counts[reply.channel] = counts.get(reply.channel, 0) + 1
                    changed = True
                replies.append(reply)
            except ValueError as exc:
                self._invalid += 1
                self._warning = f"{exc}: {reply.text}"
        if chunk:
            if self._streaming:
                self._cadence.observe(counts, self._received_at)
            self.latest.publish(tuple(self._values) if changed else None,
                                self._valid, self._invalid, self._warning, self._cadence.snapshot())
        return replies

    def _command(self, connection, command, complete, timeout=None):
        if self.isInterruptionRequested():
            raise InterruptedError()
        wire = encode_command(command)
        if connection.write(wire) != len(wire):
            raise IOError(f"Incomplete serial write for {command}")
        deadline = time.monotonic() + (COMMAND_TIMEOUT if timeout is None else timeout)
        while not self.isInterruptionRequested():
            if time.monotonic() >= deadline:
                detail = f"; last input issue: {self._warning}" if self._warning else ""
                raise TimeoutError(f"Timed out waiting for {command}{detail}")
            # Consume the entire chunk before advancing: an ACK and a new
            # reading can arrive together, and both must reach the mailbox.
            matched = None
            for reply in self._receive(connection):
                if matched is None and complete(reply):
                    matched = reply
            if matched is not None:
                return matched
        raise InterruptedError()

    def _detect(self, connection, channel):
        started = clock_stuck = False

        def complete(reply):
            nonlocal started, clock_stuck
            if reply.kind == "detect":
                started = reply.channel == channel
            if not started:
                return False
            if reply.kind == "clock":
                clock_stuck = "stuck" in reply.text
            return reply.kind == "device" or (reply.kind == "data" and clock_stuck)

        reply = self._command(connection, f"d{channel}", complete, DETECT_TIMEOUT)
        if reply.scale_type is not None:
            self._types[channel] = reply.scale_type

    def _run_demo(self) -> None:
        self.connected.emit()
        started = time.monotonic()
        count = 0
        while not self.isInterruptionRequested():
            t = time.monotonic() - started
            count += 1
            self.latest.publish(
                simulated_xyz(t),
                count, 0,
            )
            self.msleep(10)  # ~100 input samples/s, still ~30 UI redraws/s.


class MainWindow(QMainWindow):
    def __init__(self, port: str, baud: int = SERIAL_SETTINGS["baudrate"], demo: bool = False, settings=None) -> None:
        configure_fonts()
        super().__init__()
        self.port, self.baud, self.demo = port, baud, demo
        self.machine_settings = settings if settings is not None else QSettings('DADRO', 'Machine')
        self.precision_error = ''
        self.toolhead_config = dict(width_mm=5.08, inches=True)
        try:
            toolhead = json.loads(self.machine_settings.value('toolhead', 'null'))
            validate_toolhead(toolhead)
            self.toolhead_config = toolhead
        except (ValueError, TypeError):
            pass  # Keep unreadable preferences untouched until the user saves a valid replacement.
        self.toolhead_draft = None
        self.profile = 'mill3'
        self.machine_config = self._default_setup('mill3')
        self.configuration_verified = True  # Direct monitor construction retains its XYZ API.
        self.after_stop = None
        self.machine_page = None
        self.setup_draft = None
        self.setup_error = ''
        self.startup_active = False
        self.datum_message = ''
        self.worker: Optional[SerialWorker] = None
        self.latest = LatestReading()
        self.offset: XYZ = (0.0, 0.0, 0.0)
        self.relative_offset: XYZ = (0.0, 0.0, 0.0)
        self.displayed: Optional[XYZ] = None
        self.last_drawn = -1
        self.stopping = False
        self.closing = False
        self.connection_ready = False
        self.error_text = ""
        self.connection_status = "Connecting…"
        self.fast_busy = self.fast_attempted = False
        self.fast_message = ""
        self.hole_dialog = None
        self.hole_circle = None
        self.hole_visualizer = None
        self.hole_visualizer_state = None
        self.hole_visualizer_progress_value = 0.0
        self.hole_message = "Enter the center in the current X/Y coordinate modes."
        self.remember_dialog = None
        self.remember_visualizer = None
        self.remember_visualizer_progress_value = 0.0
        self.remembered_points = []
        self.remembered_setup = dict(self.machine_config)
        self.remember_error = ''
        self.selected_point = None
        self._load_remembered_points()
        self.active_panel = None
        # ponytail: retain 3,600 accepted positions/gaps in memory; use a recorder for durable traces.
        self.tool_path_samples = deque(maxlen=3600)
        self.tool_path_tick = 0

        self.setWindowTitle('DADRO — Mill · Table + vertical')
        self.resize(1920, 1080)
        self.setMinimumSize(850, 600)
        self._build_ui()
        self._build_startup()
        self.keyboard = KeyboardNavigation(self)
        self.axis_cards[0].sign.setFocus()

        # This timer belongs to the GUI thread. All widget access stays here.
        self.refresh_timer = QTimer(self)
        self.refresh_timer.timeout.connect(self.refresh_display)
        self.refresh_timer.start(UI_INTERVAL_MS)
        self.tool_path_timer = QTimer(self)
        self.tool_path_timer.setTimerType(Qt.TimerType.PreciseTimer)
        self.tool_path_timer.timeout.connect(self.sample_tool_path)
        self.tool_path_timer.start(2000)

    def _build_ui(self) -> None:
        self.readout_page = QWidget()
        self.setCentralWidget(self.readout_page)
        page_layout = QVBoxLayout(self.readout_page)
        page_layout.setContentsMargins(0, 0, 0, 0)
        page_layout.setSpacing(0)
        self.fast_banner = QFrame()
        self.fast_banner.setStyleSheet('QFrame { background: #dce9f1; }')
        self.fast_banner.setFixedHeight(0)
        banner_layout = QHBoxLayout(self.fast_banner)
        banner_layout.setContentsMargins(16, 6, 16, 6)
        self.fast_status = QLabel()
        self.fast_status.setStyleSheet('font-weight: 400;')
        self.fast_status.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        banner_layout.addWidget(self.fast_status, 1)
        self.fast_apply_button = QPushButton('Enable Fast mode')
        self.fast_apply_button.setMinimumHeight(40)
        self.fast_apply_button.setEnabled(False)
        self.fast_apply_button.setToolTip('Check each scale, enable fast transfer where needed, then verify. '
                                          'Keep the machine still; scale pulses can reset the datum.')
        self.fast_apply_button.clicked.connect(self.apply_fast_mode)
        banner_layout.addWidget(self.fast_apply_button)
        self.fast_cancel_button = QPushButton('Cancel')
        self.fast_cancel_button.setMinimumHeight(40)
        self.fast_cancel_button.setToolTip('Disconnect and stop subsequent commands; a pulse already sent cannot be undone')
        self.fast_cancel_button.clicked.connect(self.stop_acquisition)
        banner_layout.addWidget(self.fast_cancel_button)
        page_layout.addWidget(self.fast_banner)
        self.fast_banner_open = False
        self.fast_animation = QVariantAnimation(self)
        self.fast_animation.setDuration(220)
        self.fast_animation.setEasingCurve(QEasingCurve.Type.OutCubic)
        self.fast_animation.valueChanged.connect(self._set_fast_banner_height)
        central = self.work_area = QWidget()
        page_layout.addWidget(central, 1)
        outer = QHBoxLayout(central)
        outer.setContentsMargins(16, 8, 16, 16)
        outer.setSpacing(0)
        main_content = QWidget()
        outer.addWidget(main_content, 1)
        main_layout = QVBoxLayout(main_content)
        main_layout.setContentsMargins(0, 0, 0, 0)
        main_layout.setSpacing(0)
        columns = self.content_columns = QHBoxLayout()
        main_layout.addLayout(columns, 1)
        columns.setContentsMargins(0, 0, 0, 0)
        columns.setSpacing(16)

        self.readouts = QWidget()
        readout_layout = QVBoxLayout(self.readouts)
        readout_layout.setContentsMargins(0, 0, 0, 0)
        readout_layout.setSpacing(12)
        self.value_labels = []
        self.axis_cards, self.axis_names = [], []
        self.sign_buttons, self.axis_zero_buttons, self.mode_buttons = [], [], []
        for index, axis in enumerate("XYZ"):
            card = AxisWidget(axis)
            self._restore_axis_decimals(card)
            card.precision_changed.connect(lambda card=card: self._save_axis_decimals(card))
            card.sign.toggled.connect(self._axis_options_changed)
            card.mode.toggled.connect(self._axis_options_changed)
            card.zero.clicked.connect(lambda checked=False, i=index: self.zero_axis(i))
            readout_layout.addWidget(card, 1)
            self.value_labels.append(card.value)
            self.axis_cards.append(card)
            self.axis_names.append(card.name)
            self.sign_buttons.append(card.sign)
            self.axis_zero_buttons.append(card.zero)
            self.mode_buttons.append(card.mode)
        self.number_font = self.axis_cards[0].number_font

        self.controls = QWidget()
        buttons = QVBoxLayout(self.controls)
        buttons.setContentsMargins(0, 0, 0, 0)
        buttons.setSpacing(12)
        self.action_grid = QGridLayout()
        self.action_grid.setSpacing(4)
        self.action_grid.setAlignment(Qt.AlignmentFlag.AlignTop)
        buttons.addLayout(self.action_grid)
        self.settings_button = QPushButton('⚙\ufe0e')
        self.settings_button.setFixedSize(44, 44)
        self.settings_button.setStyleSheet('font-size: 26px;')
        self.settings_button.setAccessibleName('Settings')
        self.settings_button.setToolTip('Settings — machine profile, scales and coordinates')
        self.settings_button.clicked.connect(self.show_settings)
        self.connect_button = QPushButton("Connect")
        self.connect_button.clicked.connect(self.toggle_connection)
        self.clear_zero_button = QPushButton("Clear zero")
        self.clear_zero_button.setToolTip("Clear each axis's selected ABS/REL zero offset.\n"
                                          "Keep its other mode's offset and direction.")
        self.clear_zero_button.clicked.connect(self.clear_zero)
        self.units_button = AxisToggle("mm", "in", self.controls)
        self.units_button.setToolTip("Select mm or in for all readouts; Space toggles")
        self.units_button.toggled.connect(self.set_inches)
        self.hole_button = QPushButton("Lay out holes")
        QShortcut(QKeySequence("F3"), self).activated.connect(self.show_hole_circle)
        self.hole_button.setAccessibleName("Lay out holes in the XY plane")
        self.hole_button.setToolTip("Lay out holes: step through an XY circle (F3)")
        self.hole_button.clicked.connect(self.show_hole_circle)
        self.remember_button = QPushButton("Remember point")
        self.remember_button.setToolTip("Save the displayed position and show distances from a remembered point")
        self.remember_button.clicked.connect(self.show_remember_point)

        self.main_actions = (self.hole_button, self.units_button, self.connect_button,
                             self.remember_button, self.clear_zero_button, self.settings_button)
        for button in self.main_actions[:-1]:
            button.setFixedHeight(44)
        self.main_actions_wide = None
        self._layout_main_actions()
        self._create_tool_path()
        buttons.addWidget(self.tool_path_panel, 1)
        quit_button = QPushButton("Quit")
        quit_button.setFixedSize(120, 44)
        quit_button.clicked.connect(self.close)
        buttons.addWidget(quit_button, 0, Qt.AlignmentFlag.AlignRight)

        columns.addWidget(self.readouts, 62)
        columns.addWidget(self.controls, 38)
        self.drawer = QWidget()
        self.drawer.setFixedWidth(0)
        outer.addWidget(self.drawer)
        self.drawer_pages = QStackedWidget(self.drawer)
        self.drawer_pages.hide()
        self.drawer_animation = QVariantAnimation(self)
        self.drawer_animation.setDuration(220)
        self.drawer_animation.setEasingCurve(QEasingCurve.Type.OutCubic)
        self.drawer_animation.valueChanged.connect(self._set_drawer_width)
        central.installEventFilter(self)
        self.drawer.installEventFilter(self)
        self.readouts.installEventFilter(self)
        self.controls.installEventFilter(self)
        self.setStatusBar(AlignedStatusBar(self))
        self.statusBar().setSizeGripEnabled(False)
        self.statusBar().setMinimumHeight(40)
        self.statusBar().setContentsMargins(8, 4, 8, 4)
        self.fast_indicator = QLabel('● Fast mode')
        self.fast_indicator.setStyleSheet('color: #206356; font-size: 14px;')
        self.statusBar().addPermanentWidget(self.fast_indicator)
        self.fast_indicator.hide()
        self.statusBar().showMessage("Disconnected")
        chevron = (Path(__file__).resolve().parents[1] / 'assets' / 'chevron-down.svg').as_posix()
        self.setStyleSheet("""
            QMainWindow, QDialog { background: #e8eef2; }
            QFrame#readout { background: white; border: 1px solid #718692;
                             border-radius: 12px; }
            QFrame#holeVisualizer, QFrame#rememberVisualizer, QFrame#toolPath { background: white; border: 1px solid #708491; border-radius: 12px; }
            QLabel { color: #10212f; }
            QLabel#axis { color: #24566e; font-size: 28px; font-weight: 800; }
            QSpinBox, QDoubleSpinBox, QComboBox, QLineEdit { background: white; color: #10212f;
                                      border: 1px solid #708491; border-radius: 6px;
                                      padding: 6px 10px; }
            QSpinBox:focus, QDoubleSpinBox:focus, QLineEdit:focus { border-color: #24566e; }
            QCheckBox { padding: 6px; }
            QComboBox { border-radius: 7px; padding: 6px 44px 6px 12px;
                        font-size: 13px; combobox-popup: 0; }
            QComboBox:hover { background: #dce9f1; }
            QComboBox:focus, QComboBox:on { border-color: #24566e; }
            QSpinBox[keyboardEditing="true"], QDoubleSpinBox[keyboardEditing="true"],
            QComboBox[keyboardEditing="true"], QLineEdit[keyboardEditing="true"] { border-color: #795113; }
            QComboBox:disabled { background: #e1e7ec; color: #4f606d; }
            QComboBox::drop-down { subcontrol-origin: padding; subcontrol-position: center right;
                                  width: 44px; border: none; background: transparent; }
            QComboBox::down-arrow { image: url("%s"); width: 16px; height: 10px; }
            QComboBox QLineEdit { border: none; border-radius: 0; padding: 0;
                                 background: transparent; selection-background-color: #174b63;
                                 selection-color: white; }
            QComboBox QLineEdit:disabled { color: #4f606d; }
            QComboBox QAbstractItemView { background: white; color: #10212f;
                                         border: 1px solid #708491; border-radius: 7px;
                                         padding: 4px; outline: none;
                                         selection-background-color: #174b63; selection-color: white; }
            QComboBox QAbstractItemView::item { min-height: 36px; padding: 4px 12px; border-radius: 4px; }
            QComboBox QAbstractItemView::item:selected { background: #174b63; color: white; }
            QSpinBox::up-button, QDoubleSpinBox::up-button {
                subcontrol-origin: padding; subcontrol-position: center right;
                width: 18px; height: 14px; right: 4px; top: -7px; }
            QSpinBox::down-button, QDoubleSpinBox::down-button {
                subcontrol-origin: padding; subcontrol-position: center right;
                width: 18px; height: 14px; right: 4px; top: 7px; }
            QPushButton { background: #ffffff; color: #10212f;
                          border: 1px solid #708491; border-radius: 7px;
                          padding: 4px 8px; font-size: 13px; }
            QPushButton:hover { background: #dce9f1; }
            QPushButton:checked { background: #cfe3ef; }
            QPushButton:focus { border-color: #24566e; }
            QPushButton:disabled { color: #4f606d; background: #e1e7ec; }
            QStatusBar { color: #344b5c; font-weight: 400; background: #dce5ec; font-size: 20px; }
        """ % chevron.replace('"', r'\"'))
        self.units_button.setChecked(True)
        self._axis_options_changed()

    def _layout_main_actions(self):
        available = self.work_area.width() - 48 - (self.drawer.width() if hasattr(self, 'drawer') else 0)
        mode = 'narrow' if available < 700 else 'wide' if available * .38 >= 500 else 'compact'
        self.content_columns.setStretch(0, 85 if mode == 'narrow' else 62)
        self.content_columns.setStretch(1, 15 if mode == 'narrow' else 38)
        if hasattr(self, 'tool_path_panel'):
            self.tool_path_panel.setVisible(mode != 'narrow')
        if mode == self.main_actions_wide:
            return
        self.main_actions_wide = mode
        for button in self.main_actions:
            self.action_grid.removeWidget(button)
        for column in range(4):
            self.action_grid.setColumnStretch(column, 0)
        positions = (((0, 0), (0, 1), (0, 2), (1, 0), (1, 1), (0, 3)) if mode == 'wide' else
                     ((1, 0), (2, 0), (1, 1), (3, 0), (2, 1), (0, 1)) if mode == 'compact' else
                     ((1, 0), (3, 0), (5, 0), (2, 0), (4, 0), (0, 0)))
        for button, (row, column) in zip(self.main_actions, positions):
            self.action_grid.addWidget(button, row, column, Qt.AlignmentFlag.AlignRight if button is self.settings_button
                                       else Qt.AlignmentFlag(0))
        for column in range(3 if mode == 'wide' else 2 if mode == 'compact' else 1):
            self.action_grid.setColumnStretch(column, 1)
        order = sorted(zip(positions, self.main_actions), key=lambda item: item[0])
        for (_, previous), (_, button) in zip(order, order[1:]):
            QWidget.setTabOrder(previous, button)

    def _create_tool_path(self):
        panel = self.tool_path_panel = QFrame()
        panel.setObjectName('toolPath')
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(12, 8, 12, 8)
        layout.setSpacing(6)
        header = QHBoxLayout()
        title = QLabel('Tool path')
        title.setStyleSheet('font-size: 18px;')
        header.addWidget(title, 1)
        self.tool_path_reset = QPushButton('↺')
        self.tool_path_clear = QPushButton('×')
        for button, name in ((self.tool_path_reset, 'Reset tool-path view'), (self.tool_path_clear, 'Clear tool path')):
            button.setFixedSize(44, 44)
            button.setAccessibleName(name)
            button.setToolTip(name)
            button.setStyleSheet('font-size: 24px;')
            header.addWidget(button)
        layout.addLayout(header)
        self.tool_path_summary = QLabel('Waiting for readings · 2 s')
        self.tool_path_summary.setStyleSheet('color: #344b5c; font-size: 12px; font-weight: 400;')
        self.tool_path_summary.setWordWrap(True)
        layout.addWidget(self.tool_path_summary)
        self.tool_path_plot = RememberPointPlot(trail=True)
        self.tool_path_plot.setMinimumHeight(80)
        self.tool_path_plot.setToolTip('Checks every 2 seconds; draws after 0.001 in (0.0254 mm) of travel. '
                                       'Latest 3,600 positions/gaps. Enter to use arrow keys to rotate; / to navigate. Drag to rotate; '
                                       'scroll or use +/− to zoom; Home resets. Zero and ABS/REL do not move the path.')
        layout.addWidget(self.tool_path_plot, 1)
        note = QLabel('Drag to rotate · Scroll to zoom · Enter to use keys · / to leave')
        note.setWordWrap(True)
        note.setStyleSheet('color: #344b5c; font-size: 12px; font-weight: 400;')
        layout.addWidget(note)
        self.tool_path_reset.clicked.connect(self.tool_path_plot.reset_view)
        self.tool_path_clear.clicked.connect(self.clear_tool_path)

    def clear_tool_path(self):
        self.tool_path_samples.clear()
        self.tool_path_tick = 0
        self._update_tool_path()

    def sample_tool_path(self):
        point = None
        state = self.latest.snapshot()
        if (self.worker is not None and self.connection_ready and not self.stopping and not self.closing
                and not self.fast_busy and not self.error_text and state.values is not None):
            values = coordinates(self.profile, state.values, self.machine_config['directions'],
                                 self.machine_config['angle'], self.machine_config['summed'])
            count = sum(not card.isHidden() for card in self.axis_cards)
            if all(v is not None and math.isfinite(v) for v in values[:count]):
                point = tuple(values[i] if i < count else 0. for i in range(3))
        previous = self.tool_path_samples[-1][1] if self.tool_path_samples else None
        if point is not None and previous is not None:
            distance = math.dist(point, previous)
            # Compare physical mm before diameter/sign display transforms; tolerate float roundoff at the boundary.
            if distance < .0254 and not math.isclose(distance, .0254, rel_tol=1e-9):
                return
        if point is not None or previous is not None:
            # Skipped noise keeps the same anchor; only unavailable readings break a segment.
            self.tool_path_tick += 1
            self.tool_path_samples.append((self.tool_path_tick, point))
        self._update_tool_path()

    def _update_tool_path(self):
        axes = tuple(card.name.text() for card in self.axis_cards if not card.isHidden())
        factors = [-1 if card.sign.isChecked() else 1 for card in self.axis_cards]
        if self.profile.startswith('lathe') and self.machine_config['diameter']:
            factors[0] *= 2
        points = {tick: tuple(value * factor for value, factor in zip(point, factors))
                  for tick, point in self.tool_path_samples if point is not None}
        self.tool_path_plot.set_points(points, next(reversed(points), None), axes)
        state = ('Disconnected' if self.worker is None else 'Paused' if self.stopping or self.fast_busy or self.error_text
                 else 'Waiting for readings' if not self.connection_ready or not self.tool_path_samples
                 or self.tool_path_samples[-1][1] is None else 'Live')
        unit = 'in' if self.units_button.isChecked() else 'mm'
        scale, precision = (25.4, 4) if unit == 'in' else (1., 3)
        threshold = '0.001 in' if unit == 'in' else '0.0254 mm'
        summary = f'{state} · 2 s · ≥ {threshold} · {len(points):,} positions'
        if points:
            latest = next(reversed(points.values()))
            summary += '\n' + ' · '.join(f'{axis} {latest[i] / scale:.{precision}f}' for i, axis in enumerate(axes)) + f' {unit}'
        self.tool_path_summary.setText(summary)

    def _default_setup(self, profile):
        count = len(PROFILES[profile][2])
        return dict(profile=profile, port=self.port, channels=list(range(count)),
                    directions=[1] * count, angle=0, diameter=profile.startswith('lathe'), summed=False)

    def _build_startup(self):
        readout = self.takeCentralWidget()
        self.pages = QStackedWidget()
        self.setCentralWidget(self.pages)
        self.pages.addWidget(readout)
        self.pages.installEventFilter(self)
        self.chooser = QWidget()
        layout = QVBoxLayout(self.chooser)
        layout.setContentsMargins(24, 18, 24, 18)
        layout.setSpacing(12)
        title = self.setup_title = QLabel('Set up DADRO')
        title.setStyleSheet('font-size: 28px; font-weight: 800;')
        layout.addWidget(title)
        subtitle = QLabel('Step 1 of 2 · Choose the movements that have a scale fitted.')
        subtitle.setStyleSheet('font-size: 14px; color: #344b5c; font-weight: 400;')
        layout.addWidget(subtitle)
        scroll = QScrollArea()
        self.setup_scroll = scroll
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        content = QWidget()
        grid = QGridLayout(content)
        grid.setContentsMargins(0, 0, 0, 0)
        grid.setSpacing(10)
        self.setup_group = QButtonGroup(self)
        self.setup_cards = {}
        for row, text in ((0, 'Mill'), (2, 'Lathe')):
            label = QLabel(text)
            label.setStyleSheet('font-size: 15px; font-weight: 800;')
            grid.addWidget(label, row, 0, 1, 3)
        for index, profile in enumerate(PROFILES):
            card = ProfileCard(profile)
            card.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
            self.setup_cards[profile] = card
            self.setup_group.addButton(card, index)
            grid.addWidget(card, 1 if index < 3 else 3, index if index < 3 else index - 3)
        legend = QLabel('Look for your fitted scales.\n\n━━  Measured movement\n┄┄  Not measured\n\nQ = quill\nZ2 = compound slide')
        legend.setStyleSheet('font-size: 13px; color: #344b5c; font-weight: 400; padding: 12px;')
        legend.setWordWrap(True)
        grid.addWidget(legend, 3, 2)
        for column in range(3): grid.setColumnStretch(column, 1)
        grid.setRowStretch(1, 1); grid.setRowStretch(3, 1)
        scroll.setWidget(content)
        scroll.viewport().setAutoFillBackground(False)
        content.setAutoFillBackground(False)
        layout.addWidget(scroll, 1)
        actions = QHBoxLayout()
        self.setup_summary = QLabel('Choose a setup to continue.')
        self.setup_summary.setStyleSheet('font-weight: 400;')
        self.setup_summary.setWordWrap(True)
        actions.addWidget(self.setup_summary, 1)
        self.cancel_setup_button = QPushButton('Cancel')
        self.cancel_setup_button.clicked.connect(self.return_to_readout)
        self.startup_demo_button = QPushButton('Try XYZ demo')
        self.startup_demo_button.clicked.connect(self.start_xyz_demo)
        self.open_setup_button = QPushButton('Next: scales →')
        self.open_setup_button.setEnabled(False)
        self.open_setup_button.setStyleSheet('QPushButton:enabled { background: #07565f; color: white; }')
        self.open_setup_button.clicked.connect(self.open_selected_setup)
        for button in (self.cancel_setup_button, self.startup_demo_button, self.open_setup_button):
            button.setMinimumHeight(44)
            actions.addWidget(button)
        layout.addLayout(actions)
        self.setup_group.idToggled.connect(self._update_setup_selection)
        self.pages.addWidget(self.chooser)

    def _update_setup_selection(self):
        selected = self.setup_group.checkedButton()
        self.open_setup_button.setEnabled(selected is not None and not self.stopping)
        if selected:
            group, title, axes, description = PROFILES[selected.profile]
            self.setup_summary.setText(f'{group} · {title} · {len(axes)} scales. {description}' +
                (' Applying this setup ends the active hole circle and clears software zeros.' if self.hole_circle else ''))

    def begin_startup(self, port_override=None):
        self.startup_active = True
        if self.demo:
            self.start_xyz_demo()
            return
        try:
            config = json.loads(self.machine_settings.value('setup', 'null'))
            validate_setup(config)
        except (ValueError, TypeError):
            self.configuration_verified = False
            self.show_setup_chooser()
            return
        if port_override is not None:
            config['port'] = port_override
        self._apply_machine_config(config, verified=True)
        self.start_acquisition(demo=False)

    def show_setup_chooser(self):
        if self.fast_busy or self.stopping or self.closing:
            return
        self.cancel_setup_button.setVisible(not self.startup_active)
        self.setup_title.setText('Set up DADRO' if self.startup_active else 'Settings')
        if self.setup_draft is not None:
            self.setup_cards[self.setup_draft['profile']].setChecked(True)
        self._update_setup_selection()
        self.pages.setCurrentWidget(self.chooser)
        self.setWindowTitle('DADRO — ' + self.setup_title.text())

    def return_to_readout(self):
        self.setup_draft = None
        self.toolhead_draft = None
        self.pages.setCurrentWidget(self.readout_page)
        self.setWindowTitle('DADRO — ' + ' · '.join(PROFILES[self.profile][:2]))
        self.settings_button.setFocus()

    def _after_acquisition_stops(self, action):
        if self.stopping or self.closing:
            return
        if self.worker is None:
            action()
        else:
            self.after_stop = action
            self.stop_acquisition()
            self.setup_summary.setText('Stopping the current acquisition before changing setup…')

    def show_settings(self):
        if self.fast_busy or self.stopping or self.closing:
            return
        self.setup_draft = dict(self.machine_config)
        self.toolhead_draft = dict(self.toolhead_config)
        if not self.configuration_verified:
            try:
                saved = json.loads(self.machine_settings.value('setup', 'null'))
                validate_setup(saved)
                self.setup_draft = saved
            except (ValueError, TypeError):
                pass
        self.show_machine_setup()

    def open_selected_setup(self):
        selected = self.setup_group.checkedButton()
        if selected is None or self.fast_busy or self.stopping or self.closing:
            return
        profile = selected.profile
        if self.setup_draft is None or self.setup_draft['profile'] != profile:
            port = self.setup_draft['port'] if self.setup_draft else self.port
            self.setup_draft = dict(self.machine_config) if profile == self.profile else self._default_setup(profile)
            self.setup_draft['port'] = port
        self.show_machine_setup()

    def back_to_profiles(self):
        if self.stopping or self.closing:
            return
        self.compound_angle.interpretText()
        self.toolhead_width_field.interpretText()
        self.setup_draft = self._setup_from_fields()
        self.show_setup_chooser()

    def start_xyz_demo(self):
        def apply():
            self._apply_machine_config(self._default_setup('mill3'), verified=False)
            self.start_acquisition(demo=True)
        self._after_acquisition_stops(apply)

    def _apply_machine_config(self, config, verified):
        validate_setup(config)
        setup_changed = config != self.remembered_setup
        keep_origin = config['profile'] == self.remembered_setup['profile']
        self.remembered_setup = dict(config)
        if setup_changed:
            self._clear_remembered_points(keep_origin=keep_origin)
        self.finish_hole_circle('Machine setup changed. Software zeros cleared.')
        if self.active_panel is not None:
            self.active_panel.reject()
        self.machine_config = dict(config)
        self.profile, self.port = config['profile'], config['port']
        self.demo = False
        self.fast_busy = self.fast_attempted = False
        self.fast_message = ''
        self.configuration_verified = verified
        self.startup_active = False
        self.setup_draft = None
        self.toolhead_draft = None
        self.latest = LatestReading()
        self.displayed = None
        self.last_drawn = -1
        self.offset = self.relative_offset = (0.0, 0.0, 0.0)
        self.datum_message = 'Software zeros cleared — establish the datum'
        self.error_text = ''
        for button in self.sign_buttons + self.mode_buttons:
            button.setChecked(False)
        group, title, axes, _ = PROFILES[self.profile]
        names = ('X', 'Y', 'Z') if group == 'Mill' else ('X', 'Z' if config['summed'] else 'Z1', 'Z2')
        show_third = self.profile in ('mill3', 'mill3q') or (self.profile == 'lathe3' and not config['summed'])
        for index, card in enumerate(self.axis_cards):
            card.set_axis_name(('Ø X' if config['diameter'] else 'R X') if group == 'Lathe' and index == 0 else names[index])
            self._restore_axis_decimals(card)
            card.set_reading(None, self.units_button.isChecked())
            card.setVisible(index != 2 or show_third)
        self.clear_tool_path()
        self.setWindowTitle(f'DADRO — {group} · {title}')
        self.hole_button.setVisible(group == 'Mill')
        self._axis_options_changed()
        self.pages.setCurrentWidget(self.readout_page)
        self.refresh_display()
        self.axis_cards[0].sign.setFocus()

    def show_machine_setup(self):
        if self.fast_busy or self.stopping or self.closing:
            return
        if self.setup_draft is None:
            self.setup_draft = dict(self.machine_config)
        if self.toolhead_draft is None:
            self.toolhead_draft = dict(self.toolhead_config)
        config = self.setup_draft
        profile = config['profile']
        if self.machine_page is not None:
            self.pages.removeWidget(self.machine_page)
            self.machine_page.deleteLater()
        page = self.machine_page = QWidget()
        outer = QVBoxLayout(page)
        outer.setContentsMargins(24, 18, 24, 18)
        outer.setSpacing(12)
        title = QLabel('Fine tune your setup')
        title.setStyleSheet('font-size: 28px; font-weight: 800;')
        outer.addWidget(title)
        group, name, axes, _ = PROFILES[profile]
        subtitle = QLabel(f'Step 2 of 2 · {group} · {name}')
        subtitle.setStyleSheet('font-size: 14px; color: #344b5c; font-weight: 400;')
        outer.addWidget(subtitle)
        self.machine_scroll = QScrollArea()
        self.machine_scroll.setWidgetResizable(True)
        self.machine_scroll.setFrameShape(QFrame.Shape.NoFrame)
        body = QWidget()
        layout = QVBoxLayout(body)
        layout.setContentsMargins(0, 0, 12, 0)
        layout.setSpacing(12)
        columns = QHBoxLayout()
        columns.setSpacing(28)
        columns.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop)
        self.machine_profile_card = ProfileCard(profile)
        self.machine_profile_card.setChecked(True)
        self.machine_profile_card.setEnabled(False)
        self.machine_profile_card.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.machine_profile_card.setCursor(Qt.CursorShape.ArrowCursor)
        self.machine_profile_card.setFixedSize(320, 224)
        columns.addWidget(self.machine_profile_card, 0, Qt.AlignmentFlag.AlignTop)
        fields = QWidget()
        fields.setMaximumWidth(640)
        field_layout = QVBoxLayout(fields)
        field_layout.setContentsMargins(0, 0, 0, 0)
        field_layout.setSpacing(12)
        interface_title = QLabel('Serial interface and scale direction')
        interface_title.setStyleSheet('font-size: 18px; font-weight: 800;')
        interface_title.setWordWrap(True)
        field_layout.addWidget(interface_title)
        form = QGridLayout()
        form.setHorizontalSpacing(12)
        form.setVerticalSpacing(12)
        form.setColumnMinimumWidth(0, 40)
        form.setColumnStretch(1, 1)
        self.port_field = QComboBox()
        self.port_field.setEditable(True)
        self.port_field.addItems([port.device for port in list_ports.comports()])
        self.port_field.setCurrentText(config['port'])
        self.port_field.setAccessibleName('Serial port')
        self.port_field.setMinimumHeight(44)
        interface_label = QLabel('Interface')
        interface_label.setStyleSheet('font-size: 12px; color: #344b5c; font-weight: 400;')
        interface_label.setBuddy(self.port_field)
        form.addWidget(interface_label, 0, 0, 1, 3)
        form.addWidget(self.port_field, 1, 0, 1, 3)
        for column, text in enumerate(('Scale', 'Controller channel', 'Direction')):
            label = QLabel(text)
            label.setStyleSheet('font-size: 12px; color: #344b5c; font-weight: 400;')
            label.setContentsMargins(0, 8, 0, 0)
            form.addWidget(label, 2, column)
        self.channel_fields, self.direction_fields = [], []
        for row, (axis, channel, sign) in enumerate(zip(axes, config['channels'], config['directions']), 3):
            field = QComboBox(); field.addItems([f'Channel {n}' for n in range(4)])
            field.setCurrentIndex(channel); field.setAccessibleName(f'{axis} channel'); field.setFixedHeight(44)
            direction = AxisToggle('+', '-', fields); direction.setChecked(sign < 0)
            direction.setFixedSize(104, 44)
            direction.setAccessibleName(f'Reverse incoming {axis} scale')
            direction.setAccessibleDescription('+ keeps the incoming scale direction; − reverses it before calculating coordinates.')
            direction.setToolTip(direction.accessibleDescription())
            label = QLabel(axis)
            label.setStyleSheet('font-size: 20px; font-weight: 800; color: #24566e;')
            label.setBuddy(field)
            form.addWidget(label, row, 0)
            form.addWidget(field, row, 1)
            form.addWidget(direction, row, 2)
            self.channel_fields.append(field); self.direction_fields.append(direction)
        field_layout.addLayout(form)
        self.compound_angle = CompactDoubleSpinBox()
        self.compound_angle.setRange(-180, 180); self.compound_angle.setDecimals(3)
        self.compound_angle.setSuffix('°'); self.compound_angle.setValue(config['angle'])
        self.compound_angle.setAccessibleName('Compound angle from the spindle axis')
        self.compound_angle.setMinimumHeight(44)
        if profile == 'lathe3':
            angle_label = QLabel('Compound angle')
            angle_label.setBuddy(self.compound_angle)
            field_layout.addWidget(angle_label)
            field_layout.addWidget(self.compound_angle)
        else:
            self.compound_angle.hide()
        if profile == 'lathe3':
            explanation = QLabel('0° is along +Z. Positive angles make positive compound travel reduce X. Verify with known movement before setting the datum.')
            explanation.setStyleSheet('font-weight: 400;')
            explanation.setWordWrap(True); field_layout.addWidget(explanation)
        self.diameter_field = QCheckBox('Show X as diameter (otherwise radius)')
        self.diameter_field.setChecked(config['diameter'])
        self.sum_field = QCheckBox('Combine carriage and compound into Z')
        self.sum_field.setChecked(config['summed'])
        field_layout.addWidget(self.diameter_field); field_layout.addWidget(self.sum_field)
        self.diameter_field.setVisible(profile.startswith('lathe'))
        self.sum_field.setVisible(profile == 'lathe3')
        field_layout.addSpacing(8)
        divider = QFrame()
        divider.setFixedHeight(1)
        divider.setStyleSheet('background: #708491; border: none;')
        field_layout.addWidget(divider)
        toolhead_title = QLabel('Edge finding toolhead')
        toolhead_title.setStyleSheet('font-size: 18px; font-weight: 800;')
        field_layout.addWidget(toolhead_title)
        width_label = QLabel('Width')
        width_label.setStyleSheet('font-size: 12px; color: #344b5c; font-weight: 400;')
        field_layout.addWidget(width_label)
        toolhead_row = QHBoxLayout()
        self.toolhead_width_field = ToolheadWidthSpinBox()
        self.toolhead_width_field.setDecimals(6)
        self.toolhead_width_field.setMinimumHeight(44)
        self.toolhead_width_field.setAccessibleName('Edge finding toolhead width')
        width_label.setBuddy(self.toolhead_width_field)
        toolhead_row.addWidget(self.toolhead_width_field, 1)
        self.toolhead_units = AxisToggle('in', 'mm', fields)
        self.toolhead_units.setChecked(not self.toolhead_draft['inches'])
        self.toolhead_units.setFixedSize(104, 44)
        self.toolhead_units.setToolTip('Select in or mm for the toolhead width; Space toggles')
        toolhead_row.addWidget(self.toolhead_units)
        field_layout.addLayout(toolhead_row)
        self.toolhead_offset = self.toolhead_width_field.offset
        self.toolhead_direction_note = QLabel('Offset direction can be adjusted for each point.')
        self.toolhead_direction_note.setStyleSheet('font-size: 12px; color: #344b5c; font-weight: 400;')
        self.toolhead_direction_note.setWordWrap(True)
        field_layout.addWidget(self.toolhead_direction_note)
        self._render_toolhead_width()
        columns.addWidget(fields, 1, Qt.AlignmentFlag.AlignTop)
        layout.addLayout(columns)
        layout.addStretch(1)
        self.machine_scroll.setWidget(body)
        self.machine_scroll.viewport().setAutoFillBackground(False)
        body.setAutoFillBackground(False)
        outer.addWidget(self.machine_scroll, 1)
        self.machine_status = QLabel()
        self.machine_status.setStyleSheet('font-weight: 400;')
        self.machine_status.setWordWrap(True)
        outer.addWidget(self.machine_status)
        actions = QHBoxLayout()
        self.machine_back = QPushButton('← Machine profile')
        self.machine_back.clicked.connect(self.back_to_profiles)
        actions.addWidget(self.machine_back)
        actions.addStretch(1)
        self.machine_cancel = QPushButton('Cancel', page)
        self.machine_cancel.clicked.connect(self.return_to_readout)
        self.machine_cancel.setVisible(not self.startup_active)
        self.machine_apply = QPushButton('Save && connect')
        self.machine_apply.setStyleSheet('QPushButton:enabled { background: #07565f; color: white; }')
        self.machine_apply.clicked.connect(self.apply_machine_setup)
        for button in (self.machine_back, self.machine_cancel, self.machine_apply):
            button.setMinimumHeight(44); button.setAutoDefault(False)
        actions.addWidget(self.machine_cancel)
        actions.addWidget(self.machine_apply)
        outer.addLayout(actions)
        for field in self.channel_fields:
            field.currentIndexChanged.connect(self._machine_fields_changed)
        for field in self.direction_fields + [self.diameter_field, self.sum_field]:
            field.toggled.connect(self._machine_fields_changed)
        self.port_field.currentTextChanged.connect(self._machine_fields_changed)
        self.compound_angle.valueChanged.connect(self._machine_fields_changed)
        self.toolhead_width_field.valueChanged.connect(self._toolhead_width_changed)
        self.toolhead_width_field.lineEdit().textChanged.connect(self._machine_fields_changed)
        self.toolhead_units.toggled.connect(self._toolhead_units_changed)
        self.setup_error = ''
        self._update_machine_controls()
        self.pages.addWidget(page)
        self.pages.setCurrentWidget(page)
        self.setWindowTitle('DADRO — Settings')

    def _render_toolhead_width(self):
        inches = self.toolhead_draft['inches']
        scale, unit = (25.4, 'in') if inches else (1., 'mm')
        self.toolhead_units.setAccessibleName(f'Toolhead width units: {unit}')
        with QSignalBlocker(self.toolhead_width_field), QSignalBlocker(self.toolhead_width_field.lineEdit()):
            self.toolhead_width_field.setRange(0, 1000 / scale)
            self.toolhead_width_field.setSingleStep(.001 if inches else .01)
            self.toolhead_width_field.setSuffix(' ' + unit)
            self.toolhead_width_field.setValue(self.toolhead_draft['width_mm'] / scale)
        self._update_toolhead_offset()

    def _toolhead_width_changed(self, value):
        self.toolhead_draft['width_mm'] = value * (25.4 if self.toolhead_draft['inches'] else 1.)
        self._machine_fields_changed()

    def _toolhead_units_changed(self, millimetres):
        if not self.toolhead_width_field.hasAcceptableInput():
            with QSignalBlocker(self.toolhead_units):
                self.toolhead_units.setChecked(not self.toolhead_draft['inches'])
                self.toolhead_units.setText(self.toolhead_units.values[self.toolhead_units.isChecked()])
            return
        self.toolhead_draft['inches'] = not millimetres
        self._render_toolhead_width()
        self._machine_fields_changed()

    def _update_toolhead_offset(self):
        scale, unit = (25.4, 'in') if self.toolhead_draft['inches'] else (1., 'mm')
        value = self.toolhead_draft['width_mm'] / (2 * scale)
        text = self.toolhead_width_field.textFromValue(value) if value > 0 and self.toolhead_width_field.hasAcceptableInput() else '—'
        self.toolhead_width_field.set_offset(text, unit)

    def _machine_fields_changed(self):
        self.setup_error = ''
        self._update_machine_controls()

    def _setup_from_fields(self):
        return dict(profile=self.setup_draft['profile'], port=self.port_field.currentText().strip(),
                    channels=[field.currentIndex() for field in self.channel_fields],
                    directions=[-1 if field.isChecked() else 1 for field in self.direction_fields],
                    angle=self.compound_angle.value(), diameter=self.diameter_field.isChecked(), summed=self.sum_field.isChecked())

    def _update_machine_controls(self):
        if self.machine_page is None or self.setup_draft is None:
            return
        try:
            validate_setup(self._setup_from_fields())
            validate_toolhead(self.toolhead_draft)
            valid = self.compound_angle.hasAcceptableInput()
            message = '' if valid else 'Enter a compound angle between −180° and 180°'
            if not self.toolhead_width_field.hasAcceptableInput():
                valid, message = False, 'Enter a valid toolhead width.'
        except ValueError as error:
            message, valid = str(error), False
        available = not self.fast_busy and not self.stopping and not self.closing
        self.machine_status.setText('Disconnecting the current scales before applying settings…' if self.stopping else self.setup_error or message)
        self.machine_status.setVisible(bool(self.machine_status.text()))
        self.machine_apply.setEnabled(valid and available)
        self.machine_back.setEnabled(available)
        self.machine_cancel.setEnabled(available)
        self.machine_scroll.setEnabled(available)
        self._update_toolhead_offset()

    def apply_machine_setup(self):
        if self.setup_draft is None:
            return
        self._update_machine_controls()
        if not self.machine_apply.isEnabled():
            return
        self.compound_angle.interpretText()
        self.toolhead_width_field.interpretText()
        config = self._setup_from_fields()
        toolhead = dict(self.toolhead_draft)
        previous = {key: self.machine_settings.value(key) for key in ('setup', 'toolhead')}
        self.machine_settings.setValue('setup', json.dumps(config))
        self.machine_settings.setValue('toolhead', json.dumps(toolhead))
        self.machine_settings.sync()
        if self.machine_settings.status() != QSettings.Status.NoError:
            for key, value in previous.items():
                if value is None:
                    self.machine_settings.remove(key)
                else:
                    self.machine_settings.setValue(key, value)
            self.machine_settings.sync()
            self.setup_error = 'Could not save settings. Check access to your preferences; the current setup is unchanged.'
            self._update_machine_controls()
            return
        def apply():
            self.toolhead_config = toolhead
            self._apply_machine_config(config, verified=True)
            self.start_acquisition(demo=False)
        self._after_acquisition_stops(apply)

    def _create_function_panel(self, title: str):
        # Embed Qt's dialog keyboard/finished behavior; this widget is never a separate window.
        panel = QDialog(self.drawer_pages)
        panel.setWindowFlags(Qt.WindowType.Widget)
        panel.setWindowTitle(title)
        outer = QVBoxLayout(panel)
        outer.setContentsMargins(16, 0, 16, 12)
        outer.setSpacing(12)
        header = QHBoxLayout()
        heading = QLabel(title)
        heading.setObjectName("panelTitle")
        heading.setStyleSheet("font-size: 20px; font-weight: 800;")
        heading.setWordWrap(True)
        header.addWidget(heading, 1)
        close = QPushButton("×")
        close.setFixedSize(44, 44)
        close.setAutoDefault(False)
        close.setAccessibleName(f"Close {title}")
        close.setToolTip(f"Close {title}")
        close.clicked.connect(panel.reject)
        header.addWidget(close)
        outer.addLayout(header)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        content = QWidget()
        layout = QVBoxLayout(content)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(12)
        scroll.setWidget(content)
        scroll.viewport().setAutoFillBackground(False)
        content.setAutoFillBackground(False)
        outer.addWidget(scroll, 1)
        actions = QHBoxLayout()
        outer.addLayout(actions)
        self.drawer_pages.addWidget(panel)
        panel.finished.connect(lambda _: self._function_panel_finished(panel))
        return panel, heading, layout, actions

    def _show_function_panel(self, panel) -> None:
        if panel is not self.hole_dialog:
            self._set_hole_visualizer_open(False)
        if panel is not self.remember_dialog:
            self._set_remember_visualizer_open(False)
        self.active_panel = panel
        self.drawer_pages.setCurrentWidget(panel)
        self.drawer_pages.show()
        panel.show()
        panel.setFocus(Qt.FocusReason.OtherFocusReason)
        self.drawer_animation.stop()
        self.drawer_animation.setStartValue(self.drawer.width())
        self.drawer_animation.setEndValue(self._drawer_width() + 16)
        self.drawer_animation.start()

    def _function_panel_finished(self, panel) -> None:
        if panel is self.remember_dialog:
            self._set_remember_visualizer_open(False)
        if panel is self.hole_dialog:
            self._set_hole_visualizer_open(False)
            self.finish_hole_circle()
        if self.active_panel is not panel:
            return
        self.active_panel = None
        self.drawer_animation.stop()
        self.drawer_pages.hide()
        self._set_drawer_width(0)
        (self.hole_button if panel is self.hole_dialog else self.remember_button).setFocus()

    def show_remember_point(self):
        if self.fast_busy or self.stopping or self.closing or self.pages.currentWidget() is not self.readout_page:
            return
        if self.remember_dialog is None:
            panel, _, layout, actions = self._create_function_panel('Remember point')
            self.remember_dialog = panel
            self.remember_visualizer_button = QPushButton('Visualizer')
            self.remember_visualizer_button.setCheckable(True)
            self.remember_visualizer_button.setAutoDefault(False)
            self.remember_visualizer_button.setMinimumHeight(44)
            self.remember_visualizer_button.setAccessibleName('Show remembered-points visualizer')
            self.remember_visualizer_button.setToolTip('Show saved points in 3D; close to see the scales and main controls')
            self.remember_visualizer_button.toggled.connect(self._set_remember_visualizer_open)
            toolbar = QHBoxLayout()
            toolbar.addWidget(QLabel('Saved positions'), 1)
            toolbar.addWidget(self.remember_visualizer_button)
            panel.layout().insertLayout(1, toolbar)
            description = QLabel('Save a position, then select its point to show distances on the axes.')
            description.setStyleSheet('font-weight: 400;')
            description.setWordWrap(True)
            layout.addWidget(description)
            self.remember_list = QVBoxLayout()
            self.remember_list.setSpacing(8)
            layout.addLayout(self.remember_list)
            assets = Path(__file__).resolve().parents[1] / 'assets'
            self.origin_icons = {None: QIcon(), True: QIcon(str(assets / 'lock.svg')),
                                 False: QIcon(str(assets / 'unlock.svg'))}
            self._add_remember_control()
            for point in self.remembered_points:
                self._make_remember_card(point)
            self.remember_status = QLabel()
            self.remember_status.setWordWrap(True)
            self.remember_status.setStyleSheet('font-size: 12px; color: #344b5c; font-weight: 400;')
            layout.addWidget(self.remember_status)
            layout.addStretch(1)
            self.remember_clear = QPushButton('Hide distances')
            self.remember_clear.setMinimumHeight(44)
            self.remember_clear.setAutoDefault(False)
            self.remember_clear.clicked.connect(lambda: self.select_remembered_point(None))
            actions.addWidget(self.remember_clear)
            self._create_remember_visualizer()
        self._update_remember_cards()
        self._update_remember_controls()
        self._show_function_panel(self.remember_dialog)

    def _add_remember_control(self):
        self.remember_capture = QPushButton('Remember this point')
        self.remember_capture.setMinimumHeight(56)
        self.remember_capture.setDefault(True)
        self.remember_capture.clicked.connect(self.remember_point)
        self.remember_list.addWidget(self.remember_capture)

    def _can_remember_point(self):
        return (self.worker is not None and self.connection_ready and not self.stopping and not self.closing
                and not self.fast_busy and not self.error_text and self.displayed is not None
                and all(value is not None and math.isfinite(value)
                        for card, value in zip(self.axis_cards, self.displayed) if not card.isHidden()))

    def remember_point(self):
        if not self._can_remember_point() or self.remember_dialog is None:
            return
        # Store the displayed sample, not a newer mailbox sample the operator hasn't seen.
        used = {point['number'] for point in self.remembered_points}
        number = next(n for n in range(1, len(used) + 2) if n not in used)
        scale = 25.4 if self.units_button.isChecked() else 1.0
        point = dict(number=number, raw=self.displayed,
                     captured=tuple((card.name.text(), card.reading * scale)
                                    for card in self.axis_cards if not card.isHidden()))
        self.remembered_points.append(point)
        self._make_remember_card(point)
        self._save_remembered_points()
        self._update_remember_cards()
        self._update_remember_controls()
        self.remember_capture.setFocus()
        # Let Qt lay out the converted card before scrolling the new capture control into view.
        QTimer.singleShot(0, lambda: self.remember_dialog.findChild(QScrollArea).ensureWidgetVisible(self.remember_capture))

    def _make_remember_card(self, point):
        button = point['button'] = self.remember_capture
        button.clicked.disconnect(self.remember_point)
        button.setText('')
        button.setEnabled(True)
        button.setCheckable(True)
        button.setDefault(False)
        button.setAutoDefault(True)
        button.setMinimumHeight(96)
        content = QVBoxLayout(button)
        content.setContentsMargins(10, 10, 10, 10)
        content.setSpacing(4)
        summary = point['summary'] = QGridLayout()
        summary.setHorizontalSpacing(8); summary.setVerticalSpacing(4)
        summary.setColumnStretch(0, 1)
        title = point['title'] = QLabel(f"Point {point['number']}")
        title.setStyleSheet('font-size: 16px; font-weight: 800; background: transparent;')
        heading = QHBoxLayout()
        heading.setSpacing(4)
        heading.addWidget(title)
        origin = point['origin_button'] = QPushButton('Set Origin')
        origin.setFixedHeight(24)
        origin.setStyleSheet('QPushButton { font-size: 11px; font-weight: 800; border-radius: 12px; '
                            'padding: 0 8px; color: #174b63; background: #dce9f1; border: 1px solid #708491; } '
                            'QPushButton:hover { background: #cfe3ef; } '
                            'QPushButton:focus { border-color: #24566e; } '
                            'QPushButton:disabled { color: #4f606d; background: #e1e7ec; }')
        origin.setAutoDefault(True)
        origin.clicked.connect(lambda: self.set_point_origin(point))
        heading.addWidget(origin)
        heading.addStretch(1)
        summary.addLayout(heading, 0, 0, 1, 2)
        delete = point['delete'] = QPushButton()
        delete.setIcon(QIcon(str(Path(__file__).resolve().parents[1] / 'assets' / 'trash.svg')))
        delete.setFixedSize(32, 32)
        delete.setAutoDefault(True)
        delete.setAccessibleName(f"Delete Point {point['number']}")
        delete.setToolTip(delete.accessibleName())
        delete.clicked.connect(lambda: self.delete_remembered_point(point))
        summary.addWidget(delete, 1, 1, Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignTop)
        detail = point['coordinates'] = QLabel()
        detail.setStyleSheet('font-size: 12px; color: #344b5c; font-weight: 400; background: transparent;')
        # Reserve the coordinate rows before the first render.
        detail.setText('\n'.join(name + '  0.0000 in' for name, _ in point['captured']))
        summary.addWidget(detail, 1, 0)
        offset_button = point['offset_button'] = QPushButton('Offset')
        offset_button.setStyleSheet('font-size: 11px; padding: 0 6px;')
        offset_button.setCheckable(True); offset_button.setAutoDefault(True)
        offset_button.setFixedSize(58, 32)
        offset_button.setAccessibleName(f"Offset Point {point['number']}")
        offset_button.toggled.connect(lambda opened: self._set_point_offset_open(point, opened))
        heading.addWidget(offset_button)
        chip = point['offset_chip'] = QLabel()
        chip.setStyleSheet('font-size: 11px; background: #10212f; color: white; border: 1px solid #344b5c; '
                          'border-radius: 5px; padding: 2px 6px;')
        chip.setFixedHeight(22)
        chip.hide()
        chips = QHBoxLayout()
        chips.setSpacing(6)
        was_origin = point['was_origin'] = QLabel('Was origin')
        was_origin.setStyleSheet('font-size: 11px; color: #795113; background: #fff3cf; '
                                'border: 1px solid #b18b46; border-radius: 11px; padding: 0 7px;')
        was_origin.setFixedHeight(22)
        opacity = QGraphicsOpacityEffect(was_origin)
        was_origin.setGraphicsEffect(opacity)
        fade = point['was_origin_animation'] = QPropertyAnimation(opacity, b'opacity', was_origin)
        fade.setDuration(20000)
        fade.setStartValue(1.0); fade.setEndValue(0.0)
        fade.setKeyValueAt(0.75, 1.0)  # Hold for 15 seconds, then fade over five seconds.
        fade.finished.connect(was_origin.hide)
        fade.finished.connect(lambda: self._resize_remember_card(point, animate=True))
        was_origin.hide()
        chips.addWidget(was_origin)
        chips.addWidget(chip)
        chips.addStretch(1)
        summary.addLayout(chips, 2, 0, 1, 2)
        for label in (title, detail, chip, was_origin):
            label.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        content.setSpacing(0)
        content.addLayout(summary)
        self._make_point_offset_editor(point, content)
        height_animation = point['height_animation'] = QVariantAnimation(button)
        height_animation.setDuration(220)
        height_animation.setEasingCurve(QEasingCurve.Type.OutCubic)
        height_animation.valueChanged.connect(lambda height: self._set_remember_card_height(point, int(height)))
        QWidget.setTabOrder(origin, offset_button)
        QWidget.setTabOrder(offset_button, delete)
        for widget in (title, detail, delete, offset_button, origin):
            widget.ensurePolished(); widget.show()
        self._resize_remember_card(point)
        button.clicked.connect(lambda checked: self.select_remembered_point(
            self.remembered_points.index(point) if checked else None))
        self._add_remember_control()

    def _resize_remember_card(self, point, *, animate=False):
        point['summary'].invalidate()
        point['button'].layout().activate()
        height = max(96, point['summary'].sizeHint().height() + 20)
        animation = point['height_animation']
        if animation.state() == QVariantAnimation.State.Running and animation.endValue() == height:
            return
        animation.stop()
        if animate and 'collapsed_height' in point and point['collapsed_height'] != height:
            with QSignalBlocker(animation):
                animation.setStartValue(point['collapsed_height'])
                animation.setEndValue(height)
            animation.start()
        else:
            self._set_remember_card_height(point, height)

    def _set_remember_card_height(self, point, height):
        point['collapsed_height'] = height
        self._position_point_offset(point, point['offset_editor'].maximumHeight())

    def _make_point_offset_editor(self, point, content):
        editor = point['offset_editor'] = QWidget(point['button'])
        editor.setMaximumHeight(0)
        layout = QVBoxLayout(editor)
        layout.setContentsMargins(0, 12, 0, 0); layout.setSpacing(8)
        layout.setSizeConstraint(QVBoxLayout.SizeConstraint.SetNoConstraint)
        axis_label = QLabel('Offset axis')
        axis_label.setStyleSheet('font-size: 12px; color: #344b5c; font-weight: 400; background: transparent;')
        layout.addWidget(axis_label)
        track = QFrame()
        track.setFixedHeight(44)
        track.setStyleSheet('QFrame { background: #e8eef2; border: 1px solid #708491; border-radius: 7px; } '
                            'QPushButton { border: none; background: transparent; color: #344b5c; font-size: 12px; font-weight: 800; padding: 0; } '
                            'QPushButton:checked { background: #174b63; color: white; } '
                            'QPushButton:disabled { color: #4f606d; background: #e1e7ec; }')
        segments = QHBoxLayout(track)
        segments.setContentsMargins(4, 4, 4, 4); segments.setSpacing(0)
        group = point['offset_axes'] = QButtonGroup(editor)
        for axis, card in enumerate(self.axis_cards):
            choice = QPushButton(card.name.text())
            choice.setCheckable(True); choice.setAutoDefault(True); choice.setFixedHeight(32)
            choice.setEnabled(not card.isHidden())
            choice.setAccessibleName(f"Point {point['number']} offset axis {card.name.text()}")
            group.addButton(choice, axis); segments.addWidget(choice, 1)
        group.button(0).setChecked(True)
        group.idClicked.connect(lambda _: self._update_point_offset_preview(point))
        layout.addWidget(track)
        direction_row = QHBoxLayout()
        direction_label = QLabel('Direction')
        direction_label.setStyleSheet('font-size: 12px; color: #344b5c; font-weight: 400; background: transparent;')
        direction_row.addWidget(direction_label, 1)
        direction = point['offset_direction'] = AxisToggle('+', '−', editor)
        direction.setAutoDefault(True)
        direction.setFixedSize(104, 36)
        direction.setAccessibleName(f"Point {point['number']} offset direction")
        direction.setToolTip('Positive or negative in the current axis direction; Space toggles')
        direction.toggled.connect(lambda _: self._update_point_offset_preview(point))
        direction_row.addWidget(direction); layout.addLayout(direction_row)
        preview = point['offset_preview'] = QLabel()
        preview.setStyleSheet('font-size: 12px; color: #344b5c; font-weight: 400; background: transparent;')
        preview.setWordWrap(True); layout.addWidget(preview)
        actions = QHBoxLayout()
        cancel = point['offset_cancel'] = QPushButton('Cancel')
        apply = point['offset_apply'] = QPushButton('Apply offset')
        apply.setStyleSheet('QPushButton:enabled { background: #07565f; color: white; }')
        for control in (cancel, apply):
            control.setAutoDefault(True); control.setMinimumHeight(36); actions.addWidget(control)
        cancel.clicked.connect(lambda: point['offset_button'].setChecked(False))
        apply.clicked.connect(lambda: self.apply_point_offset(point))
        layout.addLayout(actions)
        remove = point['offset_remove'] = QPushButton('Remove offset')
        remove.setAutoDefault(True); remove.setMinimumHeight(32)
        remove.clicked.connect(lambda: self.remove_point_offset(point))
        layout.addWidget(remove)
        content.addWidget(editor)
        editor.hide()
        animation = point['offset_animation'] = QVariantAnimation(point['button'])
        animation.setDuration(220); animation.setEasingCurve(QEasingCurve.Type.OutCubic)
        animation.valueChanged.connect(lambda height: self._position_point_offset(point, int(height)))
        animation.finished.connect(lambda: self._finish_point_offset_animation(point))

    def _position_point_offset(self, point, height):
        point['offset_editor'].setMaximumHeight(height)
        point['button'].setFixedHeight(point['collapsed_height'] + height)

    def _finish_point_offset_animation(self, point):
        opened = point['offset_button'].isChecked()
        point['offset_editor'].setVisible(opened)
        if opened and self.active_panel is self.remember_dialog:
            self.remember_dialog.findChild(QScrollArea).ensureWidgetVisible(point['offset_apply'])

    def _set_point_offset_open(self, point, opened):
        editor, animation = point['offset_editor'], point['offset_animation']
        animation.stop()
        if opened:
            offset = point.get('offset')
            axis = offset['axis'] if offset else 0
            point['offset_axes'].button(axis).setChecked(True)
            direction = -1 if self.sign_buttons[axis].isChecked() else 1
            point['offset_direction'].setChecked(offset is not None and offset['delta_mm'] * direction < 0)
            point['offset_remove'].setVisible(offset is not None)
            self._update_point_offset_preview(point)
            editor.show(); editor.setEnabled(True)
            point['offset_axes'].button(axis).setFocus()
        else:
            editor.setEnabled(False)
            point['offset_button'].setFocus()
        animation.setStartValue(editor.maximumHeight())
        height = editor.layout().totalHeightForWidth(max(1, point['button'].width() - 28))
        animation.setEndValue(max(height, editor.layout().sizeHint().height()) if opened else 0)
        animation.start()

    def _update_point_offset_preview(self, point):
        scale, precision, unit = (25.4, 4, 'in') if self.units_button.isChecked() else (1., 3, 'mm')
        radius = self.toolhead_config['width_mm'] / 2
        axis = point['offset_axes'].checkedId()
        direction = '−' if point['offset_direction'].isChecked() else '+'
        name = self.axis_cards[axis].name.text()
        text = f'{name} {direction}{radius / scale:.{precision}f} {unit} · ½ toolhead width'
        if self.profile.startswith('lathe') and axis == 0 and self.machine_config['diameter']:
            text += f'\nDiameter changes by {direction}{2 * radius / scale:.{precision}f} {unit}.'
        point['offset_preview'].setText(text)

    def apply_point_offset(self, point):
        if (self.fast_busy or self.stopping or self.closing or point not in self.remembered_points
                or point.get('origin_setup', point.get('point_setup', self.machine_config)) != self.machine_config):
            return
        axis = point['offset_axes'].checkedId()
        if axis < 0 or self.axis_cards[axis].isHidden():
            return
        base = point.get('offset', point)
        delta = self.toolhead_config['width_mm'] / 2
        delta *= -1 if point['offset_direction'].isChecked() else 1
        delta *= -1 if self.sign_buttons[axis].isChecked() else 1
        raw = list(base['raw']); raw[axis] += delta
        captured = tuple((card.name.text(), self._coordinate_mm(i, raw[i]))
                         for i, card in enumerate(self.axis_cards) if not card.isHidden())
        if raw[axis] == base['raw'][axis] or not all(math.isfinite(value) for _, value in captured):
            self.remember_error = 'This offset cannot be represented at the saved coordinate. The point is unchanged.'
            self._update_remember_controls()
            return
        point['offset'] = dict(axis=axis, delta_mm=delta, raw=base['raw'], captured=base['captured'])
        point['raw'], point['captured'] = tuple(raw), captured
        self._save_remembered_points()
        self._update_remember_cards(); self._update_point_distances()
        point['offset_button'].setChecked(False)

    def remove_point_offset(self, point):
        if (self.fast_busy or self.stopping or self.closing or point not in self.remembered_points or 'offset' not in point
                or point.get('origin_setup', point.get('point_setup', self.machine_config)) != self.machine_config):
            return
        base = point.pop('offset')
        point['raw'], point['captured'] = base['raw'], base['captured']
        self._save_remembered_points()
        self._update_remember_cards(); self._update_point_distances()
        point['offset_button'].setChecked(False)

    def set_point_origin(self, point):
        if point not in self.remembered_points or self.fast_busy or self.stopping or self.closing:
            return
        origin = next((saved for saved in self.remembered_points if 'origin_setup' in saved), None)
        if point is origin:
            point['origin_locked'] = not point.get('origin_locked', True)
        else:
            if origin is not None:
                if origin.get('origin_locked', True):
                    return
                for saved in self.remembered_points:
                    fade = saved['was_origin_animation']
                    if not saved['was_origin'].isHidden() and fade.currentTime() < 15000:
                        fade.setCurrentTime(15000)
                used = {saved['number'] for saved in self.remembered_points}
                origin['number'] = next(n for n in range(1, len(used) + 2) if n not in used)
                # Keep the former Origin's mapping, too: its coordinates must not be reinterpreted.
                origin['point_setup'] = origin.pop('origin_setup')
                origin.pop('origin_locked', None)
                origin['was_origin_animation'].stop()
                origin['was_origin'].graphicsEffect().setOpacity(1.0)
                origin['was_origin'].show()
                origin['was_origin_animation'].start()
            point['origin_setup'] = point.pop('point_setup', dict(self.remembered_setup))
            point['origin_locked'] = True
            point['was_origin_animation'].stop()
            point['was_origin'].hide()
        self._save_remembered_points()
        self._update_remember_cards(animate=True)
        self._update_point_distances()

    def delete_remembered_point(self, point):
        index = self.remembered_points.index(point)
        self.remembered_points.pop(index)
        self.remember_list.removeWidget(point['button'])
        point['button'].hide()
        point['button'].deleteLater()
        selected = self.selected_point
        if selected is not None:
            selected = None if selected == index else selected - (selected > index)
        self._save_remembered_points()
        self.select_remembered_point(selected)
        self.remember_capture.setFocus()

    def _load_remembered_points(self):
        saved = self.machine_settings.value('remembered_points')
        if saved is None:
            return
        try:
            saved = json.loads(saved)
            validate_setup(saved['setup'])
            points, used = saved['points'], set()
            if not isinstance(points, list):
                raise ValueError('Invalid point list')
            origin_found = False
            for point in points:
                if not isinstance(point, dict):
                    raise ValueError('Invalid point record')
                config = point.get('origin_setup', point.get('point_setup', saved['setup']))
                if 'point_setup' in point:
                    validate_setup(point['point_setup'])
                    if 'origin_setup' in point:
                        raise ValueError('Conflicting point setup')
                if 'origin_locked' in point and ('origin_setup' not in point or type(point['origin_locked']) is not bool):
                    raise ValueError('Invalid Origin lock')
                if 'origin_setup' in point:
                    validate_setup(config)
                    if origin_found:
                        raise ValueError('Only one Origin is allowed')
                    origin_found = True
                count = 3 if (config['profile'] in ('mill3', 'mill3q') or
                              (config['profile'] == 'lathe3' and not config['summed'])) else 2
                number, raw, captured = point['number'], point['raw'], point['captured']
                if type(number) is not int or number < 1 or number in used:
                    raise ValueError('Invalid point number')
                used.add(number)
                offset = point.get('offset')
                if 'offset' in point and not isinstance(offset, dict):
                    raise ValueError('Invalid point offset')
                for record in (point, offset) if offset is not None else (point,):
                    raw, captured = record['raw'], record['captured']
                    if (not isinstance(raw, list) or len(raw) != 3
                            or any(v is not None and (type(v) not in (int, float) or not math.isfinite(v)) for v in raw)
                            or any(raw[i] is None for i in range(count))
                            or not isinstance(captured, list) or len(captured) != count):
                        raise ValueError('Invalid point coordinates')
                    for coordinate in captured:
                        if (not isinstance(coordinate, list) or len(coordinate) != 2
                                or coordinate[0] not in ('X', 'Y', 'Z', 'Z1', 'Z2', 'Ø X', 'R X')
                                or type(coordinate[1]) not in (int, float) or not math.isfinite(coordinate[1])):
                            raise ValueError('Invalid captured coordinate')
                if offset is not None:
                    axis, delta = offset['axis'], offset['delta_mm']
                    if (type(axis) is not int or not 0 <= axis < count or type(delta) not in (int, float)
                            or not 0 < abs(delta) <= 500):
                        raise ValueError('Invalid point offset')
                    expected = list(offset['raw']); expected[axis] += delta
                    if point['raw'] != expected or expected[axis] == offset['raw'][axis]:
                        raise ValueError('Inconsistent point offset')
            self.remembered_setup = saved['setup']
            self.remembered_points = [{key: point[key] for key in ('number', 'raw', 'captured', 'offset', 'origin_setup', 'origin_locked', 'point_setup') if key in point} for point in points]
        except (ValueError, TypeError, KeyError, OverflowError):
            self.remember_error = 'Could not load saved points. The saved data has been left unchanged.'

    def _save_remembered_points(self):
        points = [{key: point[key] for key in ('number', 'raw', 'captured', 'offset', 'origin_setup', 'origin_locked', 'point_setup') if key in point} for point in self.remembered_points]
        self.machine_settings.setValue('remembered_points', json.dumps(dict(setup=self.remembered_setup, points=points)))
        self.machine_settings.sync()
        self.remember_error = ('' if self.machine_settings.status() == QSettings.Status.NoError else
                               'Could not save points. Changes are kept in this window only.')
        self._update_remember_controls()

    def _update_remember_cards(self, *, animate=False):
        if self.remember_dialog is None:
            return
        inches = self.units_button.isChecked()
        scale, precision, unit = (25.4, 4, 'in') if inches else (1.0, 3, 'mm')
        for point in self.remembered_points:
            name = 'Origin' if 'origin_setup' in point else f'Point {point["number"]}'
            compatible = point.get('origin_setup', point.get('point_setup', self.machine_config)) == self.machine_config
            point['title'].setText(name)
            point['delete'].setAccessibleName(f'Delete {name}')
            point['delete'].setToolTip(point['delete'].accessibleName())
            point['offset_button'].setAccessibleName(f'Offset {name}')
            point['offset_direction'].setAccessibleName(f'{name} offset direction')
            for axis, card in enumerate(self.axis_cards):
                control = point['offset_axes'].button(axis)
                control.setText(card.name.text())
                control.setAccessibleName(f'{name} offset axis {card.name.text()}')
                control.setEnabled(compatible and not card.isHidden())
            text = '\n'.join(f'{name}  {value / scale:.{precision}f} {unit}' for name, value in point['captured'])
            point['coordinates'].setText(text)
            point['button'].setAccessibleName(f'{name}: {text.replace(chr(10), ", ")}')
            offset = point.get('offset')
            point['offset_chip'].setVisible(offset is not None and compatible)
            if offset is not None and compatible:
                axis = offset['axis']
                delta = offset['delta_mm'] * (-1 if self.sign_buttons[axis].isChecked() else 1)
                if self.profile.startswith('lathe') and axis == 0 and self.machine_config['diameter']:
                    delta *= 2
                chip = f"Offset · {self.axis_cards[axis].name.text()} {delta / scale:+.{precision}f} {unit}"
                point['offset_chip'].setText(chip)
                point['button'].setAccessibleName(point['button'].accessibleName() + ', ' + chip)
            point['button'].setToolTip('Coordinates at capture or offset application. Select to show current − saved distance; select again to hide.'
                                       if compatible else f'{name} uses a different scale setup. Restore its setup to use distances and offsets.')
            self._update_point_offset_preview(point)
            self._resize_remember_card(point, animate=animate)
        self._update_remember_visualizer()

    def select_remembered_point(self, index):
        self.selected_point = index
        if self.remember_dialog is not None:
            for i, point in enumerate(self.remembered_points):
                point['button'].setChecked(i == index)
        self._update_point_distances()
        self._update_remember_controls()
        self._update_remember_visualizer()

    def _update_point_distances(self):
        point = self.remembered_points[self.selected_point] if self.selected_point is not None else None
        available = (self.worker is not None and self.connection_ready and not self.stopping
                     and not self.fast_busy and not self.error_text and not self.closing)
        if point is not None and point.get('origin_setup', point.get('point_setup', self.machine_config)) != self.machine_config:
            available = False
        for index, card in enumerate(self.axis_cards):
            value = self.displayed[index] if available and self.displayed is not None else None
            saved = point['raw'][index] if point is not None else None
            distance = None if value is None or saved is None else value - saved
            if distance is not None:
                distance *= -1 if card.sign.isChecked() else 1
                distance /= 25.4 if self.units_button.isChecked() else 1.0
                if self.profile.startswith('lathe') and index == 0 and self.machine_config['diameter']:
                    distance *= 2
            card.set_distance(point['number'] if point is not None else None, distance,
                              origin=point is not None and 'origin_setup' in point)

    def _update_remember_controls(self):
        self.remember_button.setEnabled(not self.fast_busy and not self.stopping and not self.closing)
        if self.remember_dialog is None:
            return
        available = self._can_remember_point()
        self.remember_capture.setEnabled(available)
        self.remember_clear.setEnabled(self.selected_point is not None)
        origin = next((point for point in self.remembered_points if 'origin_setup' in point), None)
        locked = origin is not None and origin.get('origin_locked', True)
        editable = not self.fast_busy and not self.stopping and not self.closing
        for point in self.remembered_points:
            compatible = point.get('origin_setup', point.get('point_setup', self.machine_config)) == self.machine_config
            point['offset_button'].setEnabled(editable and compatible)
            point['offset_apply'].setEnabled(editable and compatible)
            point['offset_remove'].setEnabled(editable and compatible)
            control = point['origin_button']
            control.setText('' if point is origin else 'Set Origin')
            icon = self.origin_icons[locked if point is origin else None]
            if control.icon().cacheKey() != icon.cacheKey():
                control.setIcon(icon)
            control.setEnabled(editable and (point is origin or not locked))
            control.setAccessibleName(('Unlock Origin' if locked else 'Lock Origin') if point is origin else
                                      f'Set Point {point["number"]} as Origin')
            control.setToolTip(('Unlock Origin to choose another point.' if locked else 'Lock Origin to prevent replacement.')
                               if point is origin else 'Unlock Origin before choosing another point.' if locked else
                               'Make this point the locked Origin; keep the previous Origin as a numbered point.')
        message = ('Coordinates at capture or offset application; distances follow current direction and units. '
                   'Points are saved between launches. Scale setup changes clear ordinary points; '
                   'Origin clears when deleted or the machine profile changes.')
        if any(point.get('origin_setup', point.get('point_setup', self.machine_config)) != self.machine_config
               for point in self.remembered_points):
            message = 'Some points use a different scale setup. Restore their setup to use distances, offsets, and 3D markers.\n\n' + message
        if self.remember_error:
            message = self.remember_error + '\n\n' + message
        if not available:
            message = 'Connect and wait for all displayed axes before remembering a point.\n\n' + message
        self.remember_status.setText(message)

    def _clear_remembered_points(self, *, keep_origin):
        if not self.remembered_points:
            return  # Do not overwrite unreadable saved data when there are no loaded points to clear.
        for point in self.remembered_points:
            if keep_origin and 'origin_setup' in point:
                if 'button' in point:
                    point['offset_button'].setChecked(False)
                continue
            if 'button' in point:
                button = point['button']
                self.remember_list.removeWidget(button)
                button.hide()
                button.deleteLater()
        self.remembered_points = [point for point in self.remembered_points if keep_origin and 'origin_setup' in point]
        self.selected_point = None
        self._save_remembered_points()
        self._update_point_distances()
        self._update_remember_controls()
        self._update_remember_visualizer()

    def _create_remember_visualizer(self):
        panel = self.remember_visualizer = QFrame(self.readouts)
        panel.setObjectName('rememberVisualizer')
        panel.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(8)
        header = QHBoxLayout()
        title = QLabel('Remembered points · 3D')
        title.setWordWrap(True)
        title.setStyleSheet('font-size: 20px; font-weight: 800;')
        header.addWidget(title, 1)
        self.remember_visualizer_reset = QPushButton('Reset view')
        self.remember_visualizer_reset.setMinimumHeight(44)
        self.remember_visualizer_reset.setAutoDefault(False)
        header.addWidget(self.remember_visualizer_reset)
        self.remember_visualizer_close = QPushButton('×')
        self.remember_visualizer_close.setFixedSize(44, 44)
        self.remember_visualizer_close.setAccessibleName('Close remembered-points visualizer')
        self.remember_visualizer_close.clicked.connect(lambda: self._set_remember_visualizer_open(False))
        header.addWidget(self.remember_visualizer_close)
        layout.addLayout(header)
        self.remember_visualizer_summary = QLabel()
        self.remember_visualizer_summary.setStyleSheet('font-weight: 400;')
        self.remember_visualizer_summary.setWordWrap(True)
        layout.addWidget(self.remember_visualizer_summary)
        self.remember_plot = RememberPointPlot()
        self.remember_plot.point_selected.connect(lambda number: self.select_remembered_point(
            next((i for i, point in enumerate(self.remembered_points) if point['number'] == number), None)))
        self.remember_visualizer_reset.clicked.connect(self.remember_plot.reset_view)
        layout.addWidget(self.remember_plot, 1)
        self.remember_visualizer_selection = QLabel()
        self.remember_visualizer_selection.setWordWrap(True)
        layout.addWidget(self.remember_visualizer_selection)
        note = QLabel('Drag to rotate · Scroll to zoom · Click a point to select\n'
                      'Enter to use keys · / to leave\nArrow keys rotate · +/− zoom · Home resets\n'
                      'Zero and ABS/REL do not move saved points.')
        note.setWordWrap(True)
        note.setStyleSheet('color: #344b5c; font-weight: 400; font-size: 12px;')
        layout.addWidget(note)
        shortcut = QShortcut(QKeySequence(Qt.Key.Key_Escape), panel)
        shortcut.setContext(Qt.ShortcutContext.WidgetWithChildrenShortcut)
        shortcut.activated.connect(lambda: self._set_remember_visualizer_open(False))
        self.remember_visualizer_animation = QVariantAnimation(self)
        self.remember_visualizer_animation.setDuration(220)
        self.remember_visualizer_animation.setEasingCurve(QEasingCurve.Type.OutCubic)
        self.remember_visualizer_animation.valueChanged.connect(self._position_remember_visualizer)
        self.remember_visualizer_animation.finished.connect(self._finish_remember_visualizer_animation)
        panel.hide()

    def _finish_remember_visualizer_animation(self):
        self.remember_visualizer.setVisible(self.remember_visualizer_button.isChecked())
        self._update_visualizer_controls()

    def _update_visualizer_controls(self):
        self.controls.setVisible(not any(panel is not None and not panel.isHidden()
                                         for panel in (self.hole_visualizer, self.remember_visualizer)))

    def _set_remember_visualizer_open(self, opened):
        if self.remember_visualizer is None:
            return
        opened = opened and self.active_panel is self.remember_dialog
        with QSignalBlocker(self.remember_visualizer_button):
            self.remember_visualizer_button.setChecked(opened)
        self.remember_visualizer_animation.stop()
        if opened:
            self.controls.hide()
            self._update_remember_visualizer()
            self.remember_visualizer.show()
            self.remember_visualizer.raise_()
        self.remember_visualizer_animation.setStartValue(self.remember_visualizer_progress_value)
        self.remember_visualizer_animation.setEndValue(1.0 if opened else 0.0)
        self.remember_visualizer_animation.start()

    def _position_remember_visualizer(self, progress):
        self.remember_visualizer_progress_value = progress
        if self.remember_visualizer is not None:
            width = self.readouts.width()
            self.remember_visualizer.setGeometry(round((progress - 1) * width), 0, width, self.readouts.height())

    def _update_remember_visualizer(self):
        if self.remember_visualizer is None:
            return
        axes = tuple(card.name.text() for card in self.axis_cards if not card.isHidden())
        factors = [-1 if card.sign.isChecked() else 1 for card in self.axis_cards]
        if self.profile.startswith('lathe') and self.machine_config['diameter']:
            factors[0] *= 2
        points = {point['number']: tuple(point['raw'][i] * factors[i] if i < len(axes) else 0. for i in range(3))
                  for point in self.remembered_points if point.get('origin_setup', point.get('point_setup', self.machine_config)) == self.machine_config}
        selected = self.remembered_points[self.selected_point] if self.selected_point is not None else None
        origin = next((point['number'] for point in self.remembered_points if 'origin_setup' in point), None)
        self.remember_plot.set_points(points, selected['number'] if selected and selected['number'] in points else None, axes, origin)
        scale, precision, unit = (25.4, 4, 'in') if self.units_button.isChecked() else (1., 3, 'mm')
        summary = f'{len(points)} saved points · Equal axis scale'
        if points:
            summary += '\nSpan: ' + ' · '.join(
                f'{axis} {(max(p[i] for p in points.values()) / scale - min(p[i] for p in points.values()) / scale):.{precision}f}'
                for i, axis in enumerate(axes)) + f' {unit}'
        if len(axes) == 2:
            summary += '\nTwo-axis setup: points lie in a plane.'
        if len(points) != len(self.remembered_points):
            summary += '\nPoints saved with a different scale setup are not plotted.'
        self.remember_visualizer_summary.setText(summary)
        name = 'Origin' if selected and 'origin_setup' in selected else f'Point {selected["number"]}' if selected else ''
        self.remember_visualizer_selection.setText(
            f'{name} selected · Restore its scale setup to show distances.' if selected and selected['number'] not in points else
            f'{name} selected · Close the view to see its distances on the axes.' if selected else
            'Select a marker or a point card to show its distances.')

    def _set_fast_banner_height(self, height) -> None:
        self.fast_banner.setFixedHeight(int(height))
        # Reserve the strip without clipping the three axis control stacks at 850 × 600.
        self.work_area.layout().setContentsMargins(16, 0 if height else 8, 16, 8 if height else 16)
        self.readouts.layout().setSpacing(6 if height else 12)

    def _update_fast_controls(self) -> None:
        hardware = (self.worker is not None and not self.worker.demo and self.connection_ready
                    and not self.stopping and not self.closing)
        cadence = self.latest.snapshot().cadence
        fast = bool(cadence) and all(state == 'fast' for _, state, _ in cadence)
        slow = any(state == 'slow' for _, state, _ in cadence)
        self.fast_indicator.setVisible(hardware and fast and not self.fast_busy)
        detail = ', '.join(f'Channel {channel}: {rate:.1f} samples/s ({state})' for channel, state, rate in cadence)
        self.fast_indicator.setToolTip('Last measured pace of detected inputs. Idle scales retain their last result.\n' + detail)
        available = hardware and slow and not self.fast_busy and not self.fast_attempted
        self.fast_apply_button.setEnabled(available)
        self.fast_apply_button.setVisible(available)
        self.fast_cancel_button.setVisible(self.fast_busy)
        self.fast_cancel_button.setEnabled(self.fast_busy and not self.stopping)
        if self.fast_busy:
            message = self.connection_status
        elif hardware and slow:
            message = ('Readings remain slow — check scales and connection.' if self.fast_attempted else
                       'Slow readings — keep still; enabling Fast mode may reset zeros.')
        else:
            message = self.fast_message if self.worker is None else ''
        self.fast_status.setText(message)
        self.fast_status.setToolTip(message + ('\n' + detail if hardware else ''))
        opened = bool(message)
        if opened != self.fast_banner_open:
            self.fast_banner_open = opened
            self.fast_animation.stop()
            self.fast_animation.setStartValue(self.fast_banner.height())
            self.fast_animation.setEndValue(56 if opened else 0)
            self.fast_animation.start()

    @pyqtSlot()
    def apply_fast_mode(self) -> None:
        self._update_fast_controls()
        if not self.fast_apply_button.isEnabled():
            return
        self.fast_busy = self.fast_attempted = True
        self.connection_status = "Fast mode: waiting for serial worker…"
        self.finish_hole_circle("Fast-mode setup ended the hole circle. Re-establish the datum afterward.")
        self.displayed = None
        self.last_drawn = -1
        for card in self.axis_cards:
            card.set_reading(None, self.units_button.isChecked())
        self.worker.fast_requested.set()
        self.refresh_display()

    @pyqtSlot()
    def on_fast_mode_changing(self) -> None:
        self.clear_tool_path()
        self.offset = self.relative_offset = (0.0, 0.0, 0.0)
        self.datum_message = 'Fast-mode pulses sent — verify and re-establish the datum'

    @pyqtSlot()
    def on_fast_mode_finished(self) -> None:
        self.fast_busy = False
        self.fast_message = ""
        if not self.stopping and not self.closing:
            self.connect_button.setText("Disconnect")
        self.refresh_display()

    def _drawer_width(self) -> int:
        if self.remember_dialog is not None and self.active_panel is self.remember_dialog:
            return min(340, round(self.centralWidget().width() * .34))
        return min(520, round(self.centralWidget().width() * .43))

    def _set_drawer_width(self, width: int) -> None:
        # One animated reservation moves the drawer and reflows the readouts together.
        self.drawer.setFixedWidth(width)
        self._layout_main_actions()
        self.work_area.layout().activate()
        self.drawer_pages.setGeometry(16, 0, self._drawer_width(), self.drawer.height())

    def _create_hole_visualizer(self) -> None:
        panel = self.hole_visualizer = QFrame(self.readouts)
        panel.setObjectName("holeVisualizer")
        panel.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(8)
        header = QHBoxLayout()
        self.hole_visualizer_title = QLabel("Circle preview")
        self.hole_visualizer_title.setStyleSheet("font-size: 20px; font-weight: 800;")
        header.addWidget(self.hole_visualizer_title, 1)
        self.hole_visualizer_close = QPushButton("×")
        self.hole_visualizer_close.setFixedSize(44, 44)
        self.hole_visualizer_close.setAccessibleName("Close circle visualizer")
        self.hole_visualizer_close.clicked.connect(lambda: self._set_hole_visualizer_open(False))
        header.addWidget(self.hole_visualizer_close)
        layout.addLayout(header)
        self.hole_visualizer_summary = QLabel()
        self.hole_visualizer_summary.setStyleSheet('font-weight: 400;')
        self.hole_visualizer_summary.setWordWrap(True)
        layout.addWidget(self.hole_visualizer_summary)
        self.hole_plot = HoleCirclePlot()
        layout.addWidget(self.hole_plot, 1)
        self.hole_visualizer_progress = QLabel()
        self.hole_visualizer_progress.setWordWrap(True)
        self.hole_visualizer_progress.setStyleSheet("font-size: 16px; font-weight: 800;")
        layout.addWidget(self.hole_visualizer_progress)
        self.hole_visualizer_target = QLabel()
        self.hole_visualizer_target.setWordWrap(True)
        layout.addWidget(self.hole_visualizer_target)
        self.hole_visualizer_legend = QLabel('◎ Current &nbsp; <span style="color:#206356">● Visited</span> &nbsp; ○ Unvisited')
        self.hole_visualizer_legend.setStyleSheet('font-weight: 400;')
        self.hole_visualizer_legend.setWordWrap(True)
        layout.addWidget(self.hole_visualizer_legend)
        note = QLabel("Centered on the circle · +Y up\nVisited means selected, not drilled.")
        note.setWordWrap(True)
        note.setStyleSheet("color: #344b5c; font-weight: 400; font-size: 12px;")
        layout.addWidget(note)
        shortcut = QShortcut(QKeySequence(Qt.Key.Key_Escape), panel)
        shortcut.setContext(Qt.ShortcutContext.WidgetWithChildrenShortcut)
        shortcut.activated.connect(lambda: self._set_hole_visualizer_open(False))
        self.hole_visualizer_animation = QVariantAnimation(self)
        self.hole_visualizer_animation.setDuration(220)
        self.hole_visualizer_animation.setEasingCurve(QEasingCurve.Type.OutCubic)
        self.hole_visualizer_animation.valueChanged.connect(self._position_hole_visualizer)
        self.hole_visualizer_animation.finished.connect(self._finish_hole_visualizer_animation)
        panel.hide()

    def _finish_hole_visualizer_animation(self) -> None:
        opened = self.hole_visualizer_button.isChecked()
        self.hole_visualizer.setVisible(opened)
        self._update_visualizer_controls()

    def _set_hole_visualizer_open(self, opened: bool) -> None:
        if self.hole_visualizer is None:
            return
        opened = opened and self.active_panel is self.hole_dialog
        with QSignalBlocker(self.hole_visualizer_button):
            self.hole_visualizer_button.setChecked(opened)
        self.hole_visualizer_animation.stop()
        if opened:
            self.controls.hide()
            self._update_hole_visualizer()
            self.hole_visualizer.show()
            self.hole_visualizer.raise_()
        self.hole_visualizer_animation.setStartValue(self.hole_visualizer_progress_value)
        self.hole_visualizer_animation.setEndValue(1.0 if opened else 0.0)
        self.hole_visualizer_animation.start()

    def _position_hole_visualizer(self, progress: float) -> None:
        self.hole_visualizer_progress_value = progress
        if self.hole_visualizer is not None:
            width = self.readouts.width()
            self.hole_visualizer.setGeometry(round((progress - 1) * width), 0, width, self.readouts.height())

    def _update_hole_visualizer(self) -> None:
        if self.hole_visualizer is None:
            return
        circle = self.hole_circle
        index, visited = (circle["index"], circle["visited"]) if circle else (0, set())
        params = dict(circle["params"]) if circle else {name: field.value() for name, field in self.hole_inputs.items()}
        if not circle:
            for name in ("center_x", "center_y", "diameter"):
                params[name] *= self.hole_input_scale
        error = ""
        try:
            if not circle and any(not field.hasAcceptableInput() for field in self.hole_inputs.values()):
                raise ValueError("Enter valid numbers to preview the circle.")
            target = hole_circle_target(**params, index=index)
        except ValueError as exc:
            params, error = None, str(exc)
        scale = 25.4 if self.units_button.isChecked() else 1.0
        state = (tuple(params.items()) if params else None, index, len(visited), bool(circle), scale, error)
        if state == self.hole_visualizer_state:
            return
        self.hole_visualizer_state = state
        self.hole_plot.set_circle(params, index, visited)
        self.hole_visualizer_title.setText("Circle progress" if circle else "Circle preview")
        self.hole_visualizer_legend.setVisible(bool(circle))
        if params is None:
            self.hole_visualizer_summary.setText(error)
            self.hole_plot.setAccessibleDescription(error)
            self.hole_visualizer_progress.clear()
            self.hole_visualizer_target.clear()
            return
        unit, precision = ("in", 4) if scale == 25.4 else ("mm", 3)
        def length(value):
            value /= scale
            return f"{0.0 if round(value, precision) == 0 else value:.{precision}f}"
        summary = (f"Ø {length(params['diameter'])} {unit} · {params['holes']} holes · {params['angle']:g}°\n"
                   f"Center X {length(params['center_x'])} · Y {length(params['center_y'])} {unit}")
        if len(self.hole_plot.points) < params["holes"]:
            summary += f"\nDense pattern: {len(self.hole_plot.points)} representative markers"
        self.hole_visualizer_summary.setText(summary)
        progress = f"Hole {index + 1} of {params['holes']}"
        if circle:
            progress += f" · {len(visited)} of {params['holes']} visited"
        self.hole_visualizer_progress.setText(progress)
        self.hole_visualizer_target.setText(f"Target X {length(target[0])} · Y {length(target[1])} {unit}")
        self.hole_plot.setAccessibleDescription(summary + ". " + progress + ". " + self.hole_visualizer_target.text())

    @pyqtSlot()
    def show_hole_circle(self) -> None:
        if self.fast_busy or self.profile.startswith('lathe') or (hasattr(self, 'pages') and self.pages.currentWidget() is not self.readout_page):
            return
        self.select_remembered_point(None)
        if self.hole_dialog is None:
            dialog, self.hole_title, layout, actions = self._create_function_panel("Lay out holes")
            self.hole_dialog = dialog
            self.hole_scroll = dialog.findChild(QScrollArea)
            self.hole_scroll.viewport().installEventFilter(self)
            dialog.setStyleSheet("""
                QLabel#holeHelp, QLabel#holeCaption { font-size: 15px; color: #344b5c; font-weight: 400; }
                QWidget#holeForm QLabel { font-size: 22px; }
                QDoubleSpinBox, QSpinBox { font-size: 26px; }
                QLabel#holeCount { font-size: 24px; font-weight: 800; }
                QLabel#holeUnit { font-size: 20px; color: #24566e; }
                QLabel#holeStatus { font-size: 16px; font-weight: 400; }
                QPushButton { font-size: 18px; }
                QPushButton#holePrimary { background: #174b63; color: white; border-color: #174b63; }
                QPushButton#holePrimary:hover { background: #10384d; }
                QPushButton#holePrimary:disabled { background: #e1e7ec; color: #4f606d; border-color: #708491; }
                QPushButton#holePrimary:focus { border-color: #84b9d0; }
            """)
            self.hole_help = QLabel("XY plane · 0° = −Y · 90° = −X")
            self.hole_help.setObjectName("holeHelp")
            self.hole_help.setWordWrap(True)
            self.hole_visualizer_button = QPushButton("Visualizer")
            self.hole_visualizer_button.setCheckable(True)
            self.hole_visualizer_button.setAutoDefault(False)
            self.hole_visualizer_button.setMinimumHeight(44)
            self.hole_visualizer_button.setStyleSheet("font-size: 14px;")
            self.hole_visualizer_button.setAccessibleName("Show hole-circle visualizer")
            self.hole_visualizer_button.setToolTip("Show the circle over the readouts; close it to see the scales and main controls")
            self.hole_visualizer_button.toggled.connect(self._set_hole_visualizer_open)
            toolbar = QHBoxLayout()
            toolbar.addWidget(self.hole_help, 1)
            toolbar.addWidget(self.hole_visualizer_button)
            dialog.layout().insertLayout(1, toolbar)
            self.hole_pages = QStackedWidget()
            layout.addWidget(self.hole_pages, 1)
            self.hole_form = QWidget()
            self.hole_form.setObjectName("holeForm")
            form = QFormLayout(self.hole_form)
            form.setContentsMargins(0, 0, 0, 0)
            form.setVerticalSpacing(12)
            form.setHorizontalSpacing(24)
            form.setLabelAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
            form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
            form.setRowWrapPolicy(QFormLayout.RowWrapPolicy.WrapLongRows)
            self.hole_inputs = {}
            self.hole_input_scale = 25.4 if self.units_button.isChecked() else 1.0
            for name, title in (("center_x", "Center X"), ("center_y", "Center Y"),
                                ("diameter", "Circle diameter"), ("holes", "Number of holes"),
                                ("angle", "Starting angle")):
                field = QSpinBox() if name == "holes" else CompactDoubleSpinBox()
                if name == "holes":
                    field.setRange(1, 10000)
                    field.setValue(4)
                else:
                    field.setDecimals(8)
                    limit = 360 if name == "angle" else 1e7 / self.hole_input_scale
                    field.setRange(0 if name == "diameter" else -limit, limit)
                    field.setSuffix("°" if name == "angle" else (" in" if self.hole_input_scale == 25.4 else " mm"))
                field.setAccessibleName(title)
                field.setMinimumHeight(56)
                field.setMinimumWidth(220)
                field.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Fixed)
                form.addRow(title, field)
                self.hole_inputs[name] = field
            if self.displayed is not None:
                for index, name in enumerate(("center_x", "center_y")):
                    value = self.displayed[index]
                    if value is not None:
                        zero = self.offset[index] + (self.relative_offset[index] if self.mode_buttons[index].isChecked() else 0)
                        direction = -1 if self.sign_buttons[index].isChecked() else 1
                        self.hole_inputs[name].setValue(direction * (value - zero) / self.hole_input_scale)
            self.hole_pages.addWidget(self.hole_form)
            self.hole_targets = QWidget()
            targets = QVBoxLayout(self.hole_targets)
            targets.setContentsMargins(0, 0, 0, 0)
            targets.setSpacing(12)
            heading = QHBoxLayout()
            self.hole_count = QLabel()
            self.hole_count.setObjectName("holeCount")
            self.hole_unit = QLabel()
            self.hole_unit.setObjectName("holeUnit")
            heading.addWidget(self.hole_count, 1)
            heading.addWidget(self.hole_unit)
            targets.addLayout(heading)
            self.hole_target_labels = []
            for axis in "XY":
                card = QFrame()
                card.setObjectName("readout")
                row = QHBoxLayout(card)
                row.setContentsMargins(20, 8, 20, 8)
                name = QLabel(axis)
                name.setObjectName("axis")
                value = QLabel("—")
                value.setFont(self.number_font)
                value.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
                value.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Ignored)
                value.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
                value.setAccessibleName(f"Target {axis} coordinate")
                row.addWidget(name)
                row.addWidget(value, 1)
                targets.addWidget(card, 1)
                self.hole_target_labels.append(value)
                value.installEventFilter(self)
            caption = QLabel("Target position in your original X/Y coordinates")
            caption.setObjectName("holeCaption")
            caption.setWordWrap(True)
            targets.addWidget(caption)
            targets.addStretch(1)
            self.hole_pages.addWidget(self.hole_targets)
            self.hole_status = QLabel()
            self.hole_status.setObjectName("holeStatus")
            self.hole_status.setWordWrap(True)
            self.hole_status.setMinimumHeight(76)
            layout.addWidget(self.hole_status)
            self.hole_navigation = QWidget()
            navigation = QHBoxLayout(self.hole_navigation)
            navigation.setContentsMargins(0, 0, 0, 0)
            navigation.setSpacing(8)
            self.hole_previous_button = QPushButton("Previous")
            self.hole_previous_button.setAccessibleName("Previous hole")
            self.hole_previous_button.clicked.connect(self.previous_hole)
            self.hole_selector = QSpinBox()
            self.hole_selector.setAccessibleName("Target hole number")
            self.hole_selector.setToolTip("Choose any hole number; press Enter to apply typed changes")
            self.hole_selector.setKeyboardTracking(False)
            self.hole_selector.setMinimumSize(120, 56)
            self.hole_selector.valueChanged.connect(self.select_hole)
            self.hole_selector.installEventFilter(self)
            self.hole_next_button = QPushButton("Next")
            self.hole_next_button.setAccessibleName("Next hole")
            self.hole_next_button.clicked.connect(self.next_hole)
            for control in (self.hole_previous_button, self.hole_selector, self.hole_next_button):
                navigation.addWidget(control)
            dialog.layout().insertWidget(dialog.layout().count() - 1, self.hole_navigation)
            self.hole_start_button = QPushButton("Start circle")
            self.hole_start_button.clicked.connect(self.start_hole_circle)
            self.hole_finish_button = QPushButton("Finish circle")
            self.hole_finish_button.clicked.connect(lambda: self.finish_hole_circle("Hole circle complete. X/Y settings restored."))
            self.hole_cancel_button = QPushButton("Close")
            self.hole_cancel_button.clicked.connect(dialog.close)
            for button in (self.hole_start_button, self.hole_finish_button, self.hole_cancel_button):
                actions.addWidget(button)
            for button in (self.hole_start_button, self.hole_previous_button, self.hole_next_button,
                           self.hole_finish_button, self.hole_cancel_button):
                button.setAutoDefault(False)
                button.setMinimumHeight(56)
            self.hole_start_button.setObjectName("holePrimary")
            self.hole_next_button.setObjectName("holePrimary")
            self._create_hole_visualizer()
            for field in self.hole_inputs.values():
                field.valueChanged.connect(self._update_hole_visualizer)
                field.lineEdit().textChanged.connect(self._update_hole_visualizer)
        self._update_hole_controls()
        self._show_function_panel(self.hole_dialog)
        if self.hole_circle is None:
            self._set_hole_visualizer_open(True)

    @pyqtSlot()
    def start_hole_circle(self) -> None:
        if self.hole_circle is not None or self.stopping or self.fast_busy or self.displayed is None:
            return
        if any(value is None or not math.isfinite(value) for value in self.displayed[:2]):
            return
        for field in self.hole_inputs.values():
            field.interpretText()
        params = {name: field.value() for name, field in self.hole_inputs.items()}
        for name in ("center_x", "center_y", "diameter"):
            params[name] *= self.hole_input_scale
        try:
            hole_circle_target(**params, index=0)
        except ValueError as exc:
            self.hole_message = str(exc)
            self._update_hole_controls()
            return
        modes = tuple(button.isChecked() for button in self.mode_buttons[:2])
        self.hole_circle = dict(params=params, index=0, visited={0}, modes=modes,
                                relative=self.relative_offset[:2],
                                origins=tuple(self.offset[i] + (self.relative_offset[i] if modes[i] else 0)
                                              for i in range(2)),
                                directions=tuple(-1 if button.isChecked() else 1 for button in self.sign_buttons[:2]))
        for button in self.mode_buttons[:2]:
            button.setChecked(True)
        self._set_hole_visualizer_open(False)
        self._apply_hole_target()

    def _apply_hole_target(self) -> None:
        circle = self.hole_circle
        target = hole_circle_target(**circle["params"], index=circle["index"])
        self.relative_offset = tuple(circle["origins"][i] + circle["directions"][i] * target[i] - self.offset[i]
                                     for i in range(2)) + self.relative_offset[2:]
        if self.displayed is not None:
            self._draw(self.displayed)
        self.refresh_display()

    @pyqtSlot()
    def next_hole(self) -> None:
        if self.hole_circle is not None:
            self.select_hole(self.hole_circle["index"] + 2)

    @pyqtSlot()
    def previous_hole(self) -> None:
        if self.hole_circle is not None:
            self.select_hole(self.hole_circle["index"])

    @pyqtSlot(int)
    def select_hole(self, number: int) -> None:
        circle = self.hole_circle
        if circle is None or not 1 <= number <= circle["params"]["holes"] or number == circle["index"] + 1:
            return
        circle["index"] = number - 1
        circle["visited"].add(number - 1)
        self._apply_hole_target()

    def finish_hole_circle(self, message="Hole circle cancelled. X/Y settings restored.") -> None:
        circle = self.hole_circle
        if circle is None:
            return
        self.hole_circle = None
        self._set_hole_visualizer_open(False)
        self.relative_offset = circle["relative"] + self.relative_offset[2:]
        for button, mode in zip(self.mode_buttons, circle["modes"]):
            button.setChecked(mode)
        self.hole_message = message
        if self.displayed is not None:
            self._draw(self.displayed)
        self.refresh_display()

    def _fit_hole_targets(self):
        # Keep both target coordinates visible with the optional Fast mode strip.
        height = max(56, min(110, (self.hole_scroll.viewport().height()
                                  - self.hole_count.sizeHint().height() - 24) // 2))
        for label in self.hole_target_labels:
            label.parentWidget().setFixedHeight(height)
        self.hole_targets.layout().activate()

    def _update_hole_controls(self) -> None:
        if self.hole_dialog is None:
            return
        circle = self.hole_circle
        running = circle is not None
        self.hole_pages.setCurrentWidget(self.hole_targets if running else self.hole_form)
        self._fit_hole_targets()
        self.hole_title.setText("Hole targets" if running else "Lay out holes")
        self.hole_help.setText("Move main X/Y to zero at this hole." if running else "XY plane · 0° = −Y · 90° = −X")
        self.hole_start_button.setVisible(not running)
        self.hole_start_button.setDefault(not running)
        self.hole_navigation.setVisible(running)
        self.hole_finish_button.setVisible(running)
        self.hole_next_button.setDefault(running)
        self.hole_cancel_button.setText("Cancel circle" if running else "Close")
        available = self.displayed is not None and all(value is not None and math.isfinite(value) for value in self.displayed[:2])
        self.hole_start_button.setEnabled(available and not self.stopping)
        if running:
            with QSignalBlocker(self.hole_selector):
                if self.hole_selector.maximum() != circle["params"]["holes"]:
                    self.hole_selector.setRange(1, circle["params"]["holes"])
                if self.hole_selector.value() != circle["index"] + 1:
                    self.hole_selector.setValue(circle["index"] + 1)
            self.hole_previous_button.setEnabled(circle["index"] > 0)
            self.hole_next_button.setEnabled(circle["index"] + 1 < circle["params"]["holes"])
            scale = 25.4 if self.units_button.isChecked() else 1
            unit = "in" if scale == 25.4 else "mm"
            precision = 4 if scale == 25.4 else 3
            x, y = hole_circle_target(**circle["params"], index=circle["index"])
            self.hole_count.setText(f"Hole {circle['index'] + 1} of {circle['params']['holes']}")
            self.hole_unit.setText(unit)
            for label, coordinate in zip(self.hole_target_labels, (x, y)):
                result = coordinate / scale
                label.setText(f"{0.0 if round(result, precision) == 0 else result:.{precision}f}")
                fit_readout_font(label, self.number_font)
            message = ("Choose a hole number or use Previous/Next. Finish circle restores your X/Y settings.\n"
                       "X/Y datum controls are locked while stepping.")
            if self.worker is None:
                message += "\nDisconnected — showing the last position."
        else:
            message = self.hole_message if available else "Waiting for X and Y readings before starting."
        self.hole_status.setText(message)
        self._update_hole_visualizer()

    def eventFilter(self, watched, event):
        if watched in (getattr(self, 'controls', None), getattr(self, 'work_area', None)) and event.type() == QEvent.Type.Resize:
            self._layout_main_actions()
        if (event.type() == QEvent.Type.Resize and hasattr(self, 'hole_scroll')
                and watched is self.hole_scroll.viewport() and hasattr(self, 'hole_target_labels')):
            self._fit_hole_targets()
        if watched is getattr(self, "readouts", None) and event.type() == QEvent.Type.Resize:
            self._position_hole_visualizer(self.hole_visualizer_progress_value)
            self._position_remember_visualizer(self.remember_visualizer_progress_value)
        if (watched is getattr(self, "hole_selector", None) and event.type() == QEvent.Type.KeyPress
                and event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter)):
            watched.interpretText()  # Commit the selected hole without also activating Next.
            return True
        if event.type() == QEvent.Type.Resize and watched is self.centralWidget():
            if self.active_panel is not None:
                if self.drawer_animation.state() == QVariantAnimation.State.Running:
                    self.drawer_animation.setEndValue(self._drawer_width() + 16)
                else:
                    self._set_drawer_width(self._drawer_width() + 16)
        if event.type() == QEvent.Type.Resize and watched is getattr(self, "drawer", None):
            self.drawer_pages.setGeometry(16, 0, self._drawer_width(), self.drawer.height())
        if event.type() == QEvent.Type.Resize and watched in getattr(self, "hole_target_labels", ()):
            fit_readout_font(watched, self.number_font)
        return super().eventFilter(watched, event)

    @pyqtSlot()
    def _axis_options_changed(self) -> None:
        if self.displayed is not None:
            self._draw(self.displayed)
        self._update_remember_cards()
        self._update_tool_path()

    @pyqtSlot()
    def toggle_connection(self) -> None:
        if self.worker is None:
            if not self.configuration_verified:
                self.show_settings()
            else:
                self.start_acquisition(demo=False)
        else:
            self.stop_acquisition()

    @pyqtSlot()
    def start_acquisition(self, demo: Optional[bool] = None) -> None:
        if self.worker is not None or self.closing:
            return
        if self.remembered_setup != self.machine_config:
            keep_origin = self.machine_config['profile'] == self.remembered_setup['profile']
            self.remembered_setup = dict(self.machine_config)
            self._clear_remembered_points(keep_origin=keep_origin)
        self.finish_hole_circle("Acquisition changed. Hole circle ended; X/Y settings restored.")
        self.latest = LatestReading()
        self.offset = (0.0, 0.0, 0.0)
        self.relative_offset = (0.0, 0.0, 0.0)
        self.displayed = None
        self.last_drawn = -1
        self.fast_busy = self.fast_attempted = False
        self.fast_message = ""
        self.stopping = self.connection_ready = False
        self.error_text = ""
        self.connection_status = "Connecting…"
        self.clear_tool_path()
        self.statusBar().setToolTip("")
        for card in self.axis_cards:
            card.set_reading(None, self.units_button.isChecked())

        self.datum_message = 'Software zeros cleared — establish the datum'
        worker = SerialWorker(self.port, self.baud, self.latest,
                              self.demo if demo is None else demo, self,
                              channels=self.machine_config['channels'], axes=PROFILES[self.profile][2])
        self.worker = worker  # Keep alive until its finished signal arrives.
        worker.connected.connect(self.on_connected)
        worker.failed.connect(self.on_error)
        worker.progress.connect(self.on_progress)
        worker.fast_mode_changing.connect(self.on_fast_mode_changing)
        worker.fast_mode_finished.connect(self.on_fast_mode_finished)
        worker.finished.connect(self.on_finished)
        self.connect_button.setText("Cancel")
        self.connect_button.setEnabled(True)
        worker.start()
        self.refresh_display()

    @pyqtSlot()
    def stop_acquisition(self) -> None:
        self.finish_hole_circle("Acquisition stopped. Hole circle ended; X/Y settings restored.")
        if self.worker is not None:
            self.stopping = True
            self.connect_button.setText("Stopping…")
            self.connect_button.setEnabled(False)
            self.worker.requestInterruption()
            self.refresh_display()

    @pyqtSlot()
    def on_connected(self) -> None:
        self.connection_ready = True
        if not self.stopping and not self.closing:
            self.connect_button.setText("Disconnect")
            self.connect_button.setEnabled(True)
        self.refresh_display()

    @pyqtSlot(str)
    def on_progress(self, message: str) -> None:
        self.connection_status = message
        self.refresh_display()

    @pyqtSlot(str)
    def on_error(self, message: str) -> None:
        if self.fast_busy:
            message = "Fast-mode setup incomplete: " + message
        self.error_text = message
        self.finish_hole_circle("Serial error. Hole circle ended; X/Y settings restored.")
        self.statusBar().setToolTip(message)

    @pyqtSlot()
    def on_finished(self) -> None:
        worker = self.worker
        self.worker = None
        self.connection_ready = self.stopping = False
        if self.fast_busy:
            self.fast_busy = False
            self.fast_message = "Setup interrupted — restore scales to normal, then reconnect."
            self.latest = LatestReading()  # Never display partial setup readings as a valid position.
            self.displayed = None
            self.last_drawn = -1
        self.connect_button.setText("Connect")
        self.connect_button.setEnabled(True)
        if worker is not None:
            worker.deleteLater()
        self.refresh_display()
        if self.closing:
            self.close()  # The second close can safely destroy the window.
        elif self.after_stop is not None:
            action, self.after_stop = self.after_stop, None
            action()

    def _restore_axis_decimals(self, card):
        axis = card.name.text().split()[-1]  # X keeps its precision in both radius and diameter modes.
        for inches, unit, default in ((False, 'mm', 3), (True, 'in', 4)):
            value = self.machine_settings.value(f'readout_decimals/{axis}/{unit}', default)
            card.decimals[inches] = int(value) if str(value) in ('1', '2', '3', '4') else default
        card.set_reading(card.reading, card.inches)

    def _save_axis_decimals(self, card):
        axis, unit = card.name.text().split()[-1], 'in' if card.inches else 'mm'
        self.machine_settings.setValue(f'readout_decimals/{axis}/{unit}', card.decimals[card.inches])
        self.machine_settings.sync()
        self.precision_error = ('' if self.machine_settings.status() == QSettings.Status.NoError else
                                'Could not save decimal settings. Changes apply to this session only.')
        self.refresh_display()

    def zero_axis(self, index: int) -> None:
        if self.fast_busy or (self.hole_circle is not None and index < 2):
            return
        if self.displayed is not None and self.displayed[index] is not None:
            zero = self.displayed[index]
            if self.mode_buttons[index].isChecked():
                zero -= self.offset[index]
            self._set_axis_zero(index, zero)
            self._draw(self.displayed)

    def _set_axis_zero(self, index: int, zero: float) -> None:
        if self.mode_buttons[index].isChecked():
            self.relative_offset = tuple(zero if i == index else value
                                         for i, value in enumerate(self.relative_offset))
        else:
            self.offset = tuple(zero if i == index else value for i, value in enumerate(self.offset))

    @pyqtSlot()
    def clear_zero(self) -> None:
        if self.fast_busy or self.hole_circle is not None:
            return
        for index in range(3):
            self._set_axis_zero(index, 0.0)
        if self.displayed is not None:
            self._draw(self.displayed)

    @pyqtSlot(bool)
    def set_inches(self, inches: bool) -> None:
        self.units_button.setAccessibleName(f"Display units: {'in' if inches else 'mm'}")
        self._update_remember_cards()
        self._update_tool_path()
        if self.displayed is None:
            for card in self.axis_cards:
                card.set_reading(None, inches)
        if self.hole_dialog is not None:
            scale = 25.4 if inches else 1.0
            for name in ("center_x", "center_y", "diameter"):
                field = self.hole_inputs[name]
                value = field.value() * self.hole_input_scale / scale
                field.setRange(0 if name == "diameter" else -1e7 / scale, 1e7 / scale)
                field.setSuffix(" in" if inches else " mm")
                field.setValue(value)
            self.hole_input_scale = scale
        if self.displayed is not None:
            self._draw(self.displayed)
        self.refresh_display()

    def _coordinate_mm(self, index, value):
        relative = self.relative_offset[index] if self.mode_buttons[index].isChecked() else 0.0
        direction = -1 if self.sign_buttons[index].isChecked() else 1
        result = direction * (value - self.offset[index] - relative)
        if self.profile.startswith('lathe') and index == 0 and self.machine_config['diameter']:
            result *= 2
        return result

    def _draw(self, values: XYZ) -> None:
        # ponytail: samples and offsets stay in mm; convert only for display,
        # so repeated unit switches never round or alter the stored position.
        self.displayed = values
        inches = self.units_button.isChecked()
        for index, (card, value) in enumerate(zip(self.axis_cards, values)):
            result = None if value is None else self._coordinate_mm(index, value) / (25.4 if inches else 1.0)
            card.set_reading(result, inches)
        self._update_point_distances()

    @pyqtSlot()
    def refresh_display(self) -> None:
        state = self.latest.snapshot()
        if not self.fast_busy and state.values is not None and state.valid != self.last_drawn:
            self._draw(coordinates(self.profile, state.values, self.machine_config['directions'],
                                   self.machine_config['angle'], self.machine_config['summed']))
            self.last_drawn = state.valid

        circling = self.hole_circle is not None
        self.hole_button.setEnabled(self.profile.startswith('mill') and not self.fast_busy and not self.stopping and not self.closing)
        self.clear_zero_button.setEnabled(self.displayed is not None and not self.stopping and not circling)
        for index, button in enumerate(self.axis_zero_buttons):
            unlocked = not self.fast_busy and not (circling and index < 2)
            self.sign_buttons[index].setEnabled(unlocked)
            self.mode_buttons[index].setEnabled(unlocked)
            button.setEnabled(self.displayed is not None and self.displayed[index] is not None
                              and not self.stopping and unlocked)
        self._update_hole_controls()
        self._update_remember_controls()
        self._update_point_distances()
        self._update_fast_controls()
        self._update_machine_controls()
        self.settings_button.setEnabled(not self.stopping and not self.closing and not self.fast_busy)
        if hasattr(self, 'open_setup_button'):
            self.open_setup_button.setEnabled(self.setup_group.checkedId() >= 0 and not self.stopping and not self.closing)
            self.startup_demo_button.setEnabled(not self.stopping and not self.closing)
            self.cancel_setup_button.setEnabled(not self.stopping and not self.closing)
        if self.stopping:
            status = "Stopping…"
        elif self.error_text:
            status = "Serial error: " + self.error_text
        elif self.fast_busy:
            status = self.connection_status + " | Readings unavailable during setup"
        elif self.worker is None:
            status = "Disconnected — showing last values" if state.values else "Disconnected"
        elif not self.connection_ready:
            status = self.connection_status
        else:
            source = "Test — simulated data (0–30 mm)" if self.worker.demo else f"{self.port} @ {self.baud}"
            units = "in" if self.units_button.isChecked() else "mm"
            status = source + f" | {units} | Live"
            status += f" | Samples: {state.valid:,} | Invalid: {state.invalid:,}"
            if state.values is None:
                status += " | Waiting for scale readings"
            elif any(value is None for value in state.values):
                missing = ", ".join(axis for axis, value in zip(PROFILES[self.profile][2], state.values) if value is None)
                status += f" | No scale: {missing}"
            if state.warning:
                status += " | Last input issue: " + state.warning
                self.statusBar().setToolTip(state.warning)
        if circling:
            status += f" | HCXY hole {self.hole_circle['index'] + 1}/{self.hole_circle['params']['holes']}"
        if self.worker is None and self.fast_message:
            status += " | " + self.fast_message
        if self.datum_message:
            status += ' | ' + self.datum_message
        if self.precision_error:
            status += ' | ' + self.precision_error
        self.statusBar().showMessage(status)

    def closeEvent(self, event: QCloseEvent) -> None:
        if self.remember_dialog is not None:
            self.remember_dialog.close()
        if self.hole_dialog is not None:
            self.hole_dialog.close()
        if self.worker is not None:
            self.closing = True
            self.after_stop = None
            self.stop_acquisition()
            event.ignore()  # Keep servicing GUI events until the worker exits.
        else:
            self.tool_path_timer.stop()
            event.accept()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--port", default=None, help="Serial device or simulator socket://127.0.0.1:8765 (overrides saved interface)")
    parser.add_argument("--baud", type=int, default=SERIAL_SETTINGS["baudrate"], help="Baud rate (default: %(default)s)")
    parser.add_argument("--demo", action="store_true", help="Generate sample data without a device")
    args = parser.parse_args()
    if args.baud <= 0:
        parser.error("--baud must be positive")

    app = QApplication([sys.argv[0]])
    app.setWindowIcon(QIcon(str(Path(__file__).resolve().parents[1] / "assets" / "zero-hero-rounded.png")))
    app.setApplicationName("DADRO")
    window = MainWindow(args.port or '/dev/ttyUSB0', args.baud, args.demo)
    window.begin_startup(port_override=args.port)
    window.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
