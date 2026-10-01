import re

import pytest

from unitwatt.mv import fractional_savings_uncertainty
from unitwatt.report import emissions_statement_html, savings_report_html


def test_savings_are_found_and_pass_the_statistical_checks(demo):
    s = demo.savings
    truth = demo.synthetic.truth
    idle_cut_kwh = (truth["idle_kw_before"] - truth["idle_kw_after"]) * 24 * 92
    # The idle cut is the only energy saving; the September drift eats into it.
    assert 0.6 * idle_cut_kwh < s.avoided_kwh < 1.05 * idle_cut_kwh
    assert s.model_acceptable
    assert s.uncertainty_kwh < 0.5 * s.avoided_kwh
    assert not s.meets_adeetie and s.gap_to_target_kwh > 0


def test_no_savings_claimed_when_nothing_changed(no_change_demo):
    s = no_change_demo.savings
    assert abs(s.savings_pct) < 0.01


def test_fsu_formula():
    fsu, n_eff = fractional_savings_uncertainty(cv_rmse=0.1, n=90, m=90, rho=0.0, savings_fraction=0.1, dof=85)
    assert n_eff == 90
    assert fsu == pytest.approx(1.663 * 1.26 * 0.1 * ((1 + 2 / 90) / 90) ** 0.5 / 0.1, rel=0.01)
    worse, _ = fractional_savings_uncertainty(0.1, 90, 90, 0.5, 0.1, 85)
    assert worse > fsu


def test_emissions_reconcile_to_the_plant(demo):
    e = demo.emissions
    assert e.products["electricity_kwh"].sum() == pytest.approx(e.plant["electricity_kwh"], rel=1e-9)
    assert e.plant["scope2_t"] == pytest.approx(e.plant["grid_kwh"] * 0.675 / 1000, rel=1e-9)
    covered = dict(zip(e.products.index, e.products["cbam_covered"]))
    assert covered == {"flange": True, "crank": False, "gear": True}


def test_reports_carry_hashes_and_quality(demo):
    html = savings_report_html(demo, ["Leak repair"])
    for loaded in demo.bills:
        entry = demo.audit.find(loaded.source)
        assert entry is not None and entry.sha256 in html
    assert "Data quality score" in html and "ADEETIE" in html
    statement = emissions_statement_html(demo)
    assert "73079190" in statement and re.search(r"CBAM", statement)
