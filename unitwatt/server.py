"""UnitWatt's API server: the WhatsApp webhook and a small REST API over the same pipeline.

    uvicorn unitwatt.server:app --port 8000

- ``GET /health``: what is configured.
- ``GET, POST /webhook/whatsapp``: Meta's verification handshake and message deliveries.
- ``/api/...``: report, schedule, production entry, bill reading and the data the bot has saved.
  These need ``UNITWATT_API_TOKEN`` set and sent as the ``X-API-Key`` header (P19); without it the
  REST API stays off, so exposing the webhook publicly never exposes the data.

The prototype serves the synthetic demo unit; a pilot points it at a factory's own inputs.
"""

from __future__ import annotations

import hmac
import json
import logging
import os
import threading
from functools import lru_cache
from pathlib import Path

from fastapi import BackgroundTasks, FastAPI, File, Header, HTTPException, Request, UploadFile
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel

from unitwatt import __version__
from unitwatt.bot.core import Bot, Incoming
from unitwatt.bot.store import BotStore
from unitwatt.bot.whatsapp import WhatsAppClient, WhatsAppConfig, process_webhook, signature_ok, verify_subscription

log = logging.getLogger(__name__)
app = FastAPI(title="UnitWatt", version=__version__, description="Energy and carbon ledger for MSME factories")
_lock = threading.Lock()


@lru_cache(maxsize=1)
def get_bot() -> Bot:
    """The analysis and the bot, built once on first use (about 3 seconds for the demo unit)."""
    from unitwatt.pipeline import run_demo

    with _lock:
        db = Path(os.environ.get("UNITWATT_BOT_DB", "data/unitwatt_bot.sqlite"))
        if str(db) != ":memory:":
            db.parent.mkdir(parents=True, exist_ok=True)
        return Bot(run_demo(), BotStore(db))


def whatsapp_config() -> WhatsAppConfig:
    return WhatsAppConfig.from_env()


def whatsapp_client() -> WhatsAppClient:
    return WhatsAppClient(whatsapp_config())


@app.get("/health")
def health() -> dict:
    from unitwatt.bot.transcribe import configured
    from unitwatt.extract import default_reader, reader_label

    cfg, stt = whatsapp_config(), configured()
    return {
        "status": "ok", "version": __version__,
        "whatsapp": {"ready": cfg.ready, "verify_token_set": bool(cfg.verify_token), "signature_check": bool(cfg.app_secret)},
        "bill_reader": reader_label(default_reader()),
        "speech_to_text": stt.label if stt else None,
        "rest_api": bool(os.environ.get("UNITWATT_API_TOKEN")),
    }


# WhatsApp webhook ----------------------------------------------------------------------------


@app.get("/webhook/whatsapp", response_class=PlainTextResponse)
def whatsapp_verify(request: Request) -> str:
    challenge = verify_subscription(dict(request.query_params), whatsapp_config().verify_token)
    if challenge is None:
        raise HTTPException(status_code=403, detail="Verification failed: check WHATSAPP_VERIFY_TOKEN.")
    return challenge


@app.post("/webhook/whatsapp")
async def whatsapp_receive(request: Request, background: BackgroundTasks) -> dict:
    body = await request.body()
    cfg = whatsapp_config()
    if not signature_ok(body, request.headers.get("X-Hub-Signature-256"), cfg.app_secret):
        raise HTTPException(status_code=401, detail="Bad signature.")
    try:
        payload = json.loads(body)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="Body is not JSON.") from exc
    if not cfg.ready:
        log.warning("WhatsApp message received but WHATSAPP_TOKEN / WHATSAPP_PHONE_NUMBER_ID are not set; ignoring it.")
        return {"ok": False, "reason": "not configured"}
    # Answer Meta at once (it retries slow webhooks) and reply to the person in the background.
    background.add_task(process_webhook, payload, get_bot(), whatsapp_client())
    return {"ok": True}


# REST API -------------------------------------------------------------------------------------


def _authorise(key: str | None) -> None:
    expected = os.environ.get("UNITWATT_API_TOKEN", "")
    if not expected:
        raise HTTPException(status_code=503, detail="The REST API is off: set UNITWATT_API_TOKEN to turn it on.")
    if not key or not hmac.compare_digest(key, expected):
        raise HTTPException(status_code=401, detail="Missing or wrong X-API-Key.")


@app.get("/api/report")
def api_report(lang: str = "en", x_api_key: str | None = Header(default=None)) -> dict:
    from unitwatt.messages import owner_message

    _authorise(x_api_key)
    bot = get_bot()
    return {"factory": bot.F.name, "month": bot.R.owner_facts.month.isoformat(), "text": owner_message(bot.R.owner_facts, lang)}


@app.get("/api/schedule")
def api_schedule(x_api_key: str | None = Header(default=None)) -> dict:
    from unitwatt.scheduler import slot_label

    _authorise(x_api_key)
    R = get_bot().R
    P, plan, current = R.problem, R.schedules["optimised"], R.schedules["current"]
    return {
        "saving_rs_per_day": round(current.total_rs - plan.total_rs, 2),
        "saving_rs_per_month": round((current.total_rs - plan.total_rs) * P.working_days, 2),
        "peak_kva": round(plan.peak_kva, 1), "contract_kva": P.contract_kva,
        "batches": [{"job": j.name, "machine": j.machine, "start": slot_label(plan.starts[j.id]),
                     "end": slot_label(plan.starts[j.id] + j.slots), "band": str(P.bands[plan.starts[j.id]])}
                    for j in sorted(P.jobs, key=lambda j: plan.starts[j.id])],
    }


class ProductionEntry(BaseModel):
    phone: str
    text: str


@app.post("/api/production")
def api_production(entry: ProductionEntry, x_api_key: str | None = Header(default=None)) -> dict:
    """Same rules as the chat: the sender must be a contact allowed to enter production."""
    _authorise(x_api_key)
    bot = get_bot()
    first = bot.handle(Incoming(sender=entry.phone, text=entry.text, channel="api"))
    pending = bot.store.session(bot.F.contact(entry.phone).phone).get("pending") if bot.F.contact(entry.phone) else None
    if not pending or pending.get("type") != "production":
        raise HTTPException(status_code=422, detail=first[0].text if first else "Not understood.")
    saved = bot.handle(Incoming(sender=entry.phone, text="yes", channel="api"))
    return {"saved": True, "day": pending["day"], "tonnes": pending["tonnes"], "shifts": pending["shifts"], "message": saved[0].text}


@app.post("/api/bills/read")
async def api_read_bill(file: UploadFile = File(...), x_api_key: str | None = Header(default=None)) -> dict:
    """Read a bill photo or PDF and run the checks; nothing is saved."""
    from unitwatt.extract import ExtractionError, extract_bill

    _authorise(x_api_key)
    content = await file.read()
    try:
        result = extract_bill(content, file.content_type or "application/octet-stream")
    except ExtractionError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return {"bill": result.bill.model_dump(mode="json"), "read_with": result.model,
            "issues": [{"severity": i.severity, "field": i.field, "message": i.message} for i in result.issues]}


@app.get("/api/data")
def api_data(x_api_key: str | None = Header(default=None)) -> dict:
    """Production entries and bills received through the bot or the API."""
    _authorise(x_api_key)
    store = get_bot().store
    return {"production": store.production_frame().to_dict("records"), "bills": store.bills_frame().to_dict("records")}
