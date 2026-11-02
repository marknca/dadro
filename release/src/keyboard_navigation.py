"""Spatial keyboard navigation for the readout's existing Qt controls."""
from PyQt6 import sip
from PyQt6.QtCore import QEvent, QObject, QPoint, QRect, QRectF, Qt
from PyQt6.QtGui import QColor, QPainter, QPen
from PyQt6.QtWidgets import (
    QAbstractButton, QAbstractSpinBox, QApplication, QComboBox, QFocusFrame, QLabel,
    QLineEdit, QScrollArea, QWidget,
)

from remember_plot import RememberPointPlot


class FocusHighlight(QFocusFrame):
    """Paint a large ring without changing the focused control's layout or hit area."""
    def setWidget(self, control):
        super().setWidget(control)
        self.clearMask()  # The native thin-ring mask would clip our thicker inset outline.
        self.raise_()

    def eventFilter(self, watched, event):
        result = super().eventFilter(watched, event)
        if event.type() in (QEvent.Type.Move, QEvent.Type.Resize, QEvent.Type.Show,
                            QEvent.Type.ParentChange, QEvent.Type.ZOrderChange):
            self.clearMask()
            self.raise_()
        return result

    def paintEvent(self, event):
        control = self.widget()
        if control is None:
            return
        bounds = QRectF(QRect(self.mapFromGlobal(control.mapToGlobal(QPoint())), control.size())).adjusted(3, 3, -3, -3)
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setBrush(Qt.BrushStyle.NoBrush)
        # The white edge keeps the blue/amber ring visible on dark primary actions and selected segments.
        for color, width in (('#ffffff', 6), ('#795113' if control.property('keyboardEditing') else '#24566e', 4)):
            painter.setPen(QPen(QColor(color), width))
            painter.drawRoundedRect(bounds, 6, 6)


class KeyboardNavigation(QObject):
    arrows = (Qt.Key.Key_Left, Qt.Key.Key_Right, Qt.Key.Key_Up, Qt.Key.Key_Down)
    editors = (QAbstractSpinBox, QComboBox, QLineEdit, RememberPointPlot)

    def __init__(self, window):
        super().__init__(window)
        self.window, self.editing = window, None
        self.highlight = FocusHighlight(window)
        self.hint = QLabel('Arrows: move · Enter: use')
        self.hint.setStyleSheet('color: #344b5c; font-size: 11px; font-weight: 400;')
        self.hint.setToolTip('Arrow keys move between controls. Enter activates or starts editing. '
                             '/ returns to navigation. Tab and Shift+Tab also move focus.')
        window.statusBar().addPermanentWidget(self.hint)
        app = QApplication.instance()
        app.installEventFilter(self)
        app.focusChanged.connect(self._focus_changed)

    def _control(self, widget):
        """A spin editor or dropdown popup is part of its owning control."""
        control = None
        while isinstance(widget, QWidget) and widget is not self.window:
            if isinstance(widget, (QAbstractSpinBox, QComboBox)):
                control = widget
            elif control is None and isinstance(widget, (QAbstractButton,) + self.editors):
                control = widget
            widget = widget.parentWidget()
        return control if widget is self.window else None

    def controls(self):
        # ponytail: scan the small live widget tree per key; index it only if measured latency warrants it.
        controls = []
        covered = any(panel is not None and panel.isVisible() for panel in
                      (self.window.hole_visualizer, self.window.remember_visualizer))
        for widget in self.window.findChildren(QWidget):
            if (self._control(widget) is not widget or not widget.isVisible() or not widget.isEnabled()
                    or widget.focusPolicy() == Qt.FocusPolicy.NoFocus):
                continue
            if covered and any(card.isAncestorOf(widget) for card in self.window.axis_cards):
                continue
            parent = widget
            while parent is not self.window and parent.width() > 0 and parent.height() > 0:
                parent = parent.parentWidget()
            if parent is self.window:
                controls.append(widget)
        return controls

    def _rect(self, widget):
        return QRect(widget.mapTo(self.window, QPoint()), widget.size())

    def _scroll(self, widget):
        parent = widget.parentWidget()
        while parent is not None:
            if isinstance(parent, QScrollArea):
                return parent
            parent = parent.parentWidget()

    def _focus(self, widget):
        scroll = self._scroll(widget)
        if scroll is not None:
            scroll.ensureWidgetVisible(widget, 12, 12)
        widget.setFocus(Qt.FocusReason.TabFocusReason)

    def _move(self, source, key):
        controls = self.controls()
        if not controls:
            return
        if source not in controls:
            panel = self.window.active_panel
            local = [w for w in controls if panel is not None and panel.isAncestorOf(w)]
            self._focus(min(local or controls, key=lambda w: (self._rect(w).top(), self._rect(w).left())))
            return
        rect = self._rect(source)
        horizontal = key in (Qt.Key.Key_Left, Qt.Key.Key_Right)
        direction = -1 if key in (Qt.Key.Key_Left, Qt.Key.Key_Up) else 1
        scroll = self._scroll(source)
        ranked = []
        for order, target in enumerate(controls):
            if target is source:
                continue
            other = self._rect(target)
            dx, dy = other.center().x() - rect.center().x(), other.center().y() - rect.center().y()
            forward, across = (dx * direction, dy) if horizontal else (dy * direction, dx)
            if forward <= 0:
                continue
            aligned = (other.top() <= rect.bottom() and other.bottom() >= rect.top()) if horizontal else (
                other.left() <= rect.right() and other.right() >= rect.left())
            # Visit the rest of a scrollable column before its pinned footer, including offscreen rows.
            same_column = not horizontal and aligned and scroll is not None and self._scroll(target) is scroll
            ranked.append(((not same_column, not aligned, forward * forward + across * across, order), target))
        if ranked:
            self._focus(min(ranked, key=lambda item: item[0])[1])

    def _set_editing(self, control):
        if control is self.editing:
            return
        previous, self.editing = self.editing, control
        for widget in (previous, control):
            if widget is not None and not sip.isdeleted(widget):
                widget.setProperty('keyboardEditing', widget is control)
                widget.style().unpolish(widget)
                widget.style().polish(widget)
                widget.update()
        self.hint.setText('Editing · /: move between controls' if control is not None else 'Arrows: move · Enter: use')
        if not sip.isdeleted(self.highlight):
            self.highlight.update()

    def _focus_changed(self, previous, current):
        # Qt briefly reports no focus while transferring it to/from a dropdown popup.
        if current is not None and self.editing is not None and self._control(current) is not self.editing:
            self._set_editing(None)
        if sip.isdeleted(self.highlight):
            self.highlight = FocusHighlight(self.window)
        self.highlight.setWidget(self._control(current))

    def eventFilter(self, watched, event):
        kind = event.type()
        if kind not in (QEvent.Type.KeyPress, QEvent.Type.MouseButtonPress, QEvent.Type.Hide, QEvent.Type.Show):
            return False
        if kind == QEvent.Type.Hide:
            if watched is self.editing:
                self._set_editing(None)
            return False
        control = self._control(watched)
        if kind == QEvent.Type.Show:
            if isinstance(control, QComboBox) and watched is control.view().window():
                self._set_editing(control)
            return False
        if not isinstance(watched, QWidget) or (control is None and watched is not self.window
                                               and not self.window.isAncestorOf(watched)):
            return False
        if kind == QEvent.Type.MouseButtonPress:
            if control is not None:
                self._set_editing(control if isinstance(control, self.editors) else None)
            return False
        if event.modifiers() & (Qt.KeyboardModifier.ControlModifier | Qt.KeyboardModifier.AltModifier |
                                Qt.KeyboardModifier.MetaModifier | Qt.KeyboardModifier.ShiftModifier):
            return False
        key = event.key()
        if key == Qt.Key.Key_Slash:
            if isinstance(control, QAbstractSpinBox):
                control.interpretText()
            if isinstance(control, QComboBox):
                control.hidePopup()
                control.setFocus(Qt.FocusReason.OtherFocusReason)
            self._set_editing(None)
            return True
        if control is not None and control is self.editing:
            if key in (Qt.Key.Key_Return, Qt.Key.Key_Enter) and isinstance(control, QAbstractSpinBox):
                control.interpretText()  # Never trigger a drawer's default action while editing a number.
                return True
            return False
        if key in self.arrows:
            self._move(control, key)
            return True
        if key in (Qt.Key.Key_Return, Qt.Key.Key_Enter) and control is not None:
            if not event.isAutoRepeat() and control.isEnabled():
                if isinstance(control, QAbstractButton):
                    control.click()
                elif isinstance(control, self.editors):
                    self._set_editing(control)
                    if isinstance(control, QComboBox):
                        control.showPopup()
                    elif isinstance(control, (QAbstractSpinBox, QLineEdit)):
                        control.selectAll()
            return True
        if isinstance(control, self.editors) and key not in (Qt.Key.Key_Tab, Qt.Key.Key_Backtab, Qt.Key.Key_Escape):
            # Reaching a field must not change a value until Enter or a pointer click enters it.
            return bool(event.text()) or key in (Qt.Key.Key_Backspace, Qt.Key.Key_Delete, Qt.Key.Key_Home,
                                                Qt.Key.Key_End, Qt.Key.Key_PageUp, Qt.Key.Key_PageDown)
        return False
