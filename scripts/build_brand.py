"""Generate the brand asset PNGs from a single source.

Produces:
  docs/brand/mark-{16,24,32,64,128,256,512,1024}.png
  docs/brand/social-card.png    (1200×630)
  suprbar/static/brand/icon-{16,32,48,64,256}.ico   (for .exe + windows)

Run:  python scripts/build_brand.py
"""

from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw, ImageFont, ImageFilter


ROOT = Path(__file__).resolve().parent.parent
OUT_DOCS = ROOT / "docs" / "brand"
OUT_STATIC = ROOT / "suprbar" / "static" / "brand"
OUT_DOCS.mkdir(parents=True, exist_ok=True)
OUT_STATIC.mkdir(parents=True, exist_ok=True)

ACCENT = (91, 141, 239)       # #5b8def
VIOLET = (140, 91, 239)       # #8c5bef
WHITE = (255, 255, 255, 255)


def gradient_tile(size: int) -> Image.Image:
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    px = img.load()
    denom = 2 * (size - 1) if size > 1 else 1
    for y in range(size):
        for x in range(size):
            t = (x + y) / denom
            r = int(ACCENT[0] + (VIOLET[0] - ACCENT[0]) * t)
            g = int(ACCENT[1] + (VIOLET[1] - ACCENT[1]) * t)
            b = int(ACCENT[2] + (VIOLET[2] - ACCENT[2]) * t)
            px[x, y] = (r, g, b, 255)
    return img


def round_mask(size: int, radius_ratio: float = 0.19) -> Image.Image:
    mask = Image.new("L", (size, size), 0)
    d = ImageDraw.Draw(mask)
    d.rounded_rectangle((0, 0, size - 1, size - 1),
                        radius=int(size * radius_ratio), fill=255)
    return mask


def load_bold_font(size_px: int) -> ImageFont.ImageFont:
    for candidate in ("seguibl.ttf", "segoeuib.ttf",
                      "arialbd.ttf", "arial.ttf"):
        try:
            return ImageFont.truetype(candidate, size_px)
        except OSError:
            continue
    return ImageFont.load_default()


def draw_S(bg: Image.Image) -> Image.Image:
    size = bg.size[0]
    bg.putalpha(round_mask(size))
    font = load_bold_font(int(size * 0.62))
    overlay = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    od = ImageDraw.Draw(overlay)
    bbox = od.textbbox((0, 0), "S", font=font)
    tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
    tx = (size - tw) // 2 - bbox[0]
    ty = (size - th) // 2 - bbox[1] - int(size * 0.035)
    od.text((tx, ty), "S", font=font, fill=WHITE)
    return Image.alpha_composite(bg, overlay)


def make_mark(size: int) -> Image.Image:
    # Render at 4× for super-clean LANCZOS downsample.
    big = max(size * 4, 256)
    img = draw_S(gradient_tile(big))
    img.thumbnail((size, size), Image.LANCZOS)
    return img


def make_social_card(w: int = 1200, h: int = 630) -> Image.Image:
    # dark gradient background with subtle violet radial in corner
    bg = Image.new("RGBA", (w, h), (13, 16, 24, 255))
    # very subtle blue radial top-left
    overlay = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    od = ImageDraw.Draw(overlay)
    for r in range(0, max(w, h), 4):
        alpha = max(0, 20 - r // 30)
        if alpha == 0:
            break
        od.ellipse((-r // 2, -r // 2, r, r),
                   outline=(91, 141, 239, alpha), width=2)
    overlay = overlay.filter(ImageFilter.GaussianBlur(40))
    bg = Image.alpha_composite(bg, overlay)

    # paste big mark + wordmark text
    mark_size = 140
    mark = make_mark(mark_size)
    bg.paste(mark, (80, 80), mark)

    d = ImageDraw.Draw(bg)
    title = load_bold_font(64)
    sub_f = load_bold_font(28)
    d.text((80, 240), "supr.bar", font=title, fill=(244, 245, 247, 255))
    d.text((80, 320),
           "a coach in your tray, not a counter.",
           font=sub_f,
           fill=(140, 145, 160, 255))
    tag = load_bold_font(20)
    d.text((80, h - 80),
           "MIT  ·  github.com/omertaji/suprbar",
           font=tag,
           fill=(110, 115, 130, 255))
    return bg


def main() -> None:
    sizes_png = [16, 24, 32, 64, 128, 256, 512, 1024]
    for s in sizes_png:
        out = OUT_DOCS / f"mark-{s}.png"
        make_mark(s).save(out, "PNG", optimize=True)
        print(f"  wrote {out.relative_to(ROOT)}")

    # social card
    out = OUT_DOCS / "social-card.png"
    make_social_card().save(out, "PNG", optimize=True)
    print(f"  wrote {out.relative_to(ROOT)}")

    # ICO with multiple sizes for the .exe + Windows app icon
    ico_sizes = [(16, 16), (32, 32), (48, 48), (64, 64), (256, 256)]
    biggest = make_mark(256)
    ico_path = OUT_STATIC / "suprbar.ico"
    biggest.save(ico_path, format="ICO", sizes=ico_sizes)
    print(f"  wrote {ico_path.relative_to(ROOT)}")

    # SVG mark — write a minimal hand-rolled SVG so the wordmark scales.
    svg_path = OUT_DOCS / "mark.svg"
    svg_path.write_text(
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64">\n'
        '  <defs>\n'
        '    <linearGradient id="g" x1="0%" y1="0%" x2="100%" y2="100%">\n'
        '      <stop offset="0%" stop-color="#5b8def"/>\n'
        '      <stop offset="100%" stop-color="#8c5bef"/>\n'
        '    </linearGradient>\n'
        '  </defs>\n'
        '  <rect x="0" y="0" width="64" height="64" rx="12" ry="12" fill="url(#g)"/>\n'
        '  <text x="50%" y="58%" text-anchor="middle" dominant-baseline="middle"\n'
        '        font-family="Segoe UI, system-ui, sans-serif" font-weight="900" font-size="40"\n'
        '        fill="white">S</text>\n'
        '</svg>\n',
        encoding="utf-8",
    )
    print(f"  wrote {svg_path.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
