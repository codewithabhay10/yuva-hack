"""Stage 4: a tariff-aware production schedule (P12).

Shifting loads into cheap hours can create a new demand peak that costs more than it saves.
The optimiser schedules a typical day's shiftable batches with CP-SAT over 96 15-minute
slots and minimises energy cost + demand charges + excess-demand surcharge + furnace reheat
losses, subject to the owner's shift limits, machine capacity (one batch per machine at a
time) and process order (heat before forge, within a maximum gap).
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
from ortools.sat.python import cp_model

from unitwatt.profiles import Factory, hhmm_to_minutes
from unitwatt.tariff import SLOT_HOURS, SLOT_MINUTES, SLOTS_PER_DAY, Tariff


@dataclass(frozen=True)
class Job:
    id: str
    name: str
    machine: str
    kw: float
    slots: int
    current_start: int
    after: str | None = None
    max_gap_slots: int = 0
    reheat_kw: float = 0.0


@dataclass
class ScheduleProblem:
    jobs: list[Job]
    base_kw: np.ndarray
    prices: np.ndarray            # Rs per kWh for each slot
    bands: np.ndarray             # ToD band name for each slot
    earliest: int
    latest_end: int
    demand_rate: float            # Rs per kVA per month
    floor_kva: float
    contract_kva: float
    surcharge: float              # e.g. 0.5 for a 50% excess-demand surcharge
    pf: float
    working_days: int = 26

    def job(self, job_id: str) -> Job:
        return next(j for j in self.jobs if j.id == job_id)


@dataclass
class ScheduleResult:
    starts: dict[str, int]
    load_kw: np.ndarray
    energy_rs: float
    demand_rs: float
    reheat_rs: float
    peak_kw: float
    peak_kva: float
    kwh_by_band: dict[str, float]
    status: str = "evaluated"
    notes: list[str] = field(default_factory=list)

    @property
    def total_rs(self) -> float:
        return self.energy_rs + self.demand_rs + self.reheat_rs


def slot_of(hhmm: str) -> int:
    return hhmm_to_minutes(hhmm) // SLOT_MINUTES


def slot_label(slot: int) -> str:
    minutes = slot * SLOT_MINUTES
    return f"{minutes // 60:02d}:{minutes % 60:02d}"


def problem_from_factory(factory: Factory, tariff: Tariff, working_days: int = 26) -> ScheduleProblem:
    cfg = factory.schedule_day
    base = np.zeros(SLOTS_PER_DAY)
    for load in cfg["base_loads"]:
        base[slot_of(load["start"]) : slot_of(load["end"])] += float(load["kw"])
    jobs = [
        Job(
            id=j["id"],
            name=j["name"],
            machine=j["machine"],
            kw=float(j["kw"]),
            slots=int(round(float(j["hours"]) / SLOT_HOURS)),
            current_start=slot_of(j["current_start"]),
            after=j.get("after"),
            max_gap_slots=int(round(float(j.get("max_gap_hours", 0.0)) / SLOT_HOURS)),
            reheat_kw=float(j.get("reheat_kw", 0.0)),
        )
        for j in cfg["jobs"]
    ]
    minutes = np.arange(SLOTS_PER_DAY) * SLOT_MINUTES
    return ScheduleProblem(
        jobs=jobs,
        base_kw=base,
        prices=tariff.slot_prices(),
        bands=tariff.band_for_minutes(minutes),
        earliest=slot_of(cfg["hard_limits"]["earliest"]),
        latest_end=slot_of(cfg["hard_limits"]["latest_end"]),
        demand_rate=tariff.demand_rate,
        floor_kva=tariff.billing_floor_pct / 100 * factory.contract_demand_kva,
        contract_kva=factory.contract_demand_kva,
        surcharge=tariff.excess_surcharge_pct / 100,
        pf=float(cfg.get("assumed_pf", 0.95)),
        working_days=working_days,
    )


def evaluate(problem: ScheduleProblem, starts: dict[str, int], status: str = "evaluated") -> ScheduleResult:
    """Exact daily cost of a schedule, in rupees."""
    load = problem.base_kw.copy()
    for job in problem.jobs:
        s = starts[job.id]
        load[s : s + job.slots] += job.kw
    energy_kwh = load * SLOT_HOURS
    energy_rs = float(np.sum(energy_kwh * problem.prices))
    peak_kw = float(load.max())
    peak_kva = peak_kw / problem.pf
    billed = max(peak_kva, problem.floor_kva)
    excess = max(0.0, peak_kva - problem.contract_kva)
    demand_rs = (billed + problem.surcharge * excess) * problem.demand_rate / problem.working_days
    reheat_rs = 0.0
    mean_price = float(problem.prices.mean())
    for job in problem.jobs:
        if job.after:
            prev = problem.job(job.after)
            gap = starts[job.id] - (starts[prev.id] + prev.slots)
            reheat_rs += max(gap, 0) * job.reheat_kw * SLOT_HOURS * mean_price
    kwh_by_band: dict[str, float] = {}
    for band in np.unique(problem.bands):
        kwh_by_band[str(band)] = float(energy_kwh[problem.bands == band].sum())
    return ScheduleResult(dict(starts), load, energy_rs, demand_rs, reheat_rs, peak_kw, peak_kva, kwh_by_band, status)


def current_schedule(problem: ScheduleProblem) -> ScheduleResult:
    return evaluate(problem, {j.id: j.current_start for j in problem.jobs}, "as run today")


def naive_shift(problem: ScheduleProblem) -> ScheduleResult:
    """What an owner might do unaided: pack every batch into the cheapest hours, ignoring demand."""
    cheapest = problem.prices.min()
    cheap_slots = np.where(problem.prices == cheapest)[0]
    window_start = max(int(cheap_slots.min()), problem.earliest)
    machine_free: dict[str, int] = {}
    starts: dict[str, int] = {}
    for job in problem.jobs:
        s = max(window_start, machine_free.get(job.machine, 0))
        if job.after:
            prev = problem.job(job.after)
            s = max(s, starts[prev.id] + prev.slots)
        s = min(s, problem.latest_end - job.slots)
        starts[job.id] = s
        machine_free[job.machine] = s + job.slots
    return evaluate(problem, starts, "everything moved to cheap hours")


def optimise(problem: ScheduleProblem, time_limit_s: float = 10.0, max_peak_kva: float | None = None) -> ScheduleResult:
    model = cp_model.CpModel()
    scale = 10  # load in units of 0.1 kW
    x: dict[str, dict[int, cp_model.IntVar]] = {}
    start_var: dict[str, cp_model.IntVar] = {}
    obj_terms = []
    for job in problem.jobs:
        allowed = range(problem.earliest, problem.latest_end - job.slots + 1)
        x[job.id] = {s: model.NewBoolVar(f"x_{job.id}_{s}") for s in allowed}
        model.AddExactlyOne(x[job.id].values())
        start_var[job.id] = model.NewIntVar(problem.earliest, problem.latest_end, f"s_{job.id}")
        model.Add(start_var[job.id] == sum(s * v for s, v in x[job.id].items()))
        for s, v in x[job.id].items():
            cost_paise = int(round(job.kw * SLOT_HOURS * problem.prices[s : s + job.slots].sum() * 100))
            obj_terms.append(cost_paise * v)

    def active(job: Job, t: int) -> list[cp_model.IntVar]:
        return [v for s, v in x[job.id].items() if s <= t < s + job.slots]

    # Process order and furnace reheat losses.
    mean_price = float(problem.prices.mean())
    for job in problem.jobs:
        if job.after:
            prev = problem.job(job.after)
            gap = model.NewIntVar(0, job.max_gap_slots or SLOTS_PER_DAY, f"gap_{job.id}")
            model.Add(gap == start_var[job.id] - start_var[prev.id] - prev.slots)
            obj_terms.append(int(round(job.reheat_kw * SLOT_HOURS * mean_price * 100)) * gap)

    # One batch per machine at a time; identical batches keep their order (symmetry breaking).
    by_machine: dict[str, list[Job]] = {}
    for job in problem.jobs:
        by_machine.setdefault(job.machine, []).append(job)
    for jobs in by_machine.values():
        if len(jobs) > 1:
            for t in range(SLOTS_PER_DAY):
                terms = [v for job in jobs for v in active(job, t)]
                if len(terms) > 1:
                    model.Add(sum(terms) <= 1)
            for a, b in zip(jobs, jobs[1:]):
                if a.kw == b.kw and a.slots == b.slots and a.after is None and b.after is None:
                    model.Add(start_var[b.id] >= start_var[a.id] + a.slots)

    # Demand: billed = max(peak, floor); excess = max(0, peak - contract demand).
    peak = model.NewIntVar(0, 10_000 * scale, "peak")
    for t in range(SLOTS_PER_DAY):
        load_terms = [int(round(job.kw * scale)) * v for job in problem.jobs for v in active(job, t)]
        model.Add(peak >= int(round(problem.base_kw[t] * scale)) + sum(load_terms))
    if max_peak_kva is not None:
        model.Add(peak <= int(max_peak_kva * problem.pf * scale))
    billed = model.NewIntVar(0, 10_000 * scale, "billed")
    excess = model.NewIntVar(0, 10_000 * scale, "excess")
    model.Add(billed >= peak)
    model.Add(billed >= int(round(problem.floor_kva * problem.pf * scale)))
    model.Add(excess >= peak - int(round(problem.contract_kva * problem.pf * scale)))
    per_unit = problem.demand_rate / problem.pf / problem.working_days * 100 / scale  # paise per 0.1 kW per day
    obj_terms.append(int(round(per_unit)) * billed)
    obj_terms.append(int(round(per_unit * problem.surcharge)) * excess)
    model.Minimize(sum(obj_terms))

    solver = cp_model.CpSolver()
    solver.parameters.max_time_in_seconds = time_limit_s
    solver.parameters.num_workers = 8
    status = solver.Solve(model)
    if status not in (cp_model.OPTIMAL, cp_model.FEASIBLE):
        raise RuntimeError("No feasible schedule within the owner's limits.")
    starts = {job.id: int(solver.Value(start_var[job.id])) for job in problem.jobs}
    label = "optimised (proven optimal)" if status == cp_model.OPTIMAL else "optimised (best found in time limit)"
    return evaluate(problem, starts, label)


def compare(problem: ScheduleProblem, time_limit_s: float = 10.0) -> dict[str, ScheduleResult]:
    return {
        "current": current_schedule(problem),
        "naive": naive_shift(problem),
        "optimised": optimise(problem, time_limit_s),
    }
