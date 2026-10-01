from datetime import date

import numpy as np
import pandas as pd
import pytest

from unitwatt.schemas import BillDocument, ZoneReading, validate_bill
from unitwatt.tariff import get_tariff


def test_tod_bands(tariff):
    minutes = np.array([3 * 60, 10 * 60, 17 * 60 + 30, 22 * 60 + 59, 23 * 60])
    assert list(tariff.band_for_minutes(minutes)) == ["normal", "solar", "peak", "peak", "normal"]
    assert tariff.multiplier("peak") == pytest.approx(1.2)
    assert tariff.multiplier("solar") == pytest.approx(0.8)
    assert tariff.cheapest_band == "solar" and tariff.costliest_band == "peak"


def test_band_wrapping_past_midnight():
    kerala = get_tariff("kerala-ht-industrial")
    assert list(kerala.band_for_minutes(np.array([23 * 60, 2 * 60, 7 * 60, 19 * 60]))) == ["off-peak", "off-peak", "normal", "peak"]


def test_demand_floor_and_excess_surcharge():
    kerala = get_tariff("kerala-ht-industrial")
    # Billed on 75% of contract demand when actual is lower.
    normal, excess = kerala.demand_charges(max_demand_kva=150, contract_demand_kva=300)
    assert normal == pytest.approx(225 * 500) and excess == 0
    # Above contract demand the excess pays 150% of the rate.
    normal, excess = kerala.demand_charges(max_demand_kva=320, contract_demand_kva=300)
    assert normal == pytest.approx(300 * 500) and excess == pytest.approx(20 * 500 * 1.5)


def test_power_factor_slabs(tariff):
    assert tariff.pf_rule.adjustment_pct(0.86) == pytest.approx(4.0)
    assert tariff.pf_rule.adjustment_pct(0.93) == 0
    assert tariff.pf_rule.adjustment_pct(0.97) == pytest.approx(-1.0)


def test_version_lookup_by_date():
    assert get_tariff("bihar-hts-industrial", on=date(2026, 6, 1)).effective_from == date(2026, 4, 1)
    with pytest.raises(KeyError):
        get_tariff("bihar-hts-industrial", on=date(2025, 6, 1))


def test_simulated_bill_passes_its_own_checks(demo):
    for loaded in demo.bills:
        assert validate_bill(loaded.bill) == []
        assert demo.tariff.reprice(loaded.bill).total_amount == pytest.approx(loaded.bill.total_amount, abs=1.0)


def test_kvah_billing_charges_poor_power_factor(tariff):
    from dataclasses import replace

    kvah_tariff = replace(tariff, energy_basis="kVAh")
    idx = pd.date_range("2026-04-01", periods=96 * 30, freq="15min")
    survey = pd.DataFrame({"kwh": 10.0, "kvah": 10.0 / 0.85}, index=idx)
    kwh_bill = tariff.simulate_bill(survey, date(2026, 4, 1), date(2026, 4, 30), 300)
    kvah_bill = kvah_tariff.simulate_bill(survey, date(2026, 4, 1), date(2026, 4, 30), 300)
    assert kvah_bill.pf_adjustment == 0  # no separate penalty line under kVAh billing...
    assert kvah_bill.energy_charges == pytest.approx(kwh_bill.energy_charges / 0.85, rel=1e-4)  # ...every unit costs more


def _bill(**overrides):
    base = dict(
        period_start=date(2026, 4, 1), period_end=date(2026, 4, 30), energy_basis="kWh", kwh_total=1000, kvah_total=1100,
        zones=[ZoneReading(zone="peak", units=400), ZoneReading(zone="normal", units=600)], max_demand_kva=100,
        contract_demand_kva=150, power_factor=0.909, energy_charges=8000, demand_charges=5000, electricity_duty=780,
        total_amount=13780,
    )
    return BillDocument(**{**base, **overrides})


def test_validation_accepts_consistent_bill():
    assert validate_bill(_bill()) == []


@pytest.mark.parametrize(
    "overrides, field",
    [
        ({"zones": [ZoneReading(zone="peak", units=400), ZoneReading(zone="normal", units=500)]}, "zones"),
        ({"kvah_total": 900, "power_factor": None}, "kvah_total"),
        ({"total_amount": 15000}, "total_amount"),
        ({"period_end": date(2026, 3, 1)}, "period_end"),
    ],
)
def test_validation_catches_extraction_errors(overrides, field):
    issues = validate_bill(_bill(**overrides))
    assert any(i.field == field and i.severity == "error" for i in issues)


def test_bill_forensics_finds_the_planted_penalties(demo):
    kinds = {(f.month, f.kind) for f in demo.findings if f.is_loss}
    assert ("May 2026", "excess_demand") in kinds
    assert ("Jun 2026", "pf_penalty") in kinds
    excess = next(f for f in demo.findings if f.kind == "excess_demand")
    assert "19 May" in excess.detail  # traced to the restart spike in the load survey
