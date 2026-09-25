"""
Realistic inputs for the live end-to-end tests (built at test time when
demo/inputs/ has nothing from Dev B): a 2-page SCANNED inspection report, a
fake P&ID drawing with 10 tags, and matching SOPs for a temporary KB.
No real company names or logos.
"""
from __future__ import annotations

from pathlib import Path

import pymupdf
from PIL import Image, ImageDraw, ImageFont

REPORT_PAGES = [
    "INSPECTION REPORT - STORAGE TANK T-104\n"
    "Report no: IR-2026-0412    Inspection date: 14-09-2026    Inspector: Level II UT\n\n"
    "1. Scope: external visual and ultrasonic thickness (UT) survey during annual shutdown.\n\n"
    "2. Findings\n"
    "Finding 1 - Shell course 2, north side: UT wall thickness 6.1 mm against nominal 8.0 mm "
    "(24 percent loss). Severity: HIGH.\n\n"
    "Finding 2 - Bottom plate near sump: pitting corrosion up to 1.2 mm deep, isolated pits. "
    "Severity: MEDIUM.\n\n"
    "Finding 3 - Nozzle N3 flange: minor weep at gasket, no active leak. Severity: MEDIUM.",

    "INSPECTION REPORT - STORAGE TANK T-104 (page 2)\n\n"
    "Finding 4 - Roof handrail and stairway: surface rust and coating breakdown. Severity: LOW.\n\n"
    "3. Recommendation: replace shell course 2 plates at the north side before the next run; "
    "monitor bottom plate pits at the next inspection; replace N3 gasket.\n\n"
    "4. Cost estimate: Rs 4,50,000 as per contractor quote for plate replacement.\n\n"
    "Prepared by: Inspection Engineer    Checked by: Section Head",
]

SOPS: dict[str, list[str]] = {
    "SOP-INSP-012_tank_inspection.pdf": [
        "SOP-INSP-012 STORAGE TANK INSPECTION. Section 1: Scope. This procedure covers external and internal "
        "inspection of atmospheric storage tanks, including ultrasonic thickness (UT) survey of shell courses, "
        "bottom plates, roof and nozzles.",
        "SOP-INSP-012 Section 4: Minimum wall thickness and repair criteria. A shell course must be repaired or "
        "replaced when the measured wall thickness is below 80 percent of nominal thickness, or below the "
        "minimum required thickness plus corrosion allowance. Wall thinning above 20 percent loss is classed "
        "HIGH severity and must be repaired before the next run.",
        "SOP-INSP-012 Section 5: Bottom plate pitting. Isolated pits shallower than 50 percent of the plate "
        "thickness may remain in service and are re-inspected at the next shutdown. Pitting corrosion deeper "
        "than this requires patch plates.",
    ],
    "SOP-MNT-003_gasket_and_flange.pdf": [
        "SOP-MNT-003 FLANGE AND GASKET MAINTENANCE. Section 2: Leaks and weeps. A weep at a flange gasket is "
        "recorded as MEDIUM severity. The gasket is replaced at the next opportunity; bolts are re-torqued in "
        "a star pattern to the specified torque.",
        "SOP-MNT-003 Section 3: Nozzle flange inspection. Check flange faces for scoring and corrosion before "
        "fitting a new spiral wound gasket.",
    ],
    "SOP-HSE-011_hot_work_permit.pdf": [
        "SOP-HSE-011 HOT WORK PERMIT. A hot work permit is valid for 8 hours. A gas test must be done before "
        "welding or cutting starts. A fire watch stays for 30 minutes after the work ends.",
        "SOP-HSE-011 Section 2: Welding repairs on tanks need the tank to be cleaned, gas freed and isolated.",
    ],
}

# 10 tags placed well inside the four quadrants so each is inside one tile.
PID_TAGS: list[tuple[str, str, tuple[int, int]]] = [
    ("P-101A", "pump", (180, 170)), ("P-101B", "pump", (520, 170)), ("FIC-101", "instrument", (300, 420)),
    ("V-201", "valve", (1020, 170)), ("PT-102", "instrument", (1350, 400)),
    ("T-301", "vessel", (200, 800)), ("LT-301", "instrument", (520, 1000)),
    ("E-401", "exchanger", (1050, 800)), ("TT-401", "instrument", (1350, 1000)), ("V-202", "valve", (1180, 620)),
]
EXPECTED_TAGS = [tag for tag, _, _ in PID_TAGS]


def _page_textbox(page, text: str, fontsize: int) -> None:
    page.insert_textbox(pymupdf.Rect(50, 50, 545, 800), text, fontsize=fontsize)


def write_text_pdf(path: Path, pages: list[str]) -> Path:
    doc = pymupdf.open()
    for text in pages:
        _page_textbox(doc.new_page(), text, 12)
    doc.save(str(path))
    doc.close()
    return path


def write_scanned_pdf(path: Path, pages: list[str], dpi: int = 200) -> Path:
    """Render pages to bitmaps and keep only the images: no text layer, so OCR is required."""
    text_doc = pymupdf.open()
    for text in pages:
        _page_textbox(text_doc.new_page(), text, 13)
    scanned = pymupdf.open()
    for page in text_doc:
        pix = page.get_pixmap(dpi=dpi)
        out = scanned.new_page(width=page.rect.width, height=page.rect.height)
        out.insert_image(out.rect, stream=pix.tobytes("png"))
    scanned.save(str(path))
    scanned.close()
    text_doc.close()
    return path


def build_report(folder: Path) -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    return write_scanned_pdf(folder / "inspection_report_T104_scanned.pdf", REPORT_PAGES)


def build_sops(folder: Path) -> list[str]:
    folder.mkdir(parents=True, exist_ok=True)
    for name, pages in SOPS.items():
        write_text_pdf(folder / name, pages)
    return list(SOPS)


def _font(size: int):
    for candidate in (Path("C:/Windows/Fonts/arialbd.ttf"), Path("C:/Windows/Fonts/arial.ttf")):
        if candidate.exists():
            return ImageFont.truetype(str(candidate), size)
    return ImageFont.load_default(size=size)


def build_pid_image(folder: Path) -> Path:
    """A simple black-on-white P&ID-style drawing: symbols, lines and 10 tag labels."""
    folder.mkdir(parents=True, exist_ok=True)
    width, height = 1600, 1200
    image = Image.new("RGB", (width, height), "white")
    draw = ImageDraw.Draw(image)
    label_font, title_font = _font(34), _font(26)
    draw.rectangle([5, 5, width - 6, height - 6], outline="black", width=3)
    # process lines between the symbols
    for (x1, y1), (x2, y2) in [((180, 250), (520, 250)), ((520, 250), (1020, 250)), ((1020, 250), (1350, 480)),
                               ((200, 880), (1050, 880)), ((1050, 880), (1350, 1080)), ((520, 250), (520, 1080)),
                               ((1180, 250), (1180, 700))]:
        draw.line([x1, y1, x2, y2], fill="black", width=4)
    for tag, kind, (x, y) in PID_TAGS:
        if kind == "pump":
            draw.ellipse([x - 40, y + 40, x + 40, y + 120], outline="black", width=4)
        elif kind == "instrument":
            draw.ellipse([x - 45, y - 45, x + 45, y + 45], outline="black", width=4, fill="white")
        elif kind == "valve":
            draw.polygon([(x - 40, y + 50), (x + 40, y + 110), (x + 40, y + 50), (x - 40, y + 110)],
                         outline="black", width=4)
        else:
            draw.rectangle([x - 70, y + 40, x + 70, y + 200], outline="black", width=4, fill="white")
        box = draw.textbbox((0, 0), tag, font=label_font)
        tw = box[2] - box[0]
        draw.rectangle([x - tw // 2 - 8, y - 22, x + tw // 2 + 8, y + 24], fill="white")
        draw.text((x - tw // 2, y - 20), tag, fill="black", font=label_font)
    draw.text((40, height - 60), "P&ID - TANK FARM UNIT 100 (TEST DRAWING)", fill="black", font=title_font)
    path = folder / "pid_unit100.png"
    image.save(path)
    return path


def demo_input(name_contains: str, repo_root: Path) -> Path | None:
    """A matching file from demo/inputs/ (Dev B's real inputs), if any."""
    folder = repo_root / "demo" / "inputs"
    if not folder.is_dir():
        return None
    for path in sorted(folder.iterdir()):
        if path.is_file() and name_contains.lower() in path.name.lower():
            return path
    return None
