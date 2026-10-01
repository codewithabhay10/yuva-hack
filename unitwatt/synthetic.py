"""A synthetic forging unit for the demo, until a real factory shares its bills.

The unit is billed under a real state tariff structure, with realistic noise and the
faults the product is meant to find:

* a night and Sunday base load from air leaks and machines left on (P9),
* billet-heating batches running in the evening peak (P12),
* a May day when everything restarted together after a power cut (excess demand),
* a capacitor bank that failed in June (power-factor penalty),
* induction-heater lining wear from late August (energy-per-tonne drift, P11),
* gaps and duplicate rows in the meter export, and missing register pages (P3, P6).

On ``actions_date`` the owner acts on the top recommendations: half the avoidable idle
load is switched off or repaired and billet heating moves into solar hours. The M&V module
(P15) should then find the savings without being told what changed.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path

import numpy as np
import pandas as pd

from unitwatt.profiles import Factory, hhmm_to_minutes
from unitwatt.schemas import BillDocument
from unitwatt.tariff import SLOT_HOURS, SLOT_MINUTES, SLOTS_PER_DAY, Tariff

TRUE_SEC_KWH_PER_T = {"flange": 380.0, "crank": 520.0, "gear": 450.0}
MEAN_TONNES_PER_DAY = {"flange": 1.5, "crank": 1.3, "gear": 1.4}
HOLIDAYS = (date(2026, 8, 15), date(2026, 9, 14))


@dataclass
class Scenario:
    start: date = date(2026, 4, 1)
    end: date = date(2026, 9, 30)
    actions_date: date | None = date(2026, 7, 1)
    drift_start: date | None = date(2026, 8, 31)
    drift_max_pct: float = 10.0
    pf_fault_month: int | None = 6
    demand_spike_date: date | None = date(2026, 5, 19)
    idle_kw_before: float = 16.0
    avoidable_idle_cut: float = 0.5
    overhead_kw: float = 12.0
    slot_noise: float = 0.08
    seed: int = 7


@dataclass
class SyntheticData:
    scenario: Scenario
    survey: pd.DataFrame          # meter export as delivered: gaps and duplicate rows included
    survey_true: pd.DataFrame     # what the meter actually recorded (the DISCOM bills on this)
    production: pd.DataFrame      # long format: date, product, tonnes (missing pages absent)
    dg_log: pd.DataFrame          # date, run_hours, litres, kwh
    fuel_purchases: pd.DataFrame  # date, fuel, quantity, rate, invoice_no
    fuel_stock: pd.DataFrame      # date, fuel, closing_stock (month-end dip readings)
    bills: list[BillDocument]
    truth: dict = field(default_factory=dict)

    def write(self, directory: Path) -> dict[str, Path]:
        """Write the inputs in the formats a factory would hand over."""
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "bills").mkdir(exist_ok=True)
        paths: dict[str, Path] = {}

        portal = pd.DataFrame(
            {
                "Date": self.survey.index.strftime("%d-%m-%Y"),
                "Time": self.survey.index.strftime("%H:%M"),
                "kWh": self.survey["kwh"].round(3).to_numpy(),
                "kVAh": self.survey["kvah"].round(3).to_numpy(),
            }
        )
        paths["load_survey"] = directory / "load_survey_portal.csv"
        portal.to_csv(paths["load_survey"], index=False)

        paths["production"] = directory / "production_register.csv"
        self.production.to_csv(paths["production"], index=False)
        paths["dg_log"] = directory / "dg_log.csv"
        self.dg_log.to_csv(paths["dg_log"], index=False)
        paths["fuel_purchases"] = directory / "fuel_invoices.csv"
        self.fuel_purchases.to_csv(paths["fuel_purchases"], index=False)
        paths["fuel_stock"] = directory / "fuel_stock.csv"
        self.fuel_stock.to_csv(paths["fuel_stock"], index=False)

        for bill in self.bills:
            path = directory / "bills" / f"bill_{bill.period_start:%Y_%m}.json"
            path.write_text(bill.model_dump_json(indent=2), encoding="utf-8")
            paths[f"bill_{bill.period_start:%Y_%m}"] = path
        (directory / "truth.json").write_text(json.dumps(self.truth, indent=2, default=str), encoding="utf-8")
        return paths


def _hourly_shape(after_actions: bool) -> np.ndarray:
    """Relative production intensity for each hour of the shift window."""
    shape = np.zeros(24)
    if not after_actions:
        shape[7:23] = 1.0
    else:
        shape[7:9] = 1.0
        shape[9:17] = 1.25  # billet heating moved into solar hours
        shape[17:23] = 0.6
    return shape


def _drift_factor(day: date, sc: Scenario) -> float:
    if sc.drift_start is None or day < sc.drift_start:
        return 1.0
    span = max((sc.end - sc.drift_start).days, 1)
    return 1.0 + sc.drift_max_pct / 100 * min((day - sc.drift_start).days / span, 1.0)


def generate(factory: Factory, tariff: Tariff, scenario: Scenario | None = None) -> SyntheticData:
    sc = scenario or Scenario()
    rng = np.random.default_rng(sc.seed)
    days = pd.date_range(sc.start, sc.end, freq="D")
    shift_start = hhmm_to_minutes(factory.shift_start)
    shift_end = hhmm_to_minutes(factory.shift_end)
    minutes = np.arange(SLOTS_PER_DAY) * SLOT_MINUTES
    in_shift = (minutes >= shift_start) & (minutes < shift_end)
    essential = factory.essential_night_load_kw
    products = factory.product_ids

    kw_rows, pf_rows, prod_rows, dg_rows = [], [], [], []
    daily_truth = []
    for ts in days:
        day = ts.date()
        acted = sc.actions_date is not None and day >= sc.actions_date
        idle_kw = sc.idle_kw_before
        if acted:
            idle_kw = essential + (sc.idle_kw_before - essential) * (1 - sc.avoidable_idle_cut)
        working = day.weekday() in factory.working_weekdays and day not in HOLIDAYS

        kw = idle_kw * (1 + rng.normal(0, 0.06, SLOTS_PER_DAY))
        pf = np.clip(rng.normal(0.82, 0.01, SLOTS_PER_DAY), 0.7, 0.95)
        tonnes = {p: 0.0 for p in products}
        if working:
            for p in products:
                mean = MEAN_TONNES_PER_DAY[p]
                t = float(np.clip(rng.normal(mean, 0.25 * mean), 0.0, 1.6 * mean))
                if rng.random() < 0.08:   # a product line not run that day
                    t = 0.0
                tonnes[p] = round(t / 0.05) * 0.05
            drift = _drift_factor(day, sc)
            energy = sum(TRUE_SEC_KWH_PER_T[p] * tonnes[p] for p in products) * drift
            energy *= 1 + rng.normal(0, 0.03)
            hourly = _hourly_shape(acted)
            weights = np.repeat(hourly, 60 // SLOT_MINUTES) * rng.lognormal(0, sc.slot_noise, SLOTS_PER_DAY)
            weights = weights / weights.sum()
            kw = kw + weights * energy / SLOT_HOURS
            kw[in_shift] += sc.overhead_kw * (1 + rng.normal(0, 0.05, in_shift.sum()))
            fault_pf = sc.pf_fault_month is not None and day.month == sc.pf_fault_month
            pf[in_shift] = np.clip(rng.normal(0.86 if fault_pf else 0.945, 0.008, in_shift.sum()), 0.7, 0.99)
            if sc.demand_spike_date == day:
                spike = slice(hhmm_to_minutes("10:00") // SLOT_MINUTES, hhmm_to_minutes("10:45") // SLOT_MINUTES)
                kw[spike] += 140.0
                kw[spike.stop : spike.stop + 4] *= 0.4   # the rest of the hour is lost to the restart

        # Grid outages during the shift: the DG set carries the load.
        grid_kw = kw.copy()
        if working and rng.random() < 0.22:
            start_slot = rng.integers(shift_start // SLOT_MINUTES, shift_end // SLOT_MINUTES - 12)
            length = int(rng.integers(4, 13))
            outage = slice(start_slot, start_slot + length)
            dg_kwh = float(kw[outage].sum() * SLOT_HOURS)
            grid_kw[outage] = 0.0
            pf[outage] = 1.0
            litres = dg_kwh / 3.3 * (1 + rng.normal(0, 0.03))
            dg_rows.append({"date": day, "run_hours": length * SLOT_HOURS, "litres": round(litres, 1), "kwh": round(dg_kwh, 1)})

        kw_rows.append(grid_kw)
        pf_rows.append(pf)
        for p in products:
            prod_rows.append({"date": day, "product": p, "tonnes": tonnes[p]})
        daily_truth.append({"date": day, "idle_kw": idle_kw, "working": working, "drift": _drift_factor(day, sc)})

    index = pd.date_range(pd.Timestamp(sc.start), periods=len(days) * SLOTS_PER_DAY, freq=f"{SLOT_MINUTES}min")
    kwh = np.concatenate(kw_rows) * SLOT_HOURS
    kvah = kwh / np.concatenate(pf_rows)
    survey_true = pd.DataFrame({"kwh": kwh, "kvah": kvah}, index=index)
    survey_true.index.name = "timestamp"

    # The meter export as delivered: a few gaps and duplicated rows.
    survey = survey_true.copy()
    gaps = [("2026-05-12 10:00", "2026-05-12 14:45"), ("2026-08-03 00:00", "2026-08-03 23:45")]
    for g_start, g_end in gaps:
        survey = survey.drop(survey.loc[g_start:g_end].index)
    dup = survey.loc["2026-06-08 12:00":"2026-06-08 12:45"]
    survey = pd.concat([survey, dup]).sort_index(kind="stable")

    production = pd.DataFrame(prod_rows)
    missing_pages = [date(2026, 4, 22), date(2026, 9, 8)]
    production = production[~production["date"].isin(missing_pages)].reset_index(drop=True)

    dg_log = pd.DataFrame(dg_rows, columns=["date", "run_hours", "litres", "kwh"])
    fuel_purchases, fuel_stock, fo_true = _fuel_records(factory, production, dg_log, days, rng)

    bills = []
    for month_start in pd.date_range(sc.start, sc.end, freq="MS"):
        month_end = (month_start + pd.offsets.MonthEnd(0)).date()
        bills.append(
            tariff.simulate_bill(
                survey_true,
                month_start.date(),
                min(month_end, sc.end),
                factory.contract_demand_kva,
                consumer_number=factory.consumer_number,
            )
        )

    truth = {
        "sec_kwh_per_t": TRUE_SEC_KWH_PER_T,
        "idle_kw_before": sc.idle_kw_before,
        "idle_kw_after": essential + (sc.idle_kw_before - essential) * (1 - sc.avoidable_idle_cut),
        "overhead_kw": sc.overhead_kw,
        "actions_date": sc.actions_date,
        "drift_start": sc.drift_start,
        "drift_max_pct": sc.drift_max_pct,
        "survey_gaps": gaps,
        "missing_register_pages": missing_pages,
        "furnace_oil_litres_true": round(fo_true, 1),
    }
    return SyntheticData(sc, survey, survey_true, production, dg_log, fuel_purchases, fuel_stock, bills, truth)


def _fuel_records(
    factory: Factory,
    production: pd.DataFrame,
    dg_log: pd.DataFrame,
    days: pd.DatetimeIndex,
    rng: np.random.Generator,
) -> tuple[pd.DataFrame, pd.DataFrame, float]:
    """Purchase invoices and month-end dip readings; consumption is never recorded directly (P4)."""
    heat_treated = {p.id for p in factory.products if p.heat_treated}
    daily_t = production[production["product"].isin(heat_treated)].groupby("date")["tonnes"].sum()
    dg_by_day = dg_log.set_index("date")["litres"] if not dg_log.empty else pd.Series(dtype=float)

    stock = {"furnace_oil": 2500.0, "diesel": 300.0}
    reorder = {"furnace_oil": (1500.0, 4000.0, 62.0), "diesel": (150.0, 400.0, 92.0)}
    purchases, stock_rows = [], []
    invoice_no = 1
    fo_total = 0.0
    for ts in days:
        day = ts.date()
        t = float(daily_t.get(day, 0.0))
        use = {"furnace_oil": (38.0 * t + 45.0) * (1 + rng.normal(0, 0.04)) if t > 0 else 0.0,
               "diesel": float(dg_by_day.get(day, 0.0))}
        fo_total += use["furnace_oil"]
        for fuel in stock:
            low, qty, rate = reorder[fuel]
            if stock[fuel] - use[fuel] < low:
                purchases.append(
                    {"date": day, "fuel": fuel, "quantity": qty, "rate": round(rate * (1 + rng.normal(0, 0.02)), 2),
                     "invoice_no": f"INV-{invoice_no:04d}"}
                )
                invoice_no += 1
                stock[fuel] += qty
            stock[fuel] -= use[fuel]
        if (ts + timedelta(days=1)).day == 1 or day == days[-1].date():
            for fuel in stock:
                reading = stock[fuel] + rng.normal(0, 8 if fuel == "furnace_oil" else 2)
                stock_rows.append({"date": day, "fuel": fuel, "closing_stock": round(reading, 0)})
    opening = [{"date": days[0].date() - timedelta(days=1), "fuel": "furnace_oil", "closing_stock": 2500.0},
               {"date": days[0].date() - timedelta(days=1), "fuel": "diesel", "closing_stock": 300.0}]
    return pd.DataFrame(purchases), pd.DataFrame(opening + stock_rows), fo_total
