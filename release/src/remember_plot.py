"""A small, interactive orthographic view of saved scale coordinates; no GPU dependency."""
import math
from itertools import product

from PyQt6.QtCore import QPointF, QRectF, Qt, pyqtSignal
from PyQt6.QtGui import QColor, QPainter, QPen
from PyQt6.QtWidgets import QWidget


class RememberPointPlot(QWidget):
    point_selected = pyqtSignal(object)

    def __init__(self, parent=None, *, trail=False):
        super().__init__(parent)
        self.trail = trail
        self.points, self.positions = {}, {}
        self.selected, self.axes = None, ('X', 'Y', 'Z')
        self.origin = None
        self._trail_frame = None
        self.limits = ((0., 0.),) * 3
        self.press = self.previous = None
        self.dragged = False
        self.setMinimumHeight(160)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setCursor(Qt.CursorShape.OpenHandCursor)
        self.setAccessibleName('3D tool path' if trail else '3D remembered points')
        self.reset_view()
        self.set_points({}, None, self.axes)

    def set_points(self, points, selected, axes, origin=None):
        changed = points != self.points
        if changed:
            self.points = dict(points)
            # Normalize before subtraction so even extreme finite saved values cannot overflow.
            magnitude = max(1., max((abs(v) for p in points.values() for v in p), default=0.))
            if self.trail and self._trail_frame and points:
                magnitude = max(magnitude, self._trail_frame[0])
            normalized = {n: tuple(v / magnitude for v in p) for n, p in points.items()}
            bounds = tuple((min(p[i] for p in normalized.values()), max(p[i] for p in normalized.values()))
                           for i in range(3)) if points else ((0., 0.),) * 3
            center = tuple((lo + hi) / 2 for lo, hi in bounds)
            centered = {n: tuple(v - c for v, c in zip(p, center)) for n, p in normalized.items()}
            radius = max((math.hypot(*p) for p in centered.values()), default=0.) or 1.
            if self.trail:
                if points:
                    # ponytail: a fixed, padded frame avoids camera motion; clear the trail to fit afresh.
                    radius = max(1.27 / magnitude, max((math.hypot(*p) for p in centered.values()), default=0.) * 1.25)
                    if self._trail_frame:
                        old_magnitude, old_center, old_radius = self._trail_frame
                        ratio = old_magnitude / magnitude
                        old_center = tuple(v * ratio for v in old_center)
                        old_radius *= ratio
                        if all(math.dist(p, old_center) <= old_radius for p in normalized.values()):
                            center, radius = old_center, old_radius
                    self._trail_frame = magnitude, center, radius
                    centered = {n: tuple(v - c for v, c in zip(p, center)) for n, p in normalized.items()}
                else:
                    self._trail_frame = None
            self.positions = {n: tuple(v / radius for v in p) for n, p in centered.items()}
            self.limits = tuple(((lo - c) / radius, (hi - c) / radius) for (lo, hi), c in zip(bounds, center))
        if changed or selected != self.selected or axes != self.axes or origin != self.origin or not self.accessibleDescription():
            self.selected, self.axes = selected, tuple(axes)
            self.origin = origin
            name = 'Origin' if selected == origin else f'Point {selected}'
            self.setAccessibleDescription(
                (f'{len(points)} tool-path samples. Latest position highlighted. '
                 'Enter to use keys; slash returns to navigation. Drag or use arrow keys to rotate; '
                 'scroll or use plus/minus to zoom; Home resets.') if self.trail else
                (f'{len(points)} saved points. ' if points else 'Remember a point to see it here. ')
                + (f'{name} selected. ' if selected is not None else 'No point selected. ')
                + 'Enter to use keys; slash returns to navigation. Drag or use arrow keys to rotate; '
                  'scroll or use plus/minus to zoom; Home resets. '
                  'Select points using the cards or click a marker.')
            self.update()

    def reset_view(self):
        self.yaw, self.pitch, self.zoom = 45., 30., 1.
        self.update()

    def _rotate(self, point):
        x, y, z = point
        yaw, pitch = math.radians(self.yaw), math.radians(self.pitch)
        across, back = x * math.cos(yaw) - y * math.sin(yaw), x * math.sin(yaw) + y * math.cos(yaw)
        return across, back * math.sin(pitch) - z * math.cos(pitch), back * math.cos(pitch) + z * math.sin(pitch)

    def _project(self, point):
        x, y, _ = self._rotate(point)
        scale = max(1., min(self.width(), self.height()) - 100) / 2 * self.zoom
        return QPointF(self.width() / 2 + x * scale, self.height() / 2 + y * scale)

    def projected_points(self):
        return {n: self._project(p) for n, p in self.positions.items()}

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setPen(QPen(QColor('#718692'), 1))
        for corner in product((0, 1), repeat=3):
            point = tuple(self.limits[i][side] for i, side in enumerate(corner))
            for axis in range(3):
                if corner[axis] == 0:
                    other = list(point)
                    other[axis] = self.limits[axis][1]
                    painter.drawLine(self._project(point), self._project(other))
        # Depth guides help distinguish height from the two horizontal coordinates.
        painter.setPen(QPen(QColor('#718692'), 1, Qt.PenStyle.DotLine))
        for number, point in self.positions.items():
            if self.trail and number != self.selected:
                continue
            painter.drawLine(self._project(point), self._project((*point[:2], self.limits[2][0])))
        projected = self.projected_points()
        if self.trail:
            painter.setPen(QPen(QColor('#24566e'), 2))
            for number, point in projected.items():
                if number - 1 in projected:
                    painter.drawLine(projected[number - 1], point)
        for number in sorted(self.positions, key=lambda n: (n == self.selected, self._rotate(self.positions[n])[2])):
            point, selected = projected[number], number == self.selected
            if self.trail and not selected and number - 1 in self.positions:
                continue
            painter.setPen(QPen(QColor('#174b63'), 2 if selected else 1.5))
            painter.setBrush(QColor('#174b63' if selected else '#cfe3ef'))
            painter.drawEllipse(point, 6 if selected else 4, 6 if selected else 4)
            if selected:
                painter.setBrush(Qt.BrushStyle.NoBrush)
                painter.drawEllipse(point, 10, 10)
            # ponytail: label up to 40 markers plus Origin/selection; use cards for dense/coincident sets.
            if (self.trail or len(self.points) <= 40 or selected or number == self.origin) and self.rect().contains(point.toPoint()):
                label = ('Tool' if selected else 'Start') if self.trail else 'Origin' if number == self.origin else str(number)
                width = painter.fontMetrics().horizontalAdvance(label) + 8
                rect = QRectF(point.x() + 10, point.y() - 20, width, 20)
                rect.moveLeft(max(0., min(rect.left(), self.width() - width)))
                rect.moveTop(max(0., min(rect.top(), self.height() - 20)))
                painter.fillRect(rect, QColor('#ffffff'))
                painter.setPen(QColor('#10212f'))
                painter.drawText(rect, Qt.AlignmentFlag.AlignCenter, label)
        origin = QPointF(48, self.height() - 48)
        for i, (name, color) in enumerate(zip(self.axes, ('#8c403a', '#206356', '#174b63'))):
            direction = self._rotate(tuple(1. if j == i else 0. for j in range(3)))
            tip = origin + QPointF(direction[0] * 30, direction[1] * 30)
            painter.setPen(QPen(QColor(color), 2))
            painter.drawLine(origin, tip)
            painter.drawText(QRectF(tip.x() - 20, tip.y() - 22, 40, 20), Qt.AlignmentFlag.AlignCenter, name)
        if not self.points:
            painter.setPen(QColor('#344b5c'))
            painter.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter | Qt.TextFlag.TextWordWrap,
                             'Waiting for tool positions' if self.trail else 'Remember a point to see it here')
        if self.hasFocus():
            painter.setPen(QPen(QColor('#795113' if self.property('keyboardEditing') else '#24566e'), 2, Qt.PenStyle.DashLine))
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawRoundedRect(QRectF(self.rect()).adjusted(1, 1, -1, -1), 6, 6)

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self.setFocus(Qt.FocusReason.MouseFocusReason)
            self.press = self.previous = event.position()
            self.dragged = False
            self.setCursor(Qt.CursorShape.ClosedHandCursor)
            event.accept()
        else:
            super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        if self.previous is not None:
            delta = event.position() - self.previous
            self.dragged |= (event.position() - self.press).manhattanLength() > 4
            if self.dragged:
                self.yaw = (self.yaw + delta.x() * .5) % 360
                self.pitch = max(-85., min(85., self.pitch + delta.y() * .5))
                self.update()
            self.previous = event.position()
            event.accept()
        else:
            super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton and self.press is not None:
            if not self.trail and not self.dragged and (event.position() - self.press).manhattanLength() <= 4:
                hits = [(math.hypot(p.x() - event.position().x(), p.y() - event.position().y()), n)
                        for n, p in self.projected_points().items()]
                if hits and min(hits)[0] <= 14:
                    number = min(hits)[1]
                    self.point_selected.emit(None if number == self.selected else number)
            self.press = self.previous = None
            self.setCursor(Qt.CursorShape.OpenHandCursor)
            event.accept()
        else:
            super().mouseReleaseEvent(event)

    def _zoom(self, factor):
        self.zoom = max(.25, min(8., self.zoom * factor))
        self.update()

    def wheelEvent(self, event):
        delta = event.angleDelta().y() or event.pixelDelta().y()
        self._zoom(1.2 ** max(-4., min(4., delta / 120)))
        event.accept()

    def keyPressEvent(self, event):
        key = event.key()
        if key in (Qt.Key.Key_Left, Qt.Key.Key_Right):
            self.yaw = (self.yaw + (10 if key == Qt.Key.Key_Right else -10)) % 360
        elif key in (Qt.Key.Key_Up, Qt.Key.Key_Down):
            self.pitch = max(-85., min(85., self.pitch + (10 if key == Qt.Key.Key_Down else -10)))
        elif key in (Qt.Key.Key_Plus, Qt.Key.Key_Equal, Qt.Key.Key_Minus):
            self._zoom(1 / 1.2 if key == Qt.Key.Key_Minus else 1.2)
        elif key == Qt.Key.Key_Home:
            self.reset_view()
        else:
            super().keyPressEvent(event)
            return
        self.update()
        event.accept()
