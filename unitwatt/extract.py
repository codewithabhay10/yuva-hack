"""Reading bills and register pages (P1, P2) with free tools only.

Two kinds of reader fill the same fixed ``BillDocument`` schema:

- ``offline``: the PDF's own text layer, or RapidOCR for scans and photos, then a label-based
  parser (``offline_extract.py``). Free, no key, and the bill never leaves the computer.
- A free vision model behind an OpenAI-compatible API: Google Gemini's free tier
  (``GEMINI_API_KEY`` from aistudio.google.com), Groq or OpenRouter free models, or a model
  running locally in Ollama. It reads layouts the offline labels do not cover, and handwriting.

``UNITWATT_READER`` picks the reader; otherwise the first free API with a key set is used, and
the offline reader when there is none. ``UNITWATT_MODEL``, ``UNITWATT_BASE_URL`` and
``UNITWATT_API_KEY`` override the preset model, endpoint and key. Whichever reader is used, the
arithmetic checks in ``validate_bill`` and a human confirmation screen stand between the
extraction and the ledger.
"""

from __future__ import annotations

import base64
import io
import json
import os
import re
from dataclasses import dataclass, replace
from datetime import date

from pydantic import BaseModel, Field, ValidationError

from unitwatt.profiles import Factory
from unitwatt.schemas import BillDocument, Issue, validate_bill

BILL_PROMPT = """This is an Indian electricity bill (HT or LT industrial) from a DISCOM. It may be a PDF, a scan or a
phone photo, and may mix English with Hindi or another Indian language.

Fill the schema with the values printed on the bill:
- Copy numbers exactly as printed. Do not compute or correct anything; arithmetic checks run afterwards.
- energy_basis is "kVAh" if energy charges are billed on kVAh units, otherwise "kWh".
- zones: one entry per time-of-day zone (for example peak, normal, off-peak, solar), with the units billed in
  that zone and the zone's energy charges if printed. Leave zones empty if the bill has no ToD breakdown.
- demand_charges excludes any excess-demand penalty, which goes in excess_demand_charges.
- pf_adjustment is positive for a power-factor penalty and negative for an incentive or rebate.
- Put taxes and electricity duty in electricity_duty, and anything else (arrears, rebates, meter rent if not
  shown as fixed charges) in other_charges, so that the line items add up to total_amount.
- Use null for any optional field that is not printed. Dates are YYYY-MM-DD."""

JSON_INSTRUCTIONS = """Reply with one JSON object and nothing else: no prose and no code fences. It must match this
JSON Schema:
{schema}"""


@dataclass(frozen=True)
class Provider:
    """A free vision model behind an OpenAI-compatible chat completions endpoint."""

    name: str
    label: str
    base_url: str
    model: str
    key_env: tuple[str, ...] = ()  # empty: no key needed
    signup: str = ""
    note: str = ""

    @property
    def api_key(self) -> str | None:
        return next((os.environ[k] for k in self.key_env if os.environ.get(k)), None)


def _ollama_url() -> str:
    host = os.environ.get("OLLAMA_HOST", "localhost:11434").rstrip("/")
    return (host if "://" in host else "http://" + host) + "/v1"


# Free model names change often; UNITWATT_MODEL overrides the preset.
PROVIDERS: dict[str, Provider] = {
    "gemini": Provider(
        "gemini", "Google Gemini (free tier)", "https://generativelanguage.googleapis.com/v1beta/openai",
        "gemini-flash-latest", ("GEMINI_API_KEY", "GOOGLE_API_KEY"), "https://aistudio.google.com/apikey",
        "On the free tier Google may use uploads to improve its models, so use it for specimen bills or with the owner's consent.",
    ),
    "groq": Provider(
        "groq", "Groq (free tier)", "https://api.groq.com/openai/v1", "meta-llama/llama-4-scout-17b-16e-instruct",
        ("GROQ_API_KEY",), "https://console.groq.com/keys",
    ),
    "openrouter": Provider(
        "openrouter", "OpenRouter (free models)", "https://openrouter.ai/api/v1", "google/gemma-3-27b-it:free",
        ("OPENROUTER_API_KEY",), "https://openrouter.ai/keys",
    ),
    "ollama": Provider(
        "ollama", "Ollama on this computer", _ollama_url(),
        "qwen2.5vl:7b", (), "https://ollama.com/download", "Runs locally: free, and the bill never leaves the computer.",
    ),
}
OFFLINE = "offline"
OFFLINE_LABEL = "Offline: PDF text or OCR (free, no key, stays on this computer)"


class RegisterRow(BaseModel):
    day: date = Field(description="Date of the entry, YYYY-MM-DD")
    product: str = Field(description="Product id from the list given in the instructions, or 'unknown'")
    tonnes: float = Field(description="Output in tonnes (convert kg or quintal to tonnes)")
    as_written: str = Field(description="The entry exactly as written on the page")


class RegisterPage(BaseModel):
    rows: list[RegisterRow] = Field(description="One row per product per day")
    unreadable: list[str] = Field(default_factory=list, description="Anything that could not be read with confidence")


@dataclass
class BillExtraction:
    bill: BillDocument
    issues: list[Issue]
    model: str


class ExtractionError(RuntimeError):
    pass


class _RejectedRequest(ExtractionError):
    """HTTP 400: the server refused a parameter, such as JSON mode on a model without it."""


# --- Choosing a reader ------------------------------------------------------------------------


def resolve_provider(name: str) -> Provider:
    """The preset for ``name`` with any UNITWATT_MODEL / UNITWATT_BASE_URL / UNITWATT_API_KEY overrides."""
    if name not in PROVIDERS:
        raise ExtractionError(f"Unknown reader {name!r}; choose one of: {', '.join([OFFLINE, *PROVIDERS])}.")
    provider = PROVIDERS[name]
    overrides = {}
    if os.environ.get("UNITWATT_MODEL"):
        overrides["model"] = os.environ["UNITWATT_MODEL"]
    if os.environ.get("UNITWATT_BASE_URL"):
        overrides["base_url"] = os.environ["UNITWATT_BASE_URL"].rstrip("/")
    if os.environ.get("UNITWATT_API_KEY"):
        overrides["key_env"] = ("UNITWATT_API_KEY",)
    return replace(provider, **overrides) if overrides else provider


def available_readers() -> list[str]:
    """Readers usable right now: offline always, each free API whose key is set, Ollama if configured."""
    readers = [OFFLINE]
    configured = os.environ.get("UNITWATT_READER", "")
    for name, provider in PROVIDERS.items():
        if (provider.key_env and resolve_provider(name).api_key) or name == configured or (
            name == "ollama" and os.environ.get("OLLAMA_HOST")
        ):
            readers.append(name)
    return readers


def default_reader() -> str:
    configured = os.environ.get("UNITWATT_READER")
    if configured:
        return configured
    online = [r for r in available_readers() if r != OFFLINE and PROVIDERS[r].key_env]
    return online[0] if online else OFFLINE


def reader_label(name: str) -> str:
    return OFFLINE_LABEL if name == OFFLINE else PROVIDERS[name].label


# --- Talking to a free vision model -----------------------------------------------------------


def document_images(content: bytes, media_type: str, max_side: int = 2000) -> list[tuple[bytes, str]]:
    """Images to send: each PDF page rendered to PNG, or the photo itself (resized and upright)."""
    from PIL import Image, ImageOps

    from unitwatt.offline_extract import render_pdf

    if media_type == "application/pdf":
        return [(png, "image/png") for png in render_pdf(content, scale=2.0)]
    picture = ImageOps.exif_transpose(Image.open(io.BytesIO(content))).convert("RGB")
    picture.thumbnail((max_side, max_side))
    buf = io.BytesIO()
    picture.save(buf, format="JPEG", quality=90)
    return [(buf.getvalue(), "image/jpeg")]


def pdf_text(content: bytes, limit: int = 8000) -> str:
    """The PDF's text layer, if any: given to the model as a hint alongside the page images."""
    from unitwatt.offline_extract import pdf_cells

    lines = []
    for page in pdf_cells(content):
        for cell in sorted(page, key=lambda c: (round(c.cy / max(c.h, 1)), c.x0)):
            lines.append(cell.text)
    return "\n".join(lines)[:limit]


def _schema_prompt(task: str, schema: type[BaseModel]) -> str:
    return task + "\n\n" + JSON_INSTRUCTIONS.format(schema=json.dumps(schema.model_json_schema(), separators=(",", ":")))


def _json_from(text: str) -> object:
    text = re.sub(r"^\s*```(?:json)?\s*|\s*```\s*$", "", text.strip())
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end < start:
        raise json.JSONDecodeError("no JSON object in the reply", text, 0)
    return json.loads(text[start: end + 1])


def _post(provider: Provider, body: dict, session, timeout: float) -> dict:
    import requests

    if provider.key_env and not provider.api_key:
        signup = f" (free from {provider.signup})" if provider.signup else ""
        raise ExtractionError(f"Set {provider.key_env[0]}{signup} to use {provider.label}, or use the offline reader.")
    headers = {"Content-Type": "application/json"}
    if provider.api_key:
        headers["Authorization"] = f"Bearer {provider.api_key}"
    try:
        response = (session or requests).post(f"{provider.base_url}/chat/completions", json=body, headers=headers, timeout=timeout)
    except requests.RequestException as exc:
        hint = " Is Ollama running, with the model pulled?" if provider.name == "ollama" else ""
        raise ExtractionError(f"Could not reach {provider.label} at {provider.base_url}.{hint} ({exc.__class__.__name__})") from exc
    if response.status_code != 200:
        try:
            payload = response.json()
            payload = payload[0] if isinstance(payload, list) and payload else payload
            error = payload.get("error") if isinstance(payload, dict) else None
            detail = str((error.get("message") if isinstance(error, dict) else error) or payload)[:300]
        except ValueError:
            detail = response.text[:300]
        status = response.status_code
        if status in (401, 403):
            keys = " or ".join(provider.key_env) or "the API key"
            raise ExtractionError(f"{provider.label} rejected the key ({detail}). Check {keys}.")
        if status == 404:
            raise ExtractionError(f"{provider.label} has no model {body['model']!r} ({detail}). Set UNITWATT_MODEL to a current free vision model.")
        if status == 429:
            raise ExtractionError(f"{provider.label} free-tier limit reached ({detail}). Wait a minute, or use the offline reader.")
        error_type = _RejectedRequest if status == 400 else ExtractionError
        raise error_type(f"{provider.label} returned HTTP {status}: {detail}")
    return response.json()


def _reply_text(data: dict) -> tuple[str, str | None]:
    try:
        choice = data["choices"][0]
    except (KeyError, IndexError, TypeError) as exc:
        raise ExtractionError("The model's reply had no answer in it.") from exc
    content = choice.get("message", {}).get("content")
    if isinstance(content, list):  # some servers return content parts
        content = "".join(part.get("text", "") for part in content if isinstance(part, dict))
    if choice.get("finish_reason") == "length":
        raise ExtractionError("The model's reply was cut off; try a clearer or shorter document.")
    if not content:
        raise ExtractionError("The model declined to read this document or returned nothing.")
    return content, data.get("model")


def ask_for_json(provider: Provider, task: str, images: list[tuple[bytes, str]], schema: type[BaseModel],
                 session=None, timeout: float = 180.0) -> tuple[BaseModel, str]:
    """One request to a free vision model for a filled ``schema``; one retry if the JSON is off."""
    content = [{"type": "text", "text": _schema_prompt(task, schema)}]
    for data, media_type in images:
        url = f"data:{media_type};base64,{base64.standard_b64encode(data).decode('ascii')}"
        content.append({"type": "image_url", "image_url": {"url": url}})
    messages: list[dict] = [{"role": "user", "content": content}]
    json_mode = True
    for attempt in range(2):
        body = {"model": provider.model, "messages": messages, "temperature": 0}
        if json_mode:
            body["response_format"] = {"type": "json_object"}
        try:
            data = _post(provider, body, session, timeout)
        except _RejectedRequest as exc:
            if not json_mode or not re.search(r"response_format|json", str(exc), re.IGNORECASE):
                raise
            json_mode = False  # this model has no JSON mode: the prompt alone asks for JSON
            del body["response_format"]
            data = _post(provider, body, session, timeout)
        reply, served_by = _reply_text(data)
        try:
            return schema.model_validate(_json_from(reply)), served_by or provider.model
        except (json.JSONDecodeError, ValidationError) as exc:
            if attempt:
                raise ExtractionError(f"{provider.label} did not return a valid filled schema: {str(exc)[:400]}") from exc
            messages += [
                {"role": "assistant", "content": reply},
                {"role": "user", "content": f"That reply does not match the schema:\n{str(exc)[:1500]}\n"
                                            "Reply again with the corrected JSON object only."},
            ]
    raise AssertionError("unreachable")


# --- Public API -------------------------------------------------------------------------------


def _provider_for(reader: str | Provider | None) -> Provider | str:
    reader = reader or default_reader()
    if isinstance(reader, Provider) or reader == OFFLINE:
        return reader
    return resolve_provider(reader)


def extract_bill(content: bytes, media_type: str, reader: str | Provider | None = None, *,
                 session=None, timeout: float = 180.0) -> BillExtraction:
    """A bill PDF or photo -> ``BillDocument`` plus its arithmetic-check issues."""
    provider = _provider_for(reader)
    if provider == OFFLINE:
        from unitwatt.offline_extract import read_bill

        return read_bill(content, media_type)
    task = BILL_PROMPT
    if media_type == "application/pdf":
        text = pdf_text(content)
        if text.strip():
            task += "\n\nText layer of the PDF, for reference (layout lost; trust the page images for structure):\n" + text
    bill, model = ask_for_json(provider, task, document_images(content, media_type), BillDocument, session, timeout)
    return BillExtraction(bill, validate_bill(bill), f"{provider.label}: {model}")


def extract_register(content: bytes, media_type: str, factory: Factory, reader: str | Provider | None = None, *,
                     session=None, timeout: float = 180.0) -> RegisterPage:
    """A photo of a handwritten production register page -> rows of (day, product, tonnes)."""
    provider = _provider_for(reader)
    if provider == OFFLINE:
        raise ExtractionError(
            "Handwriting needs a vision model: set GEMINI_API_KEY (free) or use the 30-second chat entry instead."
        )
    products = "\n".join(f"- {p.id}: {p.name} / {p.name_hi} (also written as: {', '.join(p.aliases)})" for p in factory.products)
    task = (
        "This is a page from a factory's handwritten daily production register. It may mix Hindi and English "
        "and use Devanagari digits.\n\nMap each entry to one of these product ids:\n"
        f"{products}\n\nUse 'unknown' if an entry matches none of them. Give tonnes (1 quintal = 0.1 t, "
        "1000 kg = 1 t). Copy each entry into as_written exactly. List anything you cannot read in unreadable."
    )
    page, _ = ask_for_json(provider, task, document_images(content, media_type), RegisterPage, session, timeout)
    return page
