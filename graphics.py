"""
graphics.py — Branded image cards for Facebook photo posts
==============================================================
Facebook's algorithm meaningfully favors photo posts over plain text.
This renders on-brand PNGs so bot.py can use poster.post_photo()
instead of poster.post() — no external image APIs, no stock-photo
licensing.

IMPORTANT DESIGN NOTE (read before touching marker/badge text):
Pillow's bundled default font ("Aileron") has NO emoji glyphs. Any
emoji character drawn with ImageFont.load_default() renders as a
blank box, not the emoji. So this file never draws emoji onto the
canvas — every "marker" is a real vector badge (colored pill +
plain-text label) instead. Emoji are still fine in the Facebook
*caption* (poster.py), since Facebook's own renderer draws those,
not Pillow.

Two families of card:
  render_card         — generic stat/news card (title + body lines)
  render_score_card    — kickoff / goal / full-time, two-team layout
  render_photo_card    — NEW: wraps a real downloaded photo (e.g. a
                          player photo from a transfer headline) with
                          the same badge/ribbon branding, instead of
                          generating a text-only card. Falls back to
                          render_card automatically if no usable image
                          is available.
"""

import os
import time
import math
import textwrap
import requests
from io import BytesIO
from PIL import Image, ImageDraw, ImageFont, ImageFilter

CARD_SIZE = (1080, 1080)
OUT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "cards")
os.makedirs(OUT_DIR, exist_ok=True)

# ── Real bundled font (Poppins, OFL-licensed via Google Fonts) ─────────
# Pillow's load_default() only ships one thin weight, which is the #1
# reason generated cards read as "generic/flat" next to a template made
# in Canva. Poppins ships ExtraBold/Bold/SemiBold/Regular, so headlines,
# scores and body text can each get the right weight instead of every
# line looking the same thickness. Ship these four .ttf files in a
# "fonts/" folder next to graphics.py. If they're missing (e.g. a fresh
# clone before committing the fonts folder), everything falls back to
# Pillow's default font automatically — nothing crashes.
FONTS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fonts")
_FONT_FILES = {
    "extrabold": "Poppins-ExtraBold.ttf",
    "bold":      "Poppins-Bold.ttf",
    "semibold":  "Poppins-SemiBold.ttf",
    "regular":   "Poppins-Regular.ttf",
}
_font_cache: dict = {}

BRAND_NAME = "MATCH CORNA LIVE"
BRAND_TAGLINE = "Follow the page for live scores"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                  "AppleWebKit/537.36 (KHTML, like Gecko) "
                  "Chrome/131.0.0.0 Safari/537.36",
}

# Background color per post category
ACCENTS = {
    "default":  "#14532D",  # pitch green
    "goal":     "#14532D",
    "kickoff":  "#166534",
    "halftime": "#1E3A5F",  # deep blue — same family as full-time
    "redcard":  "#7A1F1F",  # red — same family as VAR
    "fulltime": "#1E3A5F",  # deep blue
    "var":      "#7A1F1F",  # red
    "transfer": "#7C2D12",  # burnt orange/red — fallback for any uncategorized news
    "manager":  "#0C4A6E",  # deep sky blue — manager transfer/appointment
    "manager_sacking": "#7A1F1F",  # red — a sacking reads as more urgent/negative than an appointment
    "deal_done": "#166534",  # green — confirmed/positive
    "deal_collapsed": "#7A1F1F",  # red — the move fell through
    "player_quote": "#B45309",  # amber — spotlight/quote feel, distinct from transfer's burnt orange
    "injury":   "#374151",  # slate gray — subdued/clinical, distinct from the red urgency of a sacking
    "stat":     "#4C1D95",  # violet — visually distinct from live-match and news colors, reads as "stats/trivia" at a glance
}

# Badge (pill) style per category — drawn, never emoji.
BADGES = {
    "default":  ("MATCH UPDATE",     "#14532D"),
    "goal":     ("GOAL",             "#B45309"),
    "kickoff":  ("KICK-OFF",         "#15803D"),
    "halftime": ("HALF TIME",        "#1E3A5F"),
    "redcard":  ("RED CARD",         "#7A1F1F"),
    "fulltime": ("FULL TIME",        "#1E3A5F"),
    "var":      ("VAR REVIEW",       "#7A1F1F"),
    "transfer": ("TRANSFER NEWS",    "#C2410C"),
    "manager":  ("MANAGER NEWS",     "#0369A1"),
    "manager_sacking": ("MANAGER SACKING", "#DC2626"),
    "deal_done": ("DEAL DONE",       "#16A34A"),
    "deal_collapsed": ("DEAL COLLAPSED", "#DC2626"),
    "player_quote": ("PLAYER SPOTLIGHT", "#B45309"),
    "injury":   ("INJURY NEWS",      "#4B5563"),
    "stat":     ("STAT SPOTLIGHT",   "#6D28D9"),
}

GOLD  = "#FFD400"
WHITE = "#FFFFFF"
INK   = "#0B0F0C"

# Deterministic palette used for initials-avatar fallback when a crest
# can't be fetched — keeps team "identity" visually consistent run to run.
_AVATAR_PALETTE = [
    "#EF4444", "#F59E0B", "#10B981", "#3B82F6",
    "#8B5CF6", "#EC4899", "#14B8A6", "#F97316",
]


def _hex_to_rgb(h: str) -> tuple:
    h = h.lstrip("#")
    return tuple(int(h[i:i + 2], 16) for i in (0, 2, 4))


def _font(size: int, weight: str = "semibold") -> ImageFont.FreeTypeFont:
    """Loads the bundled Poppins weight at `size`, cached per (weight,
    size) pair. `weight` is one of "extrabold"/"bold"/"semibold"/
    "regular". Falls back to Pillow's built-in font if the .ttf files
    haven't been shipped (so this never crashes a deploy)."""
    key = (weight, size)
    if key in _font_cache:
        return _font_cache[key]
    filename = _FONT_FILES.get(weight, _FONT_FILES["semibold"])
    path = os.path.join(FONTS_DIR, filename)
    try:
        font = ImageFont.truetype(path, size)
    except Exception:
        font = ImageFont.load_default(size=size)
    _font_cache[key] = font
    return font


def _text_w(draw, text, font) -> int:
    bbox = draw.textbbox((0, 0), text, font=font)
    return bbox[2] - bbox[0]


def _shadow_text(draw, xy, text, font, fill=WHITE, stroke_width=0,
                  shadow=(0, 0, 0, 130), offset=(0, 3)):
    """Draws a soft drop shadow behind text so it stays readable over
    photos or busy gradients, then the text itself on top."""
    x, y = xy
    draw.text((x + offset[0], y + offset[1]), text, font=font, fill=shadow)
    draw.text((x, y), text, font=font, fill=fill, stroke_width=stroke_width,
               stroke_fill=fill)


def _fetch_image(url: str, timeout: int = 8):
    """Best-effort image fetch (crest, photo, etc). Returns a PIL Image
    in RGBA, or None on any failure — callers must have a fallback."""
    if not url:
        return None
    try:
        r = requests.get(url, headers=HEADERS, timeout=timeout)
        if r.status_code != 200:
            return None
        return Image.open(BytesIO(r.content)).convert("RGBA")
    except Exception:
        return None


# Kept for backwards-compat call sites (bot.py imports this name).
def _fetch_crest(url: str, max_size=(170, 170)):
    img = _fetch_image(url)
    if img is None:
        return None
    img.thumbnail(max_size)
    return img


def _save(img) -> str:
    path = os.path.join(OUT_DIR, f"card_{int(time.time() * 1000)}.png")
    img.convert("RGB").save(path, "PNG")
    return path


# ══════════════════════════════════════════════════════════════════
# SHARED CHROME — background, badge pill, brand ribbon
# ══════════════════════════════════════════════════════════════════

def _draw_light_rays(img, n_rays: int = 6, spread: int = 46,
                      length: int = 980, color=(255, 255, 255), max_alpha: int = 30):
    """Stadium floodlight fan, beamed down from both top corners — the
    crisscross rays seen in broadcast-style scoreboard templates. Each
    ray is a thin blurred triangle; overlapping rays near the corner
    naturally brighten, exactly like real floodlight banks."""
    W, H = img.size
    layer = Image.new("RGBA", img.size, (0, 0, 0, 0))
    ldraw = ImageDraw.Draw(layer)
    for corner_x, base_angle in ((0, 55), (W, 125)):
        for i in range(n_rays):
            angle = base_angle + (i - (n_rays - 1) / 2) * (spread / n_rays)
            rad = math.radians(angle)
            dx, dy = math.cos(rad) * length, math.sin(rad) * length
            half_w = 34
            perp = math.radians(angle + 90)
            ox, oy = math.cos(perp) * half_w, math.sin(perp) * half_w
            p1 = (corner_x - ox, -oy)
            p2 = (corner_x + ox, oy)
            p3 = (corner_x + dx, dy)
            ldraw.polygon([p1, p2, p3], fill=(*color, max_alpha))
    layer = layer.filter(ImageFilter.GaussianBlur(10))
    return Image.alpha_composite(img.convert("RGBA"), layer).convert("RGB")


def _base_canvas(accent_key: str):
    color = ACCENTS.get(accent_key, ACCENTS["default"])
    top = _hex_to_rgb(color)
    bottom = tuple(max(0, c - 40) for c in top)
    img = Image.new("RGB", CARD_SIZE, top)
    draw = ImageDraw.Draw(img)
    for y in range(CARD_SIZE[1]):
        t = y / CARD_SIZE[1]
        row = tuple(int(top[i] + (bottom[i] - top[i]) * t) for i in range(3))
        draw.line([(0, y), (CARD_SIZE[0], y)], fill=row)

    # Subtle diagonal texture stripes — cheap way to make a flat gradient
    # look like a designed template instead of a solid color fill.
    stripe = Image.new("RGBA", CARD_SIZE, (0, 0, 0, 0))
    sdraw = ImageDraw.Draw(stripe)
    W, H = CARD_SIZE
    step = 90
    for x in range(-H, W, step):
        sdraw.line([(x, H), (x + H, 0)], fill=(255, 255, 255, 10), width=26)
    img = Image.alpha_composite(img.convert("RGBA"), stripe).convert("RGB")

    # Floodlight beam fans from both top corners — the same broadcast
    # scoreboard cue used across the stadium-canvas cards, so every
    # card family (not just kickoff/goal/full-time) shares the look.
    img = _draw_light_rays(img)
    return img, ImageDraw.Draw(img)


def _draw_badge_pill(draw, text: str, accent_hex: str, xy=(60, 56)):
    """Solid colored pill with bold-ish text — the vector replacement
    for an emoji marker, guaranteed to render on every platform."""
    font = _font(30)
    pad_x, pad_y = 26, 14
    tw = _text_w(draw, text, font)
    x, y = xy
    w, h = tw + pad_x * 2, 30 + pad_y * 2 - 12
    draw.rounded_rectangle([x, y, x + w, y + h], radius=h // 2, fill=WHITE)
    draw.text((x + pad_x, y + pad_y - 6), text, font=font, fill=accent_hex, stroke_width=1, stroke_fill=accent_hex)
    return y + h  # bottom edge, for layout below it


def _draw_ribbon_badge(draw, cx: float, cy: float, text: str, font,
                        fill="#0B0F0C", text_color=WHITE,
                        pad_x: int = 34, pad_y: int = 16, notch: int = 20):
    """A pointed-end ribbon/chevron badge, centered at (cx, cy) — the
    angular banner shape used for scorelines and status labels on
    broadcast scoreboard templates, instead of a plain rounded pill.
    Returns (width, height) of the drawn badge."""
    tw = _text_w(draw, text, font)
    ascent, descent = font.getmetrics()
    th = ascent + descent
    w = tw + pad_x * 2
    h = th + pad_y * 2
    x0, x1 = cx - w / 2, cx + w / 2
    y0, y1 = cy - h / 2, cy + h / 2
    pts = [
        (x0, y0), (x1, y0), (x1 + notch, cy), (x1, y1),
        (x0, y1), (x0 - notch, cy),
    ]
    fill_rgb = _hex_to_rgb(fill) if isinstance(fill, str) else fill
    draw.polygon(pts, fill=fill_rgb)
    draw.text((cx - tw / 2, cy - th / 2), text, font=font, fill=text_color)
    return w, h


def _draw_wordmark(draw, xy=(60, 50)):
    draw.text(xy, BRAND_NAME, font=_font(30), fill=GOLD)


def _draw_brand_ribbon(img, draw):
    """Solid footer ribbon spanning the width — replaces the old loose
    corner caption, reads as an intentional template element. Taller
    + bigger type than the original (30/22pt) — that was hard to read
    at feed thumbnail size, especially the tagline."""
    W, H = img.size
    ribbon_h = 96
    overlay = Image.new("RGBA", (W, ribbon_h), (0, 0, 0, 150))
    img.paste(Image.alpha_composite(img.crop((0, H - ribbon_h, W, H)).convert("RGBA"), overlay), (0, H - ribbon_h))
    draw = ImageDraw.Draw(img)
    draw.text((44, H - ribbon_h + 16), BRAND_NAME, font=_font(38), fill=GOLD)
    draw.text((44, H - ribbon_h + 58), BRAND_TAGLINE, font=_font(27), fill="#E5E7EB")
    return draw


def _hexagon_points(size, inset: float = 0.0, orientation: str = "vertical"):
    """Hexagon vertices inscribed in `size`. `orientation="vertical"`
    gives a flat-top hex (points at top/bottom, flat left/right sides)
    — used for crest/flag badges. `orientation="horizontal"` gives a
    hex pointed on the left/right, flat top/bottom — the wide shape
    used for a scoreline container. `inset` shrinks the hexagon inward
    by that many pixels — used to draw a slightly smaller hexagon for
    a border ring effect."""
    w, h = size
    cx, cy = w / 2, h / 2
    r_x = w / 2 - inset
    r_y = h / 2 - inset
    start = 90 if orientation == "vertical" else 0
    pts = []
    for i in range(6):
        angle = math.radians(start + 60 * i)
        pts.append((cx + r_x * math.cos(angle), cy + r_y * math.sin(angle)))
    return pts


def _hex_badge_frame(size=(170, 170), border_hex: str = "#8FD3E8", fill_hex: str = WHITE):
    """The hexagon 'frame' behind a crest/flag/initials — a light
    border hexagon with a white hexagon inset, so any logo dropped on
    top (dark or light) sits on a clean, consistent hex badge, same
    idea as the reference scoreboard template's logo hexagons.

    The border ring is intentionally thin (an 8% inset, not the old
    6%) and understated so the crest itself stays the focal point
    rather than the hex outline competing with it."""
    img = Image.new("RGBA", size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    draw.polygon(_hexagon_points(size), fill=_hex_to_rgb(border_hex))
    draw.polygon(_hexagon_points(size, inset=size[0] * 0.08), fill=_hex_to_rgb(fill_hex))
    return img


def _hex_mask(size=(170, 170), inset: float = 0.0):
    """Single-channel mask matching the inner hexagon shape (white =
    opaque, black = transparent) — used to CLIP a crest/photo to the
    hex outline when pasting, instead of pasting a full square on top
    of it. Without this, a crest whose own image fills its square
    thumbnail shows its square corners poking out past the hex border,
    which is exactly what buried the border ring and made the badge
    read as "washed out"."""
    mask = Image.new("L", size, 0)
    mdraw = ImageDraw.Draw(mask)
    mdraw.polygon(_hexagon_points(size, inset=inset), fill=255)
    return mask


def _paste_clipped_to_hex(badge, content, size, margin: float = 0.14):
    """Pastes `content` (a crest/photo image) into `badge`, scaled down
    by `margin` so it doesn't touch the border ring, and clipped to the
    inner hexagon so nothing spills past the outline into the badge's
    corners. Returns `badge`."""
    inner_w = max(1, int(size[0] * (1 - margin)))
    inner_h = max(1, int(size[1] * (1 - margin)))
    content = content.copy()
    content.thumbnail((inner_w, inner_h))
    layer = Image.new("RGBA", size, (0, 0, 0, 0))
    cx = (size[0] - content.width) // 2
    cy = (size[1] - content.height) // 2
    layer.paste(content, (cx, cy), content)
    mask = _hex_mask(size, inset=size[0] * 0.08)
    # Intersect the pasted content's own alpha with the hex mask, so a
    # transparent-background crest still clips cleanly at the hex edge.
    alpha = Image.composite(layer.split()[-1], Image.new("L", size, 0), mask)
    badge.paste(layer, (0, 0), alpha)
    return badge


def _initials_avatar(name: str, size=(170, 170)):
    """Fallback when a crest URL is missing or fails to download — a
    colored hexagon badge with the team's initials, so the card never
    just shows a blank gap where a badge should be."""
    initials = "".join(w[0] for w in name.split()[:2]).upper() or "?"
    idx = sum(ord(c) for c in name) % len(_AVATAR_PALETTE)
    color = _AVATAR_PALETTE[idx]
    img = Image.new("RGBA", size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    draw.polygon(_hexagon_points(size), fill=_hex_to_rgb(color))
    font = _font(int(size[1] * 0.4))
    w = _text_w(draw, initials, font)
    draw.text(((size[0] - w) / 2, size[1] * 0.28), initials, font=font, fill=WHITE)
    return img


def _crest_or_avatar(url: str, name: str, size=(170, 170)):
    crest = _fetch_crest(url, size)
    if crest is not None:
        # Plain crest, no hexagon frame or clipping — the real team
        # logo just centered on a transparent canvas at the target
        # size, so it reads as the actual badge rather than a
        # stylized hex token.
        crest = crest.copy()
        crest.thumbnail(size)
        layer = Image.new("RGBA", size, (0, 0, 0, 0))
        cx = (size[0] - crest.width) // 2
        cy = (size[1] - crest.height) // 2
        layer.paste(crest, (cx, cy), crest)
        return layer
    return _initials_avatar(name, size)


def _player_badge_or_crest(player_photo_url: str, team_crest_url: str, player_name: str, size=(170, 170)):
    """
    Man of the Match badge chain: real player photo first, then the
    player's team crest (not initials) if the photo is missing or
    fails to download, and only initials as the very last resort if
    even the team crest can't be fetched. This mirrors _crest_or_avatar
    but with an extra middle rung, since a MOTM card without any
    photo/logo reads far more broken than a scoreboard card does.
    """
    photo = _fetch_crest(player_photo_url, size)
    if photo is not None:
        badge = _hex_badge_frame(size)
        return _paste_clipped_to_hex(badge, photo, size)
    crest = _fetch_crest(team_crest_url, size)
    if crest is not None:
        badge = _hex_badge_frame(size)
        return _paste_clipped_to_hex(badge, crest, size)
    return _initials_avatar(player_name, size)


# ══════════════════════════════════════════════════════════════════
# GENERIC CARD — VAR, transfer flashback, top scorers, win probability
# ══════════════════════════════════════════════════════════════════

def render_card(kind: str, marker: str, title: str, lines: list, source: str = None) -> str:
    """`marker` is accepted for backwards compatibility with existing
    call sites but is intentionally unused for on-image rendering
    (see module docstring) — the badge pill below replaces it."""
    img, draw = _base_canvas(kind)
    W, H = CARD_SIZE
    badge_text, accent = BADGES.get(kind, BADGES["default"])
    badge_bottom = _draw_badge_pill(draw, badge_text, accent)

    y = badge_bottom + 50
    title_font = _font(56)
    for line in textwrap.wrap(title, width=22):
        _shadow_text(draw, (60, y), line, title_font, fill=WHITE)
        y += 68

    y += 24
    # Ranked-list detection ("1. Player - 6") gets a numbered chip;
    # everything else gets a small square bullet. Both read as
    # intentional list styling instead of raw wrapped paragraphs.
    body_font = _font(38)
    for i, line in enumerate(lines):
        rank = None
        rest = line
        parts = line.split(".", 1)
        if len(parts) == 2 and parts[0].strip().isdigit():
            rank, rest = parts[0].strip(), parts[1].strip()

        chip_x = 60
        if rank:
            chip_d = 46
            draw.ellipse([chip_x, y - 4, chip_x + chip_d, y - 4 + chip_d], fill=GOLD)
            rw = _text_w(draw, rank, body_font)
            draw.text((chip_x + (chip_d - rw) / 2, y - 2), rank, font=body_font, fill=INK)
            text_x = chip_x + chip_d + 20
        else:
            draw.rectangle([chip_x, y + 14, chip_x + 14, y + 28], fill=GOLD)
            text_x = chip_x + 34

        for j, wrapped in enumerate(textwrap.wrap(rest, width=26) or [""]):
            draw.text((text_x, y + j * 46), wrapped, font=body_font, fill=WHITE)
        y += max(54, 46 * len(textwrap.wrap(rest, width=26) or [""])) + 16

    if source:
        draw.text((60, y + 10), f"Source: {source}", font=_font(26), fill="#D1D5DB")

    _draw_brand_ribbon(img, draw)
    return _save(img)


def render_stat_highlight_card(kind: str, topic: str, stat_pairs: list[tuple[str, str]],
                                hook: str | None = None) -> str:
    """
    A stat spotlight card with the actual numbers rendered big on the
    image itself — not just the topic headline (that's what
    render_headline_card does, and it was the gap flagged: a follower
    scrolling past should see the number at a glance, not have to read
    the caption). stat_pairs is [(value, label), ...], e.g.
    [("7","APPS"), ("4","GOALS"), ("1","ASSIST")] — laid out as a row of
    big-number/small-label pairs, like a real stat graphic.
    """
    img, draw = _base_canvas(kind)
    W, H = CARD_SIZE
    badge_text, accent = BADGES.get(kind, BADGES["default"])
    badge_bottom = _draw_badge_pill(draw, badge_text, accent)

    footer_h = 96  # matches _draw_brand_ribbon's ribbon_h
    y = badge_bottom + 50

    topic_font = _font(44, "extrabold")
    topic_lines = textwrap.wrap(topic, width=26)[:3]
    for line in topic_lines:
        lw = _text_w(draw, line, topic_font)
        _shadow_text(draw, (W / 2 - lw / 2, y), line, topic_font, fill=WHITE)
        y += 54
    y += 30

    # Big number row — evenly spaced across the card width.
    n = len(stat_pairs)
    col_w = W / n
    value_font = _font(72, "extrabold")
    label_font = _font(26, "bold")
    for i, (value, label) in enumerate(stat_pairs):
        cx = col_w * i + col_w / 2
        vw = _text_w(draw, str(value), value_font)
        # Was `accent` (a purple close to the card's own purple
        # background) — hard to read against the diagonal stripe
        # texture, same issue as the VS text on the comparison card.
        # GOLD is a different hue from any background this template
        # uses, so it holds up regardless.
        _shadow_text(draw, (cx - vw / 2, y), str(value), value_font, fill=GOLD)
        lw = _text_w(draw, label.upper(), label_font)
        _shadow_text(draw, (cx - lw / 2, y + 84), label.upper(), label_font, fill=WHITE)
    y += 84 + 40 + 40

    if hook:
        hook_font = _font(30)
        hook_lines = textwrap.wrap(hook, width=34)[:4]
        for line in hook_lines:
            hw = _text_w(draw, line, hook_font)
            _shadow_text(draw, (W / 2 - hw / 2, y), line, hook_font, fill="#F2E9FF")
            y += 40

    _draw_brand_ribbon(img, draw)
    return _save(img)


def _fit_font(draw, text: str, max_width: float, start_size: int, weight: str = "extrabold", min_size: int = 22):
    """Returns the largest font (down to min_size) at which `text` fits
    within max_width. Comparison-card stat values range from short
    numbers ('36') to full phrases ('Golden Ball + red card') — a fixed
    font size that works for the former runs off the edge of the card
    for the latter, which is exactly the kind of thing that looks
    obviously broken/bot-generated rather than just imperfect."""
    size = start_size
    while size > min_size:
        f = _font(size, weight)
        if _text_w(draw, str(text), f) <= max_width:
            return f
        size -= 4
    return _font(min_size, weight)


def _fit_lines(draw, text: str, max_width: float, start_size: int, weight: str = "extrabold",
               min_size: int = 22, max_lines: int = 2) -> tuple[list[str], "ImageFont"]:
    """Like _fit_font, but falls back to wrapping onto up to `max_lines`
    lines (at min_size) if the text still doesn't fit on one line even
    at the smallest allowed font — rather than silently returning a
    font that still overflows the given width, which is what let long
    phrases like 'Golden Ball + red card' run into a neighboring column
    on the comparison card. Returns (lines, font)."""
    font = _fit_font(draw, text, max_width, start_size, weight, min_size)
    if _text_w(draw, text, font) <= max_width:
        return [text], font

    # Doesn't fit on one line even at min_size — wrap it. Pick a wrap
    # width (in characters) empirically: shrink the character count
    # until every resulting line actually measures within max_width.
    font = _font(min_size, weight)
    for wrap_width in range(len(text), 2, -1):
        lines = textwrap.wrap(text, width=wrap_width)
        if len(lines) > max_lines:
            continue
        if all(_text_w(draw, line, font) <= max_width for line in lines):
            return lines, font
    # Last resort — hard character-count split, still capped at max_lines.
    return textwrap.wrap(text, width=max(3, len(text) // max_lines))[:max_lines], font


def render_comparison_card(kind: str, a_name: str, b_name: str,
                            stat_rows: list[tuple[str, str, str]],
                            a_team: str = "", b_team: str = "") -> str:
    """
    Player-vs-player comparison card — two names up top, then one row
    per stat with both players' numbers visible side by side and the
    stat label in between, so the comparison reads at a glance without
    needing the caption. stat_rows is [(label, a_value, b_value), ...].
    """
    img, draw = _base_canvas(kind)
    W, H = CARD_SIZE
    badge_text, accent = BADGES.get(kind, BADGES["default"])
    badge_bottom = _draw_badge_pill(draw, badge_text, accent)

    y = badge_bottom + 50
    name_font = _font(40, "extrabold")

    def _draw_name(name, cx):
        lines = textwrap.wrap(name, width=16)[:2]
        yy = y
        for line in lines:
            lw = _text_w(draw, line, name_font)
            _shadow_text(draw, (cx - lw / 2, yy), line, name_font, fill=WHITE)
            yy += 48
        return yy

    left_end = _draw_name(a_name, W * 0.27)
    right_end = _draw_name(b_name, W * 0.73)

    vs_font = _font(36, "extrabold")
    vw = _text_w(draw, "VS", vs_font)
    # Was drawn in `accent` (a purple close to the card's own purple
    # background) — nearly invisible against the diagonal stripe
    # texture. GOLD is a completely different hue from any card
    # background this template uses, so it stays legible regardless,
    # plus the shadow holds up against the stripe pattern specifically.
    _shadow_text(draw, (W / 2 - vw / 2, y + 4), "VS", vs_font, fill=GOLD)

    y = max(left_end, right_end) + 30
    if a_team or b_team:
        team_font = _font(24, "semibold")
        if a_team:
            tw = _text_w(draw, a_team, team_font)
            _shadow_text(draw, (W * 0.27 - tw / 2, y), a_team, team_font, fill="#F2E9FF")
        if b_team:
            tw = _text_w(draw, b_team, team_font)
            _shadow_text(draw, (W * 0.73 - tw / 2, y), b_team, team_font, fill="#F2E9FF")
        y += 44

    y += 20
    label_font = _font(26, "bold")
    line_h = 58
    row_gap = 30
    for label, a_val, b_val in stat_rows:
        label_text = label.upper()
        label_w = _text_w(draw, label_text, label_font)
        # The column budget isn't a fixed guess — it's however much
        # space is actually left once the center label's own width is
        # reserved. A wide label like "MOMENT" leaves less room than
        # "VS", and a fixed budget that ignored this is exactly what
        # let a long value run into the label instead of stopping
        # short of it.
        safe_margin = 24
        col_max_width = (W / 2 - label_w / 2 - safe_margin) - W * 0.27

        a_lines, a_font = _fit_lines(draw, str(a_val), col_max_width, 52)
        b_lines, b_font = _fit_lines(draw, str(b_val), col_max_width, 52)

        ay = y
        for line in a_lines:
            aw = _text_w(draw, line, a_font)
            draw.text((W * 0.27 - aw / 2, ay), line, font=a_font, fill=WHITE)
            ay += line_h
        by = y
        for line in b_lines:
            bw = _text_w(draw, line, b_font)
            draw.text((W * 0.73 - bw / 2, by), line, font=b_font, fill=WHITE)
            by += line_h

        label_y = y + (line_h * max(len(a_lines), len(b_lines)) - line_h) / 2 + 4
        _shadow_text(draw, (W / 2 - label_w / 2, label_y), label_text, label_font, fill=GOLD)

        y += line_h * max(len(a_lines), len(b_lines), 1) + row_gap

    _draw_brand_ribbon(img, draw)
    return _save(img)


def render_headline_card(kind: str, headline: str, source: str | None = None) -> str:
    """
    Text-only fallback used when a news item has no usable real photo
    (render_photo_card returned None). Unlike render_card — which
    top-anchors content right under the badge, sized for multi-line
    stat/VAR lists — this is built for exactly one thing (a headline),
    so it vertically (and horizontally) centers that headline in the
    space between the badge and the footer ribbon. Top-anchoring a
    single short headline here left a large empty gap at the bottom of
    the card, which read as a broken/unfinished layout rather than an
    intentional design.
    """
    img, draw = _base_canvas(kind)
    W, H = CARD_SIZE
    badge_text, accent = BADGES.get(kind, BADGES["default"])
    badge_bottom = _draw_badge_pill(draw, badge_text, accent)

    footer_h = 96  # matches _draw_brand_ribbon's ribbon_h
    top_bound = badge_bottom + 40
    bottom_bound = H - footer_h - 40

    title_font = _font(56)
    lines = textwrap.wrap(headline, width=22)[:6]
    line_h = 68
    block_h = line_h * len(lines)

    y = top_bound + max(0, (bottom_bound - top_bound - block_h) / 2)
    for line in lines:
        lw = _text_w(draw, line, title_font)
        _shadow_text(draw, (W / 2 - lw / 2, y), line, title_font, fill=WHITE)
        y += line_h

    if source:
        src_text = f"Source: {source}"
        sw = _text_w(draw, src_text, _font(26))
        draw.text((W / 2 - sw / 2, bottom_bound - 6), src_text, font=_font(26), fill="#D1D5DB")

    _draw_brand_ribbon(img, draw)
    return _save(img)


# ══════════════════════════════════════════════════════════════════
# SCORE CARD — kickoff / goal / fulltime, with crest logos (or
# initials-avatar fallback so a missing crest never leaves a gap)
# ══════════════════════════════════════════════════════════════════

def render_score_card(kind: str, marker: str, home_name: str, away_name: str,
                       home_score, away_score, event_line: str = "",
                       home_crest_url: str = "", away_crest_url: str = "") -> str:
    img, draw = _base_canvas(kind)
    W, H = CARD_SIZE
    badge_text, accent = BADGES.get(kind, BADGES["default"])
    _draw_badge_pill(draw, badge_text, accent)
    _draw_wordmark(draw, xy=(W - 300, 62))

    crest_size = (190, 190)
    crest_y = 260
    home_crest = _crest_or_avatar(home_crest_url, home_name, crest_size)
    away_crest = _crest_or_avatar(away_crest_url, away_name, crest_size)
    img.paste(home_crest, (110, crest_y), home_crest)
    img.paste(away_crest, (W - 110 - crest_size[0], crest_y), away_crest)
    draw = ImageDraw.Draw(img)

    # Scoreline inside an angular ribbon badge (pointed ends) instead of
    # a plain rounded chip — the same broadcast-scoreboard styling used
    # across the premium cards, so it reads as a designed banner.
    score_text = f"{home_score}  -  {away_score}"
    score_font = _font(90)
    _draw_ribbon_badge(draw, W / 2, crest_y + 95, score_text, score_font,
                        fill="#0B0F0C", text_color=WHITE, pad_x=48, pad_y=18, notch=26)

    # Thin divider under the crests to visually separate identity from
    # the event line below, like a real scoreboard graphic.
    name_font = _font(34)
    for name, cx in ((home_name, 205), (away_name, W - 205)):
        ty = crest_y + crest_size[1] + 26
        for i, line in enumerate(textwrap.wrap(name, width=14)[:2]):
            lw = _text_w(draw, line, name_font)
            draw.text((cx - lw / 2, ty + i * 42), line, font=name_font, fill=WHITE)

    divider_y = crest_y + crest_size[1] + 120
    draw.line([(120, divider_y), (W - 120, divider_y)], fill=(255, 255, 255, 90), width=2)

    if event_line:
        ev_font = _font(44)
        y = divider_y + 34
        for raw_line in event_line.split("\n"):
            for line in textwrap.wrap(raw_line, width=30) or [""]:
                lw = _text_w(draw, line, ev_font)
                _shadow_text(draw, ((W - lw) / 2, y), line, ev_font, fill=GOLD)
                y += 54

    _draw_brand_ribbon(img, draw)
    return _save(img)


def _spaced_text(draw, xy, text, font, fill, tracking=6):
    """Draws text with extra letter-spacing (Pillow has no native
    tracking control) — this is what makes a header like
    'INTERNATIONAL FRIENDLY' read as sleek/broadcast instead of a
    plain wrapped string."""
    x, y = xy
    for ch in text:
        draw.text((x, y), ch, font=font, fill=fill)
        x += _text_w(draw, ch, font) + tracking
    return x  # right edge, if the caller needs it


def _stadium_canvas(accent_key: str):
    """Night-stadium backdrop: soft floodlight glows, a dark vignette,
    and a faint blurred pitch texture — built entirely with Pillow
    (gradients + gaussian blur), so it's instant, free, and has zero
    risk of hallucinated text/flags the way a generative image would.
    """
    W, H = CARD_SIZE
    base_dark = (10, 14, 12)
    img = Image.new("RGB", CARD_SIZE, base_dark)

    # Floodlight glows — a few large soft-edged bright ellipses, blurred
    # heavily, positioned like stadium floodlights from above.
    glow_layer = Image.new("RGB", CARD_SIZE, (0, 0, 0))
    gdraw = ImageDraw.Draw(glow_layer)
    accent_rgb = _hex_to_rgb(ACCENTS.get(accent_key, ACCENTS["default"]))
    for cx, cy, r, color in [
        (170, 60, 260, (255, 244, 214)),
        (W - 170, 60, 260, tuple(min(255, c + 60) for c in accent_rgb)),
        (W // 2, H + 80, 520, accent_rgb),
    ]:
        gdraw.ellipse([cx - r, cy - r, cx + r, cy + r], fill=color)
    glow_layer = glow_layer.filter(ImageFilter.GaussianBlur(120))
    img = Image.blend(img, glow_layer, 0.55)

    # Faint blurred "pitch" texture along the bottom third — soft
    # diagonal stripes suggesting mown grass, kept subtle so it never
    # competes with the scoreboard.
    pitch = Image.new("RGB", CARD_SIZE, base_dark)
    pdraw = ImageDraw.Draw(pitch)
    stripe_w = 70
    for i, x in enumerate(range(-H, W, stripe_w)):
        shade = (20, 46, 30) if i % 2 == 0 else (16, 38, 24)
        pdraw.polygon([(x, H), (x + H, 0), (x + H + stripe_w, 0), (x + stripe_w, H)], fill=shade)
    pitch = pitch.filter(ImageFilter.GaussianBlur(8))
    mask = Image.new("L", CARD_SIZE, 0)
    mdraw = ImageDraw.Draw(mask)
    mdraw.rectangle([0, int(H * 0.62), W, H], fill=140)
    img = Image.composite(pitch, img, mask)

    # Dark vignette so the edges recede and the scoreboard stays the
    # clear focal point.
    vignette = Image.new("L", CARD_SIZE, 0)
    vdraw = ImageDraw.Draw(vignette)
    vdraw.ellipse([-260, -260, W + 260, H + 260], fill=255)
    vignette = vignette.filter(ImageFilter.GaussianBlur(180))
    black = Image.new("RGB", CARD_SIZE, (0, 0, 0))
    img = Image.composite(img, black, vignette)

    # Crisp floodlight beam fans on top of the soft glow blobs above —
    # this is the detail that reads as "broadcast template" rather
    # than "gradient with blur", matching a proper stadium scoreboard.
    img = _draw_light_rays(img, n_rays=7, spread=52, max_alpha=22)

    return img, ImageDraw.Draw(img)


def render_score_card_premium(kind: str, home_name: str, away_name: str,
                               home_score, away_score, competition: str = "",
                               event_line: str = "", status_label: str = "",
                               show_pulse: bool = False,
                               home_crest_url: str = "", away_crest_url: str = "") -> str:
    """
    Stadium-night styled variant of render_score_card — same exact,
    instantly-computed score/scorer text (no generative model in the
    loop, so nothing here can misspell a name or draw a wrong flag),
    just a more premium/cinematic backdrop for kickoff/goal/full-time.

    status_label examples: "KICK-OFF", "72' \u2022 LIVE", "FULL TIME (AET)".
    show_pulse draws the small red live-dot — use for kickoff/goal,
    leave off for full-time.
    """
    img, draw = _stadium_canvas(kind)
    W, H = CARD_SIZE

    if competition:
        header_font = _font(28)
        header = competition.upper()
        hw_est = sum(_text_w(draw, c, header_font) + 6 for c in header)
        _spaced_text(draw, ((W - hw_est) / 2, 56), header, header_font, "#D4AF6A", tracking=6)

    if status_label:
        font = _font(26)
        label_text = f"\u25CF {status_label}" if show_pulse else status_label
        badge_w, badge_h = _draw_ribbon_badge(
            draw, W / 2, 128, label_text, font,
            fill="#0B0F0C", text_color=WHITE, pad_x=30, pad_y=12, notch=16,
        )
        if show_pulse:
            # Recolor just the pulse dot glyph red by overdrawing a small
            # circle in place of the plain-text bullet drawn above.
            dot_x = W / 2 - _text_w(draw, label_text, font) / 2 + 4
            draw.ellipse([dot_x, 118, dot_x + 18, 136], fill="#EF4444")

    crest_size = (200, 200)
    crest_y = 230
    home_crest = _crest_or_avatar(home_crest_url, home_name, crest_size)
    away_crest = _crest_or_avatar(away_crest_url, away_name, crest_size)
    img.paste(home_crest, (120, crest_y), home_crest)
    img.paste(away_crest, (W - 120 - crest_size[0], crest_y), away_crest)
    draw = ImageDraw.Draw(img)

    score_text = f"{home_score}   -   {away_score}"
    score_font = _font(110)
    tw = _text_w(draw, score_text, score_font)
    _shadow_text(draw, ((W - tw) / 2, crest_y + 50), score_text, score_font, fill=WHITE, offset=(0, 6))

    name_font = _font(34)
    for name, cx in ((home_name, 220), (away_name, W - 220)):
        ty = crest_y + crest_size[1] + 20
        for i, line in enumerate(textwrap.wrap(name, width=14)[:2]):
            lw = _text_w(draw, line, name_font)
            draw.text((cx - lw / 2, ty + i * 42), line, font=name_font, fill=WHITE)

    if event_line:
        ev_font = _font(40)
        y = crest_y + crest_size[1] + 150
        for raw_line in event_line.split("\n"):
            for line in textwrap.wrap(raw_line, width=34) or [""]:
                lw = _text_w(draw, line, ev_font)
                _shadow_text(draw, ((W - lw) / 2, y), line, ev_font, fill="#D4AF6A")
                y += 50

    _draw_brand_ribbon(img, draw)
    return _save(img)




# ══════════════════════════════════════════════════════════════════
# SCOREBOARD CARD — flat navy/cyan "match-result" template style
# (kickoff / goal / full-time). This is the requested redesign:
# solid navy background, one loud accent color reserved for the
# score container, real bold type instead of Pillow's default font.
# ══════════════════════════════════════════════════════════════════

SCOREBOARD_NAVY_TOP    = "#000000"
SCOREBOARD_NAVY_BOTTOM = "#000000"
SCOREBOARD_INK         = "#0B1E4D"   # score text color (dark navy on bright accent)

# One bright accent per card kind — this is the ONLY loud color on the
# card (everything else is navy/white), which is exactly what makes
# the reference template's score hexagon pop instead of blending in.
SCOREBOARD_ACCENTS = {
    "default":   "#3FD3E8",
    "kickoff":   "#3FD3E8",   # bright cyan
    "goal":      "#FFC93C",   # bright gold
    "halftime":  "#3FD3E8",
    "redcard":   "#FF5C5C",   # bright red
    "fulltime":  "#3FD3E8",
    "var":       "#FF5C5C",   # bright red
    "transfer":  "#FF8A3D",   # bright orange
    "manager":   "#3FD3E8",
    "extratime": "#FFC93C",   # bright gold — matches goal's urgency, distinct from the blue half/full-time cards
    "motm":      "#FFC93C",   # bright gold — award/spotlight moment
}

SCOREBOARD_HEADERS = {
    "default":   "MATCH",
    "kickoff":   "KICK-OFF",
    "goal":      "GOAL",
    "halftime":  "HALF TIME",
    "redcard":   "RED CARD",
    "fulltime":  "FULL TIME",
    "var":       "VAR REVIEW",
    "extratime": "EXTRA TIME",
    "motm":      "MAN OF THE MATCH",
}


def _scoreboard_canvas(accent_hex: str):
    """Flat navy gradient + a very faint diagonal chevron texture —
    deliberately calm/flat (no floodlight rays, no vignette) so the
    score hexagon is the only thing competing for attention, the way
    a clean template card reads instead of a busy broadcast graphic."""
    W, H = CARD_SIZE
    top = _hex_to_rgb(SCOREBOARD_NAVY_TOP)
    bottom = _hex_to_rgb(SCOREBOARD_NAVY_BOTTOM)
    img = Image.new("RGB", CARD_SIZE, top)
    draw = ImageDraw.Draw(img)
    for y in range(H):
        t = y / H
        row = tuple(int(top[i] + (bottom[i] - top[i]) * t) for i in range(3))
        draw.line([(0, y), (W, y)], fill=row)
    return img, draw


def _draw_score_pill(img, draw, cx: float, cy: float, home_score, away_score,
                      accent_hex: str, w: int = 340, h: int = 172):
    """Rounded-rectangle scoreline pill — replaces the old pointed
    hexagon. The hexagon's tapered points ate into the horizontal
    space on either side without actually using it for the score
    digits, which crowded the team-name columns. A rounded rect uses
    its full box width for the fill, so the same footprint leaves
    more real room for team names."""
    box = (int(w), int(h))
    layer = Image.new("RGBA", box, (0, 0, 0, 0))
    ldraw = ImageDraw.Draw(layer)
    radius = int(h * 0.30)
    ldraw.rounded_rectangle([0, 0, w - 1, h - 1], radius=radius, fill=_hex_to_rgb(accent_hex))
    img.paste(layer, (int(cx - w / 2), int(cy - h / 2)), layer)
    draw = ImageDraw.Draw(img)

    score_text = f"{home_score} - {away_score}"
    score_font = _font(120, "extrabold")
    tw = _text_w(draw, score_text, score_font)
    max_tw = w - 44
    if tw > max_tw:
        # Double-digit scorelines (10-0 cup blowouts, etc.) can run
        # wider than the pill — shrink just enough to fit rather than
        # letting the digits spill past the rounded edges.
        size = 120
        while tw > max_tw and size > 60:
            size -= 6
            score_font = _font(size, "extrabold")
            tw = _text_w(draw, score_text, score_font)
    ascent, descent = score_font.getmetrics()
    draw.text((cx - tw / 2, cy - (ascent + descent) / 2 - 6), score_text,
              font=score_font, fill=SCOREBOARD_INK)
    return draw


def _wrap_text_to_width(draw, text: str, font, max_width: float, max_lines: int = 2):
    """Greedy word-wrap sized by actual pixel width rather than a
    guessed character count, so team names reliably end up as up to
    two lines that fit their column — never shrinking the font and
    never overlapping the score pill next to them. A single word that
    alone is wider than the column (a long, unbroken club name) gets
    truncated with an ellipsis rather than running off the canvas
    edge; any words left over after max_lines are folded into the
    last line the same way."""
    words = text.split()
    if not words:
        return [""]

    def _truncate(word: str) -> str:
        w = word
        while w and _text_w(draw, w + "\u2026", font) > max_width:
            w = w[:-1]
        return (w + "\u2026") if w != word else word

    lines = []
    current = ""
    i = 0
    while i < len(words) and len(lines) < max_lines:
        word = words[i]
        candidate = f"{current} {word}".strip()
        if _text_w(draw, candidate, font) <= max_width:
            current = candidate
            i += 1
            continue
        if current:
            lines.append(current)
            current = ""
            continue  # retry this word as the start of the next line
        # current line is empty and this single word alone doesn't fit
        lines.append(_truncate(word))
        i += 1

    if current and len(lines) < max_lines:
        lines.append(current)
        current = ""

    if i < len(words) or current:
        # Words remain beyond max_lines — fold onto the last line with
        # an ellipsis instead of dropping them silently.
        last = (lines[-1] if lines else "").rstrip("\u2026")
        while last and _text_w(draw, last + "\u2026", font) > max_width:
            last = last[:-1]
        if lines:
            lines[-1] = last + "\u2026"
        else:
            lines = [last + "\u2026"]

    return lines[:max_lines]


def render_scoreboard_card(kind: str, home_name: str, away_name: str,
                            home_score, away_score, competition: str = "",
                            event_line: str = "", status_label: str = "",
                            home_event_line: str = "", away_event_line: str = "",
                            home_crest_url: str = "", away_crest_url: str = "",
                            show_pulse: bool = False,
                            home_penalty: int | None = None,
                            away_penalty: int | None = None) -> str:
    """
    Match-result template card for kickoff / goal / full-time — solid
    navy background, one bright accent color reserved for the score
    hexagon, real Poppins weights instead of Pillow's thin default
    font. Layout mirrors a clean scoreboard template: two-tier header,
    team crest + score hexagon + team crest in a row, team names under
    the crests, optional event line (goal scorer/minute) below, brand
    footer at the bottom.

    Scorer/assist placement: pass `home_event_line`/`away_event_line`
    (not the generic `event_line`) whenever the event belongs to one
    specific team — a goal, a full-time scorer list, a red card — so
    the name is drawn under THAT team's crest instead of spanning the
    center of the card. `event_line` still exists for content with no
    natural side (e.g. a generic status note) and stays centered.
    """
    accent = SCOREBOARD_ACCENTS.get(kind, SCOREBOARD_ACCENTS["default"])
    img, draw = _scoreboard_canvas(accent)
    W, H = CARD_SIZE

    # Big header is the event name itself (KICK-OFF, GOAL, HALF TIME,
    # RED CARD, FULL TIME, EXTRA TIME, ...) — no "MATCH UPDATE" eyebrow
    # or brand line anywhere on the card. status_label (when it says
    # something the header doesn't already say — a minute, "- LIVE",
    # "- AET", "- PENALTIES") renders as a small sub-line underneath;
    # when it's just a restatement of the header (e.g. "HALF TIME"
    # passed as status_label for a halftime card) it's dropped instead
    # of duplicating the headline.
    header_text = SCOREBOARD_HEADERS.get(kind, SCOREBOARD_HEADERS["default"])
    sub_text = status_label if (status_label and status_label != header_text) else ""

    header_font = _font(57, "extrabold")
    hw = _text_w(draw, header_text, header_font)
    draw.text((W / 2 - hw / 2, 68), header_text, font=header_font, fill="#FFFFFF")

    if sub_text:
        sub_font = _font(28, "semibold")
        sw = sum(_text_w(draw, c, sub_font) + 5 for c in sub_text)
        _spaced_text(draw, (W / 2 - sw / 2, 148), sub_text, sub_font, accent, tracking=5)

    if competition:
        comp_font = _font(24, "regular")
        comp_text = competition.upper()
        cw = _text_w(draw, comp_text, comp_font)
        # Was a muted blue-gray (#93A3D1) directly on the navy
        # background — same hue family, poor legibility. Near-white
        # reads clearly against the navy gradient at every card kind.
        # A bit more clearance when there's a sub-line underneath the
        # header so the competition text doesn't crowd it.
        comp_y = 198 if sub_text else 184
        draw.text((W / 2 - cw / 2, comp_y), comp_text, font=comp_font, fill="#E7ECFA")

    # Row: team name — score pill — team name. No crest images here
    # (Railway can't reliably reach the crest image hosts, which made
    # cards ship with broken/blank badges) — a text-only row can never
    # fail to render, matching the confirmed-working reference layout.
    row_cy = 420
    pill_w, pill_h = 340, 172
    name_col_w = (W - pill_w) / 2
    home_cx = name_col_w / 2
    away_cx = W - name_col_w / 2
    name_max_w = name_col_w - 60  # keeps names off the pill and off the canvas edge

    draw = _draw_score_pill(img, draw, W / 2, row_cy, home_score, away_score,
                             accent, w=pill_w, h=pill_h)

    # Team names, vertically centered on the score row and flanking it
    # left/right — the space a crest used to occupy. Long names wrap
    # to a second line at a fixed font size (never shrunk) instead of
    # crowding the pill.
    name_font = _font(46, "extrabold")
    name_line_h = 54
    name_lines_drawn = 0
    for name, cx in ((home_name, home_cx), (away_name, away_cx)):
        lines = _wrap_text_to_width(draw, name.upper(), name_font, name_max_w, max_lines=2)
        name_lines_drawn = max(name_lines_drawn, len(lines))
        total_h = len(lines) * name_line_h
        ty = row_cy - total_h / 2
        for i, line in enumerate(lines):
            lw = _text_w(draw, line, name_font)
            draw.text((cx - lw / 2, ty + i * name_line_h), line, font=name_font, fill="#FFFFFF")

    row_bottom = max(row_cy + pill_h / 2, row_cy + (name_lines_drawn * name_line_h) / 2) + 24

    # Penalty shootout score (full-time-with-penalties card only) — the
    # hex above already shows the AET/regulation score (e.g. "1-1");
    # this is the actual shootout score (e.g. "5" / "4"), one number
    # per side, sitting right under that team's crest+name where
    # there's real room, rather than squeezed into the ~30px gap
    # between crest and hex (too tight for a number this size without
    # risking a collision with either). The winning side's number is
    # drawn in the card's accent color and bold-sized; the losing
    # side's is dimmed gray — same idea as the sketch (winner's number
    # reads as the "loud" one) without the tight-space risk.
    pens_block_h = 0
    if home_penalty is not None or away_penalty is not None:
        pens_won_home = (home_penalty or 0) > (away_penalty or 0)
        pens_label_font = _font(20, "regular")
        pens_num_font = _font(56, "extrabold")
        pens_y = row_bottom - 10
        for pens, cx, is_winner in (
            (home_penalty, home_cx, pens_won_home),
            (away_penalty, away_cx, not pens_won_home),
        ):
            if pens is None:
                continue
            label = "WON ON PENS" if is_winner else "PENS"
            lw = _text_w(draw, label, pens_label_font)
            draw.text((cx - lw / 2, pens_y), label, font=pens_label_font,
                      fill=accent if is_winner else "#8B94B8")
            num_text = str(pens)
            nw = _text_w(draw, num_text, pens_num_font)
            draw.text((cx - nw / 2, pens_y + 26), num_text, font=pens_num_font,
                      fill=accent if is_winner else "#8B94B8")
        pens_block_h = 26 + 66

    # Event line(s) below the crest/name row. Side-anchored lines (goal
    # scorer, full-time scorer list, red card) sit under the scoring
    # team's own crest so it's immediately clear whose event it is,
    # rather than a single line straddling the middle of the card that
    # reads ambiguous when both teams have scored.
    side_y_start = row_bottom + pens_block_h
    side_font = _font(32, "semibold")

    def _draw_side_lines(text: str, cx: float):
        raw_lines = text.split("\n")
        line_h = 40
        # Blowout scorelines (a real cup mismatch can be 8-0, 9-0, etc.)
        # can produce more scorer lines than fit before the footer
        # ribbon — without a limit, text used to run under the ribbon
        # or past the canvas edge entirely (silently invisible). Stop
        # drawing once the NEXT line would cross into the footer's
        # safe margin, and summarize whatever's left as "+N more"
        # instead. The full list is never lost — it's still in the
        # post caption (poster.fmt_fulltime), which isn't image-bound.
        footer_h = 92  # matches _draw_brand_ribbon_v2's ribbon_h
        safe_bottom = H - footer_h - 20
        y = side_y_start
        for i, raw_line in enumerate(raw_lines):
            wrapped = _wrap_text_to_width(draw, raw_line, side_font, name_max_w, max_lines=99)
            if y + len(wrapped) * line_h > safe_bottom:
                remaining = len(raw_lines) - i
                more_text = f"+{remaining} more"
                lw = _text_w(draw, more_text, side_font)
                draw.text((cx - lw / 2, y), more_text, font=side_font, fill="#FFFFFF")
                return
            for line in wrapped:
                lw = _text_w(draw, line, side_font)
                draw.text((cx - lw / 2, y), line, font=side_font, fill="#FFFFFF")
                y += line_h

    if home_event_line:
        _draw_side_lines(home_event_line, home_cx)
    if away_event_line:
        _draw_side_lines(away_event_line, away_cx)

    # Generic centered event line — only used when no side is known.
    if event_line and not (home_event_line or away_event_line):
        ev_font = _font(34, "semibold")
        y = row_cy + pill_h / 2 + 90
        for raw_line in event_line.split("\n"):
            for line in textwrap.wrap(raw_line, width=32) or [""]:
                lw = _text_w(draw, line, ev_font)
                draw.text((W / 2 - lw / 2, y), line, font=ev_font, fill="#FFFFFF")
                y += 44

    _draw_brand_ribbon_v2(img, draw, accent)
    return _save(img)


def render_motm_card(player_name: str, team_name: str, rating,
                      competition: str = "", opponent_name: str = "",
                      player_photo_url: str = "", team_crest_url: str = "") -> str:
    """
    Man of the Match card — same flat navy scoreboard family as
    kickoff/goal/half/full-time, but single-badge instead of two-team,
    since there's one subject (the player) rather than two sides.
    Badge chain: real player photo -> player's team crest -> initials
    (see _player_badge_or_crest) so a missing headshot still shows the
    team crest rather than a blank/generic gap.
    """
    accent = SCOREBOARD_ACCENTS.get("motm", SCOREBOARD_ACCENTS["default"])
    img, draw = _scoreboard_canvas(accent)
    W, H = CARD_SIZE

    top_line, _ = SCOREBOARD_HEADERS.get("motm", SCOREBOARD_HEADERS["default"])
    header_font = _font(50, "extrabold")
    hw = _text_w(draw, top_line, header_font)
    draw.text((W / 2 - hw / 2, 64), top_line, font=header_font, fill="#FFFFFF")

    if competition:
        comp_font = _font(24, "regular")
        comp_text = competition.upper()
        cw = _text_w(draw, comp_text, comp_font)
        draw.text((W / 2 - cw / 2, 134), comp_text, font=comp_font, fill="#E7ECFA")

    # No player photo / crest image here (Railway can't reliably reach
    # those hosts, which made this card ship with a broken or blank
    # badge) — the player's name is the whole visual, same text-only
    # approach as the scoreboard cards.
    name_font = _font(44, "extrabold")
    name_lines = textwrap.wrap(player_name.upper(), width=18)[:2]
    ny = 340
    for line in name_lines:
        lw = _text_w(draw, line, name_font)
        draw.text((W / 2 - lw / 2, ny), line, font=name_font, fill="#FFFFFF")
        ny += 54

    team_font = _font(28, "semibold")
    team_text = team_name.upper()
    if opponent_name:
        team_text += f"  vs  {opponent_name.upper()}"
    tw = _text_w(draw, team_text, team_font)
    draw.text((W / 2 - tw / 2, ny + 6), team_text, font=team_font, fill=accent)
    ny += 6 + 40

    if rating is not None:
        rating_text = f"RATING {rating}"
        rating_font = _font(30, "semibold")
        rw = _text_w(draw, rating_text, rating_font)
        draw.text((W / 2 - rw / 2, ny + 14), rating_text, font=rating_font, fill="#E7ECFA")

    _draw_brand_ribbon_v2(img, draw, accent)
    return _save(img)


def _draw_brand_ribbon_v2(img, draw, accent_hex: str):
    """Footer bar matching the scoreboard template's flat bottom band —
    solid dark navy strip, brand name in the accent color, tagline in
    light gray, both in real Poppins weights. Taller + bigger type
    than the original (28/20pt) — that was hard to read at feed
    thumbnail size, especially the tagline."""
    W, H = img.size
    ribbon_h = 92
    band = Image.new("RGBA", (W, ribbon_h), (*_hex_to_rgb(SCOREBOARD_NAVY_BOTTOM), 235))
    img.paste(Image.alpha_composite(img.crop((0, H - ribbon_h, W, H)).convert("RGBA"), band), (0, H - ribbon_h))
    draw = ImageDraw.Draw(img)
    draw.text((44, H - ribbon_h + 14), BRAND_NAME, font=_font(36, "extrabold"), fill=accent_hex)
    draw.text((44, H - ribbon_h + 56), BRAND_TAGLINE, font=_font(25, "regular"), fill="#E7ECFA")
    return draw


# Two thresholds instead of one hard cutoff:
#  >= STANDARD_MIN         -> crisp, full-detail treatment
#  HARD_MIN..STANDARD_MIN  -> "standard" treatment: a mild blur pass
#                             smooths blocky upscaling artifacts, then
#                             an unsharp mask restores perceived edge
#                             crispness — the same soften-then-sharpen
#                             trick used to clean up any low-res photo.
#                             Still a real photo, just not pin-sharp.
#  <  HARD_MIN             -> too small/likely an icon — generated
#                             card is more honest than a mangled photo
HARD_MIN = 150
STANDARD_MIN = 480


def render_photo_card(kind: str, headline: str, image_url: str,
                       source: str = None, sub_line: str = None) -> str | None:
    """
    Builds a branded card from a REAL photo pulled from the news
    source (e.g. the player/club photo attached to the ESPN/BBC/Sky
    article), instead of a text-only generated card. This is what
    transfer-news posts should use: people want to see who the player
    is, and a solid-color text card can't show that.

    Returns None only if the image can't be downloaded/decoded, or is
    below HARD_MIN in its shortest dimension — the caller should fall
    back to render_card() in that case. Anything above HARD_MIN gets
    used (see tiers above), so a small-but-real photo is still
    preferred over a fallback text card whenever reasonably possible.
    """
    photo = _fetch_image(image_url)
    if photo is None:
        return None
    source_dim = min(photo.size)
    if source_dim < HARD_MIN:
        print(f"[GRAPHICS] Source image {photo.size} below {HARD_MIN}px — using generated card instead")
        return None
    needs_softening = source_dim < STANDARD_MIN
    if needs_softening:
        print(f"[GRAPHICS] Source image {photo.size} below {STANDARD_MIN}px — using standard-quality treatment")

    W, H = CARD_SIZE
    # Center-crop to a square so any source aspect ratio fills the card
    # without letterboxing, then upscale/downscale to CARD_SIZE.
    pw, ph = photo.size
    side = min(pw, ph)
    left = (pw - side) // 2
    top = (ph - side) // 2
    photo = photo.crop((left, top, left + side, top + side)).resize(CARD_SIZE, Image.LANCZOS)

    if needs_softening:
        # Soften-then-sharpen: a light blur smooths the blocky edges an
        # upscale produces from a small source, then an unsharp mask
        # brings back perceived edge crispness on top of the smoothed
        # base — a real photo that reads as "acceptable web quality"
        # rather than visibly pixelated.
        photo = photo.filter(ImageFilter.GaussianBlur(1.4))
        photo = photo.filter(ImageFilter.UnsharpMask(radius=2, percent=130, threshold=2))

    photo = photo.convert("RGBA")

    # Bottom-weighted gradient so the headline block is always legible
    # regardless of how bright/busy the source photo is, while the top
    # of the photo (the player's face) stays untouched. Slightly darker
    # overall on the standard tier — a touch more shadow helps hide any
    # remaining softness without hiding the subject.
    base_darken = 40 if needs_softening else 0
    if base_darken:
        dark = Image.new("RGBA", CARD_SIZE, (0, 0, 0, base_darken))
        photo = Image.alpha_composite(photo, dark)
    overlay = Image.new("RGBA", CARD_SIZE, (0, 0, 0, 0))
    odraw = ImageDraw.Draw(overlay)
    for y in range(H):
        t = max(0, (y - H * 0.40) / (H * 0.60))
        alpha = int(220 * t)
        if alpha > 0:
            odraw.line([(0, y), (W, y)], fill=(0, 0, 0, alpha))
    img = Image.alpha_composite(photo, overlay)
    draw = ImageDraw.Draw(img)

    badge_text, accent = BADGES.get(kind, BADGES["transfer"])
    _draw_badge_pill(draw, badge_text, accent)

    title_font = _font(52)
    y = H - 250
    wrapped_lines = textwrap.wrap(headline, width=28)[:4]
    y -= 68 * (len(wrapped_lines) - 1)
    for line in wrapped_lines:
        _shadow_text(draw, (60, y), line, title_font, fill=WHITE)
        y += 68

    if sub_line:
        draw.text((60, y + 6), sub_line, font=_font(30), fill=GOLD)

    _draw_brand_ribbon(img, draw)
    return _save(img)
