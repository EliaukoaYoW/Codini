"""Codini branding and mascot rendering engine.

Provides custom high-resolution subpixel ANSI graphics for the welcome screen.
"""

from __future__ import annotations

import math

DISPLAY_NAME = "Codini"
DISPLAY_HANDLE = "codini"
SUBTITLE = "Magic local coding agent"
WELCOME_STATUS = "Ready to cast code spells"

# ANSI Block Constants
ANSI_RESET = "\x1b[0m"
UPPER_HALF_BLOCK = "▀"
LOWER_HALF_BLOCK = "▄"

BASE_MASCOT_WIDTH = 40
BASE_MASCOT_HEIGHT = 36
MASCOT_WIDTH = 28
MASCOT_HEIGHT = 18
MASCOT_TOP_PADDING_ROWS = 1

# Colors
OUTLINE = "#334155"         # Slate-700
FACE_FILL = "#f8fafc"       # Slate-50 (White bunny)
EAR_PINK = "#fecdd3"        # Rose-200 (Pink ears)
CHEEK_GLOW = "#ffe4e6"      # Rose-100
CHEEK_FILL = "#fca5a5"      # Rose-300
FEATURE = "#1e293b"         # Slate-800
EYE_DARK = "#581c87"        # Purple-900
EYE_GLOW = "#a855f7"        # Purple-500
HAT_COLOR = "#0f172a"       # Slate-900
HAT_BAND = "#ef4444"        # Red-500
HAT_BRIM = "#020617"        # Slate-950
SPARKLE_YELLOW = "#fbbf24"  # Amber-400
SPARKLE_CYAN = "#22d3ee"    # Cyan-400


def mascot_pixels() -> tuple[tuple[str | None, ...], ...]:
    canvas: list[list[str | None]] = [
        [None for _ in range(MASCOT_WIDTH)] for _ in range(MASCOT_HEIGHT)
    ]

    # 1. Background Magic Sparkles
    _draw_sparkle(canvas, int(_sx(4.0)), int(_sy(15.0)), SPARKLE_YELLOW)
    _draw_sparkle(canvas, int(_sx(36.0)), int(_sy(12.0)), SPARKLE_CYAN)
    _draw_sparkle(canvas, int(_sx(35.0)), int(_sy(26.0)), SPARKLE_YELLOW)
    _draw_sparkle(canvas, int(_sx(5.0)), int(_sy(28.0)), SPARKLE_CYAN)

    # 2. Ears
    _fill_rotated_ellipse(canvas, _sx(11.5), _sy(9.0), _sx(5.4), _sy(12.5), -0.55, OUTLINE)
    _fill_rotated_ellipse(canvas, _sx(28.5), _sy(9.0), _sx(5.4), _sy(12.5), 0.55, OUTLINE)
    _fill_rotated_ellipse(canvas, _sx(11.5), _sy(9.0), _sx(4.2), _sy(11.3), -0.55, FACE_FILL)
    _fill_rotated_ellipse(canvas, _sx(28.5), _sy(9.0), _sx(4.2), _sy(11.3), 0.55, FACE_FILL)
    _fill_rotated_ellipse(canvas, _sx(11.5), _sy(9.0), _sx(2.5), _sy(9.0), -0.55, EAR_PINK)
    _fill_rotated_ellipse(canvas, _sx(28.5), _sy(9.0), _sx(2.5), _sy(9.0), 0.55, EAR_PINK)

    # 3. Head Outline
    _fill_ellipse(canvas, _sx(20.0), _sy(22.0), _sx(16.8), _sy(11.8), OUTLINE)
    _fill_ellipse(canvas, _sx(20.0), _sy(24.5), _sx(18.2), _sy(8.8), OUTLINE)
    # Head Face Fill
    _fill_ellipse(canvas, _sx(20.0), _sy(22.0), _sx(15.5), _sy(10.7), FACE_FILL)
    _fill_ellipse(canvas, _sx(20.0), _sy(24.5), _sx(16.8), _sy(7.8), FACE_FILL)

    # 4. Cheeks
    _fill_circle(canvas, _sx(8.7), _sy(24.0), _sr(4.3), CHEEK_GLOW)
    _fill_circle(canvas, _sx(31.3), _sy(24.0), _sr(4.3), CHEEK_GLOW)
    _fill_circle(canvas, _sx(8.7), _sy(24.0), _sr(2.8), CHEEK_FILL)
    _fill_circle(canvas, _sx(31.3), _sy(24.0), _sr(2.8), CHEEK_FILL)

    # 5. Bowtie (behind hat brim but in front of head)
    _fill_circle(canvas, _sx(20.0), _sy(29.5), _sr(1.5), HAT_BAND)
    _fill_rotated_ellipse(canvas, _sx(17.5), _sy(29.5), _sx(3.0), _sy(1.5), -0.2, HAT_BAND)
    _fill_rotated_ellipse(canvas, _sx(22.5), _sy(29.5), _sx(3.0), _sy(1.5), 0.2, HAT_BAND)

    # 6. Magician Top Hat
    # Hat Cylinder body (at the bottom, y from 31 to 36)
    for y_val in range(31, 37):
        _fill_ellipse(canvas, _sx(20.0), _sy(y_val), _sx(9.0), _sy(2.2), HAT_COLOR)
    # Bottom accent
    _fill_ellipse(canvas, _sx(20.0), _sy(35.5), _sx(9.0), _sy(1.8), HAT_BRIM)

    # Hat Red Ribbon Band
    _fill_ellipse(canvas, _sx(20.0), _sy(32.2), _sx(9.1), _sy(1.5), HAT_BAND)

    # Hat Brim (covers the cylinder top)
    _fill_ellipse(canvas, _sx(20.0), _sy(31.0), _sx(14.0), _sy(2.0), HAT_BRIM)

    # 7. Eyes (glowing magical purple winking eye!)
    # Left eye
    _fill_ellipse(canvas, _sx(13.2), _sy(19.8), _sx(1.8), _sy(3.0), EYE_DARK)
    _fill_ellipse(canvas, _sx(13.2), _sy(19.8), _sx(1.1), _sy(2.0), EYE_GLOW)
    _fill_circle(canvas, _sx(12.4), _sy(18.8), _sr(0.55), FACE_FILL)  # Eye highlight

    # Right eye (wink)
    _draw_thick_line(canvas, _sx(23.2), _sy(19.5), _sx(27.1), _sy(21.0), _sr(1.4), EYE_DARK)
    _draw_thick_line(canvas, _sx(23.2), _sy(19.5), _sx(27.0), _sy(17.6), _sr(1.4), EYE_DARK)

    # 8. Nose & Smile
    _fill_ellipse(canvas, _sx(20.0), _sy(21.5), _sx(1.9), _sy(1.2), EAR_PINK)
    _draw_arc(canvas, _sx(20.0), _sy(25.1), _sx(3.2), _sy(2.6), 0.25, 2.9, _sr(1.1), FEATURE)

    return tuple(tuple(row) for row in canvas)


def mascot_visible_width() -> int:
    return MASCOT_WIDTH


def render_mascot_plain_rows(fill: str = "[]", blank: str = "  ") -> tuple[str, ...]:
    """Fallback plain text representation of the mascot."""
    rows = [blank * MASCOT_WIDTH for _ in range(MASCOT_TOP_PADDING_ROWS)]
    for row in mascot_pixels():
        chunks = []
        for color in row:
            chunks.append(blank if color is None else fill)
        rows.append("".join(chunks))
    return tuple(rows)


def mascot_stacked_rows() -> tuple[tuple[tuple[str | None, str | None], ...], ...]:
    """Combines vertical pixels into pairs for half-block terminal rendering."""
    pixels = mascot_pixels()
    rows = []
    for index in range(0, len(pixels), 2):
        top = pixels[index]
        bottom = pixels[index + 1] if index + 1 < len(pixels) else tuple(
            None for _ in top
        )
        rows.append(tuple(zip(top, bottom)))
    return tuple(rows)


def render_mascot_ansi_rows() -> tuple[str, ...]:
    """Renders the mascot as ANSI terminal escape sequences using half-blocks."""
    rows = []
    for row in mascot_stacked_rows():
        chunks = []
        for top_color, bottom_color in row:
            chunks.append(_ansi_half_block(top_color, bottom_color))
        rows.append("".join(chunks))
    return tuple(rows)


def render_mascot_rich_text():
    """Renders the mascot directly as a styled Rich Text object, avoiding raw ANSI bugs."""
    from rich.text import Text
    from rich.style import Style

    text = Text()
    if MASCOT_TOP_PADDING_ROWS:
        text.append("\n" * MASCOT_TOP_PADDING_ROWS)
    pixels = mascot_stacked_rows()
    for row_idx, row in enumerate(pixels):
        for top_color, bottom_color in row:
            if top_color is None and bottom_color is None:
                text.append(" ")
            elif top_color is None:
                text.append(LOWER_HALF_BLOCK, style=Style(color=bottom_color))
            elif bottom_color is None:
                text.append(UPPER_HALF_BLOCK, style=Style(color=top_color))
            else:
                text.append(UPPER_HALF_BLOCK, style=Style(color=top_color, bgcolor=bottom_color))
        if row_idx < len(pixels) - 1:
            text.append("\n")
    return text


def _ansi_half_block(top_color: str | None, bottom_color: str | None) -> str:
    if top_color is None and bottom_color is None:
        return " "
    if top_color is None:
        r, g, b = _hex_to_rgb(bottom_color)
        return f"\x1b[38;2;{r};{g};{b}m{LOWER_HALF_BLOCK}{ANSI_RESET}"
    if bottom_color is None:
        r, g, b = _hex_to_rgb(top_color)
        return f"\x1b[38;2;{r};{g};{b}m{UPPER_HALF_BLOCK}{ANSI_RESET}"
    fr, fg, fb = _hex_to_rgb(top_color)
    br, bg, bb = _hex_to_rgb(bottom_color)
    return (
        f"\x1b[38;2;{fr};{fg};{fb}m"
        f"\x1b[48;2;{br};{bg};{bb}m"
        f"{UPPER_HALF_BLOCK}{ANSI_RESET}"
    )


def _hex_to_rgb(color: str) -> tuple[int, int, int]:
    color = color.lstrip("#")
    return int(color[0:2], 16), int(color[2:4], 16), int(color[4:6], 16)


def _sx(value: float) -> float:
    return value * MASCOT_WIDTH / BASE_MASCOT_WIDTH


def _sy(value: float) -> float:
    return value * MASCOT_HEIGHT / BASE_MASCOT_HEIGHT


def _sr(value: float) -> float:
    return value * min(MASCOT_WIDTH / BASE_MASCOT_WIDTH, MASCOT_HEIGHT / BASE_MASCOT_HEIGHT)


def _fill_circle(
    canvas: list[list[str | None]], cx: float, cy: float, radius: float, color: str
) -> None:
    _fill_ellipse(canvas, cx, cy, radius, radius, color)


def _fill_ellipse(
    canvas: list[list[str | None]],
    cx: float,
    cy: float,
    rx: float,
    ry: float,
    color: str,
) -> None:
    _fill_rotated_ellipse(canvas, cx, cy, rx, ry, 0.0, color)


def _fill_rotated_ellipse(
    canvas: list[list[str | None]],
    cx: float,
    cy: float,
    rx: float,
    ry: float,
    angle: float,
    color: str,
) -> None:
    cos_a = math.cos(angle)
    sin_a = math.sin(angle)
    min_x = max(0, int(math.floor(cx - rx - 2)))
    max_x = min(MASCOT_WIDTH - 1, int(math.ceil(cx + rx + 2)))
    min_y = max(0, int(math.floor(cy - ry - 2)))
    max_y = min(MASCOT_HEIGHT - 1, int(math.ceil(cy + ry + 2)))
    for y in range(min_y, max_y + 1):
        for x in range(min_x, max_x + 1):
            dx = (x + 0.5) - cx
            dy = (y + 0.5) - cy
            xr = dx * cos_a + dy * sin_a
            yr = -dx * sin_a + dy * cos_a
            if (xr / rx) ** 2 + (yr / ry) ** 2 <= 1.0:
                canvas[y][x] = color


def _draw_thick_line(
    canvas: list[list[str | None]],
    x1: float,
    y1: float,
    x2: float,
    y2: float,
    thickness: float,
    color: str,
) -> None:
    steps = max(2, int(max(abs(x2 - x1), abs(y2 - y1)) * 3))
    radius = thickness / 2
    for step in range(steps + 1):
        t = step / steps
        x = x1 + (x2 - x1) * t
        y = y1 + (y2 - y1) * t
        _fill_circle(canvas, x, y, radius, color)


def _draw_arc(
    canvas: list[list[str | None]],
    cx: float,
    cy: float,
    rx: float,
    ry: float,
    start_angle: float,
    end_angle: float,
    thickness: float,
    color: str,
) -> None:
    steps = max(12, int((end_angle - start_angle) * 18))
    radius = thickness / 2
    for step in range(steps + 1):
        t = start_angle + (end_angle - start_angle) * (step / steps)
        x = cx + math.cos(t) * rx
        y = cy + math.sin(t) * ry
        _fill_circle(canvas, x, y, radius, color)


def _draw_sparkle(
    canvas: list[list[str | None]], cx: int, cy: int, color: str
) -> None:
    """Draws a 4-point magic sparkle star."""
    if 0 <= cx < MASCOT_WIDTH and 0 <= cy < MASCOT_HEIGHT:
        canvas[cy][cx] = color
    if 0 <= cx - 1 < MASCOT_WIDTH and 0 <= cy < MASCOT_HEIGHT:
        canvas[cy][cx - 1] = color
    if 0 <= cx + 1 < MASCOT_WIDTH and 0 <= cy < MASCOT_HEIGHT:
        canvas[cy][cx + 1] = color
    if 0 <= cx < MASCOT_WIDTH and 0 <= cy - 1 < MASCOT_HEIGHT:
        canvas[cy - 1][cx] = color
    if 0 <= cx < MASCOT_WIDTH and 0 <= cy + 1 < MASCOT_HEIGHT:
        canvas[cy + 1][cx] = color
