"""Stage 4: turning analysis into decisions (P10 bill forensics, P13 ranking) and the impact model.

The ten-minute bill check needs nothing but bills and finds money before the owner is asked
for production data. Opportunities from every module are ranked by rupees per year,
capital cost, payback and confidence, and the owner is shown the top three only.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import date

import numpy as np
import pandas as pd

from unitwatt.analytics import DriftResult, IdleResult
from unitwatt.scheduler import ScheduleProblem, ScheduleResult
from unitwatt.schemas import BillDocument
from unitwatt.tariff import SLOT_HOURS, Tariff

WORKING_DAYS_PER_YEAR = 300
ACHIEVABLE_IDLE_CUT = 0.5   # operational fixes only: switching off, leak repair
CAPEX_PER_KVAR = 900.0      # illustrative APFC panel cost, Rs per kVAr


# --------------------------------------------------------------------------------------
# P10: bill forensics
# --------------------------------------------------------------------------------------


LOSS_KINDS = {"billing_error", "excess_demand", "demand_floor", "pf_penalty"}


@dataclass
class BillFinding:
    month: str
    kind: str
    title: str
    rupees: float
    detail: str

    @property
    def is_loss(self) -> bool:
        """Money already lost on this bill, as opposed to an opportunity or a what-if."""
        return self.kind in LOSS_KINDS


def bill_forensics(bills: list[BillDocument], tariff: Tariff, survey_data: pd.DataFrame | None = None) -> list[BillFinding]:
    findings: list[BillFinding] = []
    duty = 1 + tariff.electricity_duty_pct / 100
    for bill in bills:
        month = f"{bill.period_start:%b %Y}"
        expected = tariff.reprice(bill)
        diff = bill.total_amount - expected.total_amount
        if abs(diff) > max(100.0, 0.002 * expected.total_amount):
            findings.append(
                BillFinding(month, "billing_error", "Bill does not match the tariff order", diff,
                            f"Printed Rs {bill.total_amount:,.0f}; the tariff config gives Rs {expected.total_amount:,.0f}. "
                            "Check the rates applied before paying.")
            )
        if bill.excess_demand_charges > 0:
            when = ""
            if survey_data is not None:
                part = survey_data[(survey_data.index.date >= bill.period_start) & (survey_data.index.date <= bill.period_end)]
                if not part["kvah"].dropna().empty:
                    peak_at = (part["kvah"] / SLOT_HOURS).idxmax()
                    when = f" The meter shows the peak at {peak_at:%H:%M on %d %b}."
            findings.append(
                BillFinding(month, "excess_demand", "Excess-demand penalty", bill.excess_demand_charges * duty,
                            f"Maximum demand {bill.max_demand_kva:.0f} kVA went over the contract demand of "
                            f"{bill.contract_demand_kva:.0f} kVA.{when} Stagger machine start-ups, especially after a power cut.")
            )
        if bill.billing_demand_kva and bill.billing_demand_kva > bill.max_demand_kva + 0.5:
            over = (bill.billing_demand_kva - bill.max_demand_kva) * tariff.demand_rate * duty
            findings.append(
                BillFinding(month, "demand_floor", "Paying for unused contract demand", over,
                            f"Billed on {bill.billing_demand_kva:.0f} kVA ({tariff.billing_floor_pct:.0f}% of contract demand) "
                            f"although the factory only drew {bill.max_demand_kva:.0f} kVA.")
            )
        if bill.pf_adjustment > 0:
            findings.append(
                BillFinding(month, "pf_penalty", "Power-factor penalty", bill.pf_adjustment * duty,
                            f"Average power factor {bill.power_factor:.2f} is below {tariff.pf_rule.reference:.2f}. "
                            "Check the capacitor bank or fit an automatic (APFC) panel.")
            )
        zones = bill.zone_units()
        costly, cheap = tariff.costliest_band, tariff.cheapest_band
        if zones.get(costly, 0) > 0 and tariff.multiplier(costly) > tariff.multiplier(cheap):
            premium = zones[costly] * tariff.energy_rate * (tariff.multiplier(costly) - tariff.multiplier(cheap)) * duty
            share = zones[costly] / bill.billing_units
            findings.append(
                BillFinding(month, "tod_premium", f"{share:.0%} of units used in {costly} hours", premium,
                            f"{zones[costly]:,.0f} units at {tariff.multiplier(costly):.0%} of the base rate. Every unit moved "
                            f"to {cheap} hours saves Rs {tariff.energy_rate * (tariff.multiplier(costly) - tariff.multiplier(cheap)):.2f}. "
                            "This is the upper limit; only shiftable loads can move.")
            )
        if bill.energy_basis == "kWh" and bill.kvah_total and bill.kvah_total > bill.kwh_total:
            exposure = (bill.kvah_total - bill.kwh_total) * tariff.energy_rate * duty
            findings.append(
                BillFinding(month, "kvah_exposure", "Cost of poor power factor under kVAh billing", exposure,
                            f"If the DISCOM moves to kVAh billing, as several northern DISCOMs have, this month's PF of "
                            f"{bill.power_factor:.2f} would add about Rs {exposure:,.0f}.")
            )
    return findings


def optimise_contract_demand(bills: list[BillDocument], tariff: Tariff, step: float = 5.0) -> pd.DataFrame:
    """Annual demand charges for each candidate contract demand, from the months on record."""
    mds = np.array([b.max_demand_kva for b in bills])
    if len(mds) == 0:
        return pd.DataFrame()
    duty = 1 + tariff.electricity_duty_pct / 100
    candidates = np.arange(math.floor(mds.min() * 0.8 / step) * step, math.ceil(mds.max() * 1.25 / step) * step + step, step)
    rows = []
    for cd in candidates:
        cost = sum(sum(tariff.demand_charges(md, cd)) for md in mds) * duty * 12 / len(mds)
        rows.append({"contract_demand_kva": cd, "annual_demand_rs": cost, "months_over": int((mds > cd).sum())})
    return pd.DataFrame(rows)


# --------------------------------------------------------------------------------------
# P13: opportunity ranking
# --------------------------------------------------------------------------------------


@dataclass
class Opportunity:
    id: str
    title: str
    title_hi: str
    rupees_per_year: float
    kwh_per_year: float
    capex: float
    confidence: str
    evidence: str
    module: str
    saves_energy: bool = True
    tco2_per_year: float = 0.0
    actions: list[str] = field(default_factory=list)

    @property
    def payback_months(self) -> float:
        if self.capex <= 0:
            return 0.0
        return self.capex / (self.rupees_per_year / 12) if self.rupees_per_year > 0 else float("inf")


def _months(start: date, end: date) -> float:
    return ((end - start).days + 1) / 30.44


def rank_opportunities(
    *,
    window: tuple[date, date],
    ledger: pd.DataFrame,
    bills: list[BillDocument],
    tariff: Tariff,
    idle: IdleResult | None,
    schedule: dict[str, ScheduleResult] | None,
    problem: ScheduleProblem | None,
    drift: DriftResult | None,
    sector_benchmark: dict | None,
    grid_kg_per_kwh: float,
    data_quality_score: float,
    avg_rs_per_kwh: float,
) -> list[Opportunity]:
    start, end = window
    months = _months(start, end)
    per_year = 12 / months
    duty = 1 + tariff.electricity_duty_pct / 100
    conf_data = "High" if data_quality_score >= 85 else "Medium" if data_quality_score >= 70 else "Low"
    win = ledger[(ledger.index >= start) & (ledger.index <= end)]
    window_bills = [b for b in bills if b.period_start >= start and b.period_end <= end]
    out: list[Opportunity] = []

    if idle is not None and idle.avoidable_kwh > 0:
        kwh = idle.avoidable_kwh * ACHIEVABLE_IDLE_CUT * per_year
        out.append(
            Opportunity(
                "idle", "Switch off idle machines and fix air leaks at night and on Sundays",
                "रात और रविवार को बेकार चल रही मशीनें बंद करें और हवा की लीकेज ठीक करें",
                idle.avoidable_rs * ACHIEVABLE_IDLE_CUT * per_year * duty, kwh, 15_000, conf_data,
                f"Median load with no production is {idle.base_kw:.1f} kW against an essential {idle.essential_kw:.1f} kW; "
                f"{idle.idle_pct:.1%} of energy is used when nothing is being made. Assumes half of it can be cut.",
                "P9", actions=["Walk the plant at 11 pm with the electrician and list what is running",
                               "Ultrasonic or soap test on compressed-air lines; fix the leaks",
                               "Compressor and lighting timers"],
                tco2_per_year=kwh * grid_kg_per_kwh / 1000,
            )
        )

    if schedule is not None and problem is not None:
        per_day_sched = schedule["current"].total_rs - schedule["optimised"].total_rs
        peak_band, cheap_band = tariff.costliest_band, tariff.cheapest_band
        fixed_peak_kwh = float(problem.base_kw[problem.bands == peak_band].sum() * SLOT_HOURS)
        prod_days = win[win["is_production_day"]]
        if len(prod_days) and f"kwh_{peak_band}" in win:
            shiftable = (prod_days[f"kwh_{peak_band}"] - fixed_peak_kwh).clip(lower=0).mean()
            per_day_data = shiftable * tariff.energy_rate * (tariff.multiplier(peak_band) - tariff.multiplier(cheap_band))
        else:
            per_day_data = per_day_sched
        per_day = max(min(per_day_sched, per_day_data), 0.0) * duty
        if per_day > 0:
            out.append(
                Opportunity(
                    "tod", f"Move billet heating and shot blasting into {cheap_band} hours",
                    "बिलेट हीटिंग और शॉट ब्लास्टिंग को दिन के सस्ते (सोलर) घंटों में करें",
                    per_day * WORKING_DAYS_PER_YEAR, 0.0, 0.0, "Medium",
                    f"The optimised schedule keeps peak demand at {schedule['optimised'].peak_kva:.0f} kVA (moving everything "
                    f"without the optimiser would hit {schedule['naive'].peak_kva:.0f} kVA). Saves money, not energy: the CEA "
                    "publishes one annual grid factor, so no carbon is claimed for shifting.",
                    "P12", saves_energy=False,
                    actions=["Confirm with the supervisor which batches can move", "Try the schedule for one week",
                             "UnitWatt checks the next load survey for a new peak"],
                )
            )

    pf_rs = sum(b.pf_adjustment for b in window_bills if b.pf_adjustment > 0) * duty * per_year
    if pf_rs > 0:
        worst = min(window_bills, key=lambda b: b.power_factor or 1.0)
        avg_kw = worst.kwh_total / ((worst.period_end - worst.period_start).days + 1) / 16
        kvar = avg_kw * (math.tan(math.acos(worst.power_factor)) - math.tan(math.acos(0.97)))
        out.append(
            Opportunity(
                "pf", "Repair the capacitor bank or fit an APFC panel",
                "कैपेसिटर बैंक ठीक कराएं या APFC पैनल लगवाएं",
                pf_rs, 0.0, max(kvar, 0.0) * CAPEX_PER_KVAR, "High",
                f"Power-factor penalties on the bills; PF fell to {worst.power_factor:.2f} in {worst.period_start:%B}. "
                f"About {kvar:.0f} kVAr restores PF to 0.97.", "P10", saves_energy=False,
                actions=["Electrician checks capacitor fuses and contactors", "Fit APFC if the bank keeps failing"],
            )
        )

    excess_rs = sum(b.excess_demand_charges for b in window_bills) * duty * per_year
    if excess_rs > 0:
        out.append(
            Opportunity(
                "excess_demand", "Stagger machine start-ups to stay under contract demand",
                "मशीनें एक साथ चालू न करें, बारी-बारी से चालू करें",
                excess_rs, 0.0, 0.0, "Medium",
                "Excess-demand surcharge on the bills, from short spikes when several loads start together.",
                "P10", saves_energy=False,
                actions=["Written start-up order after every power cut", "Interlock heater start with press start"],
            )
        )

    if drift is not None and drift.alarm_date is not None and drift.alarm_date <= end:
        recent = drift.daily.tail(14)
        excess_per_day = float(recent["residual"].clip(lower=0).mean())
        kwh = excess_per_day * WORKING_DAYS_PER_YEAR
        out.append(
            Opportunity(
                "drift", "Inspect the induction heater: energy per tonne is drifting up",
                "इंडक्शन हीटर की जांच कराएं: प्रति टन बिजली बढ़ रही है",
                kwh * avg_rs_per_kwh, kwh, 50_000, "Medium",
                f"Since {drift.alarm_date:%d %b} the plant uses {drift.recent_excess_pct:.1%} more electricity than its own "
                "recent pattern predicts for the same product mix (CUSUM alarm).",
                "P11", actions=["Check coil, refractory lining and cooling water", "Compare kWh per billet with the heater's rating plate"],
                tco2_per_year=kwh * grid_kg_per_kwh / 1000,
            )
        )

    if sector_benchmark is not None:
        good = float(sector_benchmark["electricity_sec_kwh_per_t"]["good"])
        tonnes = float(win["total_t"].sum())
        sec = float(win["elec_kwh"].sum() / tonnes) if tonnes else 0.0
        if sec > good and tonnes > 0:
            kwh = (sec - good) * tonnes * per_year * 0.5
            out.append(
                Opportunity(
                    "benchmark", "Close half the gap to the cluster's best units (equipment upgrade)",
                    "क्लस्टर की सबसे अच्छी यूनिटों के बराबर आने के लिए मशीनें अपग्रेड करें",
                    kwh * avg_rs_per_kwh, kwh, 600_000, "Low",
                    f"{sec:.0f} kWh per tonne against a 'good' benchmark of {good:.0f} ({sector_benchmark.get('status', '')} "
                    "benchmark). Candidate for an ADEETIE investment-grade audit and 5% interest subvention.",
                    "P13", actions=["Investment-grade audit under ADEETIE", "Quotes for heater and motor upgrades"],
                    tco2_per_year=kwh * grid_kg_per_kwh / 1000,
                )
            )

    out.sort(key=priority, reverse=True)
    return out


CONFIDENCE_WEIGHT = {"High": 1.0, "Medium": 0.8, "Low": 0.4}


def priority(o: Opportunity) -> float:
    """Expected rupees a year, discounted for uncertainty and for waiting on payback."""
    return o.rupees_per_year * CONFIDENCE_WEIGHT[o.confidence] / (1 + o.payback_months / 12)


# --------------------------------------------------------------------------------------
# The deep dive's illustrative impact model
# --------------------------------------------------------------------------------------


@dataclass
class ImpactAssumptions:
    grid_kwh_per_month: float = 60_000
    base_rate: float = 8.0
    peak_multiplier: float = 1.20
    solar_multiplier: float = 0.80
    shifted_kwh_per_month: float = 10_000
    idle_share: float = 0.08
    idle_cut: float = 0.50
    grid_tco2_per_mwh: float = 0.675


def impact_model(a: ImpactAssumptions, units: int = 1, shift_realisation: float = 1.0) -> dict[str, float]:
    tod = a.shifted_kwh_per_month * shift_realisation * a.base_rate * (a.peak_multiplier - a.solar_multiplier)
    idle_kwh = a.grid_kwh_per_month * a.idle_share * a.idle_cut
    idle_rs = idle_kwh * a.base_rate
    co2 = idle_kwh * a.grid_tco2_per_mwh / 1000
    return {
        "tod_rs_month": tod,
        "idle_rs_month": idle_rs,
        "idle_kwh_month": idle_kwh,
        "total_rs_month": tod + idle_rs,
        "tco2_month": co2,
        "tod_rs_year": tod * 12,
        "idle_rs_year": idle_rs * 12,
        "idle_mwh_year": idle_kwh * 12 / 1000,
        "total_rs_year": (tod + idle_rs) * 12,
        "tco2_year": co2 * 12,
        "fleet_rs_year": (tod + idle_rs) * 12 * units,
        "fleet_tco2_year": co2 * 12 * units,
    }
