"""
Generate the extra demo inputs for the general agent tools.

    python demo/tools/make_extra_inputs.py

Writes (overwriting):
  demo/inputs/field_note_handwritten.jpg  a photographed, handwritten shift-round note
                                          (inspect_image: multimodal / handwriting demo)
  demo/inputs/equipment_thickness.csv     equipment list with thickness readings
                                          (analyze_table: spreadsheet demo)

The "handwriting" is the Windows Ink Free font (fallback: Segoe Print, then Pillow's default
font) on a lined page, rotated slightly with paper tint and noise so it reads like a phone photo.
Fictional plant ("Sahyadri Demo Refinery"), fictional readings. Deterministic. Pillow only.
"""
from __future__ import annotations

import csv
import random
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter, ImageFont

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
INPUTS = REPO_ROOT / "demo" / "inputs"
NOTE_PATH = INPUTS / "field_note_handwritten.jpg"
CSV_PATH = INPUTS / "equipment_thickness.csv"
FONT_CANDIDATES = (Path("C:/Windows/Fonts/Inkfree.ttf"), Path("C:/Windows/Fonts/segoepr.ttf"))
SEED = 27
JPEG_QUALITY = 85   # a phone photo is a JPEG; keeps the file small

NOTE_LINES = [
    "Shift round - Unit 300 - 27/09 night",
    "P-201B: bearing noise, casing 78 C (high)",
    "FL-3 flange near CML-04: weeping,",
    "   H2S monitor 8 ppm at 1 m",
    "PSV-201: tag plate missing",
    "LT-201 reads 62%, sight glass ~55%",
    "Action: WO for P-201B, re-torque FL-3",
    "                         - R. Menon",
]

EQUIPMENT_ROWS = [
    # tag, service, design_pressure_bar, operating_pressure_bar, nominal_thickness_mm, last_thickness_mm, years_in_service
    # Wall loss (nominal - last) / nominal: 4 items above 15 % (E-201 17.5, V-102 18.1, T-201 25.0, 6-P-101 26.9);
    # none sits exactly on 15 %, so the demo answer does not depend on float rounding.
    ("V-101", "Crude flash drum", 12.0, 9.5, 12.0, 10.3, 14),
    ("V-102", "Naphtha stabiliser receiver", 18.0, 15.2, 16.0, 13.1, 11),
    ("E-201", "Crude preheat exchanger shell", 25.0, 21.0, 8.0, 6.6, 9),
    ("T-201", "Crude storage tank bottom plate", 1.0, 0.2, 8.0, 6.0, 22),
    ("T-301", "Slop tank shell course 1", 1.5, 0.4, 6.0, 5.2, 18),
    ("V-103", "Fuel gas knock-out drum", 10.0, 8.1, 10.0, 9.4, 7),
    ("P-201A", "Charge pump casing", 30.0, 24.0, 14.0, 13.6, 6),
    ("6-P-101", "Crude line CML-03 elbow", 20.0, 16.5, 7.11, 5.20, 12),
]
CSV_HEADER = ["tag", "service", "design_pressure_bar", "operating_pressure_bar", "nominal_thickness_mm",
              "last_thickness_mm", "years_in_service"]


def handwriting_font(size: int) -> ImageFont.ImageFont:
    for path in FONT_CANDIDATES:
        if path.exists():
            return ImageFont.truetype(str(path), size)
    return ImageFont.load_default(size=size)


def make_note(lines: list[str] = NOTE_LINES, seed: int = SEED) -> Image.Image:
    """A lined page with handwritten-looking lines, slightly rotated, tinted and noisy like a phone photo."""
    rng = random.Random(seed)
    width, height, top, gap = 1400, 1000, 120, 100
    page = Image.new("RGB", (width, height), (246, 242, 228))
    draw = ImageDraw.Draw(page)
    for y in range(top + gap - 20, height - 40, gap):
        draw.line([(60, y), (width - 60, y)], fill=(170, 190, 215), width=2)
    draw.line([(130, 40), (130, height - 40)], fill=(215, 140, 140), width=2)
    font = handwriting_font(52)
    for i, line in enumerate(lines):
        x = 150 + rng.randint(-6, 10)
        y = top + gap - 84 + i * gap + rng.randint(-4, 3)     # sits on its ruled line
        ink = (25 + rng.randint(0, 20), 35 + rng.randint(0, 15), 110 + rng.randint(0, 40))
        draw.text((x, y), line, font=font, fill=ink)
    photo = page.rotate(-1.8, resample=Image.BICUBIC, expand=True, fillcolor=(90, 88, 80))
    noise = Image.effect_noise(photo.size, 18).convert("RGB")
    photo = Image.blend(photo, noise, 0.06).filter(ImageFilter.GaussianBlur(0.6))
    return photo


def write_csv(path: Path = CSV_PATH) -> None:
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(CSV_HEADER)
        writer.writerows(EQUIPMENT_ROWS)


def main() -> None:
    INPUTS.mkdir(parents=True, exist_ok=True)
    make_note().save(NOTE_PATH, quality=JPEG_QUALITY, optimize=True)
    write_csv()
    print(f"wrote {NOTE_PATH.relative_to(REPO_ROOT)} and {CSV_PATH.relative_to(REPO_ROOT)}")


if __name__ == "__main__":
    main()
