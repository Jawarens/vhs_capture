"""
Modern VHS & Camcorder Icon Generator for VHS Capture.

Combines the iconic camcorder body & precision lens with the classic VHS tape cassette & spools.
Renders with anti-aliasing, rich lighting, optical glass reflections, and glowing tally REC light.
Outputs:
  - vhs_capture.ico (multi-resolution: 256, 128, 64, 48, 32, 16 px)
  - vhs_camcorder_icon.png (high-resolution 1024x1024 master)
"""

import sys
import os
import math
from PyQt6.QtCore import Qt, QRectF, QPointF
from PyQt6.QtGui import (
    QGuiApplication, QImage, QPainter, QPainterPath, QColor,
    QLinearGradient, QRadialGradient, QPen, QBrush, QFont
)
from PIL import Image

def render_icon(canvas_size=1024):
    """Render the modern VHS Camcorder icon into a QImage and multi-size ICO."""
    # Ensure QGuiApplication exists for font metrics and painter rendering
    app = QGuiApplication.instance()
    _app_created = False
    if app is None:
        app = QGuiApplication(sys.argv if sys.argv else ["vhs_icon"])
        _app_created = True

    img = QImage(canvas_size, canvas_size, QImage.Format.Format_ARGB32_Premultiplied)
    img.fill(Qt.GlobalColor.transparent)

    p = QPainter(img)
    p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
    p.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, True)

    scale = canvas_size / 1024.0

    def s(val):
        return val * scale

    # ── 1. Soft Ambient Drop Shadow ──────────────────────────────────────
    shadow_path = QPainterPath()
    shadow_path.addRoundedRect(QRectF(s(100), s(200), s(824), s(660)), s(90), s(90))
    for i in range(12, 0, -1):
        p.save()
        p.setPen(Qt.PenStyle.NoPen)
        alpha = int(4 * (13 - i))
        p.setBrush(QColor(0, 0, 0, alpha))
        p.translate(0, s(i * 3.5))
        p.drawPath(shadow_path)
        p.restore()

    # ── 2. Top Handle & Viewfinder ───────────────────────────────────────
    # Handle bridge
    handle_path = QPainterPath()
    handle_path.moveTo(s(280), s(220))
    handle_path.lineTo(s(310), s(110))
    handle_path.lineTo(s(710), s(110))
    handle_path.lineTo(s(740), s(220))
    handle_path.closeSubpath()

    handle_grad = QLinearGradient(0, s(110), 0, s(220))
    handle_grad.setColorAt(0.0, QColor(58, 62, 74))
    handle_grad.setColorAt(0.5, QColor(38, 41, 50))
    handle_grad.setColorAt(1.0, QColor(24, 26, 32))
    p.setPen(Qt.PenStyle.NoPen)
    p.setBrush(handle_grad)
    p.drawPath(handle_path)

    # Handle inner cutout
    cutout = QPainterPath()
    cutout.moveTo(s(360), s(200))
    cutout.lineTo(s(380), s(165))
    cutout.lineTo(s(640), s(165))
    cutout.lineTo(s(660), s(200))
    cutout.closeSubpath()
    p.setBrush(QColor(22, 24, 28))
    p.drawPath(cutout)

    # Handle grip ridges
    p.setPen(QPen(QColor(18, 19, 23), s(6), Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
    for gx in range(410, 620, 32):
        p.drawLine(QPointF(s(gx), s(122)), QPointF(s(gx), s(152)))

    # Viewfinder Eyepiece (top right)
    vf_path = QPainterPath()
    vf_path.addRoundedRect(QRectF(s(710), s(118), s(150), s(82)), s(18), s(18))
    vf_grad = QLinearGradient(0, s(118), 0, s(200))
    vf_grad.setColorAt(0.0, QColor(48, 52, 62))
    vf_grad.setColorAt(1.0, QColor(22, 24, 30))
    p.setPen(Qt.PenStyle.NoPen)
    p.setBrush(vf_grad)
    p.drawPath(vf_path)

    # Eyepiece rubber cup
    p.setBrush(QColor(16, 18, 22))
    p.drawRoundedRect(QRectF(s(850), s(128), s(35), s(62)), s(10), s(10))

    # ── 3. Main Camcorder Body ───────────────────────────────────────────
    body_rect = QRectF(s(100), s(195), s(824), s(635))
    body_path = QPainterPath()
    body_path.addRoundedRect(body_rect, s(80), s(80))

    body_grad = QLinearGradient(0, s(195), 0, s(830))
    body_grad.setColorAt(0.0, QColor(50, 54, 65))
    body_grad.setColorAt(0.08, QColor(36, 39, 48))
    body_grad.setColorAt(0.70, QColor(26, 28, 35))
    body_grad.setColorAt(1.0, QColor(18, 19, 24))

    p.setBrush(body_grad)
    p.setPen(QPen(QColor(70, 76, 92), s(2.5)))
    p.drawPath(body_path)

    # Top bevel highlight line
    p.setPen(QPen(QColor(255, 255, 255, 38), s(1.5)))
    p.drawRoundedRect(QRectF(s(102), s(197), s(820), s(631)), s(78), s(78))

    # ── 4. Precision Optical Lens Barrel (Left) ──────────────────────────
    lens_cx = s(285)
    lens_cy = s(505)
    lens_r_outer = s(160)

    # Outer metallic bezel
    bezel_grad = QLinearGradient(lens_cx - lens_r_outer, lens_cy - lens_r_outer,
                                 lens_cx + lens_r_outer, lens_cy + lens_r_outer)
    bezel_grad.setColorAt(0.0, QColor(68, 73, 86))
    bezel_grad.setColorAt(0.5, QColor(32, 34, 42))
    bezel_grad.setColorAt(1.0, QColor(14, 15, 18))
    p.setPen(QPen(QColor(15, 16, 20), s(3)))
    p.setBrush(bezel_grad)
    p.drawEllipse(QPointF(lens_cx, lens_cy), lens_r_outer, lens_r_outer)

    # Focus ring grip notches (radial tick marks)
    p.setPen(QPen(QColor(18, 20, 24), s(3.5), Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
    for deg in range(0, 360, 15):
        rad = math.radians(deg)
        x1 = lens_cx + math.cos(rad) * (lens_r_outer - s(14))
        y1 = lens_cy + math.sin(rad) * (lens_r_outer - s(14))
        x2 = lens_cx + math.cos(rad) * (lens_r_outer - s(3))
        y2 = lens_cy + math.sin(rad) * (lens_r_outer - s(3))
        p.drawLine(QPointF(x1, y1), QPointF(x2, y2))

    # Inner barrel stepped ring
    lens_r_mid = s(132)
    ring_grad = QLinearGradient(0, lens_cy - lens_r_mid, 0, lens_cy + lens_r_mid)
    ring_grad.setColorAt(0.0, QColor(20, 22, 28))
    ring_grad.setColorAt(1.0, QColor(40, 44, 54))
    p.setPen(QPen(QColor(55, 60, 72), s(1.5)))
    p.setBrush(ring_grad)
    p.drawEllipse(QPointF(lens_cx, lens_cy), lens_r_mid, lens_r_mid)

    # Lens optical glass element (Deep rich coating with cyan/cobalt sheen)
    lens_r_glass = s(118)
    glass_grad = QRadialGradient(lens_cx - s(25), lens_cy - s(30), lens_r_glass * 1.2)
    glass_grad.setColorAt(0.0, QColor(0, 140, 215, 230))
    glass_grad.setColorAt(0.35, QColor(0, 60, 120, 240))
    glass_grad.setColorAt(0.70, QColor(6, 18, 38, 255))
    glass_grad.setColorAt(1.0, QColor(2, 6, 12, 255))
    p.setPen(Qt.PenStyle.NoPen)
    p.setBrush(glass_grad)
    p.drawEllipse(QPointF(lens_cx, lens_cy), lens_r_glass, lens_r_glass)

    # Iris aperture core
    iris_r = s(52)
    iris_grad = QRadialGradient(lens_cx, lens_cy, iris_r)
    iris_grad.setColorAt(0.0, QColor(8, 12, 20))
    iris_grad.setColorAt(0.85, QColor(3, 4, 8))
    iris_grad.setColorAt(1.0, QColor(20, 26, 40))
    p.setBrush(iris_grad)
    p.drawEllipse(QPointF(lens_cx, lens_cy), iris_r, iris_r)

    # Optical glass reflections (multi-coated lens glints)
    # Primary specular highlight
    p.setBrush(QColor(255, 255, 255, 220))
    p.drawEllipse(QPointF(lens_cx - s(30), lens_cy - s(32)), s(8), s(8))

    # Secondary softer glare arc
    glare_path = QPainterPath()
    glare_path.addEllipse(QPointF(lens_cx, lens_cy), lens_r_glass - s(8), lens_r_glass - s(8))
    p.save()
    p.setClipPath(glare_path)
    glare_grad = QLinearGradient(lens_cx - lens_r_glass, lens_cy - lens_r_glass,
                                 lens_cx + lens_r_glass, lens_cy + lens_r_glass)
    glare_grad.setColorAt(0.0, QColor(70, 180, 255, 110))
    glare_grad.setColorAt(0.25, QColor(140, 90, 240, 50))
    glare_grad.setColorAt(0.5, QColor(255, 255, 255, 0))
    p.setBrush(glare_grad)
    p.drawRect(QRectF(lens_cx - lens_r_glass, lens_cy - lens_r_glass, lens_r_glass * 2, lens_r_glass * 2))
    p.restore()

    # ── 5. VHS Cassette Compartment & Mechanism (Right) ──────────────────
    door_rect = QRectF(s(480), s(265), s(400), s(485))
    door_path = QPainterPath()
    door_path.addRoundedRect(door_rect, s(28), s(28))

    door_grad = QLinearGradient(0, s(265), 0, s(750))
    door_grad.setColorAt(0.0, QColor(28, 30, 38))
    door_grad.setColorAt(1.0, QColor(18, 19, 24))
    p.setPen(QPen(QColor(48, 52, 64), s(2)))
    p.setBrush(door_grad)
    p.drawPath(door_path)

    # Door inset seam
    p.setPen(QPen(QColor(12, 13, 16), s(2)))
    p.drawRoundedRect(QRectF(s(483), s(268), s(394), s(479)), s(26), s(26))

    # VHS Cassette Label (Retro cream label with header)
    label_rect = QRectF(s(505), s(288), s(350), s(45))
    label_path = QPainterPath()
    label_path.addRoundedRect(label_rect, s(8), s(8))
    label_grad = QLinearGradient(0, s(288), 0, s(333))
    label_grad.setColorAt(0.0, QColor(248, 246, 238))
    label_grad.setColorAt(1.0, QColor(230, 226, 214))
    p.setPen(Qt.PenStyle.NoPen)
    p.setBrush(label_grad)
    p.drawPath(label_path)

    # Label Brand Title: "VHS  HQ"
    p.setPen(QColor(25, 28, 35))
    label_font = QFont("Arial", int(s(22)), QFont.Weight.Black)
    label_font.setLetterSpacing(QFont.SpacingType.AbsoluteSpacing, s(4))
    p.setFont(label_font)
    p.drawText(label_rect, Qt.AlignmentFlag.AlignCenter, "VHS  HQ")

    # VHS Tape Viewing Window (Smoked acrylic)
    win_rect = QRectF(s(505), s(350), s(350), s(240))
    win_path = QPainterPath()
    win_path.addRoundedRect(win_rect, s(16), s(16))

    win_grad = QLinearGradient(0, s(350), 0, s(590))
    win_grad.setColorAt(0.0, QColor(15, 16, 20))
    win_grad.setColorAt(0.5, QColor(22, 24, 30))
    win_grad.setColorAt(1.0, QColor(10, 11, 14))
    p.setPen(QPen(QColor(42, 46, 56), s(2)))
    p.setBrush(win_grad)
    p.drawPath(win_path)

    # Tape path inside window
    p.setPen(Qt.PenStyle.NoPen)
    p.setBrush(QColor(45, 32, 22, 230))
    p.drawRect(QRectF(s(580), s(490), s(200), s(10)))

    # Dual Tape Spools (Reels)
    spool_y = s(470)
    spool1_x = s(590)
    spool2_x = s(770)
    spool_r = s(55)

    def draw_tape_spool(cx, cy, tape_amount=0.8):
        # Wound tape pack
        pack_r = spool_r + s(14 * tape_amount)
        tape_pack_grad = QRadialGradient(cx, cy, pack_r)
        tape_pack_grad.setColorAt(0.0, QColor(36, 25, 18))
        tape_pack_grad.setColorAt(0.85, QColor(24, 16, 12))
        tape_pack_grad.setColorAt(1.0, QColor(15, 10, 8))
        p.setBrush(tape_pack_grad)
        p.setPen(Qt.PenStyle.NoPen)
        p.drawEllipse(QPointF(cx, cy), pack_r, pack_r)

        # White/Ivory plastic reel hub
        hub_grad = QRadialGradient(cx, cy, spool_r)
        hub_grad.setColorAt(0.0, QColor(245, 245, 248))
        hub_grad.setColorAt(0.75, QColor(215, 218, 224))
        hub_grad.setColorAt(1.0, QColor(165, 170, 180))
        p.setBrush(hub_grad)
        p.setPen(QPen(QColor(110, 115, 125), s(1.5)))
        p.drawEllipse(QPointF(cx, cy), spool_r, spool_r)

        # Hub center hole
        p.setBrush(QColor(15, 16, 20))
        p.setPen(Qt.PenStyle.NoPen)
        p.drawEllipse(QPointF(cx, cy), s(20), s(20))

        # Reel drive splines / teeth notches (3 teeth)
        p.setPen(QPen(QColor(70, 75, 85), s(3), Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
        for deg in [0, 120, 240]:
            rad = math.radians(deg)
            nx1 = cx + math.cos(rad) * s(18)
            ny1 = cy + math.sin(rad) * s(18)
            nx2 = cx + math.cos(rad) * s(32)
            ny2 = cy + math.sin(rad) * s(32)
            p.drawLine(QPointF(nx1, ny1), QPointF(nx2, ny2))

        # Hub inspection holes
        p.setBrush(QColor(60, 64, 75))
        p.setPen(Qt.PenStyle.NoPen)
        for deg in [60, 180, 300]:
            rad = math.radians(deg)
            hx = cx + math.cos(rad) * s(36)
            hy = cy + math.sin(rad) * s(36)
            p.drawEllipse(QPointF(hx, hy), s(5), s(5))

    # Draw left spool (feed reel) & right spool (takeup reel)
    draw_tape_spool(spool1_x, spool_y, tape_amount=0.85)
    draw_tape_spool(spool2_x, spool_y, tape_amount=0.45)

    # Glass reflection on cassette window
    p.save()
    p.setClipPath(win_path)
    ref_path = QPainterPath()
    ref_path.moveTo(s(500), s(350))
    ref_path.lineTo(s(750), s(350))
    ref_path.lineTo(s(680), s(590))
    ref_path.lineTo(s(500), s(590))
    ref_path.closeSubpath()
    ref_grad = QLinearGradient(s(500), s(350), s(750), s(590))
    ref_grad.setColorAt(0.0, QColor(255, 255, 255, 30))
    ref_grad.setColorAt(0.5, QColor(255, 255, 255, 8))
    ref_grad.setColorAt(1.0, QColor(255, 255, 255, 0))
    p.setPen(Qt.PenStyle.NoPen)
    p.setBrush(ref_grad)
    p.drawPath(ref_path)
    p.restore()

    # ── 6. Iconic VHS Tricolor Racing Stripes ────────────────────────────
    # High-impact retro-modern identity: Cyan, Magenta-Red, Gold
    stripe_y = s(618)
    stripe_h = s(9)
    stripe_gap = s(4)
    stripe_w = s(350)
    stripe_x = s(500)

    # Cyan / Blue
    p.setBrush(QColor(0, 162, 232))
    p.drawRoundedRect(QRectF(stripe_x, stripe_y, stripe_w, stripe_h), s(3), s(3))

    # Vibrant Red / Magenta
    p.setBrush(QColor(235, 40, 55))
    p.drawRoundedRect(QRectF(stripe_x, stripe_y + stripe_h + stripe_gap, stripe_w, stripe_h), s(3), s(3))

    # Warm Gold / Amber
    p.setBrush(QColor(255, 185, 20))
    p.drawRoundedRect(QRectF(stripe_x, stripe_y + (stripe_h + stripe_gap) * 2, stripe_w, stripe_h), s(3), s(3))

    # ── 7. Luminous REC Tally Indicator Lamp ─────────────────────────────
    tally_cx = s(210)
    tally_cy = s(250)
    tally_r = s(18)

    # Ambient radial glow
    bloom = QRadialGradient(tally_cx, tally_cy, s(60))
    bloom.setColorAt(0.0, QColor(255, 30, 50, 160))
    bloom.setColorAt(0.4, QColor(255, 20, 40, 60))
    bloom.setColorAt(1.0, QColor(255, 0, 0, 0))
    p.setBrush(bloom)
    p.drawEllipse(QPointF(tally_cx, tally_cy), s(60), s(60))

    # Bezel around tally LED
    p.setPen(QPen(QColor(20, 22, 26), s(2)))
    p.setBrush(QColor(32, 35, 42))
    p.drawEllipse(QPointF(tally_cx, tally_cy), tally_r + s(4), tally_r + s(4))

    # Brilliant Ruby LED core
    led_grad = QRadialGradient(tally_cx - s(4), tally_cy - s(4), tally_r)
    led_grad.setColorAt(0.0, QColor(255, 120, 140))
    led_grad.setColorAt(0.4, QColor(255, 20, 50))
    led_grad.setColorAt(1.0, QColor(160, 0, 20))
    p.setPen(Qt.PenStyle.NoPen)
    p.setBrush(led_grad)
    p.drawEllipse(QPointF(tally_cx, tally_cy), tally_r, tally_r)

    # Pinpoint glint on LED
    p.setBrush(QColor(255, 255, 255, 240))
    p.drawEllipse(QPointF(tally_cx - s(5), tally_cy - s(5)), s(4), s(4))

    # Subtle REC label next to tally light
    p.setPen(QColor(220, 225, 235))
    rec_font = QFont("Arial", int(s(16)), QFont.Weight.Bold)
    p.setFont(rec_font)
    p.drawText(QRectF(tally_cx + s(28), tally_cy - s(12), s(80), s(24)),
               Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft, "REC")

    p.end()

    # Save to high-resolution PNG using QImage
    png_path = os.path.join(os.path.dirname(__file__), "vhs_camcorder_icon.png")
    img.save(png_path)
    print(f"Rendered high-res preview icon: {png_path}")

    # Generate multi-size ICO with Pillow
    sizes = [256, 128, 64, 48, 32, 16]
    pil_images = []
    
    # Convert QImage to PIL Image
    buffer = img.bits().asstring(img.sizeInBytes())
    master_pil = Image.frombuffer('RGBA', (canvas_size, canvas_size), buffer, 'raw', 'BGRA', 0, 1)

    for sz in sizes:
        resized = master_pil.resize((sz, sz), Image.Resampling.LANCZOS)
        pil_images.append(resized)

    ico_path = os.path.join(os.path.dirname(__file__), "vhs_capture.ico")
    pil_images[0].save(
        ico_path,
        format='ICO',
        sizes=[(s, s) for s in sizes],
        append_images=pil_images[1:]
    )
    print(f"Generated multi-size ICO: {ico_path} with sizes: {sizes}")

def create_vhs_icon():
    """Compatibility entry point."""
    render_icon(1024)

if __name__ == '__main__':
    render_icon(1024)
