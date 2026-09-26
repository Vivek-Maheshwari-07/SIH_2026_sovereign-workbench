"""
Generate the two fictional SCANNED inspection reports for demo Scenario A.

    python demo/tools/make_inspection_reports.py

Writes (overwriting):
    demo/inputs/scenario_a_report_1.pdf     Storage Tank T-104 Annual Inspection (2 pages)
    demo/inputs/scenario_a_report_2.pdf     Crude Line 6-P-101 Thickness Survey (1-2 pages)
    demo/inputs/_source_text/scenario_a_report_1.txt   clean text = answer key
    demo/inputs/_source_text/scenario_a_report_2.txt

Each page is drawn with Pillow at WB-style 200 DPI, then "scanned": grey
paper tone, faint rubber stamp, hand signature, light noise, small blur,
0.5-2 degree rotation, JPEG artefacts. The PDF holds only the page images
(no text layer), so the backend has to OCR it.

Everything is fictional: plant, people, report numbers. Output is
reproducible (fixed seeds, no clock). Only Pillow + PyMuPDF are used.
"""
from __future__ import annotations

import io
import math
import os
import random
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional, Union

import pymupdf
from PIL import Image, ImageChops, ImageDraw, ImageFilter, ImageFont

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
OUT_DIR = REPO_ROOT / "demo" / "inputs"
TEXT_DIR = OUT_DIR / "_source_text"

DPI = 200
PAGE_W, PAGE_H = 1654, 2339           # A4 at 200 DPI
MARGIN_X, MARGIN_TOP, MARGIN_BOTTOM = 150, 140, 190
PAPER_TONE = 232                      # grey scanner paper (255 = white)
INK = 28

PLANT = "SAHYADRI DEMO REFINERY (fictional)"
SECTION = "Inspection & Corrosion Control Section"
FOOTER_NOTE = "Fictional demo document - Sovereign AI Workbench - no real plant data"


# ------------------------------------------------------------------ content model
@dataclass
class Title:
    text: str


@dataclass
class HeaderBlock:
    rows: list[tuple[str, str]]


@dataclass
class Heading:
    text: str


@dataclass
class Para:
    text: str


@dataclass
class Table:
    columns: list[str]
    widths: list[float]               # fractions of the text width, sum to 1
    rows: list[list[str]]
    caption: Optional[str] = None


@dataclass
class Signature:
    left: tuple[str, str]             # (name line, role line)
    right: tuple[str, str]


Block = Union[Title, HeaderBlock, Heading, Para, Table, Signature]


@dataclass
class Report:
    stem: str
    report_no: str
    seed: int
    angles: list[float]               # rotation per page, degrees
    stamp_text: tuple[str, str, str]
    blocks: list[Block] = field(default_factory=list)


# ------------------------------------------------------------------ report 1
REPORT_1 = Report(
    stem="scenario_a_report_1",
    report_no="SDR/INSP/2026/0412",
    seed=4104,
    angles=[1.2, -0.8],
    stamp_text=("SDR - INSPECTION", "INSPECTED", "14 SEP 2026"),
    blocks=[
        Title("STORAGE TANK T-104 ANNUAL INSPECTION REPORT"),
        HeaderBlock([
            ("Report No.", "SDR/INSP/2026/0412"),
            ("Date of inspection", "14-09-2026"),
            ("Inspector", "M. P. Joshi, Level II UT"),
            ("Unit", "Crude Tank Farm, Unit 11 - Tank T-104 (crude oil, 12,000 m3)"),
        ]),
        Heading("1. Background"),
        Para(
            "Tank T-104 was taken out of service on 08-09-2026 for its annual inspection. "
            "The tank was drained, the manways were opened and a confined space entry was made "
            "for internal visual inspection and ultrasonic thickness (UT) survey of the shell, "
            "bottom and roof plates. The last inspection in September 2025 reported general "
            "corrosion within limits. This report lists the findings and the repair work needed "
            "before the tank is returned to service."
        ),
        Heading("2. Wall thickness readings (UT, minimum of grid)"),
        Table(
            columns=["Location", "Nominal (mm)", "Measured min (mm)", "Loss (%)"],
            widths=[0.43, 0.19, 0.22, 0.16],
            rows=[
                ["Shell course 1, north", "10.0", "9.4", "6"],
                ["Shell course 2, north", "8.0", "6.1", "24"],
                ["Shell course 2, south", "8.0", "7.6", "5"],
                ["Bottom plate near sump", "8.0", "6.8", "15"],
                ["Roof plate, centre", "6.0", "5.7", "5"],
            ],
        ),
        Heading("3. Findings"),
        Table(
            columns=["No.", "Item", "Observation", "Severity"],
            widths=[0.07, 0.25, 0.53, 0.15],
            rows=[
                ["1", "Shell course 2, north side",
                 "Wall thinning to 6.1 mm against 8.0 mm nominal (24% loss). "
                 "Below 80% of nominal thickness. Plate replacement by welding needed.", "High"],
                ["2", "Bottom plate near sump",
                 "Pitting up to 1.2 mm deep. About 150 mm sludge layer. H2S of 18 ppm "
                 "measured at manway M1 when the sludge was disturbed.", "High"],
                ["3", "Inlet nozzle N2 and mixer MX-104",
                 "No spade blind fitted on N2 during entry. Mixer motor breaker was "
                 "switched off but not locked or tagged.", "Medium"],
                ["4", "Confined space entry permit",
                 "No gas test entries on the permit after 11:00. Attendant left the "
                 "manway for about 10 minutes.", "Medium"],
                ["5", "Roof handrail and stairway",
                 "Surface rust and coating breakdown. No loss of section.", "Low"],
            ],
        ),
        Heading("4. Recommendation"),
        Para(
            "(a) Replace the two thinned plates of shell course 2, north side, before the tank "
            "is returned to service. Welding must be done under a hot work permit after the tank "
            "is cleaned, gas freed and gas tested, with a fire watch in place. "
            "(b) Remove all sludge before any further entry. Use continuous H2S monitoring and "
            "breathing apparatus if H2S is above the permissible limit. "
            "(c) Fit a spade blind on nozzle N2 and apply lockout/tagout to the mixer MX-104 motor "
            "before the next entry. "
            "(d) Brief all entrants and attendants on the confined space entry procedure. "
            "(e) Re-inspect the bottom plate pits at the next shutdown and repaint the roof handrail."
        ),
        Para(
            "Estimated cost: Rs 4,50,000 (plate replacement Rs 3,20,000; sludge removal "
            "Rs 90,000; blinds and coating Rs 40,000)."
        ),
        Signature(
            left=("M. P. Joshi", "Inspector, Level II UT"),
            right=("K. S. Iyer", "Section Head, Inspection"),
        ),
    ],
)

# ------------------------------------------------------------------ report 2
REPORT_2 = Report(
    stem="scenario_a_report_2",
    report_no="SDR/INSP/2026/0419",
    seed=6101,
    angles=[-1.5, 0.6],
    stamp_text=("SDR - INSPECTION", "INSPECTED", "18 SEP 2026"),
    blocks=[
        Title("CRUDE LINE 6-P-101 THICKNESS SURVEY"),
        HeaderBlock([
            ("Report No.", "SDR/INSP/2026/0419"),
            ("Date of survey", "18-09-2026"),
            ("Inspector", "R. T. Menon, Level II UT"),
            ("Unit", "Crude Distillation Unit, Unit 12 - line 6-P-101 (6 inch, sour crude)"),
        ]),
        Heading("1. Background"),
        Para(
            "Line 6-P-101 carries sour crude from charge pump P-101A/B to the preheat train. "
            "It is a 6 inch carbon steel line, schedule 40, nominal wall 7.11 mm, required minimum "
            "thickness 4.80 mm. The line was surveyed at six condition monitoring locations (CML) "
            "while in service. The last survey was in September 2021."
        ),
        Heading("2. Wall thickness readings (UT)"),
        Table(
            columns=["CML", "Location", "Measured (mm)", "Loss (%)"],
            widths=[0.13, 0.47, 0.22, 0.18],
            rows=[
                ["CML-01", "Straight run after P-101B", "6.95", "2"],
                ["CML-02", "Straight run, pipe rack", "6.88", "3"],
                ["CML-03", "Elbow at P-101A discharge", "5.20", "27"],
                ["CML-04", "Dead leg low point", "5.90", "17"],
                ["CML-05", "Under insulation at support PS-12", "6.10", "14"],
                ["CML-06", "Before preheat exchanger", "7.02", "1"],
            ],
        ),
        Para(
            "Corrosion rate at CML-03: 0.38 mm/year over 5 years. Remaining life at CML-03: "
            "about 1 year to the required minimum thickness of 4.80 mm."
        ),
        Heading("3. Findings"),
        Table(
            columns=["No.", "Item", "Observation", "Severity"],
            widths=[0.07, 0.25, 0.53, 0.15],
            rows=[
                ["1", "CML-03 elbow at P-101A discharge",
                 "Erosion-corrosion. 5.20 mm against 7.11 mm nominal (27% loss). "
                 "About 1 year of remaining life.", "High"],
                ["2", "Flange FL-3 near CML-04",
                 "Weep at gasket. Personal H2S monitor alarmed at 12 ppm within 1 m of "
                 "the flange. Area barricaded.", "High"],
                ["3", "CML-04 dead leg low point",
                 "5.90 mm (17% loss). Water hold-up suspected.", "Medium"],
                ["4", "CML-05 at support PS-12",
                 "Corrosion under insulation. 6.10 mm (14% loss).", "Medium"],
                ["5", "Insulation cladding",
                 "About 2 m of damaged cladding near PS-12.", "Low"],
            ],
        ),
        Heading("4. Recommendation"),
        Para(
            "(a) Replace a 3 m spool including the CML-03 elbow at the next shutdown. Before "
            "cutting, isolate the line, lock and tag the P-101A motor and valves, drain and purge. "
            "Cutting and welding only under a hot work permit with gas test and fire watch. "
            "(b) Replace the FL-3 gasket. Work on FL-3 needs H2S monitoring and breathing apparatus. "
            "(c) Re-survey CML-04 and CML-05 in 6 months and repair the insulation cladding."
        ),
        Para(
            "Estimated cost: Rs 2,85,000 (spool replacement Rs 1,95,000; insulation and CUI "
            "repair Rs 55,000; gasket and scaffolding Rs 35,000)."
        ),
        Signature(
            left=("R. T. Menon", "Inspector, Level II UT"),
            right=("K. S. Iyer", "Section Head, Inspection"),
        ),
    ],
)

REPORTS = [REPORT_1, REPORT_2]


# ------------------------------------------------------------------ fonts
def _font_dir() -> Path:
    return Path(os.environ.get("WINDIR", r"C:\Windows")) / "Fonts"


def load_font(name: str, size: int) -> ImageFont.ImageFont:
    """TrueType font from the Windows font folder; Pillow's built-in font if missing."""
    try:
        return ImageFont.truetype(str(_font_dir() / name), size)
    except OSError:
        return ImageFont.load_default(size)


@dataclass
class Fonts:
    title: ImageFont.ImageFont
    heading: ImageFont.ImageFont
    body: ImageFont.ImageFont
    bold: ImageFont.ImageFont
    small: ImageFont.ImageFont
    sign: ImageFont.ImageFont


def make_fonts() -> Fonts:
    return Fonts(
        title=load_font("arialbd.ttf", 42),
        heading=load_font("arialbd.ttf", 32),
        body=load_font("arial.ttf", 30),
        bold=load_font("arialbd.ttf", 30),
        small=load_font("arial.ttf", 24),
        sign=load_font("ariali.ttf", 28),
    )


# ------------------------------------------------------------------ layout helpers
def wrap(text: str, font: ImageFont.ImageFont, width: int) -> list[str]:
    """Greedy word wrap to `width` pixels."""
    lines: list[str] = []
    current = ""
    for word in text.split():
        trial = f"{current} {word}".strip()
        if font.getlength(trial) <= width or not current:
            current = trial
        else:
            lines.append(current)
            current = word
    if current:
        lines.append(current)
    return lines


def line_height(font: ImageFont.ImageFont) -> int:
    return int(font.size * 1.38)


class PageWriter:
    """Draws blocks top to bottom, starting a new page when one is full."""

    def __init__(self, report: Report, fonts: Fonts) -> None:
        self.report = report
        self.fonts = fonts
        self.pages: list[Image.Image] = []
        self.text_pages: list[list[str]] = []
        self.draw: ImageDraw.ImageDraw
        self.y = 0
        self.signature_y: Optional[int] = None
        self.new_page()

    @property
    def text_width(self) -> int:
        return PAGE_W - 2 * MARGIN_X

    @property
    def page_number(self) -> int:
        return len(self.pages)

    def new_page(self) -> None:
        page = Image.new("L", (PAGE_W, PAGE_H), 255)
        self.pages.append(page)
        self.text_pages.append([])
        self.draw = ImageDraw.Draw(page)
        self.y = MARGIN_TOP
        self._running_header()

    def _running_header(self) -> None:
        f = self.fonts
        self.draw.text((MARGIN_X, self.y), PLANT, font=f.bold, fill=INK)
        right = f"Report No. {self.report.report_no}"
        self.draw.text((PAGE_W - MARGIN_X - f.small.getlength(right), self.y + 4), right, font=f.small, fill=INK)
        self.y += line_height(f.bold)
        self.draw.text((MARGIN_X, self.y), SECTION, font=f.small, fill=INK)
        self.y += line_height(f.small) + 6
        self.draw.line((MARGIN_X, self.y, PAGE_W - MARGIN_X, self.y), fill=INK, width=3)
        self.y += 30

    def ensure(self, height: int) -> None:
        if self.y + height > PAGE_H - MARGIN_BOTTOM:
            self.new_page()

    def record(self, text: str) -> None:
        self.text_pages[-1].append(text)

    # ---- blocks
    def title(self, block: Title) -> None:
        f = self.fonts.title
        self.ensure(line_height(f) * 2)
        width = f.getlength(block.text)
        self.draw.text(((PAGE_W - width) / 2, self.y), block.text, font=f, fill=INK)
        self.y += line_height(f) + 20
        self.record(block.text)

    def header_block(self, block: HeaderBlock) -> None:
        f = self.fonts
        row_h = line_height(f.body) + 8
        label_w = 330
        box_h = row_h * len(block.rows) + 16
        self.ensure(box_h + 30)
        top = self.y
        self.draw.rectangle((MARGIN_X, top, PAGE_W - MARGIN_X, top + box_h), outline=INK, width=2)
        self.draw.line((MARGIN_X + label_w, top, MARGIN_X + label_w, top + box_h), fill=INK, width=2)
        y = top + 12
        for label, value in block.rows:
            self.draw.text((MARGIN_X + 16, y), label, font=f.bold, fill=INK)
            self.draw.text((MARGIN_X + label_w + 16, y), value, font=f.body, fill=INK)
            self.record(f"{label}: {value}")
            y += row_h
        self.y = top + box_h + 34

    def heading(self, block: Heading) -> None:
        f = self.fonts.heading
        self.ensure(line_height(f) + line_height(self.fonts.body) * 3)
        self.y += 6
        self.draw.text((MARGIN_X, self.y), block.text, font=f, fill=INK)
        self.y += line_height(f) + 4
        self.record("")
        self.record(block.text)

    def para(self, block: Para) -> None:
        f = self.fonts.body
        lines = wrap(block.text, f, self.text_width)
        paragraph: list[str] = []
        for line in lines:
            if self.y + line_height(f) > PAGE_H - MARGIN_BOTTOM:
                self.record(" ".join(paragraph))
                paragraph = []
                self.new_page()
            self.draw.text((MARGIN_X, self.y), line, font=f, fill=INK)
            paragraph.append(line)
            self.y += line_height(f)
        self.record(" ".join(paragraph))
        self.y += 18

    def table(self, block: Table) -> None:
        f = self.fonts
        pad = 12
        widths = [int(w * self.text_width) for w in block.widths]
        widths[-1] = self.text_width - sum(widths[:-1])

        def row_height(cells: list[str], font: ImageFont.ImageFont) -> tuple[int, list[list[str]]]:
            wrapped = [wrap(c, font, w - 2 * pad) for c, w in zip(cells, widths)]
            return max(len(w) for w in wrapped) * line_height(font) + 2 * pad, wrapped

        def draw_row(cells: list[str], font: ImageFont.ImageFont) -> None:
            height, wrapped = row_height(cells, font)
            x = MARGIN_X
            for lines, w in zip(wrapped, widths):
                self.draw.rectangle((x, self.y, x + w, self.y + height), outline=INK, width=2)
                ty = self.y + pad
                for line in lines:
                    self.draw.text((x + pad, ty), line, font=font, fill=INK)
                    ty += line_height(font)
                x += w
            self.y += height
            self.record(" | ".join(cells))

        header_h, _ = row_height(block.columns, f.bold)
        first_h, _ = row_height(block.rows[0], f.body)
        self.ensure(header_h + first_h + 10)
        draw_row(block.columns, f.bold)
        for row in block.rows:
            h, _ = row_height(row, f.body)
            if self.y + h > PAGE_H - MARGIN_BOTTOM:
                self.new_page()
                draw_row(block.columns, f.bold)   # repeat header on the new page
            draw_row(row, f.body)
        self.y += 30

    def signature(self, block: Signature, rng: random.Random) -> None:
        f = self.fonts
        height = 260
        self.ensure(height)
        self.y += 90
        self.signature_y = self.y
        col_w = self.text_width // 2
        for i, (name, role) in enumerate((block.left, block.right)):
            x = MARGIN_X + i * col_w
            draw_scribble(self.draw, x + 30, self.y - 20, rng)
            self.draw.line((x, self.y + 20, x + col_w - 80, self.y + 20), fill=INK, width=2)
            self.draw.text((x, self.y + 30), name, font=f.body, fill=INK)
            self.draw.text((x, self.y + 30 + line_height(f.body)), role, font=f.small, fill=INK)
        self.record("")
        self.record(f"Signed: {block.left[0]} ({block.left[1]})    Reviewed: {block.right[0]} ({block.right[1]})")
        self.y += height - 90

    def footers(self) -> None:
        f = self.fonts.small
        total = len(self.pages)
        for number, page in enumerate(self.pages, start=1):
            draw = ImageDraw.Draw(page)
            y = PAGE_H - MARGIN_BOTTOM + 60
            draw.line((MARGIN_X, y - 12, PAGE_W - MARGIN_X, y - 12), fill=INK, width=1)
            draw.text((MARGIN_X, y), FOOTER_NOTE, font=f, fill=INK)
            label = f"Page {number} of {total}"
            draw.text((PAGE_W - MARGIN_X - f.getlength(label), y), label, font=f, fill=INK)


def draw_scribble(draw: ImageDraw.ImageDraw, x: int, y: int, rng: random.Random) -> None:
    """A handwritten-looking signature: a wobbly looped stroke."""
    points = []
    for step in range(60):
        t = step / 59
        px = x + t * 300
        py = y + math.sin(t * math.pi * 5 + rng.uniform(0, 0.4)) * 22 * (1 - t * 0.6) + rng.uniform(-3, 3)
        points.append((px, py))
    draw.line(points, fill=40, width=4, joint="curve")


def layout(report: Report, fonts: Fonts) -> PageWriter:
    writer = PageWriter(report, fonts)
    rng = random.Random(report.seed)
    for block in report.blocks:
        if isinstance(block, Title):
            writer.title(block)
        elif isinstance(block, HeaderBlock):
            writer.header_block(block)
        elif isinstance(block, Heading):
            writer.heading(block)
        elif isinstance(block, Para):
            writer.para(block)
        elif isinstance(block, Table):
            writer.table(block)
        elif isinstance(block, Signature):
            writer.signature(block, rng)
    writer.footers()
    return writer


# ------------------------------------------------------------------ "scanner" effects
def make_stamp(lines: tuple[str, str, str], font_dir_fonts: Fonts) -> Image.Image:
    """A faint violet double-ring rubber stamp on a transparent layer."""
    size = 380
    stamp = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(stamp)
    colour = (95, 70, 150, 95)
    draw.ellipse((6, 6, size - 6, size - 6), outline=colour, width=7)
    draw.ellipse((34, 34, size - 34, size - 34), outline=colour, width=3)
    top, middle, bottom = lines
    for text, font, y in (
        (top, load_font("arialbd.ttf", 24), 100),
        (middle, load_font("arialbd.ttf", 44), 160),
        (bottom, load_font("arialbd.ttf", 30), 230),
    ):
        draw.text(((size - font.getlength(text)) / 2, y), text, font=font, fill=colour)
    return stamp


def paper_background(rng: random.Random) -> Image.Image:
    """Grey paper with a gentle top-to-bottom shading, like a flatbed scan."""
    shade = Image.linear_gradient("L").resize((PAGE_W, PAGE_H))
    base = Image.new("L", (PAGE_W, PAGE_H), PAPER_TONE)
    dark = Image.new("L", (PAGE_W, PAGE_H), PAPER_TONE - 14 - rng.randint(0, 4))
    return Image.composite(dark, base, shade.point(lambda v: v // 3))


def noise_layer(rng: random.Random, amplitude: int) -> tuple[Image.Image, Image.Image]:
    """(add, subtract) noise images in 0..amplitude, from a seeded byte stream."""
    size = PAGE_W * PAGE_H
    add = Image.frombytes("L", (PAGE_W, PAGE_H), rng.randbytes(size)).point(lambda v: v * amplitude // 255)
    sub = Image.frombytes("L", (PAGE_W, PAGE_H), rng.randbytes(size)).point(lambda v: v * amplitude // 255)
    return add, sub


def add_specks(img: Image.Image, rng: random.Random, count: int) -> None:
    draw = ImageDraw.Draw(img)
    for _ in range(count):
        x, y = rng.randint(0, PAGE_W - 1), rng.randint(0, PAGE_H - 1)
        r = rng.choice((1, 1, 1, 2, 2, 3))
        draw.ellipse((x - r, y - r, x + r, y + r), fill=(rng.randint(60, 130),) * 3)


def scan_page(
    clean: Image.Image, angle: float, stamp: Optional[Image.Image], stamp_y: int, rng: random.Random
) -> Image.Image:
    """Turns a clean white page into a grey, noisy, blurred, slightly rotated 'scan'."""
    # ink onto grey paper: multiply keeps dark text dark and turns white into the paper tone
    paper = paper_background(rng)
    page = ImageChops.multiply(paper, clean).convert("RGB")

    if stamp is not None:
        tilt = stamp.rotate(rng.uniform(-18, 18), resample=Image.BICUBIC, expand=True)
        x = PAGE_W // 2 - tilt.width // 2 + rng.randint(-40, 40)   # between the two signatures
        y = stamp_y - tilt.height // 2 + rng.randint(0, 60)
        page.paste(tilt, (x, y), tilt)

    add_specks(page, rng, 260)

    add, sub = noise_layer(rng, 26)
    page = ImageChops.subtract(ImageChops.add(page, add.convert("RGB")), sub.convert("RGB"))
    page = page.filter(ImageFilter.GaussianBlur(0.8))
    fill = (PAPER_TONE - 20,) * 3                           # darker edge where the page is rotated in
    return page.rotate(angle, resample=Image.BICUBIC, expand=False, fillcolor=fill)


def stamp_page_index(writer: PageWriter) -> int:
    return len(writer.pages) - 1                          # the signature page


def image_only_pdf(pages: list[Image.Image], path: Path) -> None:
    """One JPEG per page, page size = image size at DPI. No text layer."""
    doc = pymupdf.open()
    for img in pages:
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=72, dpi=(DPI, DPI))
        w_pt, h_pt = img.width * 72 / DPI, img.height * 72 / DPI
        page = doc.new_page(width=w_pt, height=h_pt)
        page.insert_image(page.rect, stream=buf.getvalue())
    doc.set_metadata({"title": "", "author": "", "producer": "", "creator": "",
                      "creationDate": "", "modDate": ""})
    doc.save(str(path), garbage=4, deflate=True, no_new_id=True)
    doc.close()


def source_text(writer: PageWriter) -> str:
    parts = []
    for number, lines in enumerate(writer.text_pages, start=1):
        body = "\n".join(lines).strip()
        parts.append(f"--- Page {number} ---\n{body}")
    return "\n\n".join(parts) + "\n"


# ------------------------------------------------------------------ main
def build(report: Report, fonts: Fonts) -> tuple[Path, int]:
    writer = layout(report, fonts)
    rng = random.Random(report.seed * 31)
    stamp = make_stamp(report.stamp_text, fonts)
    stamp_index = stamp_page_index(writer)
    scanned = []
    for index, clean in enumerate(writer.pages):
        angle = report.angles[index % len(report.angles)]
        stamp_y = writer.signature_y or PAGE_H // 2
        scanned.append(scan_page(clean, angle, stamp if index == stamp_index else None, stamp_y, rng))

    pdf_path = OUT_DIR / f"{report.stem}.pdf"
    image_only_pdf(scanned, pdf_path)
    (TEXT_DIR / f"{report.stem}.txt").write_text(source_text(writer), encoding="utf-8")
    return pdf_path, len(scanned)


def main() -> int:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    TEXT_DIR.mkdir(parents=True, exist_ok=True)
    fonts = make_fonts()
    for report in REPORTS:
        path, pages = build(report, fonts)
        print(f"wrote {path.relative_to(REPO_ROOT)} ({pages} pages, {path.stat().st_size // 1024} KB)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
