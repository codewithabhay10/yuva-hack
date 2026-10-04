"""Free, offline bill reading (P1): the PDF's own text layer, or OCR for scans and phone photos.

No API key and no internet: the bill never leaves the computer, which suits bills an owner has
not agreed to share (P19). A layout-aware parser finds each field by its printed label, using the
synonyms Indian DISCOM bills use, and takes the value printed beside it or just below it. It is a
baseline: the arithmetic checks and the confirmation screen catch what it misreads, and a free
vision model (``extract.py``) reads layouts its labels do not cover.
"""

from __future__ import annotations

import calendar
import io
import math
import re
import statistics
from dataclasses import dataclass, field
from datetime import date
from functools import lru_cache

from unitwatt.schemas import BillDocument, Issue, ZoneReading

# Field labels as they appear once lower-cased with spaces and most punctuation removed, in the
# order they are tried. Each label must start a printed cell (after an optional item number).
FIELD_LABELS: dict[str, list[str]] = {
    "total_amount": [r"totalamountpayable", r"netamountpayable", r"totalpayable", r"netpayable",
                     r"amountpayable(?!after)", r"totalbillamount", r"netbillamount", r"billamount", r"totalamount"],
    "energy_charges": [r"energycharges?", r"energy\(ec\)", r"ec(?=\(|$)"],
    "demand_charges": [r"demandcharges?", r"(?:fixed/)?demand(?:/fixed)?charges?", r"dc(?=\(|$)"],
    "excess_demand_charges": [r"excessdemand", r"demandpenalty", r"penal(?:ty)?charges?fordemand", r"excessmd"],
    "pf_penalty": [r"powerfactorsurcharge", r"pfsurcharge", r"powerfactorpenalty", r"pfpenalty", r"lowpfsurcharge"],
    "pf_incentive": [r"powerfactorrebate", r"pfrebate", r"powerfactorincentive", r"pfincentive"],
    "fixed_charges": [r"fixed/meterrent", r"meterrent", r"fixedcharges?(?!.*demand)", r"servicecharges?"],
    "electricity_duty": [r"electricityduty", r"elecduty", r"electricitytax", r"ed(?=@|\(|$)"],
    "other_charges": [r"othercharges", r"arrears", r"miscellaneous"],
    "max_demand_kva": [r"recordedmd", r"recorded(?:maximum|max)demand", r"maximumdemand", r"maxdemand", r"actualmd",
                       r"md\(kva\)"],
    "contract_demand_kva": [r"contractdemand", r"contracteddemand", r"sanctioneddemand", r"cd\(kva\)"],
    "billing_demand_kva": [r"billingdemand", r"billeddemand", r"chargeabledemand"],
    "power_factor": [r"averagepowerfactor", r"avgpowerfactor", r"averagepf", r"avgpf",
                     r"powerfactor(?!surcharge|penalty|rebate|incentive|adjust)"],
    "kwh_total": [r"activeenergy", r"kwhconsumption", r"billedunits", r"unitsconsumed", r"totalunits",
                  r"consumption\(kwh\)", r"energyconsumption"],
    "kvah_total": [r"apparentenergy", r"kvahconsumption", r"billedkvah", r"kvah(?:units|consumption)"],
    "consumer_number": [r"consumerno", r"consumernumber", r"consumerid", r"accountno", r"accountnumber", r"canumber",
                        r"cano(?![a-z])", r"serviceno", r"scno", r"connectionno", r"kno(?![a-z])"],
    "tariff_category": [r"tariffcategory", r"category", r"tariff(?!order)"],
    "energy_basis": [r"energybilledon", r"billingbasis", r"billedon", r"energybasis"],
    "period": [r"billingperiod", r"billperiod", r"periodofbill", r"readingperiod", r"period", r"readingdates"],
    "bill_month": [r"billmonth", r"billingmonth", r"monthofbill"],
}

# Printed labels that are not fields. Like field labels, they end the cells that belong to the
# label on their left, so a value is never credited to the wrong label.
OTHER_LABELS = [
    r"connectedload", r"securitydeposit", r"loadfactor", r"meterno", r"meternumber", r"multiplyingfactor", r"mf(?![a-z])",
    r"supplyvoltage", r"duedate", r"billdate", r"billno", r"billnumber", r"gstin", r"consumergstin", r"address", r"name",
    r"meterstatus", r"rate", r"hours", r"units", r"amount", r"previous", r"present", r"difference", r"consumption",
    r"total", r"reactiveenergy", r"history", r"promptpayment", r"rebate",
    r"(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\d{2,4}",
]

ZONE_LABEL = re.compile(r"^(?:\d{1,2}(?=[a-z]))?(?:tod|zone|slot|tz)?[-\d]*(solar|offpeak|nonpeak|peak|normal|night)")
ZONE_NAMES = {"offpeak": "off-peak", "nonpeak": "normal"}
ROW_LAST = {"kwh_total", "kvah_total", "max_demand_kva"}
REQUIRED = ["period", "kwh_total", "max_demand_kva", "contract_demand_kva", "energy_charges", "demand_charges", "total_amount"]

_ITEM_NUMBER = r"^(?:\d{1,2}(?=[a-z]))?"
_PLAIN_NUMBER = re.compile(r"^\s*(?:rs\.?|₹)?\s*-?[\d,.]+\s*$", re.IGNORECASE)
_DATE = re.compile(r"(\d{1,2})[-/.](\d{1,2}|[A-Za-z]{3,9})[-/.](\d{4}|\d{2})(?!\d)")
_MONTH_YEAR = re.compile(r"([A-Za-z]{3,9})[-/ ']*(\d{4}|\d{2})(?!\d)|(\d{1,2})[-/](\d{4})(?!\d)")
_MONTHS = {m.lower(): i for i, m in enumerate(calendar.month_abbr) if m}


@dataclass
class Cell:
    """One run of printed text and its box, in page coordinates with y growing downwards."""

    text: str
    x0: float
    y0: float
    x1: float
    y1: float

    @property
    def cy(self) -> float:
        return (self.y0 + self.y1) / 2

    @property
    def h(self) -> float:
        return max(self.y1 - self.y0, 1e-6)


# --- Text sources -----------------------------------------------------------------------------


def pdf_cells(content: bytes, max_pages: int = 3) -> list[list[Cell]]:
    """Cells from each page's text layer, one list per page. Rotated text (watermarks) is skipped."""
    import pypdfium2 as pdfium
    import pypdfium2.raw as raw

    pdf = pdfium.PdfDocument(content)
    pages = []
    for page_no in range(min(len(pdf), max_pages)):
        page = pdf[page_no]
        height = page.get_height()
        textpage = page.get_textpage()
        cells: list[Cell] = []
        current: Cell | None = None
        space = False
        for i in range(textpage.count_chars()):
            ch = textpage.get_text_range(i, 1)
            angle = raw.FPDFText_GetCharAngle(textpage.raw, i)
            if not ch or ch.isspace() or 0.02 < angle < 2 * math.pi - 0.02:
                space = True
                continue
            left, bottom, right, top = textpage.get_charbox(i, loose=True)
            box = Cell(ch, left, height - top, right, height - bottom)
            same_line = current is not None and abs(box.cy - current.cy) < 0.35 * current.h
            gap = box.x0 - current.x1 if current is not None else 0.0
            if same_line and -0.5 * current.h < gap < 0.6 * current.h:
                current.text += (" " if space else "") + ch
                current.x1, current.y0, current.y1 = max(current.x1, box.x1), min(current.y0, box.y0), max(current.y1, box.y1)
            else:
                if current is not None:
                    cells.append(current)
                current = box
            space = False
        if current is not None:
            cells.append(current)
        pages.append(cells)
    return pages


def render_pdf(content: bytes, max_pages: int = 3, scale: float = 2.5) -> list[bytes]:
    """PDF pages as PNG images (about 180 dpi), for OCR or a vision model."""
    import pypdfium2 as pdfium

    pdf = pdfium.PdfDocument(content)
    images = []
    for page_no in range(min(len(pdf), max_pages)):
        buf = io.BytesIO()
        pdf[page_no].render(scale=scale).to_pil().convert("RGB").save(buf, format="PNG")
        images.append(buf.getvalue())
    return images


@lru_cache(maxsize=1)
def _ocr_engine():
    from rapidocr_onnxruntime import RapidOCR

    return RapidOCR()


def _ocr(picture) -> tuple[list, float]:
    """RapidOCR boxes for a PIL image, and the median angle of the text lines (radians)."""
    buf = io.BytesIO()
    picture.save(buf, format="PNG")
    result, _ = _ocr_engine()(buf.getvalue())
    boxes = [(quad, text) for quad, text, score in (result or []) if float(score) >= 0.5 and text.strip()]
    # Angle from the top edge of the wider boxes.
    angles = [math.atan2(q[1][1] - q[0][1], q[1][0] - q[0][0]) for q, _ in boxes
              if math.dist(q[0], q[1]) > 3 * math.dist(q[1], q[2])]
    return boxes, statistics.median(angles) if angles else 0.0


def ocr_cells(image: bytes) -> list[Cell]:
    """Cells read by RapidOCR from a photo or scan, rotated so text lines run horizontally."""
    from PIL import Image, ImageOps

    picture = ImageOps.exif_transpose(Image.open(io.BytesIO(image))).convert("RGB")
    if max(picture.size) > 2600:  # phone photos: enough for OCR, and much faster
        picture.thumbnail((2600, 2600))
    boxes, theta = _ocr(picture)
    quarter_turns = round(theta / (math.pi / 2))
    if quarter_turns % 4:  # photo taken sideways or upside down: turn it upright and read again
        boxes, theta = _ocr(picture.rotate(math.degrees(quarter_turns * math.pi / 2), expand=True))
    if not boxes:
        return []
    cos, sin = math.cos(-theta), math.sin(-theta)  # remove the remaining tilt
    cells = []
    for quad, text in boxes:
        xs = [x * cos - y * sin for x, y in quad]
        ys = [x * sin + y * cos for x, y in quad]
        cells.append(Cell(text.strip(), min(xs), min(ys), max(xs), max(ys)))
    return cells


def document_cells(content: bytes, media_type: str) -> tuple[list[list[Cell]], str]:
    """Cells for every page, and how they were read: the PDF text layer if it has one, else OCR."""
    if media_type == "application/pdf":
        pages = pdf_cells(content)
        if sum(len(c.text) for page in pages for c in page) >= 200:
            return pages, "PDF text layer"
        return [ocr_cells(image) for image in render_pdf(content)], "OCR of the scanned PDF"
    return [ocr_cells(content)], "OCR"


# --- Layout -----------------------------------------------------------------------------------


def _norm(text: str) -> tuple[str, list[int]]:
    """Lower-case ASCII letters, digits and ()%@/, with each kept character's index in ``text``."""
    kept, index = [], []
    for i, ch in enumerate(text.lower()):
        if (ch.isascii() and ch.isalnum()) or ch in "()%@/":
            kept.append(ch)
            index.append(i)
    return "".join(kept), index


def _clean(text: str) -> str:
    """Drop bracketed notes such as '(245.4 kVA x Rs 450)', including ones a line break cuts open."""
    text = text.replace("（", "(").replace("）", ")")
    previous = None
    while previous != text:
        previous, text = text, re.sub(r"\([^()]*\)", " ", text)
    if "(" in text:
        text = text[: text.index("(")]
    if ")" in text:
        text = text[text.rindex(")") + 1:]
    return text.strip()


_LABEL_RX = [re.compile(p) for labels in FIELD_LABELS.values() for p in labels] + [re.compile(p) for p in OTHER_LABELS[:-1]]
_OWNER_RX = [re.compile(_ITEM_NUMBER + rx.pattern) for rx in _LABEL_RX] + [re.compile(_ITEM_NUMBER + OTHER_LABELS[-1]), ZONE_LABEL]


def _split_merged(cell: Cell) -> list[Cell]:
    """Split an OCR box that ran a value into the next column's label: 'SYN-HT-55021BillMonth:'."""
    norm, index = _norm(cell.text)
    for pos in range(1, len(norm)):
        if norm[pos - 1].isdigit() and norm[pos].isalpha() and cell.text[index[pos]].isupper():
            if any(rx.match(norm, pos) for rx in _LABEL_RX):
                cut = index[pos]
                x = cell.x0 + (cell.x1 - cell.x0) * cut / len(cell.text)
                head = Cell(cell.text[:cut].strip(), cell.x0, cell.y0, x, cell.y1)
                return [head] + _split_merged(Cell(cell.text[cut:].strip(), x, cell.y0, cell.x1, cell.y1))
    return [cell]


@dataclass
class _Ref:
    cell: Cell
    line: int
    pos: int
    norm: str
    index: list[int]
    owner: bool = False

    @property
    def clean(self) -> str:
        return _clean(self.cell.text)


@dataclass
class _Layout:
    """A page's cells. Lines give the reading order; values are found by geometry around each label."""

    lines: list[list[_Ref]] = field(default_factory=list)
    cells: list[_Ref] = field(default_factory=list)

    @classmethod
    def build(cls, cells: list[Cell]) -> _Layout:
        cells = [part for cell in cells for part in _split_merged(cell)]
        rows: list[list[Cell]] = []
        for cell in sorted(cells, key=lambda c: c.cy):
            for row in reversed(rows[-3:]):
                centre = statistics.median(c.cy for c in row)
                height = min(cell.h, statistics.median(c.h for c in row))
                # One line never holds two cells that overlap side to side (rows of the same column).
                overlaps = any(min(cell.x1, c.x1) - max(cell.x0, c.x0) > 0.3 * cell.h for c in row)
                if abs(cell.cy - centre) < 0.5 * height and not overlaps:
                    row.append(cell)
                    break
            else:
                rows.append([cell])
        rows.sort(key=lambda row: statistics.median(c.cy for c in row))
        layout = cls()
        for line_no, row in enumerate(rows):
            refs = []
            for pos, cell in enumerate(sorted(row, key=lambda c: c.x0)):
                norm, index = _norm(cell.text)
                refs.append(_Ref(cell, line_no, pos, norm, index, owner=any(rx.match(norm) for rx in _OWNER_RX)))
            layout.lines.append(refs)
        layout.cells = [ref for line in layout.lines for ref in line]
        return layout

    def refs(self):
        return iter(self.cells)

    @staticmethod
    def level(a: Cell, b: Cell) -> bool:
        """Whether two cells sit on the same printed line."""
        return abs(a.cy - b.cy) < 0.55 * min(a.h, b.h)

    @staticmethod
    def rest(ref: _Ref, end: int, raw: bool = False) -> str:
        """What follows the label inside its own cell."""
        rest = ref.cell.text[ref.index[end - 1] + 1:] if end else ref.cell.text
        return rest.strip(" :/-–=|") if raw else _clean(rest)

    def following(self, ref: _Ref) -> list[_Ref]:
        """Cells level with ``ref`` and to its right, up to the next label."""
        row = sorted((r for r in self.cells if r is not ref and self.level(r.cell, ref.cell)
                      and r.cell.x0 >= ref.cell.x1 - 0.5 * ref.cell.h), key=lambda r: r.cell.x0)
        cells = []
        for other in row:
            if other.owner:
                break
            cells.append(other)
        return cells

    def labelled(self, ref: _Ref, value: _Ref) -> bool:
        """Whether the line ``ref`` is on has a label in the column left of ``value``."""
        return any(r.owner and r.cell.x1 <= value.cell.x0 and self.level(r.cell, ref.cell) for r in self.cells)

    def wrapped(self, value: _Ref) -> str:
        """A value cell's text with the lines a narrow table cell wrapped onto, above and below it."""
        parts = [(value.cell.cy, value.cell.text)]
        h = value.cell.h
        for step in (-1, 1):
            previous = value
            for _ in range(2):
                nearby = [r for r in self.cells if not r.owner and abs(r.cell.x0 - value.cell.x0) < 0.6 * h
                          and 0.5 * h < step * (r.cell.cy - previous.cell.cy) < 1.6 * h]
                if not nearby:
                    break
                match = min(nearby, key=lambda r: abs(r.cell.cy - previous.cell.cy))
                if self.labelled(match, value) or (_PLAIN_NUMBER.match(match.cell.text) and _PLAIN_NUMBER.match(value.cell.text)):
                    break  # another label's line, or stacked amounts (rows of a column, not one wrapped value)
                # A wrapped line nearer the next label out belongs to that label's value.
                beyond = [r.cell.cy for r in self.cells if r.owner and r.cell.x1 <= value.cell.x0
                          and step * (r.cell.cy - match.cell.cy) > 0.5 * h]
                if beyond and min(abs(cy - match.cell.cy) for cy in beyond) < abs(match.cell.cy - value.cell.cy):
                    break
                parts.append((match.cell.cy, match.cell.text))
                previous = match
        return " ".join(text for _, text in sorted(parts))

    def texts_for(self, ref: _Ref, end: int, raw: bool = False) -> tuple[list[str], list[str]]:
        """Texts belonging to the label ``ref`` (matched up to ``end`` of its normalised text).

        First: the rest of the label's own cell and the cells to its right up to the next label.
        Then: cells on the next line or two, at or right of the label, with no label before them.
        """
        after = self.following(ref)
        texts = [self.wrapped(after[0])] + [r.cell.text for r in after[1:]] if after else []
        same = [self.rest(ref, end, raw)] + [t if raw else _clean(t) for t in texts]
        h = ref.cell.h
        reach_x = ref.cell.x0 - 2 * h
        below = []
        for other in sorted((r for r in self.cells if 0 < r.cell.cy - ref.cell.cy <= 2.6 * h and r.cell.x1 >= reach_x
                             and not self.level(r.cell, ref.cell)), key=lambda r: (r.line, r.cell.x0)):
            if other.owner or any(o.owner and reach_x <= o.cell.x0 < other.cell.x0 and self.level(o.cell, other.cell)
                                  for o in self.cells):
                continue
            below.append(other.cell.text if raw else other.clean)
        return same[:1] + [t for t in same[1:] if t], [t for t in below if t]  # same[0]: the label cell's own rest


# --- Values -----------------------------------------------------------------------------------


_UNIT_AFTER = re.compile(r"\s*(?:kva|kw|kwh|kvah|kvarh|units?|hp|mva|mw)\b", re.IGNORECASE)
_RATE_BEFORE = re.compile(r"@\s*(?:rs\.?|₹|inr)?\s*$", re.IGNORECASE)


def numbers(text: str, money: bool = False) -> list[float]:
    """Amounts and readings in printed text, skipping percentages, dates, times and identifiers.

    With ``money``, also skip quantities and rates: "187.5 kVA", "@ Rs 400".
    """
    found = []
    for m in re.finditer(r"\d[\d,.]*", text):
        token = m.group().rstrip(".,")
        start, end = m.start(), m.start() + len(token)
        before = text[start - 1] if start else " "
        before2 = text[start - 2] if start > 1 else " "
        after = text[end: end + 2]
        if before.isalpha() and before not in "₹":
            continue  # part of an identifier such as HTM7718203
        if before in "-/:–" and before2.isalnum():
            continue  # second part of a date, time or code
        if after[:1] in "-/:–" and after[1:2].isdigit():
            continue  # first part of a date, time or range
        if text[end:].lstrip().startswith("%"):
            continue
        if money and (_UNIT_AFTER.match(text, end) or _RATE_BEFORE.search(text, 0, start)):
            continue
        if token.count(".") > 1:  # OCR sometimes reads the lakh comma as a point
            head, _, tail = token.rpartition(".")
            token = head.replace(".", "") + "." + tail
        value = float(token.replace(",", ""))
        if before in "-−" and not before2.isalnum():
            value = -value
        found.append(value)
    return found


def _dates(text: str) -> list[date]:
    out = []
    for d, m, y in _DATE.findall(text):
        month = int(m) if m.isdigit() else _MONTHS.get(m[:3].lower(), 0)
        year = int(y) + (2000 if len(y) == 2 else 0)
        try:
            out.append(date(year, month, int(d)))
        except ValueError:
            continue
    return out


def _month_range(text: str) -> tuple[date, date] | None:
    for name, year, month_no, year2 in _MONTH_YEAR.findall(text):
        month = _MONTHS.get(name[:3].lower(), 0) if name else int(month_no)
        y = int(year or year2)
        y += 2000 if y < 100 else 0
        if 1 <= month <= 12 and 2000 <= y <= 2100:
            return date(y, month, 1), date(y, month, calendar.monthrange(y, month)[1])
    return None


class _Parser:
    def __init__(self, pages: list[list[Cell]]):
        self.layouts = [_Layout.build(cells) for cells in pages if cells]

    def matches(self, key: str):
        for pattern in FIELD_LABELS[key]:
            rx = re.compile(_ITEM_NUMBER + pattern)
            for layout in self.layouts:
                for ref in layout.refs():
                    m = rx.match(ref.norm)
                    if m:
                        yield layout, ref, m.end()

    def number(self, key: str, low: float = -math.inf, high: float = math.inf, money: bool = False) -> float | None:
        for layout, ref, end in self.matches(key):
            same, below = layout.texts_for(ref, end)
            if key in ROW_LAST:  # meter tables: the billed consumption is the last column
                values = [n for t in same for n in numbers(t) if low <= n <= high]
                if values:
                    return values[-1]
                same = []
            elif money:  # an amount column beats figures inside the label ("on 187.5 kVA @ Rs 400")
                same = same[1:] + same[:1]
            for text in same + below:
                values = [n for n in numbers(text, money) if low <= n <= high]
                if values:
                    return values[0]
        return None

    def text(self, key: str) -> str | None:
        for layout, ref, end in self.matches(key):
            rest, cells = layout.rest(ref, end, raw=True), layout.following(ref)
            if re.search(r"[A-Za-z0-9]", rest):
                return " ".join([rest] + [c.cell.text for c in cells]).strip()
            if cells:
                return " ".join([layout.wrapped(cells[0])] + [c.cell.text for c in cells[1:]]).strip()
            _, below = layout.texts_for(ref, end, raw=True)
            for text in below:
                if re.search(r"[A-Za-z0-9]", text):
                    return text.strip()
        return None

    def identifier(self, key: str) -> str | None:
        for layout, ref, end in self.matches(key):
            same, below = layout.texts_for(ref, end, raw=True)
            for text in same + below:
                for token in re.findall(r"[A-Za-z0-9][A-Za-z0-9/-]{3,}", text):
                    if any(ch.isdigit() for ch in token):
                        return token
        return None

    def period(self) -> tuple[date, date] | None:
        for layout, ref, end in self.matches("period"):
            same, below = layout.texts_for(ref, end, raw=True)
            found = [d for text in same + below for d in _dates(text)]
            if len(found) >= 2:
                start, finish = found[0], found[1]
                if ref.norm.startswith("readingdates"):  # previous reading date is the day before the period
                    start = date.fromordinal(start.toordinal() + 1)
                return (start, finish) if start <= finish else (finish, start)
        for layout, ref, end in self.matches("bill_month"):
            same, below = layout.texts_for(ref, end, raw=True)
            for text in same + below:
                if (found := _month_range(text)) is not None:
                    return found
        return None

    def energy_basis(self) -> str:
        texts = []
        for layout, ref, end in self.matches("energy_basis"):
            same, below = layout.texts_for(ref, end, raw=True)
            texts += same + below
            break
        for text in texts:
            norm = _norm(text)[0]
            if "kvah" in norm:
                return "kVAh"
            if "kwh" in norm:
                return "kWh"
        zone_header = next((r.norm for lay in self.layouts for r in lay.refs() if r.norm.startswith("units(kvah")), None)
        return "kVAh" if zone_header else "kWh"

    def discom(self) -> str | None:
        for layout in self.layouts[:1]:
            for line in layout.lines[:12]:
                for ref in line:
                    if re.search(r"limited|ltd|nigam|corporation|company|vidyut|board", ref.norm) and "specimen" not in ref.norm:
                        # OCR often drops the spaces between words: "PatliputraPower Distribution".
                        return re.sub(r"(?<=[a-z])(?=[A-Z])", " ", ref.cell.text.strip())
        return None

    def zones(self) -> tuple[list[ZoneReading], float | None, float | None]:
        """ToD zone rows, plus the zone table's Total row (units, amount) if it has one."""
        best: dict[str, list[float]] = {}
        order: list[str] = []
        total: tuple[float | None, float | None] = (None, None)
        for layout in self.layouts:
            for line in layout.lines:
                for ref in line:
                    m = ZONE_LABEL.match(ref.norm)
                    if not m or "demand" in ref.norm:
                        continue
                    same, _ = layout.texts_for(ref, m.end())
                    values = [n for t in same for n in numbers(t)]
                    if not values:
                        continue
                    name = ZONE_NAMES.get(m.group(1), m.group(1))
                    if len(values) > len(best.get(name, [])):
                        best[name] = values
                        if name not in order:
                            order.append(name)
                    # The Total row sits just below the last zone row.
                    for row in layout.cells:
                        if row.norm == "total" and 0.55 * ref.cell.h < row.cell.cy - ref.cell.cy < 1.8 * ref.cell.h \
                                and abs(row.cell.x0 - ref.cell.x0) < 2 * ref.cell.h:
                            tot = [n for t in layout.texts_for(row, len(row.norm))[0] for n in numbers(t)]
                            if tot:
                                total = (tot[0], tot[-1] if len(tot) > 1 else None)
        zones = []
        for name in order:
            v = best[name]
            charges = v[-1] if len(v) >= 3 else (v[1] if len(v) == 2 and v[1] > v[0] else None)
            zones.append(ZoneReading(zone=name, units=v[0], charges=charges))
        return zones, total[0], total[1]


def parse_bill(pages: list[list[Cell]]) -> tuple[BillDocument, list[Issue]]:
    """Cells -> ``BillDocument``, with an error issue for each required field that was not found."""
    from unitwatt.extract import ExtractionError

    p = _Parser(pages)
    zones, zone_units, zone_amount = p.zones()
    found: dict[str, object] = {
        "consumer_number": p.identifier("consumer_number"),
        "discom": p.discom(),
        "tariff_category": p.text("tariff_category"),
        "energy_basis": p.energy_basis(),
        "kwh_total": p.number("kwh_total", low=1),
        "kvah_total": p.number("kvah_total", low=1),
        "max_demand_kva": p.number("max_demand_kva", low=0.1, high=1e5),
        "contract_demand_kva": p.number("contract_demand_kva", low=0.1, high=1e5),
        "billing_demand_kva": p.number("billing_demand_kva", low=0.1, high=1e5),
        "power_factor": p.number("power_factor", low=0.3, high=1.0),
        "energy_charges": p.number("energy_charges", money=True),
        "demand_charges": p.number("demand_charges", money=True),
        "excess_demand_charges": p.number("excess_demand_charges", money=True),
        "fixed_charges": p.number("fixed_charges", money=True),
        "electricity_duty": p.number("electricity_duty", money=True),
        "other_charges": p.number("other_charges", money=True),
        "total_amount": p.number("total_amount", low=1, money=True),
        "zones": zones,
    }
    penalty, incentive = p.number("pf_penalty", money=True), p.number("pf_incentive", money=True)
    found["pf_adjustment"] = (abs(penalty) if penalty else 0.0) - (abs(incentive) if incentive else 0.0)
    if found["kwh_total"] is None and found["energy_basis"] == "kWh":
        found["kwh_total"] = zone_units or (sum(z.units for z in zones) if zones else None)
    if found["energy_charges"] is None:
        found["energy_charges"] = zone_amount
    period = p.period()
    if period is not None:
        found["period_start"], found["period_end"] = period

    missing = [key for key in REQUIRED if (found.get(key) if key != "period" else period) is None]
    if len(missing) > len(REQUIRED) // 2:
        raise ExtractionError(
            "Could not find enough bill fields on this page (missing: " + ", ".join(missing) + "). "
            "Try a sharper photo, or a free vision model."
        )
    issues = [Issue("error", key if key != "period" else "period_start", "Not found on the bill: enter it from the paper bill.")
              for key in missing]
    if period is None:
        today = date.today()
        last = date.fromordinal(today.replace(day=1).toordinal() - 1)
        found["period_start"], found["period_end"] = last.replace(day=1), last
    for key in ("kwh_total", "max_demand_kva", "contract_demand_kva", "energy_charges", "demand_charges", "total_amount"):
        if found[key] is None:
            found[key] = 0.0
    for key in ("excess_demand_charges", "fixed_charges", "electricity_duty", "other_charges"):
        if found[key] is None:
            found[key] = 0.0
    return BillDocument.model_validate(found), issues


def read_bill(content: bytes, media_type: str):
    """A bill PDF or photo -> ``BillExtraction``, entirely on this computer."""
    from unitwatt.extract import BillExtraction, ExtractionError
    from unitwatt.schemas import validate_bill

    try:
        pages, how = document_cells(content, media_type)
    except ImportError as exc:
        raise ExtractionError(f"Offline reading needs the {exc.name} package: pip install -r requirements.txt") from exc
    bill, issues = parse_bill(pages)
    return BillExtraction(bill, issues + validate_bill(bill), f"offline ({how})")
