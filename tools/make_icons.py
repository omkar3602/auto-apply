"""Regenerates the PWA icons in static/icons/.

    pip install pillow && python tools/make_icons.py

Design: a fanned stack of job cards with a check on the front one - the
app's actual loop, flipping through cards and deciding. Colours are the
app's own (#111 chrome, #00f0a0 accent from style.css).

Two variants are produced, because they get cropped differently:
- `icon-*.png` ("any"): content fills ~72% of the frame. Also serves as the
  iOS apple-touch-icon, which is why the background is full-bleed and
  opaque - iOS ignores transparency and rounds the corners itself.
- `icon-maskable-512.png` ("maskable"): Android crops this to a circle or
  squircle, keeping only the inner 80%. Content is scaled down so the whole
  stack stays inside that safe zone.

Pillow is a build-time-only dependency - deliberately not in
requirements.txt, since the app never generates icons at runtime.
"""

import os

from PIL import Image, ImageDraw

SS = 4  # supersample factor; downsampled at the end for smooth edges

BG = "#111111"      # matches the app's topbar/button black
GREEN = "#00f0a0"   # --Green accent
MID = "#0f9c6c"
DEEP = "#0c5f45"

OUT_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "static", "icons")


def _card(w, h, radius, fill, angle):
    """One rounded card, rotated. Drawn with a small margin so rotation
    doesn't clip the corners."""
    img = Image.new("RGBA", (w + 8, h + 8), (0, 0, 0, 0))
    ImageDraw.Draw(img).rounded_rectangle([4, 4, w + 3, h + 3], radius=radius, fill=fill)
    return img.rotate(angle, expand=True, resample=Image.BICUBIC)


def _paste_centred(base, img, cx, cy):
    base.alpha_composite(img, (int(cx - img.width / 2), int(cy - img.height / 2)))


def render(size=512, content_scale=1.0):
    canvas = size * SS
    k = SS * content_scale
    base = Image.new("RGBA", (canvas, canvas), BG)

    cw, ch, radius = int(190 * k), int(240 * k), int(26 * k)
    # +9 nudge re-centres the fanned group, which otherwise leans left
    cx, cy = canvas // 2 + int(9 * k), canvas // 2

    _paste_centred(base, _card(cw, ch, radius, DEEP, 15), cx - 46 * k, cy - 8 * k)
    _paste_centred(base, _card(cw, ch, radius, MID, 7), cx - 22 * k, cy + 1 * k)

    fx, fy = cx + 24 * k, cy + 9 * k
    _paste_centred(base, _card(cw, ch, radius, GREEN, -5), fx, fy)

    s = 54 * k
    ImageDraw.Draw(base).line(
        [(fx - s, fy + 2 * k), (fx - s * 0.22, fy + s * 0.70), (fx + s * 1.02, fy - s * 0.78)],
        fill=BG, width=int(26 * k), joint="curve",
    )

    return base.convert("RGB").resize((size, size), Image.LANCZOS)


def main():
    os.makedirs(OUT_DIR, exist_ok=True)

    standard = render(512, content_scale=1.15)
    standard.save(os.path.join(OUT_DIR, "icon-512.png"))
    standard.resize((192, 192), Image.LANCZOS).save(os.path.join(OUT_DIR, "icon-192.png"))

    # 0.88 keeps the stack's corners inside the 80% maskable safe circle
    render(512, content_scale=0.88).save(os.path.join(OUT_DIR, "icon-maskable-512.png"))

    print(f"wrote icon-192.png, icon-512.png, icon-maskable-512.png -> {OUT_DIR}")


if __name__ == "__main__":
    main()
