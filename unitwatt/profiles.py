"""Loaders for factory profiles, sector templates and conversion factors (P5, P20).

Everything that differs between factories, sectors and states lives in YAML under
``unitwatt/config``; this module only turns it into typed objects.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml

CONFIG_DIR = Path(__file__).parent / "config"


@dataclass(frozen=True)
class Product:
    id: str
    name: str
    name_hi: str
    hsn: str
    heat_treated: bool
    aliases: tuple[str, ...]


@dataclass(frozen=True)
class Machine:
    id: str
    name: str
    rated_kw: float
    shiftable: bool


@dataclass(frozen=True)
class Factory:
    id: str
    name: str
    city: str
    state: str
    sector: str
    tariff_id: str
    consumer_number: str
    contract_demand_kva: float
    owner_name: str
    language: str
    shift_start: str
    shift_end: str
    working_weekdays: tuple[int, ...]
    essential_night_load_kw: float
    products: tuple[Product, ...]
    machines: tuple[Machine, ...]
    precursor: dict[str, Any]
    schedule_day: dict[str, Any]
    sector_template: dict[str, Any] = field(repr=False)
    workers: int = 0

    @property
    def product_ids(self) -> list[str]:
        return [p.id for p in self.products]

    def product(self, product_id: str) -> Product:
        for p in self.products:
            if p.id == product_id:
                return p
        raise KeyError(product_id)


@dataclass(frozen=True)
class FuelFactor:
    key: str
    label: str
    unit: str
    density_kg_per_unit: float
    ncv_mj_per_kg: float
    co2_kg_per_tj: float
    source: str
    biogenic: bool = False

    @property
    def mj_per_unit(self) -> float:
        return self.density_kg_per_unit * self.ncv_mj_per_kg

    @property
    def kg_co2_per_unit(self) -> float:
        return self.mj_per_unit * 1e-6 * self.co2_kg_per_tj

    @property
    def kwh_eq_per_unit(self) -> float:
        return self.mj_per_unit / 3.6


@dataclass(frozen=True)
class Factors:
    grid_tco2_per_mwh: float
    grid_source: str
    fuels: dict[str, FuelFactor]

    @property
    def grid_kg_per_kwh(self) -> float:
        return self.grid_tco2_per_mwh

    def with_fuel_override(self, key: str, **changes: float | str) -> "Factors":
        """Replace a default fuel factor with a value from a supplier test certificate."""
        base = self.fuels[key]
        updated = FuelFactor(**{**base.__dict__, **changes})
        return Factors(self.grid_tco2_per_mwh, self.grid_source, {**self.fuels, key: updated})


def _read_yaml(path: Path) -> dict[str, Any]:
    with open(path, encoding="utf-8") as fh:
        return yaml.safe_load(fh)


@lru_cache(maxsize=None)
def load_sector(sector: str) -> dict[str, Any]:
    return _read_yaml(CONFIG_DIR / "sectors" / f"{sector}.yaml")


def load_factory(path_or_id: str | Path = "demo_forge") -> Factory:
    path = Path(path_or_id)
    if not path.suffix:
        path = CONFIG_DIR / "factories" / f"{path_or_id}.yaml"
    raw = _read_yaml(path)
    products = tuple(
        Product(
            id=p["id"],
            name=p["name"],
            name_hi=p.get("name_hi", p["name"]),
            hsn=str(p.get("hsn", "")),
            heat_treated=bool(p.get("heat_treated", False)),
            aliases=tuple(str(a).lower() for a in p.get("aliases", [p["id"]])),
        )
        for p in raw["products"]
    )
    machines = tuple(
        Machine(m["id"], m["name"], float(m["rated_kw"]), bool(m.get("shiftable", False)))
        for m in raw.get("machines", [])
    )
    return Factory(
        id=raw["id"],
        name=raw["name"],
        city=raw.get("city", ""),
        state=raw.get("state", ""),
        sector=raw["sector"],
        tariff_id=raw["tariff_id"],
        consumer_number=str(raw.get("consumer_number", "")),
        contract_demand_kva=float(raw["contract_demand_kva"]),
        owner_name=raw.get("owner_name", ""),
        language=raw.get("language", "en"),
        shift_start=raw["shift_window"]["start"],
        shift_end=raw["shift_window"]["end"],
        working_weekdays=tuple(raw.get("working_weekdays", [0, 1, 2, 3, 4, 5])),
        essential_night_load_kw=float(raw.get("essential_night_load_kw", 0.0)),
        products=products,
        machines=machines,
        precursor=raw.get("precursor", {}),
        schedule_day=raw.get("schedule_day", {}),
        sector_template=load_sector(raw["sector"]),
        workers=int(raw.get("workers", 0)),
    )


@lru_cache(maxsize=None)
def load_factors() -> Factors:
    raw = _read_yaml(CONFIG_DIR / "factors.yaml")
    fuels = {
        key: FuelFactor(
            key=key,
            label=f["label"],
            unit=f["unit"],
            density_kg_per_unit=float(f["density_kg_per_unit"]),
            ncv_mj_per_kg=float(f["ncv_mj_per_kg"]),
            co2_kg_per_tj=float(f["co2_kg_per_tj"]),
            source=f["source"],
            biogenic=bool(f.get("biogenic", False)),
        )
        for key, f in raw["fuels"].items()
    }
    return Factors(
        grid_tco2_per_mwh=float(raw["grid"]["emission_factor_tco2_per_mwh"]),
        grid_source=raw["grid"]["source"],
        fuels=fuels,
    )


def hhmm_to_minutes(value: str) -> int:
    """'07:30' -> 450. '24:00' is allowed as the end of the day."""
    hours, minutes = value.split(":")
    return int(hours) * 60 + int(minutes)
