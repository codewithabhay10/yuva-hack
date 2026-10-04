"""Render a realistic HT electricity bill from a ``BillDocument``, for testing bill reading (P1).

The printed figures are exactly the bill's own fields, so a correct extraction must return
the same ``BillDocument``. Every specimen is issued by a fictional distribution company and
carries a SPECIMEN watermark and footer: it is test data, never a record of a real bill.

    python -m unitwatt.specimen_bill [YYYY-MM] [output.html]
"""

from __future__ import annotations

import math
import random
import sys
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path

from jinja2 import Environment, select_autoescape

from unitwatt.messages import format_inr
from unitwatt.profiles import Factory
from unitwatt.schemas import BillDocument
from unitwatt.tariff import Tariff

_ONES = ["", "One", "Two", "Three", "Four", "Five", "Six", "Seven", "Eight", "Nine", "Ten", "Eleven", "Twelve",
         "Thirteen", "Fourteen", "Fifteen", "Sixteen", "Seventeen", "Eighteen", "Nineteen"]
_TENS = ["", "", "Twenty", "Thirty", "Forty", "Fifty", "Sixty", "Seventy", "Eighty", "Ninety"]


def _below_hundred(n: int) -> str:
    return _ONES[n] if n < 20 else (_TENS[n // 10] + (" " + _ONES[n % 10] if n % 10 else ""))


def _below_thousand(n: int) -> str:
    hundreds, rest = divmod(n, 100)
    parts = [f"{_ONES[hundreds]} Hundred"] if hundreds else []
    if rest:
        parts.append(_below_hundred(rest))
    return " ".join(parts)


def amount_in_words(amount: float) -> str:
    """Indian numbering: 635981.94 -> 'Rupees Six Lakh Thirty Five Thousand ... and Ninety Four Paise Only'."""
    rupees = int(math.floor(round(amount, 2)))
    paise = int(round((round(amount, 2) - rupees) * 100))
    parts = []
    for size, name in ((10**7, "Crore"), (10**5, "Lakh"), (10**3, "Thousand")):
        count, rupees = divmod(rupees, size)
        if count:
            parts.append(f"{_below_thousand(count) if count < 1000 else amount_in_words(count)} {name}")
    if rupees:
        parts.append(_below_thousand(rupees))
    words = "Rupees " + (" ".join(parts) if parts else "Zero")
    if paise:
        words += f" and {_below_hundred(paise)} Paise"
    return words + " Only"


def inr_paise(value: float) -> str:
    """Indian digit grouping with paise: 635981.94 -> '6,35,981.94'."""
    rupees, paise = divmod(round(abs(value) * 100), 100)
    return f"{'-' if value < 0 else ''}{format_inr(rupees)}.{paise:02d}"


@dataclass
class Issuer:
    """A fictional distribution company. Specimens never carry a real DISCOM's name."""

    name: str = "Patliputra Power Distribution Company Limited"
    name_hi: str = "पाटलिपुत्र पावर डिस्ट्रीब्यूशन कंपनी लिमिटेड"
    short: str = "PPDCL"
    division: str = "Electric Supply Division, Patna Industrial"
    gstin: str = "10AAAAA0000A1Z5"
    helpline: str = "1800-000-0000"


@dataclass
class ConsumerDetails:
    name: str
    address: list[str]
    gstin: str = "10AAAAA1111A1Z1"
    connected_load_kw: float = 412.0
    supply_voltage: str = "11 kV"
    feeder: str = "11 kV Feeder IE-3"
    meter_no: str = "HTM-7718203"
    mf: int = 20
    security_deposit: float = 450000.0


@dataclass
class SpecimenOptions:
    issuer: Issuer = field(default_factory=Issuer)
    bill_no: str | None = None
    bill_date: date | None = None
    seed: int = 7


def _readings(bill: BillDocument, mf: int, rng: random.Random) -> list[dict]:
    """Previous and present meter register readings consistent with the billed units."""
    kvarh = math.sqrt(max((bill.kvah_total or bill.kwh_total) ** 2 - bill.kwh_total**2, 0.0))
    rows = []
    for label, units, start in (
        ("Active energy (kWh)", bill.kwh_total, rng.uniform(150_000, 400_000)),
        ("Apparent energy (kVAh)", bill.kvah_total or bill.kwh_total, rng.uniform(170_000, 450_000)),
        ("Reactive energy, lag (kVARh)", kvarh, rng.uniform(40_000, 120_000)),
    ):
        prev = round(start, 3)
        diff = round(units / mf, 3)
        rows.append({"label": label, "prev": f"{prev:,.3f}", "present": f"{prev + diff:,.3f}", "diff": f"{diff:,.3f}",
                     "units": f"{units:,.1f}"})
    rows.append({"label": "Maximum demand (kVA)", "prev": "—", "present": f"{bill.max_demand_kva / mf:,.3f}", "diff": "—",
                 "units": f"{bill.max_demand_kva:,.1f}"})
    return rows


_TEMPLATE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<title>{{ issuer.short }} HT bill {{ bill_no }}</title>
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Noto+Sans+Devanagari:wght@400;700&display=swap">
<style>
@page { size: A4; margin: 0; }
* { box-sizing: border-box; }
body { margin: 0; background: #e9e9e6; font-family: "Liberation Sans", Arial, Helvetica, "Noto Sans Devanagari", sans-serif; color: #1a1a1a; }
.hi { font-family: "Noto Sans Devanagari", "Liberation Sans", Arial, sans-serif; }
.sheet { position: relative; width: 210mm; min-height: 297mm; margin: 0 auto; background: #fff; padding: 8mm 10mm 6mm; font-size: 8.2pt; line-height: 1.28; overflow: hidden; }
.wm { position: absolute; left: 50%; top: 52%; transform: translate(-50%, -50%) rotate(-32deg); font: 700 64pt "Liberation Sans", Arial, sans-serif;
      color: rgba(196, 30, 30, .11); letter-spacing: .12em; white-space: nowrap; pointer-events: none; z-index: 5; text-align: center; line-height: 1.05; }
.wm small { display: block; font-size: 15pt; letter-spacing: .06em; }
.top { display: grid; grid-template-columns: 17mm 1fr 52mm; gap: 4mm; align-items: center; border-bottom: 2.2pt solid #0d3f6e; padding-bottom: 3mm; }
.logo { width: 17mm; height: 17mm; }
.issuer b { display: block; font-size: 12.4pt; color: #0d3f6e; letter-spacing: .01em; }
.issuer .hi { display: block; font-size: 10pt; color: #0d3f6e; }
.issuer span { display: block; color: #444; font-size: 7.8pt; }
.title { text-align: right; }
.title b { display: block; font-size: 11.5pt; letter-spacing: .03em; }
.title .hi { font-size: 10pt; }
.bar { display: grid; grid-template-columns: 1fr .8fr 1.35fr .8fr 46mm; border: 1pt solid #0d3f6e; margin-top: 3mm; }
.bar div { padding: 1.6mm 2.2mm; border-right: .6pt solid #9fb3c8; }
.bar div:last-child { border-right: 0; background: #0d3f6e; color: #fff; }
.k { display: block; font-size: 7pt; color: #50606f; text-transform: uppercase; letter-spacing: .04em; }
.bar div:last-child .k { color: #c9dbee; }
.v { font-weight: 700; font-size: 9.4pt; }
.big { font-size: 14pt; }
.grid2 { display: grid; grid-template-columns: 1.15fr 1fr; gap: 4mm; margin-top: 2.4mm; }
.panel { border: .8pt solid #9fb3c8; }
.panel h3 { margin: 0; font-size: 8pt; background: #e6eef6; color: #0d3f6e; padding: 1.2mm 2.2mm; text-transform: uppercase; letter-spacing: .05em; border-bottom: .8pt solid #9fb3c8; }
.panel h3 .hi { text-transform: none; letter-spacing: 0; font-weight: 400; margin-left: 2mm; }
dl { display: grid; grid-template-columns: 31mm 1fr; margin: 0; padding: 1.3mm 2.2mm; row-gap: .4mm; column-gap: 2mm; }
dt { color: #50606f; } dd { margin: 0; font-weight: 700; }
table { width: 100%; border-collapse: collapse; font-variant-numeric: tabular-nums; }
th, td { border: .6pt solid #b9c6d3; padding: .75mm 1.8mm; text-align: right; }
th { background: #f2f5f8; font-size: 7.4pt; color: #33475b; font-weight: 700; }
th:first-child, td:first-child { text-align: left; }
.sec { margin-top: 2.4mm; }
.charges td:first-child { width: 58%; }
.charges tr.sub td { background: #f7f9fb; font-weight: 700; }
.charges tr.tot td { background: #0d3f6e; color: #fff; font-weight: 700; font-size: 10pt; border-color: #0d3f6e; }
.words { margin-top: 1.6mm; font-weight: 700; }
.notes { margin-top: 2.4mm; display: grid; grid-template-columns: 1fr 1fr; gap: 4mm; font-size: 7.4pt; color: #333; }
.notes ol { margin: 1mm 0 0; padding-left: 4.5mm; }
.slip { margin-top: 3mm; border-top: 1pt dashed #777; padding-top: 2.5mm; display: grid; grid-template-columns: repeat(4, 1fr); gap: 3mm; font-size: 8pt; }
.foot { margin-top: 2.6mm; border-top: .8pt solid #c41e1e; padding-top: 1.6mm; font-size: 7.4pt; color: #9b1c1c; font-weight: 700; text-align: center; }
</style></head>
<body><div class="sheet">
<div class="wm">SPECIMEN<small>SYNTHETIC TEST BILL · NOT A REAL BILL</small></div>

<div class="top">
  <svg class="logo" viewBox="0 0 64 64" aria-hidden="true"><circle cx="32" cy="32" r="30" fill="#0d3f6e"/><circle cx="32" cy="32" r="24" fill="none" stroke="#f2b632" stroke-width="2.5"/><path d="M36 10 L20 36 H31 L27 54 L44 27 H33 Z" fill="#f2b632"/></svg>
  <div class="issuer">
    <b>{{ issuer.name }}</b>
    <span class="hi">{{ issuer.name_hi }}</span>
    <span>{{ issuer.division }} · GSTIN {{ issuer.gstin }} · Helpline {{ issuer.helpline }}</span>
  </div>
  <div class="title"><b>HT ELECTRICITY BILL</b><span class="hi">एच.टी. विद्युत विपत्र</span><br><span>{{ bill.tariff_category }}</span></div>
</div>

<div class="bar">
  <div><span class="k">Bill no. / <span class="hi">विपत्र संख्या</span></span><span class="v">{{ bill_no }}</span></div>
  <div><span class="k">Bill date / <span class="hi">विपत्र तिथि</span></span><span class="v">{{ bill_date }}</span></div>
  <div><span class="k">Billing period</span><span class="v">{{ period }}</span></div>
  <div><span class="k">Due date / <span class="hi">देय तिथि</span></span><span class="v">{{ due_date }}</span></div>
  <div><span class="k">Amount payable / <span class="hi">कुल देय राशि</span></span><span class="v big">&#8377; {{ total }}</span></div>
</div>

<div class="grid2">
  <div class="panel"><h3>Consumer details<span class="hi">उपभोक्ता विवरण</span></h3>
    <dl>
      <dt>Consumer no.</dt><dd>{{ bill.consumer_number }}</dd>
      <dt>Name</dt><dd>{{ consumer.name }}</dd>
      <dt>Address</dt><dd>{% for line in consumer.address %}{{ line }}{% if not loop.last %}<br>{% endif %}{% endfor %}</dd>
      <dt>Consumer GSTIN</dt><dd>{{ consumer.gstin }}</dd>
      <dt>Category</dt><dd>{{ bill.tariff_category }}</dd>
      <dt>Supply voltage</dt><dd>{{ consumer.supply_voltage }} · {{ consumer.feeder }}</dd>
      <dt>Contract demand</dt><dd>{{ num(bill.contract_demand_kva, 0) }} kVA</dd>
      <dt>Connected load</dt><dd>{{ num(consumer.connected_load_kw, 0) }} kW</dd>
      <dt>Security deposit</dt><dd>&#8377; {{ inr2(consumer.security_deposit) }}</dd>
    </dl>
  </div>
  <div class="panel"><h3>Meter and billing parameters</h3>
    <dl>
      <dt>Meter no.</dt><dd>{{ consumer.meter_no }} (HT TVM, 3&#966; 4W)</dd>
      <dt>Multiplying factor</dt><dd>{{ consumer.mf }}</dd>
      <dt>Reading dates</dt><dd>{{ prev_read }} &rarr; {{ present_read }}</dd>
      <dt>Meter status</dt><dd>OK (actual reading)</dd>
      <dt>Energy billed on</dt><dd>{{ bill.energy_basis }}</dd>
      <dt>Recorded MD</dt><dd>{{ num(bill.max_demand_kva, 1) }} kVA</dd>
      <dt>Billing demand</dt><dd>{{ num(bill.billing_demand_kva, 1) }} kVA <span style="font-weight:400">(higher of MD and {{ floor_pct }}% of CD = {{ num(floor_kva, 1) }} kVA)</span></dd>
      <dt>Average power factor</dt><dd>{{ "%.3f"|format(bill.power_factor) }}</dd>
      <dt>Load factor</dt><dd>{{ "%.1f"|format(load_factor) }} %</dd>
    </dl>
  </div>
</div>

<div class="sec"><table>
  <thead><tr><th>Meter readings / <span class="hi">मीटर रीडिंग</span></th><th>Previous</th><th>Present</th><th>Difference</th><th>MF</th><th>Consumption</th></tr></thead>
  <tbody>{% for r in readings %}<tr><td>{{ r.label }}</td><td>{{ r.prev }}</td><td>{{ r.present }}</td><td>{{ r.diff }}</td><td>{{ consumer.mf }}</td><td><b>{{ r.units }}</b></td></tr>{% endfor %}</tbody>
</table></div>

<div class="sec"><table>
  <thead><tr><th>Time-of-day zone / <span class="hi">समय क्षेत्र</span></th><th>Hours</th><th>Units ({{ bill.energy_basis }})</th><th>Rate (&#8377;/unit)</th><th>Amount (&#8377;)</th></tr></thead>
  <tbody>{% for z in zones %}<tr><td>{{ z.label }}</td><td>{{ z.hours }}</td><td>{{ z.units }}</td><td>{{ z.rate }}</td><td>{{ z.amount }}</td></tr>{% endfor %}
  <tr><td><b>Total</b></td><td></td><td><b>{{ num(bill.billing_units, 1) }}</b></td><td></td><td><b>{{ inr2(bill.energy_charges) }}</b></td></tr></tbody>
</table></div>

<div class="grid2">
  <div class="sec"><table class="charges">
    <thead><tr><th>Charges / <span class="hi">प्रभार</span></th><th>Amount (&#8377;)</th></tr></thead>
    <tbody>
      <tr><td>Energy charges</td><td>{{ inr2(bill.energy_charges) }}</td></tr>
      <tr><td>Demand charges ({{ num(bill.billing_demand_kva if bill.billing_demand_kva <= bill.contract_demand_kva else bill.contract_demand_kva, 1) }} kVA &times; &#8377;{{ num(demand_rate, 2) }})</td><td>{{ inr2(bill.demand_charges) }}</td></tr>
      <tr><td>Excess demand surcharge</td><td>{{ inr2(bill.excess_demand_charges) }}</td></tr>
      <tr><td>Power factor {{ "surcharge" if bill.pf_adjustment >= 0 else "rebate" }}{% if pf_pct %} ({{ "%.1f"|format(pf_pct) }}% of energy charges){% endif %}</td><td>{{ inr2(bill.pf_adjustment) }}</td></tr>
      <tr><td>Fixed / meter rent</td><td>{{ inr2(bill.fixed_charges) }}</td></tr>
      <tr><td>Electricity duty @ {{ num(duty_pct, 1) }}%</td><td>{{ inr2(bill.electricity_duty) }}</td></tr>
      <tr><td>Other charges / arrears</td><td>{{ inr2(bill.other_charges) }}</td></tr>
      <tr class="tot"><td>Total amount payable</td><td>{{ total }}</td></tr>
    </tbody></table>
    <div class="words">{{ words }}</div>
  </div>
  <div class="sec"><table>
    <thead><tr><th>History / <span class="hi">पिछला उपभोग</span></th><th>kWh</th><th>MD kVA</th><th>PF</th><th>Amount (&#8377;)</th></tr></thead>
    <tbody>{% for h in history %}<tr><td>{{ h.month }}</td><td>{{ h.kwh }}</td><td>{{ h.md }}</td><td>{{ h.pf }}</td><td>{{ h.amount }}</td></tr>{% endfor %}</tbody>
  </table></div>
</div>

<div class="notes">
  <div><b>Important / <span class="hi">महत्वपूर्ण</span></b><ol>
    <li>Pay by the due date to avoid a delayed payment surcharge of 1.5% per month.</li>
    <li>Prompt payment rebate of 1% of energy charges (&#8377; {{ inr2(rebate) }}) if paid by {{ rebate_date }}. Not deducted above.</li>
    <li>Keep the power factor above {{ "%.2f"|format(pf_ref) }} to avoid the PF surcharge.</li>
  </ol></div>
  <div><b>ToD tariff</b><ol>
    {% for z in zones %}<li>{{ z.label }} ({{ z.hours }}): {{ z.mult }} of the base energy rate of &#8377;{{ num(energy_rate, 2) }}.</li>{% endfor %}
    <li>This is a computer-generated bill and needs no signature.</li>
  </ol></div>
</div>

<div class="slip">
  <div><span class="k">Consumer no.</span><span class="v">{{ bill.consumer_number }}</span></div>
  <div><span class="k">Bill no.</span><span class="v">{{ bill_no }}</span></div>
  <div><span class="k">Due date</span><span class="v">{{ due_date }}</span></div>
  <div><span class="k">Amount payable</span><span class="v">&#8377; {{ total }}</span></div>
</div>

<div class="foot">SPECIMEN · Synthetic test bill generated by UnitWatt. Not issued by any distribution company. {{ issuer.name }} is fictional, and every name, number and rate on this page is made up.</div>
</div></body></html>"""

_env = Environment(autoescape=select_autoescape(default=True))


def _hours(tariff: Tariff, zone: str) -> str:
    for band in tariff.bands:
        if band.name == zone:
            return f"{band.start_min // 60:02d}:00–{(band.end_min // 60) or 24:02d}:00"
    return "rest of day"


def render_bill_html(
    bill: BillDocument,
    factory: Factory,
    tariff: Tariff,
    history: list[BillDocument],
    consumer: ConsumerDetails | None = None,
    options: SpecimenOptions | None = None,
) -> str:
    options = options or SpecimenOptions()
    rng = random.Random(f"{options.seed}-{bill.period_start}")
    consumer = consumer or ConsumerDetails(
        name=factory.name.split(" (")[0],
        address=["Plot B-14, Industrial Estate", f"{factory.city}, {factory.state} 800013"],
    )
    bill_date = options.bill_date or bill.period_end + timedelta(days=3)
    days = (bill.period_end - bill.period_start).days + 1
    zone_order = sorted(bill.zones, key=lambda z: [b.name for b in tariff.bands].index(z.zone) if z.zone in [b.name for b in tariff.bands] else 99)
    zones = []
    for i, z in enumerate(zone_order, start=1):
        mult = tariff.multiplier(z.zone)
        zones.append({
            "label": f"TOD-{i} {z.zone.title()}",
            "hours": _hours(tariff, z.zone),
            "units": f"{z.units:,.1f}",
            "rate": f"{tariff.energy_rate * mult:.2f}",
            "amount": inr_paise(z.charges if z.charges is not None else z.units * tariff.energy_rate * mult),
            "mult": f"{mult:.0%}",
        })
    pf_pct = abs(bill.pf_adjustment) / bill.energy_charges * 100 if bill.energy_charges and bill.pf_adjustment else 0.0
    shown = [h for h in history if h.period_start <= bill.period_start][-6:]
    return _env.from_string(_TEMPLATE).render(
        issuer=options.issuer,
        bill=bill,
        consumer=consumer,
        bill_no=options.bill_no or f"HT/PAT/{bill.period_start:%y%m}/{rng.randint(1000, 9999):04d}",
        bill_date=f"{bill_date:%d-%m-%Y}",
        due_date=f"{bill_date + timedelta(days=15):%d-%m-%Y}",
        rebate_date=f"{bill_date + timedelta(days=7):%d-%m-%Y}",
        period=f"{bill.period_start:%d-%m-%Y} – {bill.period_end:%d-%m-%Y}",
        prev_read=f"{bill.period_start - timedelta(days=1):%d-%m-%Y}",
        present_read=f"{bill.period_end:%d-%m-%Y}",
        total=inr_paise(bill.total_amount),
        words=amount_in_words(bill.total_amount),
        readings=_readings(bill, consumer.mf, rng),
        zones=zones,
        load_factor=bill.kwh_total / (bill.max_demand_kva * 24 * days) * 100 if bill.max_demand_kva else 0.0,
        floor_pct=f"{tariff.billing_floor_pct:.0f}",
        floor_kva=tariff.billing_floor_pct / 100 * bill.contract_demand_kva,
        demand_rate=tariff.demand_rate,
        energy_rate=tariff.energy_rate,
        duty_pct=tariff.electricity_duty_pct,
        pf_pct=pf_pct,
        pf_ref=tariff.pf_rule.reference if tariff.pf_rule else 0.9,
        rebate=bill.energy_charges * 0.01,
        history=[
            {"month": f"{h.period_start:%b %Y}", "kwh": f"{h.kwh_total:,.0f}", "md": f"{h.max_demand_kva:.1f}",
             "pf": f"{h.power_factor:.3f}" if h.power_factor else "—", "amount": inr_paise(h.total_amount)}
            for h in shown
        ],
        num=lambda v, d=0: f"{v:,.{d}f}",
        inr2=inr_paise,
    )


def specimen_bill(month: str = "2026-06", seed: int = 7) -> tuple[str, BillDocument]:
    """HTML for one month of the synthetic demo unit, and the bill exactly as printed on it."""
    from unitwatt.profiles import load_factory
    from unitwatt.synthetic import Scenario, generate
    from unitwatt.tariff import get_tariff

    factory = load_factory("demo_forge")
    tariff = get_tariff(factory.tariff_id)
    data = generate(factory, tariff, Scenario(seed=seed))
    issuer = Issuer()
    bills = [b.model_copy(update={"discom": issuer.name, "tariff_category": "HTS-I Industrial (11 kV)"}) for b in data.bills]
    bill = next(b for b in bills if f"{b.period_start:%Y-%m}" == month)
    return render_bill_html(bill, factory, tariff, bills, options=SpecimenOptions(issuer=issuer, seed=seed)), bill


if __name__ == "__main__":
    month = sys.argv[1] if len(sys.argv) > 1 else "2026-06"
    out = Path(sys.argv[2] if len(sys.argv) > 2 else f"specimen_bill_{month.replace('-', '_')}.html")
    html, bill = specimen_bill(month)
    out.write_text(html, encoding="utf-8")
    out.with_suffix(".json").write_text(bill.model_dump_json(indent=2), encoding="utf-8")
    print(f"Wrote {out} and {out.with_suffix('.json')} (total Rs {format_inr(bill.total_amount)}). Open the HTML and print to PDF.")
