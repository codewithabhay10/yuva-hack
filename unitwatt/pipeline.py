"""End to end: five kinds of paperwork in, four deliverables out.

``run`` works on files exactly as a factory hands them over; ``run_demo`` first generates
the synthetic forging unit and writes its files, so the demo exercises the same adapters a
real unit would.
"""

from __future__ import annotations

import tempfile
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path

import pandas as pd

from unitwatt import ingest
from unitwatt.analytics import DriftResult, EnergyModel, IdleResult, drift_monitor, fit_energy_model, idle_analysis
from unitwatt.audit import AuditLog
from unitwatt.emissions import EmissionsResult, product_emissions
from unitwatt.ledger import DataQuality, build_ledger, data_quality
from unitwatt.messages import OwnerFacts
from unitwatt.mv import SavingsResult, measure_savings
from unitwatt.opportunities import (
    BillFinding,
    Opportunity,
    bill_forensics,
    optimise_contract_demand,
    rank_opportunities,
)
from unitwatt.profiles import Factors, Factory, load_factors, load_factory
from unitwatt.scheduler import ScheduleProblem, ScheduleResult, compare, problem_from_factory
from unitwatt.synthetic import Scenario, SyntheticData, generate
from unitwatt.tariff import Tariff, get_tariff


@dataclass
class InputFiles:
    bills: list[Path]
    production: Path
    load_survey: Path | None = None
    dg_log: Path | None = None
    fuel_purchases: Path | None = None
    fuel_stock: Path | None = None


@dataclass
class Windows:
    baseline: tuple[date, date]
    reporting: tuple[date, date] | None = None
    drift_reference: tuple[date, date] | None = None
    latest_month: tuple[date, date] | None = None


@dataclass
class Results:
    factory: Factory
    tariff: Tariff
    factors: Factors
    windows: Windows
    survey: ingest.LoadSurvey | None
    bills: list[ingest.LoadedBill]
    fuel_periods: pd.DataFrame
    ledger: pd.DataFrame
    quality: DataQuality
    baseline_model: EnergyModel
    idle_baseline: IdleResult | None
    idle_latest: IdleResult | None
    problem: ScheduleProblem
    schedules: dict[str, ScheduleResult]
    findings: list[BillFinding]
    contract_demand: pd.DataFrame
    savings: SavingsResult | None
    drift_model: EnergyModel | None
    drift: DriftResult | None
    emissions: EmissionsResult
    opportunities_baseline: list[Opportunity]
    opportunities_latest: list[Opportunity]
    owner_facts: OwnerFacts
    audit: AuditLog
    source_entries: dict[str, int] = field(default_factory=dict)
    synthetic: SyntheticData | None = None

    @property
    def bill_documents(self):
        return [b.bill for b in self.bills]


def average_energy_price(bills, start: date, end: date) -> float:
    chosen = [b.bill for b in bills if b.bill.period_start >= start and b.bill.period_end <= end] or [b.bill for b in bills]
    units = sum(b.billing_units for b in chosen)
    return sum(b.energy_charges for b in chosen) / units if units else 0.0


def default_windows(ledger: pd.DataFrame) -> Windows:
    first, last = ledger.index.min(), ledger.index.max()
    baseline_end = min(first + timedelta(days=89), last)
    latest_start = date(last.year, last.month, 1)
    reporting = (baseline_end + timedelta(days=1), last) if baseline_end < last else None
    return Windows(baseline=(first, baseline_end), reporting=reporting, latest_month=(latest_start, last))


def run(inputs: InputFiles, factory: Factory, windows: Windows | None = None, audit: AuditLog | None = None) -> Results:
    audit = audit or AuditLog()
    factors = load_factors()
    sources: dict[str, int] = {}

    def register(kind: str, path: Path | None) -> None:
        if path is not None and path.exists():
            sources[path.name] = audit.add_document(kind, path.name, path.read_bytes()).id

    for p in inputs.bills:
        register("electricity_bill", p)
    register("load_survey", inputs.load_survey)
    register("production_register", inputs.production)
    register("dg_log", inputs.dg_log)
    register("fuel_invoices", inputs.fuel_purchases)
    register("fuel_stock", inputs.fuel_stock)

    bills = [ingest.load_bill_json(p) for p in inputs.bills]
    bills.sort(key=lambda b: b.bill.period_start)
    tariff = get_tariff(factory.tariff_id, on=bills[0].bill.period_start if bills else None)
    survey = ingest.read_load_survey(inputs.load_survey) if inputs.load_survey else None
    production = ingest.read_production(inputs.production, factory)
    dg_log = pd.read_csv(inputs.dg_log) if inputs.dg_log else None
    fuel_periods = pd.DataFrame()
    if inputs.fuel_purchases and inputs.fuel_stock:
        fuel_periods = ingest.fuel_consumption(pd.read_csv(inputs.fuel_purchases), pd.read_csv(inputs.fuel_stock), dg_log)

    ledger = build_ledger(survey, production, [b.bill for b in bills], factory, tariff, factors, dg_log, fuel_periods)
    quality = data_quality(ledger, survey, [b.issues for b in bills], fuel_periods)
    windows = windows or default_windows(ledger)
    products = factory.product_ids
    prior = {p: float(factory.sector_template.get("marginal_sec_prior_kwh_per_t", 450)) for p in products}

    baseline_model = fit_energy_model(ledger, products, *windows.baseline, prior_sec=prior)
    idle_baseline = idle_analysis(survey.data, ledger, factory, tariff, *windows.baseline) if survey else None
    latest = windows.latest_month or windows.baseline
    idle_latest = idle_analysis(survey.data, ledger, factory, tariff, *latest) if survey else None

    problem = problem_from_factory(factory, tariff)
    schedules = compare(problem, time_limit_s=10.0)
    findings = bill_forensics([b.bill for b in bills], tariff, survey.data if survey else None)
    contract_demand = optimise_contract_demand([b.bill for b in bills], tariff)

    savings = None
    if windows.reporting:
        savings = measure_savings(
            ledger, products, windows.baseline, windows.reporting,
            rs_per_kwh=average_energy_price(bills, *windows.reporting) * (1 + tariff.electricity_duty_pct / 100),
            grid_kg_per_kwh=factors.grid_kg_per_kwh,
        )

    drift_model = drift = None
    if windows.drift_reference:
        drift_model = fit_energy_model(ledger, products, *windows.drift_reference)
        drift = drift_monitor(ledger, drift_model, windows.drift_reference[1] + timedelta(days=1))

    em_window = windows.reporting or windows.baseline
    em_model = fit_energy_model(ledger, products, *em_window, prior_sec=prior)
    emissions = product_emissions(ledger, em_model, factory, factors, *em_window)

    common = dict(
        ledger=ledger,
        bills=[b.bill for b in bills],
        tariff=tariff,
        problem=problem,
        sector_benchmark=factory.sector_template.get("benchmarks"),
        grid_kg_per_kwh=factors.grid_kg_per_kwh,
        data_quality_score=quality.score,
    )
    opp_baseline = rank_opportunities(
        window=windows.baseline, idle=idle_baseline, schedule=schedules, drift=None,
        avg_rs_per_kwh=average_energy_price(bills, *windows.baseline), **common,
    )
    opp_latest = rank_opportunities(
        window=latest, idle=idle_latest, schedule=schedules, drift=drift,
        avg_rs_per_kwh=average_energy_price(bills, *latest), **common,
    )

    owner_facts = _owner_facts(factory, ledger, latest, opp_latest, savings, drift)
    audit.add_event(
        "analysis", "pipeline run",
        {"windows": windows.__dict__, "tariff": f"{tariff.id} from {tariff.effective_from}", "data_quality": quality.score,
         "baseline_cv_rmse": baseline_model.cv_rmse, "savings_kwh": savings.avoided_kwh if savings else None},
    )
    return Results(
        factory=factory, tariff=tariff, factors=factors, windows=windows, survey=survey, bills=bills,
        fuel_periods=fuel_periods, ledger=ledger, quality=quality, baseline_model=baseline_model,
        idle_baseline=idle_baseline, idle_latest=idle_latest, problem=problem, schedules=schedules,
        findings=findings, contract_demand=contract_demand, savings=savings, drift_model=drift_model, drift=drift,
        emissions=emissions, opportunities_baseline=opp_baseline, opportunities_latest=opp_latest,
        owner_facts=owner_facts, audit=audit, source_entries=sources,
    )


def _owner_facts(
    factory: Factory,
    ledger: pd.DataFrame,
    latest: tuple[date, date],
    opportunities: list[Opportunity],
    savings: SavingsResult | None,
    drift: DriftResult | None,
) -> OwnerFacts:
    start, end = latest
    month = ledger[(ledger.index >= start) & (ledger.index <= end)]
    prev_end = start - timedelta(days=1)
    prev = ledger[(ledger.index >= date(prev_end.year, prev_end.month, 1)) & (ledger.index <= prev_end)]

    def sec(df: pd.DataFrame) -> float:
        t = df["total_t"].sum()
        return float(df["elec_kwh"].sum() / t) if t else float("nan")

    sec_now, sec_prev = sec(month), sec(prev)
    actionable = [o for o in opportunities if o.id != "benchmark"]
    top = actionable[0] if actionable else (opportunities[0] if opportunities else None)
    saved_kwh = saved_rs = 0.0
    if savings is not None:
        sel = savings.daily[(savings.daily.index >= start) & (savings.daily.index <= end)]
        saved_kwh = float(sel["avoided"].sum())
        saved_rs = saved_kwh * savings.rs_per_kwh
    alert_since = drift.alarm_date if drift and drift.alarm_date and drift.alarm_date <= end else None
    return OwnerFacts(
        owner_name=factory.owner_name,
        factory_name=factory.name.split(" (")[0],
        month=start,
        rupees_lost_month=sum(o.rupees_per_year for o in actionable) / 12,
        saved_rs_month=saved_rs,
        saved_kwh_month=saved_kwh,
        sec_kwh_per_t=sec_now,
        sec_change_pct=(sec_now / sec_prev - 1) * 100 if sec_prev == sec_prev and sec_prev else 0.0,
        top_action_title=top.title if top else "Upload this month's bill",
        top_action_title_hi=top.title_hi if top else "इस महीने का बिल भेजें",
        top_action_rs_month=top.rupees_per_year / 12 if top else 0.0,
        alert_since=alert_since,
        alert_pct=drift.recent_excess_pct * 100 if alert_since else 0.0,
    )


# What the demo owner changed on 1 July; a real factory records its own measures.
DEMO_MEASURES = [
    "Night and Sunday switch-off routine; compressed-air leaks repaired (from 1 Jul 2026)",
    "Billet heating and shot blasting moved into solar hours (from 1 Jul 2026)",
]

DEMO_WINDOWS = Windows(
    baseline=(date(2026, 4, 1), date(2026, 6, 30)),
    reporting=(date(2026, 7, 1), date(2026, 9, 30)),
    drift_reference=(date(2026, 7, 1), date(2026, 8, 15)),
    latest_month=(date(2026, 9, 1), date(2026, 9, 30)),
)


def run_demo(seed: int = 7, workdir: Path | None = None, scenario: Scenario | None = None) -> Results:
    factory = load_factory("demo_forge")
    tariff = get_tariff(factory.tariff_id)
    data = generate(factory, tariff, scenario or Scenario(seed=seed))
    workdir = workdir or Path(tempfile.mkdtemp(prefix="unitwatt_demo_"))
    paths = data.write(workdir)
    inputs = InputFiles(
        bills=sorted(p for k, p in paths.items() if k.startswith("bill_")),
        production=paths["production"],
        load_survey=paths["load_survey"],
        dg_log=paths["dg_log"],
        fuel_purchases=paths["fuel_purchases"],
        fuel_stock=paths["fuel_stock"],
    )
    results = run(inputs, factory, DEMO_WINDOWS)
    results.synthetic = data
    return results
