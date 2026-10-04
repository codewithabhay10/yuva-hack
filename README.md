# UnitWatt

A software-only energy and carbon ledger for Indian MSME factories: the working prototype for
Challenge 4 (Smart Manufacturing for Indian MSMEs).

Most small factories see one monthly electricity bill and cannot say how much energy or carbon
goes into each unit they make. UnitWatt turns the paperwork a factory already has (electricity
bills, meter load surveys, fuel purchases, production registers) into four outputs:

1. **Energy per unit of product**, tracked daily, with an alert when it drifts.
2. **A tariff-aware production schedule** that moves flexible loads into cheaper time-of-day hours
   without creating a new demand peak.
3. **Audit-ready proof of savings** against a statistically valid baseline (IPMVP Option C), the
   evidence ADEETIE asks for.
4. **Product-level embedded emissions** for buyers and EU CBAM reporting.

No new hardware is needed.

## Architecture

![UnitWatt architecture](docs/architecture.svg)

Every module reads one daily ledger. The energy model fitted on it is reused as the savings
baseline, to split emissions across products, and as the expectation the drift alarm checks
against. The tariff engine reprices bills and prices every 15-minute slot for the scheduler.
Dashed boxes are built but not live-tested or use placeholder values; dotted boxes are planned.

## Run it

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

streamlit run app.py          # the dashboard
python -m unitwatt            # the same pipeline from the command line; writes reports to data/generated/
pytest                        # 46 tests
```

Photo and PDF bill reading uses Claude's vision with structured output and is optional: set
`ANTHROPIC_API_KEY` to turn it on. Everything else runs offline.

## The demo factory

There is no factory partner yet, so the prototype runs on a **synthetic forging unit**,
"Shakti Forge Works, Patna": 48 workers, three product lines (flanges, crankshaft blanks, gear
blanks), 300 kVA contract demand, billed under the Bihar HT industrial ToD structure (120% of the
base rate in the evening peak, 80% in solar hours). It has six months of 15-minute meter data,
daily registers, fuel invoices and a generator log. The data has the faults the product is meant
to find planted in it:

| Planted in the data | What UnitWatt finds |
|---|---|
| 16 kW running at night and on Sundays (air leaks, machines left on) | Idle load = 8.1% of energy (P9) |
| Billet heating in the evening peak | ₹44,000/month saving from the optimised schedule (P12) |
| Everything restarted together after a power cut on 19 May | Excess-demand penalty traced to 10:30 on 19 May (P10) |
| Capacitor bank failed in June | ₹20,000 power-factor penalty (P10) |
| Owner acts on 1 July: half the idle load cut, heating moved to solar hours | 11,847 kWh avoided, 6.6% ±8%, all M&V checks pass (P15) |
| Induction-heater lining starts wearing on 31 Aug | CUSUM alarm on 12 Sep (P11) |
| Meter export gaps, duplicate rows, two missing register pages | Gaps filled from the bill and labelled estimated; data-quality score (P3, P6, P7) |

The regression recovers the hidden per-product energy (380 / 520 / 450 kWh/t) to within 1%
without sub-meters, and the impact-model tab reproduces the deep dive's numbers exactly:
₹51,200 a month, ₹6.14 lakh and 19.4 tCO₂ a year for one unit, and ₹61.4 crore for 1,000 units.

The result is deliberately honest: zero-capex actions get this unit to **6.6%, short of ADEETIE's
10%**. The dashboard shows the gap in kWh and treats it as the case for an investment-grade audit.

## The dashboard

| Tab | For | What it shows |
|---|---|---|
| Owner | Owner | Money that could have been kept this month, top three actions, the WhatsApp message in English or Hindi |
| Bill check | Owner, accountant | The ten-minute bill check, contract-demand sizing, bill entry with arithmetic checks and confirmation |
| Energy per product | Supervisor, auditor | Regression SEC with 90% intervals, 15-minute load heatmap, idle-waste analysis |
| Schedule | Supervisor | CP-SAT schedule against today's schedule and a naive shift, with the owner's shift limits and a peak cap |
| Savings proof | Banker, auditor, scheme officer | IPMVP Option C savings, ASHRAE 14 uncertainty, ADEETIE test, consent-gated report download |
| Carbon & CBAM | Export buyer, EU importer | Scope 1, 2 and precursor emissions per tonne, CBAM coverage by HSN, default values vs actuals |
| Alerts | Owner, electrician | CUSUM and EWMA drift monitor with a plain-language explanation |
| Data & audit | Everyone | Data-quality score, meter gaps, fuel stock-flow, the daily ledger, SHA-256 audit chain, 30-second chat entry |
| Impact model | Judges | The deep dive's assumptions as inputs, with the scaling scenario and half-shift sensitivity |

## How the code maps to the deep dive

| Problem | Module | Approach |
|---|---|---|
| P1 Bills in many layouts | `schemas.py`, `extract.py` | Fixed Pydantic schema; Claude vision fills it; zone-sum, kVAh ≥ kWh and line-item checks; confirmation screen |
| P2 Handwritten registers | `ingest.parse_daily_entry`, `extract.extract_register` | 30-second chat entry (Hindi, English, Devanagari digits, kg/quintal); register photo extraction |
| P3 Meter CSV formats | `ingest.read_load_survey` | One adapter per format into a standard 15-minute series; gaps flagged, never silently filled |
| P4 Purchases ≠ consumption | `ingest.fuel_consumption` | Opening + purchases − closing, diesel reconciled with the generator log |
| P5 Incompatible units | `config/factors.yaml` | Everything to MJ and kWh-eq; every factor carries its source; supplier overrides |
| P6, P7 One trustworthy ledger | `ledger.py` | Daily table; bill residual spread over gap days by production; visible data-quality score |
| P8 Energy per product | `analytics.fit_energy_model` | Non-negative least squares, block-bootstrap intervals, benchmark prior when data is short |
| P9 Idle waste | `analytics.idle_analysis` | Non-production-hours load against an essential-load floor, in rupees |
| P10 Tariff complexity | `tariff.py`, `config/tariffs/*.yaml` | Versioned tariff configs; bill simulator and repricer; bill forensics; contract-demand sizing |
| P11 Slow degradation | `analytics.drift_monitor` | CUSUM and EWMA on residuals from a re-baselined model |
| P12 Shifting creates peaks | `scheduler.py` | OR-Tools CP-SAT: energy + demand + excess + reheat cost; machine, order and shift constraints |
| P13 Too many recommendations | `opportunities.rank_opportunities` | Ranked by ₹/year, confidence and payback; top three shown |
| P14 Plain language | `messages.py` | Template-constrained: every number comes from a computed field (tested) |
| P15 Proof of savings | `mv.py` | IPMVP Option C, CV(RMSE), NMBE, R², ASHRAE 14 fractional savings uncertainty |
| P16 Product emissions, CBAM | `emissions.py` | Scope 1, 2 and precursors allocated with P8, reconciling to the meter; CBAM scope by HSN |
| P17 Traceability | `audit.py`, `report.py` | SHA-256 per document, hash-chained append-only log, source appendix on every report |
| P19 Sensitive data | `audit.record_consent` | Explicit consent recorded before any report is shared |
| P20 Every state differs | `config/` | Tariffs, sectors and factories are data, not code |

## What is real and what is illustrative

- **From the deep dive's cited sources:** the Bihar ToD multipliers, Kerala's 75% billing-demand
  floor and 50% excess surcharge, the CEA grid factor (0.675 tCO₂/MWh), IPCC fuel factors, the
  CBAM scope rules, and the LBNL and ASHRAE M&V thresholds.
- **Placeholders, marked `illustrative` in the configs and the UI:** energy and demand rates, ToD
  windows, PF slabs, electricity duty, cluster benchmarks, billet precursor emissions and EU default
  values. Check them against the current tariff order and BEE mapping report before quoting a
  rupee figure to a real factory.
- **All factory data is synthetic.** Swapping in one real unit's bills is the most valuable next step.

## Next steps toward the pilot

- Load one real factory's bills: add its DISCOM tariff as a YAML file and its profile under
  `config/factories/`.
- A Telegram bot for the prototype and the WhatsApp Cloud API for the pilot, built on
  `parse_daily_entry` and `owner_message`.
- Voice notes through Bhashini or AI4Bharat text-to-speech.
- PostgreSQL/TimescaleDB with row-level security in place of SQLite; a FastAPI layer over `pipeline.run`.
- PDF rendering of the reports with WeasyPrint (the HTML is already print-ready).
