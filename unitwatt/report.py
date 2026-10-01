"""Auditor-, banker- and buyer-ready documents with a source appendix (P15, P16, P17).

Rendered as self-contained HTML that prints cleanly to PDF from any browser (WeasyPrint
can render the same HTML server-side).
"""

from __future__ import annotations

import json
from datetime import date

from jinja2 import Environment, select_autoescape

from unitwatt.messages import format_inr

_env = Environment(autoescape=select_autoescape(default=True), trim_blocks=True, lstrip_blocks=True)
_env.filters["inr"] = format_inr
_env.filters["num"] = lambda v, d=0: f"{v:,.{d}f}"
_env.filters["pct"] = lambda v, d=1: f"{v * 100:.{d}f}%"

_STYLE = """
:root { --ink:#1b1f24; --muted:#5b6470; --line:#d9dee4; --accent:#0f6b5c; --warn:#a45a00; --bad:#b42318; --bg:#ffffff; }
* { box-sizing: border-box; }
body { font-family: "Inter", system-ui, -apple-system, "Segoe UI", sans-serif; color: var(--ink); background: var(--bg);
       margin: 0 auto; max-width: 860px; padding: 32px 24px 64px; line-height: 1.45; font-size: 14px; }
h1 { font-size: 22px; margin: 0 0 4px; } h2 { font-size: 16px; margin: 28px 0 8px; border-bottom: 1px solid var(--line); padding-bottom: 4px; }
.sub { color: var(--muted); margin: 0 0 16px; }
.banner { background: #fff4e5; border: 1px solid #f3c58b; color: #6b3d00; padding: 8px 12px; border-radius: 6px; margin-bottom: 16px; }
.grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(170px, 1fr)); gap: 10px; }
.tile { border: 1px solid var(--line); border-radius: 8px; padding: 10px 12px; }
.tile .k { color: var(--muted); font-size: 12px; } .tile .v { font-size: 20px; font-weight: 600; }
table { border-collapse: collapse; width: 100%; margin: 6px 0; font-variant-numeric: tabular-nums; }
th, td { border-bottom: 1px solid var(--line); padding: 5px 6px; text-align: left; vertical-align: top; }
th { color: var(--muted); font-weight: 600; font-size: 12px; } td.n, th.n { text-align: right; }
.pass { color: var(--accent); font-weight: 600; } .fail { color: var(--bad); font-weight: 600; }
code, .hash { font-family: ui-monospace, "SFMono-Regular", Menlo, monospace; font-size: 11px; word-break: break-all; }
.sign { margin-top: 32px; display: grid; grid-template-columns: 1fr 1fr; gap: 24px; }
.sign div { border-top: 1px solid var(--ink); padding-top: 4px; color: var(--muted); font-size: 12px; }
.note { color: var(--muted); font-size: 12px; }
@media print { body { padding: 0; } .banner { break-inside: avoid; } h2 { break-after: avoid; } }
"""

_SAVINGS = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Energy savings report</title><style>{{ style }}</style></head><body>
{% if synthetic %}<div class="banner"><b>Demo:</b> built from a synthetic forging unit, not a real factory. Replace with real bills before use.</div>{% endif %}
<h1>Energy savings report</h1>
<p class="sub">IPMVP Option C (whole facility) &middot; {{ factory.name }}, {{ factory.city }} &middot; Consumer no. {{ factory.consumer_number }} &middot; prepared {{ today }}</p>

<div class="grid">
  <div class="tile"><div class="k">Energy avoided</div><div class="v">{{ s.avoided_kwh|inr }} kWh</div></div>
  <div class="tile"><div class="k">Savings vs adjusted baseline</div><div class="v">{{ s.savings_pct|pct }}</div></div>
  <div class="tile"><div class="k">Uncertainty (90% confidence)</div><div class="v">&plusmn;{{ s.uncertainty_kwh|inr }} kWh</div></div>
  <div class="tile"><div class="k">Value at the period's energy price</div><div class="v">&#8377;{{ s.rs_saved|inr }}</div></div>
  <div class="tile"><div class="k">CO&#8322; avoided (grid factor)</div><div class="v">{{ s.tco2_avoided|num(1) }} t</div></div>
  <div class="tile"><div class="k">Data quality score</div><div class="v">{{ quality.score|num(1) }} / 100 ({{ quality.grade }})</div></div>
</div>

<h2>ADEETIE 10% demonstrated-savings test</h2>
{% if s.meets_adeetie %}
<p class="pass">Meets the 10% requirement: {{ s.savings_pct|pct }} demonstrated.</p>
{% else %}
<p class="fail">Not yet: {{ s.savings_pct|pct }} demonstrated. A further {{ s.gap_to_target_kwh|inr }} kWh over the same period would reach 10%.</p>
<p class="note">Operational measures alone rarely reach 10% in a forging unit; the remaining gap is the case for an investment-grade audit and equipment upgrade under ADEETIE.</p>
{% endif %}

<h2>Measures implemented</h2>
<ul>{% for m in measures %}<li>{{ m }}</li>{% endfor %}</ul>

<h2>Method</h2>
<p>Daily electricity (grid + generator) is modelled on the baseline period as<br>
<code>kWh/day = {{ m.coef.base|num(1) }} + {{ m.coef.production_day|num(1) }} &times; production day{% for p in m.products %} + {{ m.coef[p]|num(1) }} &times; tonnes {{ p }}{% endfor %}</code><br>
(non-negative least squares). Savings = baseline model adjusted to the reporting period's actual production &minus; actual energy.
Uncertainty follows ASHRAE Guideline 14 with residual autocorrelation.</p>
<table><tr><th>Baseline period</th><td>{{ s.baseline[0] }} to {{ s.baseline[1] }} ({{ m.n }} days used)</td></tr>
<tr><th>Reporting period</th><td>{{ s.reporting[0] }} to {{ s.reporting[1] }} ({{ s.m }} days)</td></tr>
<tr><th>Tariff</th><td>{{ tariff.name }}, effective {{ tariff.effective_from }} ({{ tariff.status }})</td></tr></table>

<h2>Statistical checks</h2>
<table><tr><th>Check</th><th>Result</th><th>Requirement</th><th></th></tr>
{% for c in s.checks %}<tr><td>{{ c.name }}</td><td>{{ c.value }}</td><td>{{ c.threshold }}</td><td class="{{ 'pass' if c.passed else 'fail' }}">{{ 'Pass' if c.passed else 'Not met' }}</td></tr>{% endfor %}
</table>
<p class="note">Thresholds: LBNL guidance for meter-based IPMVP Option C claims (CV(RMSE) below 25%, NMBE within 0.5%).</p>

<h2>Monthly results</h2>
<table><tr><th>Month</th><th class="n">Adjusted baseline kWh</th><th class="n">Actual kWh</th><th class="n">Avoided kWh</th><th class="n">Savings</th></tr>
{% for month, row in monthly %}<tr><td>{{ month }}</td><td class="n">{{ row.adjusted_baseline|inr }}</td><td class="n">{{ row.actual|inr }}</td><td class="n">{{ row.avoided|inr }}</td><td class="n">{{ row.savings_pct|pct }}</td></tr>{% endfor %}
</table>

<h2>Appendix A &middot; Source documents</h2>
<p class="note">Every figure traces to these files. SHA-256 hashes let an auditor confirm that nothing was altered after upload; the audit chain {{ 'verifies' if chain_ok else 'is BROKEN' }}.</p>
<table><tr><th>#</th><th>Document</th><th>Type</th><th>SHA-256</th></tr>
{% for d in documents %}<tr><td>{{ d.id }}</td><td>{{ d.name }}</td><td>{{ d.kind }}</td><td class="hash">{{ d.sha256 }}</td></tr>{% endfor %}
</table>
{% if consents %}<h2>Appendix B &middot; Consent to share</h2>
<table><tr><th>When</th><th>Recipient</th><th>Granted by</th><th>Purpose</th></tr>
{% for c in consents %}<tr><td>{{ c.recorded_at }}</td><td>{{ c.recipient }}</td><td>{{ c.granted_by }}</td><td>{{ c.purpose }}</td></tr>{% endfor %}</table>{% endif %}

<p class="note">UnitWatt prepares this data pack. The certified energy auditor verifies the measurement and signs the M&amp;V.</p>
<div class="sign"><div>Unit owner</div><div>Certified energy auditor (name, BEE registration no.)</div></div>
</body></html>"""

_EMISSIONS = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Product emissions statement</title><style>{{ style }}</style></head><body>
{% if synthetic %}<div class="banner"><b>Demo:</b> built from a synthetic forging unit, not a real factory. Replace with real data before use.</div>{% endif %}
<h1>Product embedded emissions statement</h1>
<p class="sub">{{ factory.name }}, {{ factory.city }} &middot; {{ e.period[0] }} to {{ e.period[1] }} &middot; prepared {{ today }} &middot; data quality {{ quality.score|num(1) }}/100 ({{ quality.grade }})</p>

<div class="grid">
  <div class="tile"><div class="k">Output</div><div class="v">{{ e.plant.tonnes|num(1) }} t</div></div>
  <div class="tile"><div class="k">Scope 1 (fuels)</div><div class="v">{{ e.plant.scope1_t|num(1) }} tCO&#8322;</div></div>
  <div class="tile"><div class="k">Scope 2 (grid electricity)</div><div class="v">{{ e.plant.scope2_t|num(1) }} tCO&#8322;</div></div>
  <div class="tile"><div class="k">Precursors (billets)</div><div class="v">{{ e.plant.precursor_t|num(0) }} tCO&#8322;</div></div>
</div>

<h2>Emissions per tonne of product</h2>
<table><tr><th>Product</th><th>HSN</th><th>CBAM</th><th class="n">Tonnes</th><th class="n">kWh/t</th><th class="n">Direct</th><th class="n">Indirect</th><th class="n">Precursor</th><th class="n">Total</th><th class="n">CBAM-priced</th></tr>
{% for pid, r in rows %}<tr><td>{{ r.name }}</td><td>{{ r.hsn }}</td><td>{{ 'Yes' if r.cbam_covered else 'No' }}</td>
<td class="n">{{ r.tonnes|num(1) }}</td><td class="n">{{ r.kwh_per_t|num(0) }}</td><td class="n">{{ r.direct_tco2_per_t|num(3) }}</td>
<td class="n">{{ r.indirect_tco2_per_t|num(3) }}</td><td class="n">{{ r.precursor_tco2_per_t|num(3) }}</td><td class="n">{{ r.embedded_tco2_per_t|num(3) }}</td>
<td class="n">{{ r.cbam_priced_tco2_per_t|num(3) if r.cbam_covered else '&ndash;'|safe }}</td></tr>{% endfor %}
</table>
<p class="note">tCO&#8322; per tonne of product. CBAM-priced = direct + precursor emissions (iron and steel). Indirect emissions are given for buyers' Scope 3 reporting.</p>

<h2>Factors used</h2>
<table><tr><th>Carrier</th><th>Unit</th><th class="n">MJ/unit</th><th class="n">kg CO&#8322;/unit</th><th>Source</th></tr>
{% for f in factor_rows %}<tr><td>{{ f['Energy carrier'] }}</td><td>{{ f['Unit'] }}</td><td class="n">{{ f['MJ per unit'] }}</td><td class="n">{{ f['kg CO2 per unit'] }}</td><td>{{ f['Source'] }}</td></tr>{% endfor %}
</table>

<h2>Method and limits</h2>
<ul>{% for n in e.notes %}<li>{{ n }}</li>{% endfor %}
<li>This is verification-ready data, not a verification. CBAM declarations using actual values must be verified by an accredited verifier; default values need no verification but carry a markup (10% in 2026, rising to 30% in 2028).</li></ul>

<h2>Appendix &middot; Source documents</h2>
<table><tr><th>#</th><th>Document</th><th>Type</th><th>SHA-256</th></tr>
{% for d in documents %}<tr><td>{{ d.id }}</td><td>{{ d.name }}</td><td>{{ d.kind }}</td><td class="hash">{{ d.sha256 }}</td></tr>{% endfor %}
</table>
{% if consents %}<h2>Consent to share</h2>
<table><tr><th>When</th><th>Recipient</th><th>Granted by</th><th>Purpose</th></tr>
{% for c in consents %}<tr><td>{{ c.recorded_at }}</td><td>{{ c.recipient }}</td><td>{{ c.granted_by }}</td><td>{{ c.purpose }}</td></tr>{% endfor %}</table>{% endif %}
</body></html>"""


def _documents(results) -> list[dict]:
    frame = results.audit.frame()
    docs = frame[frame["kind"].str.startswith("document:")]
    return [{"id": r.id, "name": r.name, "kind": r.kind.split(":", 1)[1].replace("_", " "), "sha256": r.sha256} for r in docs.itertuples()]


def _consents(results, report_name: str) -> list[dict]:
    frame = results.audit.frame()
    rows = frame[(frame["kind"] == "event:consent") & (frame["name"] == report_name)]
    return [{"recorded_at": r.recorded_at, **json.loads(r.payload)} for r in rows.itertuples()]


def savings_report_html(results, measures: list[str]) -> str:
    s = results.savings
    return _env.from_string(_SAVINGS).render(
        style=_STYLE, synthetic=results.synthetic is not None, factory=results.factory, tariff=results.tariff,
        today=date.today().isoformat(), s=s, m=s.model, quality=results.quality, measures=measures,
        monthly=list(s.monthly.iterrows()), documents=_documents(results), consents=_consents(results, "savings report"),
        chain_ok=results.audit.verify()[0],
    )


def emissions_statement_html(results) -> str:
    from unitwatt.ingest import factor_table

    e = results.emissions
    return _env.from_string(_EMISSIONS).render(
        style=_STYLE, synthetic=results.synthetic is not None, factory=results.factory, today=date.today().isoformat(),
        e=e, rows=list(e.products.iterrows()), quality=results.quality,
        factor_rows=factor_table(results.factors).to_dict("records"),
        documents=_documents(results), consents=_consents(results, "emissions statement"),
    )
