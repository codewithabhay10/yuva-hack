"""Stage 1: getting data in (P1 to P5).

Adapters turn each messy source into one standard shape:

* bills -> ``BillDocument`` plus validation issues (P1)
* production registers and 30-second daily chat entries -> tonnes per product per day (P2)
* meter load-survey exports in several CSV formats -> one regular 15-minute series,
  with gaps flagged rather than silently filled (P3)
* fuel purchase invoices and stock readings -> fuel actually consumed (P4)
* every fuel -> MJ and kWh-equivalent with the factor and its source kept visible (P5)
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import IO

import numpy as np
import pandas as pd

from unitwatt.profiles import Factors, Factory
from unitwatt.schemas import BillDocument, Issue, validate_bill
from unitwatt.tariff import SLOT_MINUTES

# --------------------------------------------------------------------------------------
# P1: bills
# --------------------------------------------------------------------------------------


@dataclass
class LoadedBill:
    bill: BillDocument
    issues: list[Issue]
    source: str


def load_bill_json(source: Path | str) -> LoadedBill:
    """A bill from a JSON file (``Path``) or JSON text (``str``)."""
    if isinstance(source, Path):
        text, label = source.read_text(encoding="utf-8"), source.name
    else:
        text, label = source, "uploaded JSON"
    bill = BillDocument.model_validate(json.loads(text))
    return LoadedBill(bill, validate_bill(bill), label)


# --------------------------------------------------------------------------------------
# P3: meter load surveys
# --------------------------------------------------------------------------------------


@dataclass
class SurveyQuality:
    source_format: str
    n_expected: int
    n_present: int
    n_duplicates: int
    n_conflicting_duplicates: int
    n_negative: int
    gaps: list[tuple[pd.Timestamp, pd.Timestamp, int]] = field(default_factory=list)

    @property
    def coverage(self) -> float:
        return self.n_present / self.n_expected if self.n_expected else 0.0


@dataclass
class LoadSurvey:
    data: pd.DataFrame  # regular 15-minute index (slot start); columns kwh, kvah; NaN where missing
    quality: SurveyQuality


def _norm(col: str) -> str:
    return re.sub(r"[^a-z0-9]", "", col.lower())


def _detect_format(columns: list[str]) -> str:
    cols = {_norm(c) for c in columns}
    if {"date", "time", "kwh", "kvah"} <= cols:
        return "discom_portal"
    if {"timestamp", "kwhimp", "kvahimp"} <= cols:
        return "meter_download"
    if "kwh" in cols and any(c in cols for c in ("timestamp", "datetime")):
        return "generic"
    raise ValueError(f"Unrecognised load-survey format with columns {columns}")


def read_load_survey(source: str | Path | IO, interval_minutes: int = SLOT_MINUTES) -> LoadSurvey:
    """Read any supported meter export into a standard 15-minute series.

    Supported formats (one adapter each, all producing the same output):

    * ``discom_portal``: Date (dd-mm-yyyy), Time (HH:MM, interval start), kWh, kVAh
    * ``meter_download``: Timestamp (ISO, interval end), KWH_IMP, KVAH_IMP
    * ``generic``: Timestamp or DateTime (interval start), kWh, optional kVAh
    """
    raw = pd.read_csv(source)
    fmt = _detect_format(list(raw.columns))
    cols = {_norm(c): c for c in raw.columns}
    if fmt == "discom_portal":
        ts = pd.to_datetime(raw[cols["date"]] + " " + raw[cols["time"]], format="%d-%m-%Y %H:%M")
        df = pd.DataFrame({"kwh": raw[cols["kwh"]].astype(float), "kvah": raw[cols["kvah"]].astype(float)})
    elif fmt == "meter_download":
        ts = pd.to_datetime(raw[cols["timestamp"]]) - pd.Timedelta(minutes=interval_minutes)
        df = pd.DataFrame({"kwh": raw[cols["kwhimp"]].astype(float), "kvah": raw[cols["kvahimp"]].astype(float)})
    else:
        key = "timestamp" if "timestamp" in cols else "datetime"
        ts = pd.to_datetime(raw[cols[key]])
        kvah = raw[cols["kvah"]].astype(float) if "kvah" in cols else raw[cols["kwh"]].astype(float)
        df = pd.DataFrame({"kwh": raw[cols["kwh"]].astype(float), "kvah": kvah})
    df.index = pd.DatetimeIndex(ts, name="timestamp")
    return standardise_survey(df, fmt, interval_minutes)


def standardise_survey(df: pd.DataFrame, source_format: str = "dataframe", interval_minutes: int = SLOT_MINUTES) -> LoadSurvey:
    df = df.sort_index(kind="stable")
    dup_mask = df.index.duplicated(keep="first")
    n_dup = int(dup_mask.sum())
    conflicting = 0
    if n_dup:
        dups = df[df.index.duplicated(keep=False)]
        conflicting = int((dups.groupby(level=0).nunique() > 1).any(axis=1).sum())
    df = df[~dup_mask]
    negative = int((df[["kwh", "kvah"]] < 0).any(axis=1).sum())
    df = df.mask(df < 0)

    start = df.index.min().normalize()
    end = df.index.max().normalize() + pd.Timedelta(days=1) - pd.Timedelta(minutes=interval_minutes)
    full = pd.date_range(start, end, freq=f"{interval_minutes}min", name="timestamp")
    data = df.reindex(full)

    missing = data["kwh"].isna().to_numpy()
    gaps = []
    if missing.any():
        edges = np.diff(np.concatenate([[0], missing.astype(int), [0]]))
        for s, e in zip(np.where(edges == 1)[0], np.where(edges == -1)[0]):
            gaps.append((full[s], full[e - 1], int(e - s)))
    quality = SurveyQuality(
        source_format=source_format,
        n_expected=len(full),
        n_present=int((~missing).sum()),
        n_duplicates=n_dup,
        n_conflicting_duplicates=conflicting,
        n_negative=negative,
        gaps=gaps,
    )
    return LoadSurvey(data, quality)


# --------------------------------------------------------------------------------------
# P2: production registers and daily chat entries
# --------------------------------------------------------------------------------------


def read_production(source: str | Path | IO | pd.DataFrame, factory: Factory) -> pd.DataFrame:
    """Daily tonnes per product. Long (date, product, tonnes) or wide (date, <product>...) input.

    Days with no register entry are absent (later treated as missing, not as zero).
    """
    raw = source if isinstance(source, pd.DataFrame) else pd.read_csv(source)
    raw = raw.copy()
    raw["date"] = pd.to_datetime(raw["date"]).dt.date
    if "product" in raw.columns:
        wide = raw.pivot_table(index="date", columns="product", values="tonnes", aggfunc="sum")
    else:
        wide = raw.set_index("date")
    wide = wide.reindex(columns=factory.product_ids).fillna(0.0)
    wide.index.name = "date"
    wide.columns.name = None
    return wide.sort_index()


_DEVANAGARI_DIGITS = str.maketrans("०१२३४५६७८९", "0123456789")
_UNIT_TO_TONNES = {
    "t": 1.0, "ton": 1.0, "tons": 1.0, "tonne": 1.0, "tonnes": 1.0, "mt": 1.0, "टन": 1.0,
    "kg": 0.001, "kgs": 0.001, "किलो": 0.001,
    "quintal": 0.1, "qtl": 0.1, "क्विंटल": 0.1,
}
_QTY = re.compile(r"(\d+(?:\.\d+)?)\s*(tonnes|tonne|tons|ton|mt|kgs|kg|quintal|qtl|t|टन|किलो|क्विंटल)?(?![a-z])", re.I)
_SHIFTS = re.compile(r"(\d+)\s*(?:shifts?|शिफ्ट)", re.I)


@dataclass
class ParsedEntry:
    tonnes: dict[str, float]
    shifts: int | None
    warnings: list[str]


def parse_daily_entry(text: str, factory: Factory) -> ParsedEntry:
    """Parse a 30-second WhatsApp or voice-transcribed entry such as
    'flange 1.5t, crank 1.2 t, gear 1400 kg, 2 shifts' or 'फ्लेंज १.५ टन, गियर 1.2 टन'.
    """
    text = text.translate(_DEVANAGARI_DIGITS)
    warnings: list[str] = []
    shifts = None
    m = _SHIFTS.search(text)
    if m:
        shifts = int(m.group(1))
        text = text[: m.start()] + " " + text[m.end():]

    alias_to_product = {a: p.id for p in factory.products for a in p.aliases}
    tonnes: dict[str, float] = {}
    for segment in re.split(r"[,;\n]|\band\b|\bऔर\b", text):
        seg = segment.strip().lower()
        if not seg:
            continue
        found = [(seg.find(a), a) for a in alias_to_product if re.search(rf"(?<!\w){re.escape(a)}(?!\w)", seg)]
        quantities = list(_QTY.finditer(seg))
        if not quantities:
            if found:
                warnings.append(f"No quantity found for '{segment.strip()}'.")
            continue
        if not found:
            warnings.append(f"Could not tell which product '{segment.strip()}' is.")
            continue
        alias = max(found, key=lambda x: len(x[1]))[1]
        q = quantities[0]
        unit = (q.group(2) or "t").lower()
        value = float(q.group(1)) * _UNIT_TO_TONNES.get(unit, 1.0)
        product = alias_to_product[alias]
        tonnes[product] = round(tonnes.get(product, 0.0) + value, 3)
    if not tonnes:
        warnings.append("Nothing recorded. Example: 'flange 1.5t, crank 1.2t, gear 1.4t, 2 shifts'.")
    return ParsedEntry(tonnes, shifts, warnings)


# --------------------------------------------------------------------------------------
# P4: fuel stock-flow accounting
# --------------------------------------------------------------------------------------


def fuel_consumption(purchases: pd.DataFrame, stock: pd.DataFrame, dg_log: pd.DataFrame | None = None) -> pd.DataFrame:
    """Monthly consumption = opening stock + purchases - closing stock, per fuel.

    ``stock`` holds dip readings (date, fuel, closing_stock); consecutive readings bound a
    period. Diesel is reconciled against the generator log when one is supplied.
    """
    stock = stock.copy()
    stock["date"] = pd.to_datetime(stock["date"]).dt.date
    purchases = purchases.copy()
    if not purchases.empty:
        purchases["date"] = pd.to_datetime(purchases["date"]).dt.date
    rows = []
    for fuel, readings in stock.sort_values("date").groupby("fuel"):
        readings = readings.reset_index(drop=True)
        for i in range(1, len(readings)):
            start, end = readings.loc[i - 1, "date"], readings.loc[i, "date"]
            bought = 0.0
            if not purchases.empty:
                sel = purchases[(purchases["fuel"] == fuel) & (purchases["date"] > start) & (purchases["date"] <= end)]
                bought = float(sel["quantity"].sum())
            opening = float(readings.loc[i - 1, "closing_stock"])
            closing = float(readings.loc[i, "closing_stock"])
            consumed = opening + bought - closing
            row = {"fuel": fuel, "period_start": start, "period_end": end, "opening": opening,
                   "purchases": bought, "closing": closing, "consumption": consumed, "flag": ""}
            if consumed < 0:
                row["flag"] = "Negative consumption: a reading or an invoice is wrong."
            if fuel == "diesel" and dg_log is not None and not dg_log.empty:
                log = dg_log.copy()
                log["date"] = pd.to_datetime(log["date"]).dt.date
                logged = float(log[(log["date"] > start) & (log["date"] <= end)]["litres"].sum())
                row["dg_log_litres"] = logged
                if consumed > 0 and abs(logged - consumed) > max(10.0, 0.10 * consumed):
                    row["flag"] = f"Generator log shows {logged:,.0f} L against {consumed:,.0f} L from stock."
            rows.append(row)
    return pd.DataFrame(rows)


# --------------------------------------------------------------------------------------
# P5: unit conversion
# --------------------------------------------------------------------------------------


def fuel_energy(quantity: float, fuel: str, factors: Factors) -> dict[str, float | str]:
    f = factors.fuels[fuel]
    mj = quantity * f.mj_per_unit
    return {
        "fuel": fuel,
        "quantity": quantity,
        "unit": f.unit,
        "mj": mj,
        "kwh_eq": mj / 3.6,
        "kg_co2": 0.0 if f.biogenic else quantity * f.kg_co2_per_unit,
        "factor_source": f.source,
    }


def factor_table(factors: Factors) -> pd.DataFrame:
    rows = [
        {"Energy carrier": "Grid electricity", "Unit": "kWh", "MJ per unit": 3.6,
         "kg CO2 per unit": factors.grid_kg_per_kwh, "Source": factors.grid_source}
    ]
    for f in factors.fuels.values():
        rows.append({"Energy carrier": f.label, "Unit": f.unit, "MJ per unit": round(f.mj_per_unit, 2),
                     "kg CO2 per unit": 0.0 if f.biogenic else round(f.kg_co2_per_unit, 3), "Source": f.source})
    return pd.DataFrame(rows)
