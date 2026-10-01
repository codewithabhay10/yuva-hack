"""Stage 2: one trustworthy daily ledger (P6, P7).

Bills are monthly, production is daily and meter data is every 15 minutes. Everything is
resampled to one row per day. Where the meter export has gaps, the missing energy is taken
from the bill (bill total minus what the meter export shows) and spread across the gap days
using production as the weight; those rows are labelled as estimated. Fuel bought monthly
is spread the same way. Every figure keeps a flag saying how it was obtained.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from unitwatt.ingest import LoadSurvey
from unitwatt.profiles import Factors, Factory
from unitwatt.schemas import BillDocument, Issue
from unitwatt.tariff import SLOT_HOURS, SLOTS_PER_DAY, Tariff


def build_ledger(
    survey: LoadSurvey | None,
    production: pd.DataFrame,
    bills: list[BillDocument],
    factory: Factory,
    tariff: Tariff,
    factors: Factors,
    dg_log: pd.DataFrame | None = None,
    fuel_periods: pd.DataFrame | None = None,
) -> pd.DataFrame:
    """One row per day: energy by carrier and ToD band, production by product, and flags."""
    products = factory.product_ids
    bands = tariff.band_names
    starts = [b.period_start for b in bills] + ([survey.data.index.min().date()] if survey else [])
    ends = [b.period_end for b in bills] + ([survey.data.index.max().date()] if survey else [])
    days = pd.Index([d.date() for d in pd.date_range(min(starts), max(ends), freq="D")], name="date")
    ledger = pd.DataFrame(index=days)

    # Meter data, aggregated by day and ToD band.
    if survey is not None:
        data = survey.data
        day_of = pd.Index(data.index.date, name="date")
        band = tariff.band_series(data.index).to_numpy()
        ledger["meter_slots"] = data["kwh"].notna().groupby(day_of).sum().reindex(days).fillna(0)
        ledger["grid_kwh_metered"] = data["kwh"].groupby(day_of).sum(min_count=1).reindex(days)
        ledger["grid_kvah_metered"] = data["kvah"].groupby(day_of).sum(min_count=1).reindex(days)
        ledger["max_kva"] = (data["kvah"] / SLOT_HOURS).groupby(day_of).max().reindex(days)
        zones = data["kwh"].groupby([day_of, band]).sum().unstack()
        for b in bands:
            ledger[f"kwh_{b}"] = zones[b].reindex(days) if b in zones else 0.0
    else:
        ledger["meter_slots"] = 0
        ledger["grid_kwh_metered"] = np.nan
        ledger["grid_kvah_metered"] = np.nan
        ledger["max_kva"] = np.nan
        for b in bands:
            ledger[f"kwh_{b}"] = np.nan
    ledger["meter_coverage"] = ledger["meter_slots"] / SLOTS_PER_DAY

    # Production. Days without a register entry stay NaN (missing), not zero.
    prod = production.reindex(days)
    ledger["production_recorded"] = prod.notna().all(axis=1) if len(products) else False
    for p in products:
        ledger[f"t_{p}"] = prod[p]
    ledger["total_t"] = prod[products].sum(axis=1, min_count=len(products))
    ledger["is_production_day"] = ledger["total_t"] > 0
    ledger.loc[~ledger["production_recorded"], "is_production_day"] = (
        pd.Series([d.weekday() in factory.working_weekdays for d in days], index=days)[~ledger["production_recorded"]]
    )

    _fill_from_bills(ledger, bills, tariff)

    # Generator log.
    ledger["dg_kwh"] = 0.0
    ledger["diesel_l"] = 0.0
    if dg_log is not None and not dg_log.empty:
        log = dg_log.copy()
        log["date"] = pd.to_datetime(log["date"]).dt.date
        by_day = log.groupby("date")[["kwh", "litres"]].sum()
        ledger["dg_kwh"] = by_day["kwh"].reindex(days).fillna(0.0)
        ledger["diesel_l"] = by_day["litres"].reindex(days).fillna(0.0)

    # Fuels bought monthly (furnace oil, coal, gas...), spread by production.
    fuel_cols = []
    if fuel_periods is not None and not fuel_periods.empty:
        heat_treated = [f"t_{p.id}" for p in factory.products if p.heat_treated] or [f"t_{p}" for p in products]
        weight = ledger[heat_treated].sum(axis=1).fillna(0.0)
        for fuel, periods in fuel_periods[fuel_periods["fuel"] != "diesel"].groupby("fuel"):
            col = f"{fuel}_units"
            fuel_cols.append(fuel)
            ledger[col] = 0.0
            for _, row in periods.iterrows():
                mask = (ledger.index > row["period_start"]) & (ledger.index <= row["period_end"])
                w = weight[mask]
                if w.sum() > 0 and row["consumption"] > 0:
                    ledger.loc[mask, col] = row["consumption"] * w / w.sum()
    ledger.attrs["fuels"] = fuel_cols

    ledger["elec_kwh"] = ledger["grid_kwh"] + ledger["dg_kwh"]
    mj = ledger["grid_kwh"] * 3.6 + ledger["diesel_l"] * factors.fuels["diesel"].mj_per_unit
    for fuel in fuel_cols:
        mj = mj + ledger[f"{fuel}_units"] * factors.fuels[fuel].mj_per_unit
    ledger["final_energy_mj"] = mj
    ledger["sec_kwh_per_t"] = np.where(ledger["total_t"] > 0, ledger["elec_kwh"] / ledger["total_t"], np.nan)
    _flag_outliers(ledger)
    ledger.attrs["bands"] = bands
    ledger.attrs["products"] = products
    return ledger


def _expected_day_kwh(ledger: pd.DataFrame) -> pd.Series:
    """A rough expectation of each day's grid energy, used only to weight gap filling."""
    complete = ledger[(ledger["meter_coverage"] >= 1.0) & ledger["production_recorded"]]
    if len(complete) >= 10:
        slope, intercept = np.polyfit(complete["total_t"], complete["grid_kwh_metered"], 1)
        slope, intercept = max(slope, 0.0), max(intercept, 1.0)
    else:
        slope, intercept = 400.0, 300.0
    fallback_t = complete["total_t"].median() if len(complete) else 1.0
    tonnes = ledger["total_t"].where(ledger["production_recorded"], np.where(ledger["is_production_day"], fallback_t, 0.0))
    return intercept + slope * tonnes.astype(float)


def _fill_from_bills(ledger: pd.DataFrame, bills: list[BillDocument], tariff: Tariff) -> None:
    bands = tariff.band_names
    ledger["grid_kwh"] = ledger["grid_kwh_metered"].fillna(0.0)
    ledger["grid_kvah"] = ledger["grid_kvah_metered"].fillna(0.0)
    ledger["grid_kwh_estimated"] = 0.0
    ledger["bill_period"] = ""
    for b in bands:
        ledger[f"kwh_{b}"] = ledger[f"kwh_{b}"].fillna(0.0)
    expected = _expected_day_kwh(ledger)
    for bill in bills:
        mask = (ledger.index >= bill.period_start) & (ledger.index <= bill.period_end)
        ledger.loc[mask, "bill_period"] = f"{bill.period_start:%b %Y}"
        gap_share = (1 - ledger.loc[mask, "meter_coverage"]).clip(lower=0)
        weights = gap_share * expected[mask]
        if weights.sum() <= 0:
            continue
        residual = bill.kwh_total - ledger.loc[mask, "grid_kwh"].sum()
        if residual <= 0:
            continue
        w = weights / weights.sum()
        ledger.loc[mask, "grid_kwh_estimated"] = residual * w
        ledger.loc[mask, "grid_kwh"] += residual * w
        if bill.kvah_total:
            kvah_residual = max(bill.kvah_total - ledger.loc[mask, "grid_kvah"].sum(), 0.0)
            ledger.loc[mask, "grid_kvah"] += kvah_residual * w
        # Spread each ToD zone's missing units the same way (kWh-basis bills print kWh by zone).
        bill_zones = bill.zone_units()
        if bill.energy_basis == "kWh" and bill_zones:
            for b in bands:
                zone_residual = max(bill_zones.get(b, 0.0) - ledger.loc[mask, f"kwh_{b}"].sum(), 0.0)
                ledger.loc[mask, f"kwh_{b}"] += zone_residual * w
        else:
            total_units = sum(bill_zones.values()) or 1.0
            for b in bands:
                ledger.loc[mask, f"kwh_{b}"] += residual * w * bill_zones.get(b, 0.0) / total_units
    ledger["estimated"] = ledger["grid_kwh_estimated"] > 0.5


def _flag_outliers(ledger: pd.DataFrame, z_limit: float = 4.0) -> None:
    """Days whose energy is far from what the unit's own history suggests for that output."""
    ledger["outlier"] = False
    ok = ledger["production_recorded"] & ~ledger["estimated"]
    if ok.sum() < 15:
        return
    x, y = ledger.loc[ok, "total_t"].astype(float), ledger.loc[ok, "elec_kwh"].astype(float)
    slope, intercept = np.polyfit(x, y, 1)
    resid = y - (intercept + slope * x)
    mad = np.median(np.abs(resid - np.median(resid))) * 1.4826
    if mad <= 0:
        return
    ledger.loc[ok, "outlier"] = (np.abs(resid - np.median(resid)) / mad) > z_limit


# --------------------------------------------------------------------------------------
# P6: data quality score
# --------------------------------------------------------------------------------------


@dataclass
class QualityComponent:
    name: str
    score: float  # 0..1
    weight: float
    detail: str


@dataclass
class DataQuality:
    score: float
    grade: str
    components: list[QualityComponent] = field(default_factory=list)

    def as_frame(self) -> pd.DataFrame:
        return pd.DataFrame(
            [{"Check": c.name, "Score": round(c.score * 100), "Weight": c.weight, "Detail": c.detail} for c in self.components]
        )


def data_quality(
    ledger: pd.DataFrame,
    survey: LoadSurvey | None,
    bill_issues: list[list[Issue]],
    fuel_periods: pd.DataFrame | None = None,
) -> DataQuality:
    """A visible score so nobody over-trusts thin data. Shown on every report."""
    n_days = len(ledger)
    comps: list[QualityComponent] = []

    if survey is not None:
        q = survey.quality
        estimated_days = int(ledger["estimated"].sum())
        detail = f"{q.coverage:.1%} of 15-minute readings present; {len(q.gaps)} gaps"
        if estimated_days:
            detail += f", {estimated_days} day(s) filled from bills and labelled estimated"
        if q.n_duplicates:
            detail += f"; {q.n_duplicates} duplicate rows dropped"
        score = q.coverage - 0.02 * q.n_conflicting_duplicates
        comps.append(QualityComponent("Meter load survey", float(np.clip(score, 0, 1)), 30, detail))
    else:
        comps.append(QualityComponent("Meter load survey", 0.0, 30, "No load survey: bill-level analysis only"))

    recorded = int(ledger["production_recorded"].sum())
    comps.append(
        QualityComponent(
            "Production register",
            recorded / n_days if n_days else 0.0,
            25,
            f"{recorded} of {n_days} days have a register entry; {n_days - recorded} missing",
        )
    )

    months = {(d.year, d.month) for d in ledger.index}
    billed = {(d.year, d.month) for d in ledger.index[ledger["bill_period"] != ""]}
    errors = sum(1 for issues in bill_issues for i in issues if i.severity == "error")
    comps.append(
        QualityComponent(
            "Electricity bills",
            max(len(billed) / len(months) - 0.1 * errors, 0.0) if months else 0.0,
            20,
            f"{len(billed)} of {len(months)} months billed; {errors} bill check error(s)",
        )
    )

    outliers = int(ledger["outlier"].sum())
    comps.append(
        QualityComponent(
            "Outlier days", max(1 - 5 * outliers / max(n_days, 1), 0.0), 10, f"{outliers} day(s) far from the unit's own pattern"
        )
    )

    if fuel_periods is not None and not fuel_periods.empty:
        flagged = int((fuel_periods["flag"] != "").sum())
        comps.append(
            QualityComponent(
                "Fuel reconciliation",
                1 - flagged / len(fuel_periods),
                15,
                f"{len(fuel_periods) - flagged} of {len(fuel_periods)} fuel periods reconcile",
            )
        )
    else:
        comps.append(QualityComponent("Fuel reconciliation", 0.5, 15, "No fuel stock readings: fuel from invoices only"))

    total_w = sum(c.weight for c in comps)
    score = 100 * sum(c.score * c.weight for c in comps) / total_w
    grade = "A" if score >= 85 else "B" if score >= 70 else "C" if score >= 50 else "D"
    return DataQuality(round(score, 1), grade, comps)


def monthly_summary(ledger: pd.DataFrame) -> pd.DataFrame:
    df = ledger.copy()
    df.index = pd.to_datetime(df.index)
    cols = ["grid_kwh", "dg_kwh", "elec_kwh", "total_t", "diesel_l"] + [f"kwh_{b}" for b in ledger.attrs.get("bands", [])]
    cols += [f"{f}_units" for f in ledger.attrs.get("fuels", [])]
    out = df[cols].resample("MS").sum()
    out["sec_kwh_per_t"] = out["elec_kwh"] / out["total_t"]
    out.index = out.index.strftime("%b %Y")
    return out
