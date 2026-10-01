"""Document AI for bills and register pages (P1, P2), using Claude's vision with structured output.

Optional: needs ANTHROPIC_API_KEY (or an ``ant auth login`` profile). Claude fills the same
fixed schema the rest of the pipeline uses; the arithmetic checks in ``validate_bill`` and a
human confirmation screen still stand between the extraction and the ledger.
"""

from __future__ import annotations

import base64
from dataclasses import dataclass
from datetime import date

from pydantic import BaseModel, Field

from unitwatt.profiles import Factory
from unitwatt.schemas import BillDocument, Issue, validate_bill

MODEL = "claude-opus-5-5"
FALLBACK_BETA = "server-side-fallback-2026-07-01"

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


def _media_block(content: bytes, media_type: str) -> dict:
    data = base64.standard_b64encode(content).decode("utf-8")
    if media_type == "application/pdf":
        return {"type": "document", "source": {"type": "base64", "media_type": media_type, "data": data}}
    return {"type": "image", "source": {"type": "base64", "media_type": media_type, "data": data}}


def _client(client=None):
    if client is not None:
        return client
    import anthropic

    return anthropic.Anthropic()


def _parse(client, content: list[dict], output_format: type[BaseModel]):
    response = client.beta.messages.parse(
        model=MODEL,
        max_tokens=16000,
        # Opt in to server-side fallback so a classifier decline is retried on another model.
        betas=[FALLBACK_BETA],
        fallbacks="default",
        output_config={"effort": "medium"},
        output_format=output_format,
        messages=[{"role": "user", "content": content}],
    )
    if response.stop_reason == "refusal":
        raise ExtractionError("The model declined to read this document.")
    if response.stop_reason == "max_tokens":
        raise ExtractionError("The extraction was cut off; try a clearer or shorter document.")
    if response.parsed_output is None:
        raise ExtractionError("The model did not return a filled schema.")
    return response


def extract_bill(content: bytes, media_type: str, client=None) -> BillExtraction:
    """A bill PDF or photo -> ``BillDocument`` plus its arithmetic-check issues."""
    response = _parse(_client(client), [_media_block(content, media_type), {"type": "text", "text": BILL_PROMPT}], BillDocument)
    bill = response.parsed_output
    return BillExtraction(bill, validate_bill(bill), response.model)


def extract_register(content: bytes, media_type: str, factory: Factory, client=None) -> RegisterPage:
    """A photo of a handwritten production register page -> rows of (day, product, tonnes)."""
    products = "\n".join(f"- {p.id}: {p.name} / {p.name_hi} (also written as: {', '.join(p.aliases)})" for p in factory.products)
    prompt = (
        "This is a page from a factory's handwritten daily production register. It may mix Hindi and English "
        "and use Devanagari digits.\n\nMap each entry to one of these product ids:\n"
        f"{products}\n\nUse 'unknown' if an entry matches none of them. Give tonnes (1 quintal = 0.1 t, "
        "1000 kg = 1 t). Copy each entry into as_written exactly. List anything you cannot read in unreadable."
    )
    response = _parse(_client(client), [_media_block(content, media_type), {"type": "text", "text": prompt}], RegisterPage)
    return response.parsed_output
