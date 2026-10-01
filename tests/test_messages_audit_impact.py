import sqlite3
from types import SimpleNamespace

import pytest

from unitwatt.audit import AuditLog
from unitwatt.extract import FALLBACK_BETA, MODEL, ExtractionError, extract_bill
from unitwatt.messages import format_inr, format_lakh, numbers_in, owner_message
from unitwatt.opportunities import ImpactAssumptions, impact_model
from unitwatt.schemas import BillDocument


def test_indian_number_format():
    assert format_inr(614400) == "6,14,400"
    assert format_inr(999) == "999"
    assert format_inr(61_44_00_000) == "61,44,00,000"
    assert format_lakh(614400) == "₹6.14 lakh"
    assert format_lakh(6.144e8) == "₹61.44 crore"


@pytest.mark.parametrize("lang", ["en", "hi"])
def test_every_number_in_the_message_was_computed(demo, lang):
    facts = demo.owner_facts
    text = owner_message(facts, lang)
    allowed = set(facts.fields().values()) | {"1", "2"}  # the two reply options
    assert numbers_in(text) <= allowed, numbers_in(text) - allowed


def test_hindi_message_is_hindi(demo):
    text = owner_message(demo.owner_facts, "hi")
    assert "नमस्ते" in text and "सितंबर" in text


def test_audit_chain_detects_tampering(tmp_path):
    path = tmp_path / "audit.sqlite"
    log = AuditLog(path)
    log.add_document("electricity_bill", "bill.json", b'{"total": 100}')
    log.add_event("analysis", "run", {"x": 1})
    assert log.verify() == (True, None)
    with pytest.raises(sqlite3.DatabaseError):
        log.conn.execute("UPDATE entries SET name = 'other.json' WHERE id = 1")
    log.conn.rollback()
    # Someone with raw database access drops the trigger and edits a row.
    raw = sqlite3.connect(path)
    raw.execute("DROP TRIGGER entries_no_update")
    raw.execute("UPDATE entries SET payload = '{\"size_bytes\": 1}' WHERE id = 1")
    raw.commit()
    assert log.verify() == (False, 1)


def test_impact_model_reproduces_the_deep_dive():
    r = impact_model(ImpactAssumptions(), units=1000)
    assert r["tod_rs_month"] == pytest.approx(32_000)
    assert r["idle_rs_month"] == pytest.approx(19_200)
    assert r["total_rs_month"] == pytest.approx(51_200)
    assert r["total_rs_year"] == pytest.approx(614_400)
    assert r["tco2_year"] == pytest.approx(19.44)
    assert r["fleet_rs_year"] == pytest.approx(6.144e8)
    half = impact_model(ImpactAssumptions(), units=1000, shift_realisation=0.5)
    assert half["fleet_tco2_year"] == r["fleet_tco2_year"]  # shifting saves no carbon


class _FakeMessages:
    def __init__(self, response):
        self.response, self.kwargs = response, None

    def parse(self, **kwargs):
        self.kwargs = kwargs
        return self.response


def test_bill_extraction_request_and_validation(demo):
    bill = demo.bill_documents[0]
    fake = _FakeMessages(SimpleNamespace(stop_reason="end_turn", parsed_output=bill, model=MODEL))
    result = extract_bill(b"%PDF-1.4", "application/pdf", client=SimpleNamespace(beta=SimpleNamespace(messages=fake)))
    assert result.bill == bill and result.issues == []
    assert fake.kwargs["model"] == MODEL and fake.kwargs["output_format"] is BillDocument
    assert fake.kwargs["betas"] == [FALLBACK_BETA] and fake.kwargs["fallbacks"] == "default"
    assert fake.kwargs["messages"][0]["content"][0]["type"] == "document"


def test_bill_extraction_refusal():
    fake = _FakeMessages(SimpleNamespace(stop_reason="refusal", parsed_output=None, model=MODEL))
    with pytest.raises(ExtractionError):
        extract_bill(b"\x89PNG", "image/png", client=SimpleNamespace(beta=SimpleNamespace(messages=fake)))
