from datetime import date

import pytest

from unitwatt.pipeline import run_demo
from unitwatt.profiles import load_factors, load_factory
from unitwatt.synthetic import Scenario
from unitwatt.tariff import get_tariff


@pytest.fixture(scope="session")
def factory():
    return load_factory("demo_forge")


@pytest.fixture(scope="session")
def tariff(factory):
    return get_tariff(factory.tariff_id, on=date(2026, 5, 1))


@pytest.fixture(scope="session")
def factors():
    return load_factors()


@pytest.fixture(scope="session")
def demo(tmp_path_factory):
    return run_demo(seed=7, workdir=tmp_path_factory.mktemp("demo"))


@pytest.fixture(scope="session")
def no_change_demo(tmp_path_factory):
    """Same unit, but the owner changes nothing and nothing wears out."""
    scenario = Scenario(seed=11, actions_date=None, drift_start=None)
    return run_demo(workdir=tmp_path_factory.mktemp("nochange"), scenario=scenario)
