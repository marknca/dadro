"""Machine choices and raw-scale geometry; all coordinates and datums stay in mm."""
import math

from PyQt6.QtCore import QPointF, QRectF, QSize, Qt
from PyQt6.QtGui import QColor, QFont, QPainter, QPen, QPolygonF
from PyQt6.QtWidgets import QRadioButton


PROFILES = {
    'millxy': ('Mill', 'Table travel', ('X', 'Y'), 'Measure left/right and in/out.'),
    'mill3': ('Mill', 'Table + vertical', ('X', 'Y', 'Z'), 'One vertical scale, including quill-only.'),
    'mill3q': ('Mill', 'Vertical + quill', ('X', 'Y', 'Z', 'Q'), 'Separate vertical and quill scales.'),
    'lathexz1': ('Lathe', 'Cross slide + carriage', ('X', 'Z1'), 'Measure across and along the spindle.'),
    'lathe3': ('Lathe', 'With compound', ('X', 'Z1', 'Z2'), 'Include travel on the angled top slide.'),
}


def validate_setup(setup):
    if not isinstance(setup, dict) or setup.get('profile') not in PROFILES:
        raise ValueError('Choose a supported machine setup')
    count = len(PROFILES[setup['profile']][2])
    channels, directions = setup.get('channels'), setup.get('directions')
    if (not isinstance(channels, list) or len(channels) != count
            or any(type(c) is not int or not 0 <= c <= 3 for c in channels)
            or len(set(channels)) != count):
        raise ValueError('Assign a different controller channel (0–3) to each scale')
    if (not isinstance(directions, list) or len(directions) != count
            or any(type(d) is not int or d not in (-1, 1) for d in directions)):
        raise ValueError('Each scale direction must be + or −')
    if not isinstance(setup.get('port'), str) or not setup['port'].strip() or '\x00' in setup['port']:
        raise ValueError('Enter a serial port or select a detected interface')
    angle = setup.get('angle')
    if type(angle) not in (int, float) or not math.isfinite(angle) or not -180 <= angle <= 180:
        raise ValueError('Compound angle must be between −180° and 180°')
    if type(setup.get('diameter')) is not bool or type(setup.get('summed')) is not bool:
        raise ValueError('Invalid lathe display settings')


def validate_toolhead(settings):
    width = settings.get('width_mm') if isinstance(settings, dict) else None
    if type(width) not in (int, float) or not 0 < width <= 1000:
        raise ValueError('Toolhead width must be greater than zero and no more than 1,000 mm.')
    if type(settings.get('inches')) is not bool:
        raise ValueError('Choose inches or millimetres for the toolhead width.')


def coordinates(profile, values, directions=None, angle=0, summed=False):
    """Three display slots; None propagates through any required input."""
    count = len(PROFILES[profile][2])
    if len(values) != count or (directions is not None and len(directions) != count):
        raise ValueError('Scale count does not match the machine')
    if not math.isfinite(angle) or not -180 <= angle <= 180:
        raise ValueError('Invalid compound angle')
    raw = [None if v is None or not math.isfinite(v) else v * d
           for v, d in zip(values, directions or (1,) * count)]
    if profile in ('millxy', 'lathexz1'):
        return raw[0], raw[1], None
    if profile == 'mill3q':
        return raw[0], raw[1], None if None in raw[2:] else raw[2] + raw[3]
    if profile == 'lathe3':
        x, carriage, compound = raw
        r = math.radians(angle)
        x = None if x is None or compound is None else x - math.sin(r) * compound
        z2 = None if compound is None else math.cos(r) * compound
        if summed:
            return x, None if carriage is None or z2 is None else carriage + z2, None
        return x, carriage, z2
    return tuple(raw)


class ProfileCard(QRadioButton):
    """Native radio semantics with a whole-card target and a compact travel diagram."""
    def __init__(self, profile, parent=None):
        super().__init__(parent)
        self.profile = profile
        group, title, axes, description = PROFILES[profile]
        self.setAccessibleName(f'{group}: {title}, {len(axes)} scales')
        self.setAccessibleDescription(description + ' Measured axes: ' + ', '.join(axes))
        self.setToolTip(self.accessibleDescription())
        self.setMinimumSize(220, 165)
        self.setCursor(Qt.CursorShape.PointingHandCursor)

    def sizeHint(self):
        return QSize(320, 165)

    def minimumSizeHint(self):
        return QSize(220, 165)

    def hitButton(self, position):
        return self.rect().contains(position)

    def keyPressEvent(self, event):
        steps = {Qt.Key.Key_Left: -1, Qt.Key.Key_Right: 1, Qt.Key.Key_Up: -1, Qt.Key.Key_Down: 1}
        if event.key() in steps and self.group() is not None:
            buttons = self.group().buttons()
            target = buttons[(buttons.index(self) + steps[event.key()]) % len(buttons)]
            target.setFocus(Qt.FocusReason.TabFocusReason)
            target.setChecked(True)
            event.accept()
        else:
            super().keyPressEvent(event)

    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        selected = self.isChecked()
        p.setPen(QPen(QColor('#07565f' if selected or self.hasFocus() else '#708491'), 2 if selected or self.hasFocus() else 1))
        p.setBrush(QColor('#dcefed' if selected else '#ffffff'))
        p.drawRoundedRect(QRectF(self.rect()).adjusted(2, 2, -2, -2), 12, 12)
        if self.hasFocus():
            p.setPen(QPen(QColor('#07565f'), 1, Qt.PenStyle.DashLine))
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.drawRoundedRect(QRectF(self.rect()).adjusted(6, 6, -6, -6), 9, 9)
        p.setPen(QPen(QColor('#07565f' if selected else '#556e7e'), 1.5))
        p.setBrush(QColor('#07565f' if selected else '#ffffff'))
        p.drawEllipse(QPointF(self.width() - 24, 24), 7, 7)
        if selected:
            p.setBrush(QColor('white')); p.setPen(Qt.PenStyle.NoPen)
            p.drawEllipse(QPointF(self.width() - 24, 24), 2.5, 2.5)
        group, title, axes, description = PROFILES[self.profile]
        font = QFont(self.font()); font.setPixelSize(12); font.setWeight(QFont.Weight.ExtraBold)
        p.setFont(font); p.setPen(QColor('#07565f'))
        p.drawText(QRectF(16, 12, self.width() - 58, 22), ' · '.join(axes) + f'    {len(axes)} scales')
        p.save()
        scale = min((self.width() - 24) / 260, (self.height() - 88) / 92)
        p.translate((self.width() - 260 * scale) / 2, 36)
        p.scale(scale, scale)
        self.draw_machine(p)
        p.restore()
        font.setPixelSize(16); p.setFont(font); p.setPen(QColor('#10212f'))
        p.drawText(QRectF(16, self.height() - 52, self.width() - 32, 24), title)
        font.setPixelSize(11); font.setWeight(QFont.Weight.Normal); p.setFont(font); p.setPen(QColor('#344b5c'))
        p.drawText(QRectF(16, self.height() - 29, self.width() - 32, 24),
                   Qt.AlignmentFlag.AlignLeft | Qt.TextFlag.TextWordWrap, description)

    def draw_machine(self, p):
        def part(x, y, w, h):
            p.setPen(QPen(QColor('#556e7e'), 1.4)); p.setBrush(QColor('#e3e9ed'))
            p.drawRect(QRectF(x, y, w, h))
        def arrow(x1, y1, x2, y2, label, tx, ty, measured=True, extra=False):
            color = QColor('#80480c' if extra else '#07565f' if measured else '#556e7e')
            p.setPen(QPen(color, 2 if measured else 1, Qt.PenStyle.SolidLine if measured else Qt.PenStyle.DashLine))
            p.drawLine(QPointF(x1, y1), QPointF(x2, y2))
            if measured:
                r = math.atan2(y2-y1, x2-x1)
                for x, y, theta in ((x2, y2, r), (x1, y1, r + math.pi)):
                    for offset in (-.55, .55):
                        p.drawLine(QPointF(x, y), QPointF(x-5*math.cos(theta+offset), y-5*math.sin(theta+offset)))
            f=QFont(p.font()); f.setPixelSize(11); p.setFont(f)
            p.drawText(QPointF(tx, ty), label)
        if self.profile.startswith('mill'):
            for rect in ((137,9,24,64),(99,73,79,8),(109,10,49,19),(112,62,21,11),(89,53,69,9),(119,29,10,10)):
                part(*rect)
            p.drawLine(QPointF(124,39), QPointF(124,48))
            arrow(61,60,87,60,'X',68,53)
            arrow(83,82,103,67,'Y',71,85)
            arrow(183,20,183,60,'Z',190,44,self.profile!='millxy')
            arrow(102,30,102,47,'Q',88,42,self.profile=='mill3q',self.profile=='mill3q')
        else:
            for rect in ((42,19,31,44),(73,29,13,23),(44,64,169,8),(51,72,13,9),(193,72,13,9),(178,36,31,26),(119,53,35,11),(129,42,15,11)):
                part(*rect)
            p.drawLine(QPointF(87,41),QPointF(176,41))
            if self.profile=='lathe3':
                p.drawPolygon(QPolygonF([QPointF(128,46),QPointF(151,31),QPointF(156,38),QPointF(133,53)]))
            arrow(106,31,106,53,'X',94,46)
            arrow(120,82,158,82,'Z1',165,86)
            arrow(138,30,160,16,'Z2',165,20,self.profile=='lathe3',self.profile=='lathe3')
