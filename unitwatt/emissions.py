"""Stage 5: product-level embedded emissions (P16).

Scope 1 = fuel x calorific value x emission factor (diesel for the generator, furnace oil...)
Scope 2 = grid kWh x CEA grid emission factor
Both are allocated to products with the P8 energy model, so the product totals always add
back up to the plant's metered total. Purchased precursors (steel billets) are added from
supplier data or EU default values.

CBAM: for iron, steel and aluminium only direct emissions (and precursors) are priced; the
electricity emissions are still reported because buyers ask for them for their Scope 3.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

import pandas as pd

from unitwatt.analytics import EnergyModel
from unitwatt.profiles import Factors, Factory


@dataclass
class EmissionsResult:
    period: tuple[date, date]
    products: pd.DataFrame   # one row per product
    plant: dict[str, float]
    notes: list[str]


def cbam_covered(hsn: str, prefixes: list[str]) -> bool:
    return any(str(hsn).startswith(p) for p in prefixes)


def product_emissions(
    ledger: pd.DataFrame,
    model: EnergyModel,
    factory: Factory,
    factors: Factors,
    start: date,
    end: date,
) -> EmissionsResult:
    period = ledger[(ledger.index >= start) & (ledger.index <= end)]
    products = factory.product_ids
    tonnes = {p: float(period[f"t_{p}"].fillna(0).sum()) for p in products}

    elec_total = float(period["elec_kwh"].sum())
    grid_total = float(period["grid_kwh"].sum())
    diesel_total = float(period["diesel_l"].sum())
    grid_share = grid_total / elec_total if elec_total else 1.0

    # Electricity: each product's marginal energy from the model, then the fixed and residual
    # energy shared in proportion, so the allocation reconciles to the meter exactly.
    variable = {p: model.coef[p] * tonnes[p] for p in products}
    var_total = sum(variable.values())
    elec_alloc = {p: elec_total * variable[p] / var_total if var_total else 0.0 for p in products}

    # Furnace oil and other monthly fuels: heat-treated products, by tonnes.
    fuel_cols = ledger.attrs.get("fuels", [])
    heat_treated = [p.id for p in factory.products if p.heat_treated]
    ht_tonnes = sum(tonnes[p] for p in heat_treated)

    diesel_f = factors.fuels["diesel"]
    rows = []
    for prod in factory.products:
        p = prod.id
        share = elec_alloc[p] / elec_total if elec_total else 0.0
        scope2 = elec_alloc[p] * grid_share * factors.grid_kg_per_kwh / 1000
        scope1 = diesel_total * share * diesel_f.kg_co2_per_unit / 1000
        fuel_mj = diesel_total * share * diesel_f.mj_per_unit
        for fuel in fuel_cols:
            f = factors.fuels[fuel]
            units = float(period[f"{fuel}_units"].sum()) * (tonnes[p] / ht_tonnes if prod.heat_treated and ht_tonnes else 0.0)
            fuel_mj += units * f.mj_per_unit
            if not f.biogenic:
                scope1 += units * f.kg_co2_per_unit / 1000
        precursor_t = tonnes[p] * float(factory.precursor.get("yield_t_input_per_t_output", 0.0))
        precursor = precursor_t * float(factory.precursor.get("tco2_per_t", 0.0))
        covered = cbam_covered(prod.hsn, factory.sector_template.get("cbam", {}).get("covered_hsn_prefixes", []))
        t = tonnes[p] or float("nan")
        rows.append(
            {
                "product": p,
                "name": prod.name,
                "hsn": prod.hsn,
                "cbam_covered": covered,
                "tonnes": tonnes[p],
                "electricity_kwh": elec_alloc[p],
                "fuel_mj": fuel_mj,
                "scope1_t": scope1,
                "scope2_t": scope2,
                "precursor_t": precursor,
                "kwh_per_t": elec_alloc[p] / t,
                "direct_tco2_per_t": scope1 / t,
                "indirect_tco2_per_t": scope2 / t,
                "precursor_tco2_per_t": precursor / t,
                "embedded_tco2_per_t": (scope1 + scope2 + precursor) / t,
                "cbam_priced_tco2_per_t": (scope1 + precursor) / t if covered else float("nan"),
            }
        )
    df = pd.DataFrame(rows).set_index("product")
    plant = {
        "electricity_kwh": elec_total,
        "grid_kwh": grid_total,
        "diesel_l": diesel_total,
        "scope1_t": float(df["scope1_t"].sum()),
        "scope2_t": float(df["scope2_t"].sum()),
        "precursor_t": float(df["precursor_t"].sum()),
        "tonnes": sum(tonnes.values()),
    }
    notes = [
        f"Scope 2 uses the grid factor {factors.grid_tco2_per_mwh} tCO2/MWh ({factors.grid_source}).",
        "Electricity is allocated to products with the regression model (P8) and reconciles to the meter total.",
        f"Precursor: {factory.precursor.get('material', 'purchased material')}, {factory.precursor.get('source', '')}.",
        "For iron and steel, CBAM prices direct emissions and precursors only; electricity emissions are reported for buyers' Scope 3.",
    ]
    return EmissionsResult((start, end), df, plant, notes)


def default_value_comparison(actual_tco2_per_t: float, default_tco2_per_t: float, year: int, markups: dict) -> dict[str, float]:
    """Declared emissions with EU default values (plus that year's markup) against verified actuals."""
    markup = float(markups.get(year, markups.get(str(year), 0))) / 100
    declared_default = default_tco2_per_t * (1 + markup)
    return {
        "actual": actual_tco2_per_t,
        "default_with_markup": declared_default,
        "markup_pct": markup * 100,
        "excess_tco2_per_t": declared_default - actual_tco2_per_t,
    }
