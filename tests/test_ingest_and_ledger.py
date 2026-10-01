from datetime import date

import numpy as np
import pandas as pd
import pytest

from unitwatt import ingest


def test_portal_and_meter_formats_give_the_same_series(tmp_path):
    idx = pd.date_range("2026-04-01", periods=96 * 2, freq="15min")
    kwh = np.arange(len(idx), dtype=float) / 10
    portal = pd.DataFrame({"Date": idx.strftime("%d-%m-%Y"), "Time": idx.strftime("%H:%M"), "kWh": kwh, "kVAh": kwh * 1.1})
    meter = pd.DataFrame({"Timestamp": (idx + pd.Timedelta(minutes=15)).strftime("%Y-%m-%dT%H:%M:%S"), "KWH_IMP": kwh, "KVAH_IMP": kwh * 1.1})
    portal.to_csv(tmp_path / "a.csv", index=False)
    meter.to_csv(tmp_path / "b.csv", index=False)
    a, b = ingest.read_load_survey(tmp_path / "a.csv"), ingest.read_load_survey(tmp_path / "b.csv")
    assert a.quality.source_format == "discom_portal" and b.quality.source_format == "meter_download"
    pd.testing.assert_frame_equal(a.data, b.data)


def test_gaps_are_flagged_not_filled(demo):
    q = demo.survey.quality
    assert [g[2] for g in q.gaps] == [20, 96]
    assert q.n_duplicates == 4 and q.n_conflicting_duplicates == 0
    assert demo.survey.data.loc["2026-08-03", "kwh"].isna().all()


@pytest.mark.parametrize(
    "text, expected",
    [
        ("flange 1.5t, crank 1.2 t, gear 1400 kg, 2 shifts", {"flange": 1.5, "crank": 1.2, "gear": 1.4}),
        ("फ्लेंज १.५ टन, गियर 1.2 टन और क्रैंक 9 क्विंटल", {"flange": 1.5, "gear": 1.2, "crank": 0.9}),
        ("gears 2t; flanges 0.75 tonnes", {"gear": 2.0, "flange": 0.75}),
    ],
)
def test_daily_entry_parser(factory, text, expected):
    parsed = ingest.parse_daily_entry(text, factory)
    assert parsed.tonnes == pytest.approx(expected)
    assert parsed.warnings == []


def test_daily_entry_parser_asks_when_unsure(factory):
    parsed = ingest.parse_daily_entry("4.2 tonne forging, 2 shifts", factory)
    assert parsed.tonnes == {} and parsed.shifts == 2 and parsed.warnings


def test_fuel_stock_flow():
    purchases = pd.DataFrame([{"date": "2026-04-10", "fuel": "furnace_oil", "quantity": 4000}])
    stock = pd.DataFrame([{"date": "2026-03-31", "fuel": "furnace_oil", "closing_stock": 2500},
                          {"date": "2026-04-30", "fuel": "furnace_oil", "closing_stock": 2700}])
    out = ingest.fuel_consumption(purchases, stock)
    assert out.loc[0, "consumption"] == pytest.approx(3800)


def test_fuel_reconciles_with_truth(demo):
    fo = demo.fuel_periods[demo.fuel_periods["fuel"] == "furnace_oil"]["consumption"].sum()
    assert fo == pytest.approx(demo.synthetic.truth["furnace_oil_litres_true"], rel=0.01)
    assert (demo.fuel_periods["flag"] == "").all()


def test_ledger_fills_meter_gaps_from_the_bill(demo):
    truth = demo.synthetic.survey_true["kwh"].groupby(demo.synthetic.survey_true.index.date).sum()
    for day in (date(2026, 5, 12), date(2026, 8, 3)):
        row = demo.ledger.loc[day]
        assert row["estimated"]
        assert row["grid_kwh"] == pytest.approx(truth[day], rel=0.01)
    monthly = demo.ledger.groupby([d.month for d in demo.ledger.index])["grid_kwh"].sum()
    for bill in demo.bill_documents:
        assert monthly[bill.period_start.month] == pytest.approx(bill.kwh_total, rel=1e-4)


def test_missing_register_pages_are_missing_not_zero(demo):
    row = demo.ledger.loc[date(2026, 4, 22)]
    assert not row["production_recorded"] and np.isnan(row["total_t"])


def test_data_quality_score_is_visible_and_bounded(demo):
    assert 0 <= demo.quality.score <= 100
    assert {c.name for c in demo.quality.components} >= {"Meter load survey", "Production register", "Electricity bills"}
