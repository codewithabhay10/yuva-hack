"""Tariff rules engine and bill simulator (P10).

Every DISCOM tariff is stored as a versioned YAML config dated by its effective period:
rates, ToD windows, the billing-demand floor, the excess-demand surcharge, PF slabs and the
kWh or kVAh basis. The same engine prices a meter load survey (to simulate a bill), checks
a printed bill against the tariff order, and prices candidate schedules for the optimiser.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from functools import lru_cache
from pathlib import Path

import numpy as np
import pandas as pd
import yaml

from unitwatt.profiles import CONFIG_DIR, hhmm_to_minutes
from unitwatt.schemas import BillDocument, ZoneReading

SLOT_MINUTES = 15
SLOT_HOURS = SLOT_MINUTES / 60
SLOTS_PER_DAY = 24 * 60 // SLOT_MINUTES


@dataclass(frozen=True)
class TodBand:
    name: str
    start_min: int
    end_min: int  # exclusive; an end at or before the start wraps past midnight
    multiplier: float

    def contains(self, minute_of_day: np.ndarray) -> np.ndarray:
        if self.end_min > self.start_min:
            return (minute_of_day >= self.start_min) & (minute_of_day < self.end_min)
        return (minute_of_day >= self.start_min) | (minute_of_day < self.end_min)


@dataclass(frozen=True)
class PowerFactorRule:
    reference: float
    penalty_pct_per_0_01: float
    incentive_threshold: float | None
    incentive_pct_per_0_01: float

    def adjustment_pct(self, pf: float) -> float:
        """Positive = penalty, negative = incentive, as % of energy charges."""
        pf = round(pf, 2)
        if pf < self.reference:
            return round((self.reference - pf) * 100) * self.penalty_pct_per_0_01
        if self.incentive_threshold is not None and pf > self.incentive_threshold:
            return -round((pf - self.incentive_threshold) * 100) * self.incentive_pct_per_0_01
        return 0.0


@dataclass(frozen=True)
class Tariff:
    id: str
    name: str
    state: str
    discom: str
    effective_from: date
    effective_to: date | None
    status: str
    source: str
    notes: str
    energy_basis: str
    energy_rate: float
    default_band: str
    default_multiplier: float
    bands: tuple[TodBand, ...]
    demand_rate: float
    billing_floor_pct: float
    excess_surcharge_pct: float
    pf_rule: PowerFactorRule | None
    electricity_duty_pct: float
    fixed_charges: float

    @property
    def band_names(self) -> list[str]:
        names = [b.name for b in self.bands]
        return names + [self.default_band] if self.default_band not in names else names

    def multiplier(self, band: str) -> float:
        for b in self.bands:
            if b.name == band:
                return b.multiplier
        return self.default_multiplier

    def band_for_minutes(self, minute_of_day: np.ndarray) -> np.ndarray:
        minute_of_day = np.asarray(minute_of_day)
        out = np.full(minute_of_day.shape, self.default_band, dtype=object)
        for band in self.bands:
            out[band.contains(minute_of_day)] = band.name
        return out

    def band_series(self, index: pd.DatetimeIndex) -> pd.Series:
        minutes = (index.hour * 60 + index.minute).to_numpy()
        return pd.Series(self.band_for_minutes(minutes), index=index, name="band")

    def price_series(self, index: pd.DatetimeIndex) -> pd.Series:
        """Energy price in Rs per billing unit for each timestamp (ToD applied)."""
        bands = self.band_series(index)
        mult = bands.map({name: self.multiplier(name) for name in self.band_names}).astype(float)
        return (mult * self.energy_rate).rename("price")

    def slot_prices(self) -> np.ndarray:
        """Rs per unit for each of the 96 15-minute slots of a day."""
        minutes = np.arange(SLOTS_PER_DAY) * SLOT_MINUTES
        bands = self.band_for_minutes(minutes)
        return np.array([self.multiplier(b) * self.energy_rate for b in bands])

    @property
    def cheapest_band(self) -> str:
        return min(self.band_names, key=self.multiplier)

    @property
    def costliest_band(self) -> str:
        return max(self.band_names, key=self.multiplier)

    def billing_demand(self, max_demand_kva: float, contract_demand_kva: float) -> float:
        return max(max_demand_kva, self.billing_floor_pct / 100 * contract_demand_kva)

    def demand_charges(self, max_demand_kva: float, contract_demand_kva: float) -> tuple[float, float]:
        """(normal demand charges, excess-demand charges) for one month."""
        billed = self.billing_demand(max_demand_kva, contract_demand_kva)
        normal = min(billed, contract_demand_kva) * self.demand_rate
        excess_kva = max(0.0, max_demand_kva - contract_demand_kva)
        excess = excess_kva * self.demand_rate * (1 + self.excess_surcharge_pct / 100)
        return normal, excess

    def energy_charges(self, zone_units: dict[str, float]) -> dict[str, float]:
        return {zone: units * self.energy_rate * self.multiplier(zone) for zone, units in zone_units.items()}

    def price_bill(
        self,
        *,
        period_start: date,
        period_end: date,
        kwh_total: float,
        kvah_total: float | None,
        zone_units: dict[str, float],
        max_demand_kva: float,
        contract_demand_kva: float,
        consumer_number: str | None = None,
    ) -> BillDocument:
        """Compute every line item of a bill from its physical quantities.

        Quantities are rounded to what a bill prints first, so the charges can be recomputed
        from the printed bill exactly.
        """
        kwh_total = round(kwh_total, 1)
        kvah_total = round(kvah_total, 1) if kvah_total is not None else None
        zone_units = {z: round(u, 1) for z, u in zone_units.items()}
        max_demand_kva = round(max_demand_kva, 1)
        zone_charges = self.energy_charges(zone_units)
        energy = sum(zone_charges.values())
        normal, excess = self.demand_charges(max_demand_kva, contract_demand_kva)
        pf = round(kwh_total / kvah_total, 3) if kvah_total else None
        pf_adj = 0.0
        if self.pf_rule is not None and pf is not None and self.energy_basis == "kWh":
            pf_adj = energy * self.pf_rule.adjustment_pct(pf) / 100
        fixed = self.fixed_charges
        duty = (energy + normal + excess + pf_adj + fixed) * self.electricity_duty_pct / 100
        total = energy + normal + excess + pf_adj + fixed + duty
        return BillDocument(
            consumer_number=consumer_number,
            discom=self.discom,
            tariff_category=self.name,
            period_start=period_start,
            period_end=period_end,
            energy_basis=self.energy_basis,
            kwh_total=kwh_total,
            kvah_total=kvah_total,
            zones=[ZoneReading(zone=z, units=u, charges=round(zone_charges[z], 2)) for z, u in zone_units.items()],
            max_demand_kva=max_demand_kva,
            contract_demand_kva=contract_demand_kva,
            billing_demand_kva=round(self.billing_demand(max_demand_kva, contract_demand_kva), 1),
            power_factor=pf,
            energy_charges=round(energy, 2),
            demand_charges=round(normal, 2),
            excess_demand_charges=round(excess, 2),
            pf_adjustment=round(pf_adj, 2),
            fixed_charges=round(fixed, 2),
            electricity_duty=round(duty, 2),
            other_charges=0.0,
            total_amount=round(total, 2),
        )

    def simulate_bill(
        self,
        survey: pd.DataFrame,
        period_start: date,
        period_end: date,
        contract_demand_kva: float,
        consumer_number: str | None = None,
    ) -> BillDocument:
        """Bill a 15-minute load survey (columns kwh, kvah) under this tariff."""
        mask = (survey.index.date >= period_start) & (survey.index.date <= period_end)
        part = survey.loc[mask]
        units_col = "kvah" if self.energy_basis == "kVAh" else "kwh"
        bands = self.band_series(part.index)
        zone_units = part[units_col].groupby(bands).sum().to_dict()
        zone_units = {name: float(zone_units.get(name, 0.0)) for name in self.band_names}
        max_kva = float((part["kvah"] / SLOT_HOURS).max())
        return self.price_bill(
            period_start=period_start,
            period_end=period_end,
            kwh_total=float(part["kwh"].sum()),
            kvah_total=float(part["kvah"].sum()),
            zone_units=zone_units,
            max_demand_kva=max_kva,
            contract_demand_kva=contract_demand_kva,
            consumer_number=consumer_number,
        )

    def reprice(self, bill: BillDocument) -> BillDocument:
        """What the bill should have been under this tariff, from its printed quantities."""
        return self.price_bill(
            period_start=bill.period_start,
            period_end=bill.period_end,
            kwh_total=bill.kwh_total,
            kvah_total=bill.kvah_total,
            zone_units=bill.zone_units(),
            max_demand_kva=bill.max_demand_kva,
            contract_demand_kva=bill.contract_demand_kva,
            consumer_number=bill.consumer_number,
        )


def _parse_tariff(raw: dict) -> Tariff:
    tod = raw.get("tod", {})
    pf = raw.get("power_factor")
    demand = raw.get("demand", {})
    return Tariff(
        id=raw["id"],
        name=raw["name"],
        state=raw.get("state", ""),
        discom=raw.get("discom", ""),
        effective_from=pd.Timestamp(raw["effective_from"]).date(),
        effective_to=pd.Timestamp(raw["effective_to"]).date() if raw.get("effective_to") else None,
        status=raw.get("status", "verified"),
        source=raw.get("source", ""),
        notes=" ".join(str(raw.get("notes", "")).split()),
        energy_basis=raw.get("energy_basis", "kWh"),
        energy_rate=float(raw["energy_rate"]),
        default_band=tod.get("default_band", "normal"),
        default_multiplier=float(tod.get("default_multiplier", 1.0)),
        bands=tuple(
            TodBand(b["name"], hhmm_to_minutes(b["start"]), hhmm_to_minutes(b["end"]) % 1440, float(b["multiplier"]))
            for b in tod.get("bands", [])
        ),
        demand_rate=float(demand.get("rate_per_kva_month", 0.0)),
        billing_floor_pct=float(demand.get("billing_floor_pct_of_contract", 0.0)),
        excess_surcharge_pct=float(demand.get("excess_surcharge_pct", 0.0)),
        pf_rule=(
            PowerFactorRule(
                reference=float(pf["reference"]),
                penalty_pct_per_0_01=float(pf.get("penalty_pct_per_0_01", 0.0)),
                incentive_threshold=float(pf["incentive_threshold"]) if pf.get("incentive_threshold") else None,
                incentive_pct_per_0_01=float(pf.get("incentive_pct_per_0_01", 0.0)),
            )
            if pf
            else None
        ),
        electricity_duty_pct=float(raw.get("electricity_duty_pct", 0.0)),
        fixed_charges=float(raw.get("fixed_charges_per_month", 0.0)),
    )


@lru_cache(maxsize=None)
def load_all_tariffs(directory: Path = CONFIG_DIR / "tariffs") -> tuple[Tariff, ...]:
    tariffs = []
    for path in sorted(directory.glob("*.yaml")):
        with open(path, encoding="utf-8") as fh:
            tariffs.append(_parse_tariff(yaml.safe_load(fh)))
    return tuple(tariffs)


def get_tariff(tariff_id: str, on: date | None = None) -> Tariff:
    """The version of ``tariff_id`` in force on ``on`` (latest version if not given)."""
    versions = sorted((t for t in load_all_tariffs() if t.id == tariff_id), key=lambda t: t.effective_from)
    if not versions:
        raise KeyError(f"No tariff config with id {tariff_id!r}")
    if on is None:
        return versions[-1]
    for t in reversed(versions):
        if t.effective_from <= on and (t.effective_to is None or on <= t.effective_to):
            return t
    raise KeyError(f"No version of {tariff_id!r} is in force on {on}; a new tariff order may be due.")
