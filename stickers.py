"""Text sticker (lower-third / title card) renderer. Used for both the editor preview and ffmpeg export,
so what you see in the preview is exactly what is exported."""
import io
import os
from PIL import Image, ImageDraw, ImageFont, ImageFilter

FONT = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'fonts', 'Pretendard-Bold.otf')
SS = 2  # supersampling


def _hex(c, default=(245, 158, 11)):
    try:
        c = c.lstrip('#')
        return tuple(int(c[i:i + 2], 16) for i in (0, 2, 4))
    except Exception:
        return default


def render(lines, kind, color, H, scale):
    """Returns (PNG bytes, width, height). Sizes are in output-frame pixels (H = output frame height)."""
    lines = [l for l in (lines or []) if l.strip()] or [' ']
    fs = max(10, round(H * 0.045 * max(0.3, min(3.0, scale))))
    S = fs * SS
    font = ImageFont.truetype(FONT, S)
    probe = ImageDraw.Draw(Image.new('RGBA', (4, 4)))
    boxes = [probe.textbbox((0, 0), l, font=font) for l in lines]
    tw = max(b[2] - b[0] for b in boxes)
    lh = int(S * 1.32)
    padx, pady = int(S * 0.95), int(S * 0.5)
    cw, ch = tw + 2 * padx, lh * len(lines) + 2 * pady - int(S * 0.12)
    accent = _hex(color)
    margin = int(S * 0.6)
    if kind == 'name':
        ox, oy = int(S * 0.42), int(S * 0.36)
        rad = int(S * 0.5)
        top = margin
        left = margin + ox
    else:
        qh = int(S * 1.15)
        rad = int(S * 0.85)
        top = margin + int(qh * 0.55)
        left = margin
    W_, H_ = left + cw + margin + (0 if kind == 'verse' else 0), top + ch + margin + (oy if kind == 'name' else 0)
    img = Image.new('RGBA', (W_, H_), (0, 0, 0, 0))
    # soft shadow
    sh = Image.new('RGBA', img.size, (0, 0, 0, 0))
    sd = ImageDraw.Draw(sh)
    sd.rounded_rectangle((left, top + int(S * 0.1), left + cw, top + ch + int(S * 0.1)), rad, fill=(0, 0, 0, 90))
    sh = sh.filter(ImageFilter.GaussianBlur(S * 0.12))
    img = Image.alpha_composite(img, sh)
    d = ImageDraw.Draw(img)
    if kind == 'name':
        d.rounded_rectangle((left - ox, top + oy, left - ox + cw, top + oy + ch), rad, fill=accent + (255,))
    d.rounded_rectangle((left, top, left + cw, top + ch), rad, fill=(255, 255, 255, 255))
    y = top + pady
    for l, b in zip(lines, boxes):
        w = b[2] - b[0]
        d.text((left + (cw - w) / 2 - b[0], y - b[1] + (lh - (b[3] - b[1])) / 2 - S * 0.04), l, font=font, fill=(17, 17, 17, 255))
        y += lh
    if kind == 'verse':
        r = S * 0.2
        cy = margin + qh * 0.62
        for dx in (-S * 0.3, S * 0.3):
            cx = left + cw / 2 + dx
            d.ellipse((cx - r, cy - r, cx + r, cy + r), fill=accent + (255,))
            d.polygon([(cx - r * 0.98, cy), (cx - r * 0.2, cy - r * 2.05), (cx + r * 0.95, cy - r * 1.75),
                       (cx + r * 0.2, cy - r * 0.2)], fill=accent + (255,))
    img = img.resize((max(2, W_ // SS), max(2, H_ // SS)), Image.LANCZOS)
    buf = io.BytesIO()
    img.save(buf, 'PNG')
    return buf.getvalue(), img.size[0], img.size[1]
