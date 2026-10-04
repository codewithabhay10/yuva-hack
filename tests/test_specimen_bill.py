import json
from pathlib import Path

import pytest

from unitwatt.schemas import BillDocument, validate_bill
from unitwatt.specimen_bill import amount_in_words, inr_paise, specimen_bill
from unitwatt.tariff import get_tariff

SAMPLES = Path(__file__).resolve().parent.parent / "data" / "sample_bills"


def test_amount_in_words():
    assert amount_in_words(635981.94) == "Rupees Six Lakh Thirty Five Thousand Nine Hundred Eighty One and Ninety Four Paise Only"
    assert amount_in_words(10_00_00_000) == "Rupees Ten Crore Only"
    assert amount_in_words(1_05_000.5) == "Rupees One Lakh Five Thousand and Fifty Paise Only"


def test_indian_grouping_with_paise():
    assert inr_paise(635981.94) == "6,35,981.94"
    assert inr_paise(0) == "0.00"
    assert inr_paise(-1234.5) == "-1,234.50"


def test_specimen_prints_every_figure_the_extractor_must_return():
    html, bill = specimen_bill("2026-06")
    assert validate_bill(bill) == []
    for value in [bill.total_amount, bill.energy_charges, bill.demand_charges, bill.pf_adjustment, bill.electricity_duty]:
        assert inr_paise(value) in html
    for zone in bill.zones:
        assert f"{zone.units:,.1f}" in html
    assert bill.consumer_number in html and "SPECIMEN" in html and "fictional" in html
    assert "NBPDCL" not in html and "SBPDCL" not in html  # never a real DISCOM's name


def test_committed_sample_matches_its_ground_truth():
    truth = BillDocument.model_validate(json.loads((SAMPLES / "specimen_bill_2026_06.json").read_text()))
    _, bill = specimen_bill("2026-06")
    assert truth == bill
    assert get_tariff("bihar-hts-industrial").reprice(truth).total_amount == pytest.approx(truth.total_amount, abs=0.01)
