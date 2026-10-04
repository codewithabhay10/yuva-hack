"""Score a bill reader against ground truth, field by field.

    python -m unitwatt.extract_eval                         # offline reader on every sample bill
    python -m unitwatt.extract_eval --reader gemini         # the same with Gemini's free tier
    python -m unitwatt.extract_eval bill.pdf --truth bill.json

Each bill in ``data/sample_bills`` is scored against the JSON beside it (a photo or scan uses
the JSON of the same name without ``_photo`` or ``_scan``).
"""

from __future__ import annotations

import argparse
import mimetypes
import re
import sys
import time
from dataclasses import dataclass
from pathlib import Path

from unitwatt.extract import ExtractionError, default_reader, extract_bill, reader_label
from unitwatt.schemas import BillDocument

SAMPLES = Path(__file__).resolve().parent.parent / "data" / "sample_bills"
TEXT_FIELDS = {"consumer_number", "discom", "tariff_category"}


@dataclass(frozen=True)
class FieldScore:
    field: str
    expected: object
    got: object
    ok: bool


def _same_text(a: str | None, b: str | None) -> bool:
    """Equal once case, spaces and punctuation are ignored (OCR often drops spaces)."""
    def norm(s: str | None) -> str:
        return re.sub(r"[^a-z0-9]", "", (s or "").lower())
    return norm(a) == norm(b)


def _same_number(a: float | None, b: float | None) -> bool:
    if a is None or b is None:
        return a is None and b is None
    return abs(a - b) <= 0.006  # exact to the paisa as printed


def compare(truth: BillDocument, got: BillDocument) -> list[FieldScore]:
    """One score per scalar field, plus units and charges for each ToD zone on the truth bill."""
    scores = []
    for name in BillDocument.model_fields:
        if name == "zones":
            continue
        expected, value = getattr(truth, name), getattr(got, name)
        if name in TEXT_FIELDS:
            ok = _same_text(expected, value)
        elif isinstance(expected, (int, float)) or isinstance(value, (int, float)):
            ok = _same_number(expected, value)
        else:
            ok = expected == value
        scores.append(FieldScore(name, expected, value, ok))
    zones = {z.zone.lower(): z for z in got.zones}
    for zone in truth.zones:
        match = zones.get(zone.zone.lower())
        scores.append(FieldScore(f"zone {zone.zone} units", zone.units, match and match.units,
                                 match is not None and _same_number(zone.units, match.units)))
        scores.append(FieldScore(f"zone {zone.zone} charges", zone.charges, match and match.charges,
                                 match is not None and _same_number(zone.charges, match.charges)))
    extra = sorted(set(zones) - {z.zone.lower() for z in truth.zones})
    if extra:
        scores.append(FieldScore("extra zones", [], extra, False))
    return scores


def truth_for(path: Path) -> Path:
    stem = re.sub(r"_(photo|scan)$", "", path.stem)
    return path.with_name(stem + ".json")


def sample_files() -> list[Path]:
    return sorted(p for p in SAMPLES.iterdir() if p.suffix.lower() in {".pdf", ".png", ".jpg", ".jpeg", ".webp"}
                  and truth_for(p).exists())


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("files", nargs="*", type=Path, help="bill PDFs or photos (default: data/sample_bills)")
    parser.add_argument("--reader", default=None, help="offline, gemini, groq, openrouter or ollama (default: auto)")
    parser.add_argument("--truth", type=Path, help="ground-truth JSON, when scoring a single file")
    parser.add_argument("-v", "--verbose", action="store_true", help="show every field, not just the misses")
    args = parser.parse_args(argv)
    reader = args.reader or default_reader()
    files = args.files or sample_files()
    print(f"Reader: {reader_label(reader)}\n")
    total = correct = failures = 0
    for path in files:
        truth_path = args.truth if args.truth and len(files) == 1 else truth_for(path)
        truth = BillDocument.model_validate_json(truth_path.read_text())
        media_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        started = time.perf_counter()
        try:
            result = extract_bill(path.read_bytes(), media_type, reader)
        except ExtractionError as exc:
            print(f"{path.name}: FAILED: {exc}\n")
            failures += 1
            continue
        seconds = time.perf_counter() - started
        scores = compare(truth, result.bill)
        good = sum(s.ok for s in scores)
        total, correct = total + len(scores), correct + good
        errors = [i for i in result.issues if i.severity == "error"]
        print(f"{path.name}: {good}/{len(scores)} fields correct in {seconds:.1f} s "
              f"({result.model}); arithmetic checks {'pass' if not errors else 'flag ' + str(len(errors)) + ' error(s)'}")
        for s in scores:
            if args.verbose or not s.ok:
                print(f"   {'ok  ' if s.ok else 'MISS'} {s.field}: expected {s.expected!r}, got {s.got!r}")
        print()
    if total:
        print(f"Overall: {correct}/{total} fields correct ({100 * correct / total:.1f}%) across {len(files) - failures} bill(s)"
              + (f"; {failures} could not be read" if failures else ""))
    return 0 if failures == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
