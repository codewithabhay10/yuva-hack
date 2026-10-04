from datetime import date, datetime
from pathlib import Path

import pytest

from unitwatt.bot import Bot, BotStore, Incoming
from unitwatt.bot.core import IST
from unitwatt.bot.transcribe import SpeechToText, TranscriptionError, transcribe
from unitwatt.messages import format_inr
from unitwatt.pipeline import run_demo
from unitwatt.profiles import parse_contacts

from http_fakes import FakeHTTP

SAMPLES = Path(__file__).resolve().parent.parent / "data" / "sample_bills"
OWNER, SUPERVISOR, ACCOUNTANT, STRANGER = "910000000001", "910000000002", "910000000003", "919999999999"


@pytest.fixture(scope="module")
def results(tmp_path_factory):
    return run_demo(seed=7, workdir=tmp_path_factory.mktemp("bot"))


@pytest.fixture
def bot(results):
    return Bot(results, BotStore(), reader="offline", now=lambda: datetime(2026, 10, 4, 10, 0, tzinfo=IST))


def say(bot, sender, text="", **kw):
    return bot.handle(Incoming(sender=sender, text=text, **kw))


def test_unknown_numbers_get_nothing_but_a_refusal(bot):
    (reply,) = say(bot, STRANGER, "1")
    assert "not registered" in reply.text and "पंजीकृत नहीं" in reply.text
    assert bot.store.messages().empty and bot.store.session(STRANGER) == {}


def test_menu_follows_each_role(bot):
    owner, supervisor, accountant = (say(bot, p, "hi")[0].text for p in (OWNER, SUPERVISOR, ACCOUNTANT))
    assert all(line in owner for line in ("1 ", "2 ", "3 ", "4 "))
    assert "3 कल का उत्पादन प्लान" in supervisor and "1 इस महीने" not in supervisor and "2 बैंक" not in supervisor
    assert "1 This month's report" in accountant and "3 Tomorrow" not in accountant  # Priya's profile language is English


def test_report_details_and_language_switch(bot, results):
    (report,) = say(bot, OWNER, "१")  # Devanagari digit
    assert report.text.startswith("नमस्ते Ramesh जी") and f"₹{format_inr(results.owner_facts.rupees_lost_month)}" in report.text
    assert ("details", "🔍 विवरण") in report.buttons
    (details,) = say(bot, OWNER, "1")  # "1" right after the report means details
    assert details.text.startswith("🔍") and results.opportunities_latest[0].title_hi in details.text
    (switched,) = say(bot, OWNER, "english")
    assert switched.text.startswith("Language set to English") and say(bot, OWNER, "1")[0].text.startswith("Namaste Ramesh ji")


def test_plan_and_alerts_use_computed_numbers(bot, results):
    plan = results.schedules["optimised"]
    saving = (results.schedules["current"].total_rs - plan.total_rs) * results.problem.working_days
    (text,) = [r.text for r in say(bot, OWNER, "plan")]
    assert f"₹{format_inr(saving)} a month" not in text  # owner's language is Hindi
    assert f"₹{format_inr(saving)} हर महीने" in text and "☀️" in text and f"{plan.peak_kva:.0f} kVA" in text
    (alerts,) = say(bot, SUPERVISOR, "4")
    assert "12 सितंबर" in alerts.text and f"{results.drift.recent_excess_pct * 100:.1f}%" in alerts.text


def test_production_entry_is_confirmed_before_saving(bot):
    (ask,) = say(bot, SUPERVISOR, "फ्लेंज १.५ टन, crank 1.2t, gear 1400 kg, 2 shifts")
    assert "4 अक्टूबर" in ask.text and "फ्लेंज: 1.5 टन" in ask.text and ("yes", "✅ सेव करें") in ask.buttons
    assert bot.store.production_frame().empty
    (saved,) = say(bot, SUPERVISOR, "yes")
    assert saved.text.startswith("✅")
    rows = bot.store.production_frame()
    assert dict(zip(rows["product"], rows["tonnes"])) == {"flange": 1.5, "crank": 1.2, "gear": 1.4}
    assert set(rows["day"]) == {"2026-10-04"} and set(rows["shifts"]) == {2} and set(rows["source"]) == {"text"}


def test_yesterday_and_cancel(bot):
    (ask,) = say(bot, SUPERVISOR, "yesterday flange 2t")
    assert "3 अक्टूबर" in ask.text
    (cancelled,) = say(bot, SUPERVISOR, "no")
    assert "रद्द" in cancelled.text and bot.store.production_frame().empty


def test_roles_are_enforced(bot):
    assert "उपलब्ध नहीं" in say(bot, SUPERVISOR, "2")[0].text
    assert "isn't available" in say(bot, ACCOUNTANT, "flange 2t")[0].text
    assert bot.store.production_frame().empty


def test_bill_pdf_is_read_checked_and_saved(bot, results):
    pdf = (SAMPLES / "specimen_bill_2026_06.pdf").read_bytes()
    (summary,) = say(bot, ACCOUNTANT, kind="document", media=pdf, mime="application/pdf", filename="june.pdf")
    assert "Bill for June 2026, read with PDF text layer" in summary.text
    assert "✅ All arithmetic checks pass" in summary.text and "Power-factor penalty" in summary.text
    assert bot.store.bills_frame().empty
    (saved,) = say(bot, ACCOUNTANT, "yes")
    assert saved.text.startswith("✅ Bill for June 2026 saved")
    assert bot.store.bills_frame()["total"].tolist() == [635981.94]
    assert results.audit.find("june.pdf") is not None and results.audit.verify()[0]


def test_another_connections_bill_is_flagged(bot):
    pdf = (SAMPLES / "specimen_bill_layout_b.pdf").read_bytes()
    (summary,) = say(bot, OWNER, kind="document", media=pdf, mime="application/pdf", filename="b.pdf")
    assert "SYN-HT-55021" in summary.text and "SYN-BR-000123" in summary.text and "नुकसान" not in summary.text


def test_savings_report_needs_the_owners_consent(bot, results):
    (ask,) = say(bot, OWNER, "2")
    assert "सहमति" in ask.text and ask.document is None
    (sent,) = say(bot, OWNER, "हाँ")
    name, content, mime = sent.document
    assert name.endswith(".pdf") and mime == "application/pdf" and content.startswith(b"%PDF")
    consents = results.audit.frame().query("kind == 'event:consent'")
    assert "Ramesh" in consents.iloc[-1]["payload"]


def test_voice_note_goes_through_speech_to_text(bot):
    fake = FakeHTTP().route("POST", "/v1/audio/transcriptions", lambda r: (200, {"text": "flange 1.5 ton, gear 1.2 ton"})).start()
    try:
        bot.stt = SpeechToText("Test STT", f"{fake.url}/v1", "whisper-test")
        (reply,) = say(bot, SUPERVISOR, kind="audio", media=b"OggS-voice", mime="audio/ogg; codecs=opus")
        assert reply.text.startswith("🎙️ मैंने सुना: “flange 1.5 ton, gear 1.2 ton”") and "फ्लेंज: 1.5 टन" in reply.text
        (call,) = fake.requests
        assert call.form_field("model") == b"whisper-test" and call.form_field("file") == b"OggS-voice"
        assert b"flange" in call.form_field("prompt")
        say(bot, SUPERVISOR, "yes")
        assert set(bot.store.production_frame()["source"]) == {"voice"}
    finally:
        fake.stop()


def test_voice_without_speech_to_text_asks_to_type(bot, monkeypatch):
    for key in ("GROQ_API_KEY", "UNITWATT_STT", "UNITWATT_STT_BASE_URL"):
        monkeypatch.delenv(key, raising=False)
    (reply,) = say(bot, SUPERVISOR, kind="audio", media=b"x", mime="audio/ogg")
    assert "GROQ_API_KEY" in reply.text and "फ्लेंज 1.5 टन" in reply.text


def test_transcription_errors_are_explained():
    fake = FakeHTTP().route("POST", "/v1/audio/transcriptions", lambda r: (429, {"error": "rate"})).start()
    try:
        with pytest.raises(TranscriptionError, match="free-tier limit"):
            transcribe(b"x", "audio/ogg", SpeechToText("Test", f"{fake.url}/v1", "m"))
    finally:
        fake.stop()


def test_memory_survives_a_restart(results, tmp_path):
    db = tmp_path / "bot.sqlite"
    first = Bot(results, BotStore(db))
    first.handle(Incoming(sender=SUPERVISOR, text="flange 2t"))
    second = Bot(results, BotStore(db))  # a fresh process, same database
    assert second.handle(Incoming(sender=SUPERVISOR, text="yes"))[0].text.startswith("✅")
    assert len(second.store.production_frame()) == 1


def test_a_crash_still_gets_an_answer(bot, monkeypatch):
    monkeypatch.setattr(bot, "_plan", lambda lang: 1 / 0)
    (reply,) = say(bot, OWNER, "3")
    assert "गड़बड़" in reply.text


def test_contacts_from_the_environment():
    (c,) = parse_contacts("98765 43210:owner:Asha:en")
    assert (c.phone, c.role, c.name, c.language) == ("919876543210", "owner", "Asha", "en")
    with pytest.raises(ValueError, match="role"):
        parse_contacts("9876543210:boss:Asha")


def test_dates_are_iso_in_storage(bot):
    say(bot, OWNER, "flange 1t")
    say(bot, OWNER, "1")
    assert date.fromisoformat(bot.store.production_frame()["day"].iloc[0]) == date(2026, 10, 4)
