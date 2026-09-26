"""
Generate a fictional P&ID drawing for demo Scenario C.

    python demo/tools/make_pid.py

Writes (overwriting) demo/inputs/scenario_c_pid_generated.png.

A crude transfer system: storage tank -> shutdown valve -> two charge pumps
-> heat exchanger -> separator vessel, with flow / pressure / level /
temperature instruments and a relief valve. 12 tags, common ISA-style
prefixes (T-, XV-, P-, E-, V-, FT-, PT-, LT-, TT-, PSV-). Tags are written in
full next to each symbol (e.g. "FT-201") so each tag reads as one string.

Every tag is placed well inside one quadrant, so the backend's 2x2 tiling
(15% overlap) sees each tag whole in at least one tile.

Fictional plant, no real data. Deterministic. Pillow only.
"""
from __future__ import annotations

import math
import os
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
OUT_PATH = REPO_ROOT / "demo" / "inputs" / "scenario_c_pid_generated.png"

W, H = 2400, 1600
BG = 255
INK = 0
LINE_W = 4
INSTR_R = 34

# tag -> equipment type (the answer key; demo/expected.md copies this table)
TAGS: dict[str, str] = {
    "T-201": "Tank",
    "XV-201": "Shutdown (on/off) valve",
    "P-201A": "Pump",
    "P-201B": "Pump (standby)",
    "E-201": "Heat exchanger",
    "V-201": "Vessel (separator)",
    "FT-201": "Flow transmitter",
    "PT-202": "Pressure transmitter",
    "LT-201": "Level transmitter",
    "LT-202": "Level transmitter",
    "TT-203": "Temperature transmitter",
    "PSV-201": "Pressure safety valve",
}


def _font(name: str, size: int) -> ImageFont.ImageFont:
    try:
        return ImageFont.truetype(str(Path(os.environ.get("WINDIR", r"C:\Windows")) / "Fonts" / name), size)
    except OSError:
        return ImageFont.load_default(size)


TAG_FONT = _font("arialbd.ttf", 34)
NOTE_FONT = _font("arial.ttf", 24)
TITLE_FONT = _font("arialbd.ttf", 30)


# ------------------------------------------------------------------ primitives
def label(draw: ImageDraw.ImageDraw, xy: tuple[float, float], text: str, font=TAG_FONT, anchor: str = "mm") -> None:
    draw.text(xy, text, font=font, fill=INK, anchor=anchor)


def arrow_head(draw: ImageDraw.ImageDraw, tip: tuple[float, float], direction: tuple[float, float], size: int = 20) -> None:
    dx, dy = direction
    length = math.hypot(dx, dy) or 1.0
    ux, uy = dx / length, dy / length
    bx, by = tip[0] - ux * size, tip[1] - uy * size
    px, py = -uy * size * 0.55, ux * size * 0.55
    draw.polygon([tip, (bx + px, by + py), (bx - px, by - py)], fill=INK)


def pipe(draw: ImageDraw.ImageDraw, points: list[tuple[float, float]], arrows: bool = True) -> None:
    """Process line through `points`, with a flow arrow in the middle of each long segment."""
    draw.line(points, fill=INK, width=LINE_W, joint="curve")
    if not arrows:
        return
    for (x1, y1), (x2, y2) in zip(points, points[1:]):
        if math.hypot(x2 - x1, y2 - y1) > 120:
            mid = ((x1 + x2) / 2, (y1 + y2) / 2)
            arrow_head(draw, mid, (x2 - x1, y2 - y1))


def signal(draw: ImageDraw.ImageDraw, start: tuple[float, float], end: tuple[float, float]) -> None:
    """Dashed instrument connection line."""
    x1, y1 = start
    x2, y2 = end
    length = math.hypot(x2 - x1, y2 - y1)
    steps = int(length // 16)
    for i in range(0, steps, 2):
        a, b = i / steps, min(1.0, (i + 1) / steps)
        draw.line((x1 + (x2 - x1) * a, y1 + (y2 - y1) * a, x1 + (x2 - x1) * b, y1 + (y2 - y1) * b), fill=INK, width=2)


def instrument(draw: ImageDraw.ImageDraw, centre: tuple[float, float], tag: str, tap: tuple[float, float],
               label_side: str = "right") -> None:
    """ISA field-instrument bubble, dashed tap to the process, tag written in full beside it."""
    cx, cy = centre
    signal(draw, tap, centre)
    draw.ellipse((cx - INSTR_R, cy - INSTR_R, cx + INSTR_R, cy + INSTR_R), outline=INK, width=3, fill=BG)
    letters = tag.split("-")[0]
    label(draw, (cx, cy), letters, font=_font("arialbd.ttf", 24))
    offset = INSTR_R + 14
    if label_side == "right":
        label(draw, (cx + offset, cy), tag, anchor="lm")
    elif label_side == "left":
        label(draw, (cx - offset, cy), tag, anchor="rm")
    else:
        label(draw, (cx, cy - offset), tag, anchor="md")


def tank(draw: ImageDraw.ImageDraw, box: tuple[int, int, int, int], tag: str) -> None:
    x1, y1, x2, y2 = box
    draw.rectangle(box, outline=INK, width=LINE_W)
    draw.line((x1, y1, (x1 + x2) / 2, y1 - 50, x2, y1), fill=INK, width=LINE_W)   # cone roof
    label(draw, ((x1 + x2) / 2, y2 + 40), tag)
    label(draw, ((x1 + x2) / 2, y2 + 80), "CRUDE STORAGE", font=NOTE_FONT)


def pump(draw: ImageDraw.ImageDraw, centre: tuple[int, int], tag: str) -> None:
    cx, cy = centre
    r = 48
    draw.ellipse((cx - r, cy - r, cx + r, cy + r), outline=INK, width=LINE_W, fill=BG)
    draw.line((cx, cy - r, cx + r + 30, cy - r), fill=INK, width=LINE_W)          # tangential discharge
    draw.polygon([(cx - 36, cy + r + 30), (cx + 36, cy + r + 30), (cx, cy + r - 4)], outline=INK, width=3)  # base
    label(draw, (cx, cy + r + 62), tag)


def gate_valve(draw: ImageDraw.ImageDraw, centre: tuple[int, int], tag: str, actuated: bool = False) -> None:
    cx, cy = centre
    s = 28
    draw.polygon([(cx - s, cy - s), (cx - s, cy + s), (cx + s, cy - s), (cx + s, cy + s)], outline=INK, width=3, fill=BG)
    if actuated:
        draw.line((cx, cy, cx, cy - 60), fill=INK, width=3)
        draw.rectangle((cx - 26, cy - 90, cx + 26, cy - 60), outline=INK, width=3, fill=BG)
    label(draw, (cx, cy + s + 36), tag)


def exchanger(draw: ImageDraw.ImageDraw, centre: tuple[int, int], tag: str) -> None:
    cx, cy = centre
    r = 70
    draw.ellipse((cx - r, cy - r, cx + r, cy + r), outline=INK, width=LINE_W, fill=BG)
    zig = [(cx - r - 40, cy + 20), (cx - 40, cy + 20), (cx - 20, cy - 25), (cx, cy + 25), (cx + 20, cy - 25),
           (cx + 40, cy + 20), (cx + r + 40, cy + 20)]
    draw.line(zig, fill=INK, width=3)
    label(draw, (cx, cy + r + 40), tag)
    label(draw, (cx - r - 40, cy + 50), "CW", font=NOTE_FONT, anchor="rm")


def vessel(draw: ImageDraw.ImageDraw, box: tuple[int, int, int, int], tag: str) -> None:
    """Horizontal drum with elliptical heads."""
    x1, y1, x2, y2 = box
    h = y2 - y1
    draw.rounded_rectangle(box, radius=h // 2, outline=INK, width=LINE_W, fill=BG)
    label(draw, ((x1 + x2) / 2, (y1 + y2) / 2), tag)
    label(draw, ((x1 + x2) / 2, y2 + 34), "SEPARATOR", font=NOTE_FONT)


def relief_valve(draw: ImageDraw.ImageDraw, base: tuple[int, int], tag: str) -> None:
    bx, by = base
    draw.line((bx, by, bx, by - 50), fill=INK, width=LINE_W)
    draw.polygon([(bx - 22, by - 50), (bx + 22, by - 50), (bx, by - 85)], outline=INK, width=3, fill=BG)
    draw.polygon([(bx, by - 85), (bx + 40, by - 107), (bx + 40, by - 63)], outline=INK, width=3, fill=BG)
    pipe(draw, [(bx + 40, by - 85), (bx + 110, by - 85), (bx + 110, by - 160)], arrows=False)
    arrow_head(draw, (bx + 110, by - 160), (0, -1))
    label(draw, (bx + 110, by - 170), "TO FLARE", font=NOTE_FONT, anchor="md")
    label(draw, (bx - 30, by - 85), tag, anchor="rm")


def title_block(draw: ImageDraw.ImageDraw) -> None:
    x1, y1, x2, y2 = W - 760, H - 190, W - 40, H - 40
    draw.rectangle((x1, y1, x2, y2), outline=INK, width=3)
    draw.line((x1, y1 + 50, x2, y1 + 50), fill=INK, width=2)
    label(draw, (x1 + 20, y1 + 25), "SAHYADRI DEMO REFINERY (FICTIONAL)", font=TITLE_FONT, anchor="lm")
    label(draw, (x1 + 20, y1 + 80), "CRUDE TRANSFER AND PREHEAT - P&ID", font=NOTE_FONT, anchor="lm")
    label(draw, (x1 + 20, y1 + 118), "DRG: DEMO-C-001   REV 0   NOT FOR CONSTRUCTION", font=NOTE_FONT, anchor="lm")
    draw.rectangle((40, 40, W - 40, H - 40), outline=INK, width=3)                # drawing border


# ------------------------------------------------------------------ drawing
def draw_pid() -> Image.Image:
    img = Image.new("L", (W, H), BG)
    d = ImageDraw.Draw(img)
    title_block(d)

    header_y = 470                     # pump discharge header, top half
    suction_y = 1000                   # tank outlet / suction line, bottom half

    # --- tank (bottom-left quadrant)
    tank(d, (240, 700, 560, 1150), "T-201")
    instrument(d, (680, 790), "LT-201", tap=(560, 790), label_side="right")

    # --- tank outlet -> XV-201 -> suction header
    pipe(d, [(560, suction_y), (760, suction_y)])
    gate_valve(d, (800, suction_y), "XV-201", actuated=True)
    pipe(d, [(828, suction_y), (1012, suction_y)], arrows=False)

    # --- two pumps (bottom half)
    pump(d, (1060, suction_y), "P-201A")
    pump(d, (1060, 1300), "P-201B")
    pipe(d, [(920, suction_y), (920, 1300), (1012, 1300)])
    # discharges rise to the header
    pipe(d, [(1138, suction_y - 48), (1180, suction_y - 48), (1180, header_y)])
    pipe(d, [(1138, 1300 - 48), (1260, 1300 - 48), (1260, header_y)])

    # --- header to exchanger (top half)
    pipe(d, [(1180, header_y), (1520, header_y)])
    instrument(d, (1330, 300), "FT-201", tap=(1330, header_y), label_side="right")
    instrument(d, (1180, 300), "PT-202", tap=(1180, header_y), label_side="left")

    exchanger(d, (1600, header_y), "E-201")
    pipe(d, [(1670, header_y), (1880, header_y)])
    instrument(d, (1780, 300), "TT-203", tap=(1780, header_y), label_side="right")

    # --- separator vessel (right side, straddling nothing: centre in top-right)
    vessel(d, (1880, 420, 2280, 580), "V-201")
    relief_valve(d, (2180, 420), "PSV-201")
    instrument(d, (1960, 760), "LT-202", tap=(1960, 575), label_side="left")

    # --- vessel outlet to next unit
    pipe(d, [(2230, 580), (2230, 1000), (2330, 1000)])
    label(d, (2330, 1040), "TO UNIT 300", font=NOTE_FONT, anchor="rm")
    pipe(d, [(70, 780), (240, 780)])
    label(d, (75, 750), "FROM JETTY", font=NOTE_FONT, anchor="ld")
    return img


def main() -> int:
    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    draw_pid().save(OUT_PATH, format="PNG", optimize=True)
    print(f"wrote {OUT_PATH.relative_to(REPO_ROOT)} ({W}x{H} px, {len(TAGS)} tags)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
