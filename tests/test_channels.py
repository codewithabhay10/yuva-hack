import hashlib
import hmac
import json
from datetime import datetime
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from unitwatt import server
from unitwatt.bot import Bot, BotStore
from unitwatt.bot.core import IST
from unitwatt.bot.telegram import TelegramBot
from unitwatt.bot.whatsapp import WhatsAppClient, WhatsAppConfig, parse_webhook, process_webhook, signature_ok, verify_subscription
from unitwatt.pipeline import run_demo

from http_fakes import FakeHTTP

SAMPLES = Path(__file__).resolve().parent.parent / "data" / "sample_bills"
OWNER, SUPERVISOR, ACCOUNTANT = "910000000001", "910000000002", "910000000003"
PHOTO = (SAMPLES / "specimen_bill_2026_06_photo.jpg").read_bytes()


@pytest.fixture(scope="module")
def results(tmp_path_factory):
    return run_demo(seed=7, workdir=tmp_path_factory.mktemp("channels"))


@pytest.fixture
def bot(results):
    return Bot(results, BotStore(), reader="offline", now=lambda: datetime(2026, 10, 4, 10, 0, tzinfo=IST))


def delivery(*messages) -> dict:
    return {"object": "whatsapp_business_account", "entry": [{"id": "WABA", "changes": [{"field": "messages", "value": {
        "messaging_product": "whatsapp", "metadata": {"phone_number_id": "PNID"}, "messages": list(messages),
        "statuses": [{"id": "wamid.old", "status": "read"}]}}]}]}


def text(sender, body, mid):
    return {"from": sender, "id": mid, "timestamp": "1790000000", "type": "text", "text": {"body": body}}


# --- WhatsApp webhook pieces --------------------------------------------------------------


def test_verification_handshake():
    ok = {"hub.mode": "subscribe", "hub.verify_token": "s3cret", "hub.challenge": "1158201444"}
    assert verify_subscription(ok, "s3cret") == "1158201444"
    assert verify_subscription({**ok, "hub.verify_token": "wrong"}, "s3cret") is None
    assert verify_subscription(ok, "") is None  # no token configured: refuse


def test_signature_check():
    body = b'{"entry":[]}'
    good = "sha256=" + hmac.new(b"app-secret", body, hashlib.sha256).hexdigest()
    assert signature_ok(body, good, "app-secret")
    assert not signature_ok(body, good, "other-secret") and not signature_ok(body, None, "app-secret")
    assert signature_ok(body, None, "")  # not configured: skipped


def test_parse_every_message_type():
    msgs = parse_webhook(delivery(
        text(OWNER, "hi", "m1"),
        {"from": OWNER, "id": "m2", "type": "interactive", "interactive": {"type": "button_reply", "button_reply": {"id": "yes", "title": "✅ Save"}}},
        {"from": ACCOUNTANT, "id": "m3", "type": "image", "image": {"id": "MEDIA1", "mime_type": "image/jpeg", "caption": "June bill"}},
        {"from": SUPERVISOR, "id": "m4", "type": "audio", "audio": {"id": "MEDIA2", "mime_type": "audio/ogg; codecs=opus", "voice": True}},
        {"from": OWNER, "id": "m5", "type": "sticker", "sticker": {"id": "S"}},
    ))
    assert [(m.sender, m.kind, m.text, media) for m, media in msgs] == [
        (OWNER, "text", "hi", None), (OWNER, "text", "yes", None), (ACCOUNTANT, "image", "June bill", "MEDIA1"),
        (SUPERVISOR, "audio", "", "MEDIA2"), (OWNER, "unsupported", "", None)]


@pytest.fixture
def graph():
    """A stand-in for graph.facebook.com: messages, media upload, media lookup and download."""
    fake = FakeHTTP()
    fake.route("POST", r"/v23\.0/PNID/messages", lambda r: (200, {"messages": [{"id": "wamid.out"}]}))
    fake.route("POST", r"/v23\.0/PNID/media", lambda r: (200, {"id": "UPLOADED1"}))
    fake.route("GET", r"/v23\.0/MEDIA1", lambda r: (200, {"url": f"{fake.url}/files/MEDIA1", "mime_type": "image/jpeg"}))
    fake.route("GET", r"/files/MEDIA1", lambda r: (200, PHOTO))
    yield fake.start()
    fake.stop()


def client_for(graph) -> WhatsAppClient:
    return WhatsAppClient(WhatsAppConfig(token="EAAG-test", phone_number_id="PNID", base_url=graph.url))


def sent(graph) -> list[dict]:
    return [r.json for r in graph.calls("POST", r"/v23\.0/PNID/messages") if r.json.get("type")]


def test_menu_goes_out_as_text_with_reply_buttons(bot, graph):
    assert process_webhook(delivery(text(OWNER, "hi", "m1")), bot, client_for(graph)) == 1
    (out,) = sent(graph)
    assert out["to"] == OWNER and out["type"] == "interactive"
    assert out["interactive"]["body"]["text"].startswith("नमस्ते Ramesh जी")
    buttons = out["interactive"]["action"]["buttons"]
    assert [b["reply"]["id"] for b in buttons] == ["1", "3", "4"] and all(len(b["reply"]["title"]) <= 20 for b in buttons)
    assert all(r.headers["Authorization"] == "Bearer EAAG-test" for r in graph.requests)
    reads = [r.json for r in graph.calls("POST", r"/v23\.0/PNID/messages") if r.json.get("status") == "read"]
    assert reads == [{"messaging_product": "whatsapp", "status": "read", "message_id": "m1"}]


def test_redelivered_messages_are_answered_once(bot, graph):
    payload = delivery(text(OWNER, "4", "dup"))
    assert process_webhook(payload, bot, client_for(graph)) == 1
    assert process_webhook(payload, bot, client_for(graph)) == 0
    assert len(sent(graph)) == 1


def test_bill_photo_is_downloaded_read_and_answered(bot, graph):
    image = {"from": ACCOUNTANT, "id": "m3", "type": "image", "image": {"id": "MEDIA1", "mime_type": "image/jpeg"}}
    process_webhook(delivery(image), bot, client_for(graph))
    (out,) = sent(graph)
    body = out["interactive"]["body"]["text"]
    assert "Bill for June 2026, read with OCR" in body and "Power-factor penalty" in body
    assert graph.calls("GET", r"/files/MEDIA1")


def test_savings_report_pdf_is_uploaded_then_sent(bot, graph):
    client = client_for(graph)
    process_webhook(delivery(text(OWNER, "2", "a")), bot, client)
    process_webhook(delivery(text(OWNER, "yes", "b")), bot, client)
    (upload,) = graph.calls("POST", r"/v23\.0/PNID/media")
    assert upload.form_field("messaging_product") == b"whatsapp" and upload.form_field("file").startswith(b"%PDF")
    document = sent(graph)[-1]
    assert document["type"] == "document" and document["document"]["id"] == "UPLOADED1"
    assert document["document"]["filename"] == "unitwatt_savings_report.pdf"


# --- The API server ----------------------------------------------------------------------


@pytest.fixture
def api(bot, graph, monkeypatch):
    monkeypatch.setattr(server, "get_bot", lambda: bot)
    monkeypatch.setattr(server, "whatsapp_client", lambda: client_for(graph))
    for key, value in {"WHATSAPP_TOKEN": "EAAG-test", "WHATSAPP_PHONE_NUMBER_ID": "PNID", "WHATSAPP_VERIFY_TOKEN": "s3cret",
                       "WHATSAPP_APP_SECRET": "app-secret", "UNITWATT_API_TOKEN": "api-key"}.items():
        monkeypatch.setenv(key, value)
    return TestClient(server.app)


def test_webhook_end_to_end(api, graph):
    assert api.get("/webhook/whatsapp", params={"hub.mode": "subscribe", "hub.verify_token": "s3cret", "hub.challenge": "42"}).text == "42"
    assert api.get("/webhook/whatsapp", params={"hub.mode": "subscribe", "hub.verify_token": "no", "hub.challenge": "42"}).status_code == 403
    body = json.dumps(delivery(text(OWNER, "3", "w1"))).encode()
    assert api.post("/webhook/whatsapp", content=body, headers={"X-Hub-Signature-256": "sha256=bad"}).status_code == 401
    signature = "sha256=" + hmac.new(b"app-secret", body, hashlib.sha256).hexdigest()
    response = api.post("/webhook/whatsapp", content=body, headers={"X-Hub-Signature-256": signature, "Content-Type": "application/json"})
    assert response.json() == {"ok": True}
    (out,) = sent(graph)  # the background task has run by the time TestClient returns
    assert "कल का प्लान" in out["text"]["body"]


def test_rest_api_needs_its_token(api, monkeypatch):
    assert api.get("/api/report").status_code == 401
    assert api.get("/api/report", headers={"X-API-Key": "wrong"}).status_code == 401
    report = api.get("/api/report", params={"lang": "en"}, headers={"X-API-Key": "api-key"}).json()
    assert report["text"].startswith("Namaste Ramesh ji")
    monkeypatch.delenv("UNITWATT_API_TOKEN")
    assert api.get("/api/report", headers={"X-API-Key": "api-key"}).status_code == 503


def test_rest_api_schedule_production_and_bill(api, bot):
    key = {"X-API-Key": "api-key"}
    plan = api.get("/api/schedule", headers=key).json()
    assert round(plan["saving_rs_per_month"]) == 44167 and plan["peak_kva"] < 260 and len(plan["batches"]) == 10
    saved = api.post("/api/production", json={"phone": SUPERVISOR, "text": "flange 1.5t, gear 1.2t"}, headers=key).json()
    assert saved["saved"] and saved["tonnes"] == {"flange": 1.5, "gear": 1.2}
    assert api.post("/api/production", json={"phone": ACCOUNTANT, "text": "flange 1t"}, headers=key).status_code == 422
    pdf = (SAMPLES / "specimen_bill_2026_06.pdf").read_bytes()
    read = api.post("/api/bills/read", files={"file": ("june.pdf", pdf, "application/pdf")}, headers=key).json()
    assert read["bill"]["total_amount"] == 635981.94 and read["issues"] == []
    data = api.get("/api/data", headers=key).json()
    assert len(data["production"]) == 2 and data["bills"] == []
    health = api.get("/health").json()
    assert health["whatsapp"] == {"ready": True, "verify_token_set": True, "signature_check": True} and health["rest_api"]


# --- Telegram ----------------------------------------------------------------------------


@pytest.fixture
def telegram():
    fake = FakeHTTP()
    for method in ("sendMessage", "sendDocument", "answerCallbackQuery"):
        fake.route("POST", rf"/botTOKEN/{method}", lambda r: (200, {"ok": True, "result": {"message_id": 1}}))
    fake.route("POST", r"/botTOKEN/getFile", lambda r: (200, {"ok": True, "result": {"file_path": "photos/bill.jpg"}}))
    fake.route("GET", r"/file/botTOKEN/photos/bill\.jpg", lambda r: (200, PHOTO))
    yield fake.start()
    fake.stop()


def tg_sent(fake) -> list[dict]:
    return [r.json for r in fake.calls("POST", r"/botTOKEN/sendMessage")]


def test_telegram_links_a_chat_to_a_contact_then_answers(bot, telegram):
    tg = TelegramBot("TOKEN", bot, base_url=telegram.url)
    tg.handle_update({"update_id": 1, "message": {"message_id": 10, "chat": {"id": 555}, "from": {"id": 7}, "text": "hi"}})
    ask = tg_sent(telegram)[-1]
    assert ask["reply_markup"]["keyboard"][0][0]["request_contact"] is True
    tg.handle_update({"update_id": 2, "message": {"message_id": 11, "chat": {"id": 555}, "from": {"id": 7},
                                                  "contact": {"phone_number": "+91 00000 00001", "user_id": 7}}})
    menu = tg_sent(telegram)[-1]
    assert menu["text"].startswith("नमस्ते Ramesh जी")
    assert [b["callback_data"] for b in menu["reply_markup"]["inline_keyboard"][0]] == ["1", "3", "4"]
    tg.handle_update({"update_id": 3, "callback_query": {"id": "cb1", "data": "4", "message": {"chat": {"id": 555}}}})
    assert "12 सितंबर" in tg_sent(telegram)[-1]["text"] and telegram.calls("POST", r"/botTOKEN/answerCallbackQuery")


def test_telegram_refuses_someone_elses_number(bot, telegram):
    tg = TelegramBot("TOKEN", bot, base_url=telegram.url)
    tg.handle_update({"update_id": 1, "message": {"message_id": 1, "chat": {"id": 9}, "from": {"id": 7},
                                                  "contact": {"phone_number": "910000000001", "user_id": 8}}})
    assert "own number" in tg_sent(telegram)[-1]["text"] and bot.store.linked_phone("tg:9") is None


def test_telegram_photo_and_polling(bot, telegram):
    bot.store.link("tg:777", ACCOUNTANT)
    updates = [{"update_id": 41, "message": {"message_id": 5, "chat": {"id": 777}, "from": {"id": 3},
                                             "photo": [{"file_id": "small"}, {"file_id": "big"}]}}]
    telegram.route("POST", r"/botTOKEN/getUpdates", lambda r: (200, {"ok": True, "result": updates}))
    tg = TelegramBot("TOKEN", bot, base_url=telegram.url)
    assert tg.poll_once() == 1 and tg.offset == 42
    (get_file,) = telegram.calls("POST", r"/botTOKEN/getFile")
    assert get_file.json == {"file_id": "big"}
    reply = tg_sent(telegram)[-1]
    assert "Bill for June 2026" in reply["text"] and [b["callback_data"] for b in reply["reply_markup"]["inline_keyboard"][0]] == ["yes", "no"]
