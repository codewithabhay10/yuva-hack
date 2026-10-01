from datetime import date

import pytest

from unitwatt.analytics import fit_energy_model
from unitwatt.synthetic import TRUE_SEC_KWH_PER_T


def test_regression_recovers_energy_per_product(demo):
    m = demo.baseline_model
    for product, truth in TRUE_SEC_KWH_PER_T.items():
        assert m.coef[product] == pytest.approx(truth, rel=0.03)
        lo, hi = m.ci90[product]
        assert lo <= m.coef[product] <= hi
    assert m.coef["base"] / 24 == pytest.approx(demo.synthetic.truth["idle_kw_before"], rel=0.03)
    assert m.cv_rmse < 0.05 and m.r2 > 0.98


def test_short_data_leans_on_the_benchmark_prior(demo):
    m = fit_energy_model(demo.ledger, demo.factory.product_ids, date(2026, 4, 1), date(2026, 4, 20), prior_sec={p: 450.0 for p in TRUE_SEC_KWH_PER_T})
    assert m.method == "regression with benchmark prior" and m.warnings


def test_idle_share_matches_the_deep_dive_assumption(demo):
    idle = demo.idle_baseline
    assert idle.idle_pct == pytest.approx(0.08, abs=0.01)
    assert idle.base_kw == pytest.approx(demo.synthetic.truth["idle_kw_before"], rel=0.05)
    assert demo.idle_latest.base_kw == pytest.approx(demo.synthetic.truth["idle_kw_after"], rel=0.05)


def test_drift_alarm_follows_the_planted_fault(demo):
    alarm = demo.drift.alarm_date
    assert alarm is not None
    assert demo.synthetic.truth["drift_start"] <= alarm <= date(2026, 9, 25)
    assert demo.drift.recent_excess_pct > 0.02


def test_no_false_alarm_without_a_fault(no_change_demo):
    assert no_change_demo.drift.alarm_date is None
