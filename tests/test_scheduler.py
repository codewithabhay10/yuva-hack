from dataclasses import replace

import pytest

from unitwatt.scheduler import current_schedule, naive_shift, optimise, problem_from_factory


@pytest.fixture(scope="module")
def problem(factory, tariff):
    return problem_from_factory(factory, tariff)


@pytest.fixture(scope="module")
def schedules(problem):
    return {"current": current_schedule(problem), "naive": naive_shift(problem), "optimised": optimise(problem, 10.0)}


def test_optimised_is_cheapest(schedules):
    assert schedules["optimised"].total_rs <= schedules["naive"].total_rs <= schedules["current"].total_rs


def test_naive_shift_creates_the_new_peak(schedules):
    assert schedules["naive"].peak_kva > schedules["optimised"].peak_kva > schedules["current"].peak_kva


def test_constraints_hold(problem, schedules):
    starts = schedules["optimised"].starts
    for job in problem.jobs:
        s = starts[job.id]
        assert problem.earliest <= s and s + job.slots <= problem.latest_end
        if job.after:
            prev = problem.job(job.after)
            gap = s - (starts[prev.id] + prev.slots)
            assert 0 <= gap <= job.max_gap_slots
    by_machine = {}
    for job in problem.jobs:
        by_machine.setdefault(job.machine, []).append(range(starts[job.id], starts[job.id] + job.slots))
    for spans in by_machine.values():
        slots = [t for span in spans for t in span]
        assert len(slots) == len(set(slots)), "two batches on one machine at once"


def test_owner_limits_and_peak_cap(problem):
    tight = replace(problem, earliest=36, latest_end=88)  # 09:00 to 22:00
    res = optimise(tight, 10.0, max_peak_kva=250)
    assert min(res.starts.values()) >= 36
    assert res.peak_kva <= 250 + 1e-6
