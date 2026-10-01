"""Stage 5: audit-ready proof of savings (P15), IPMVP Option C (whole facility).

savings = baseline model adjusted to reporting-period production - actual energy

The baseline model is the P8 regression fitted on the baseline period only. The module
reports the statistics an auditor checks (CV(RMSE), NMBE, R2) and the savings uncertainty
from ASHRAE Guideline 14, and compares the result with ADEETIE's 10% savings requirement.
UnitWatt prepares the data pack; the certified energy auditor still signs the M&V.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date

import numpy as np
import pandas as pd

from unitwatt.analytics import EnergyModel, fit_energy_model, model_rows, t_value

CV_RMSE_LIMIT = 0.25
NMBE_LIMIT = 0.005
ADEETIE_TARGET = 0.10


@dataclass
class Check:
    name: str
    value: str
    threshold: str
    passed: bool


@dataclass
class SavingsResult:
    model: EnergyModel
    baseline: tuple[date, date]
    reporting: tuple[date, date]
    daily: pd.DataFrame
    monthly: pd.DataFrame
    adjusted_baseline_kwh: float
    actual_kwh: float
    avoided_kwh: float
    fsu90: float
    m: int
    n_eff: float
    rs_per_kwh: float
    grid_kg_per_kwh: float
    checks: list[Check] = field(default_factory=list)

    @property
    def savings_pct(self) -> float:
        return self.avoided_kwh / self.adjusted_baseline_kwh if self.adjusted_baseline_kwh else 0.0

    @property
    def uncertainty_kwh(self) -> float:
        return abs(self.avoided_kwh) * self.fsu90

    @property
    def rs_saved(self) -> float:
        return self.avoided_kwh * self.rs_per_kwh

    @property
    def tco2_avoided(self) -> float:
        return self.avoided_kwh * self.grid_kg_per_kwh / 1000

    @property
    def meets_adeetie(self) -> bool:
        return self.savings_pct >= ADEETIE_TARGET

    @property
    def gap_to_target_kwh(self) -> float:
        return max(ADEETIE_TARGET * self.adjusted_baseline_kwh - self.avoided_kwh, 0.0)

    @property
    def model_acceptable(self) -> bool:
        return all(c.passed for c in self.checks if c.name.startswith("Baseline"))


def fractional_savings_uncertainty(cv_rmse: float, n: int, m: int, rho: float, savings_fraction: float, dof: int, confidence: float = 0.90) -> tuple[float, float]:
    """ASHRAE Guideline 14 fractional savings uncertainty, with residual autocorrelation.

    FSU = t * 1.26 * CV * sqrt((n / n') * (1 + 2 / n) * (1 / m)) / F, n' = n (1 - rho) / (1 + rho)
    Negative autocorrelation is treated as zero, which keeps the estimate conservative.
    """
    rho = max(rho, 0.0)
    n_eff = n * (1 - rho) / (1 + rho)
    if savings_fraction <= 0 or m <= 0 or n_eff <= 0:
        return float("inf"), n_eff
    fsu = t_value(dof, confidence) * 1.26 * cv_rmse * np.sqrt((n / n_eff) * (1 + 2 / n) / m) / savings_fraction
    return float(fsu), float(n_eff)


def measure_savings(
    ledger: pd.DataFrame,
    products: list[str],
    baseline: tuple[date, date],
    reporting: tuple[date, date],
    rs_per_kwh: float,
    grid_kg_per_kwh: float,
    target: str = "elec_kwh",
) -> SavingsResult:
    model = fit_energy_model(ledger, products, baseline[0], baseline[1], target=target)
    rows = model_rows(ledger, reporting[0], reporting[1], exclude_estimated=False)
    data = ledger[rows]
    adjusted = model.predict(data)
    actual = data[target]
    daily = pd.DataFrame({"actual": actual, "adjusted_baseline": adjusted})
    daily["avoided"] = daily["adjusted_baseline"] - daily["actual"]
    daily["cumulative_avoided"] = daily["avoided"].cumsum()

    monthly = daily.copy()
    monthly.index = pd.to_datetime(monthly.index)
    monthly = monthly[["actual", "adjusted_baseline", "avoided"]].resample("MS").sum()
    monthly["savings_pct"] = monthly["avoided"] / monthly["adjusted_baseline"]
    monthly.index = monthly.index.strftime("%b %Y")

    adj_total, act_total = float(adjusted.sum()), float(actual.sum())
    avoided = adj_total - act_total
    fraction = avoided / adj_total if adj_total else 0.0
    fsu, n_eff = fractional_savings_uncertainty(model.cv_rmse, model.n, len(data), model.rho, fraction, model.n - model.n_params)

    checks = [
        Check("Baseline CV(RMSE)", f"{model.cv_rmse:.1%}", f"< {CV_RMSE_LIMIT:.0%}", model.cv_rmse < CV_RMSE_LIMIT),
        Check("Baseline NMBE", f"{model.nmbe:.3%}", f"within ±{NMBE_LIMIT:.1%}", abs(model.nmbe) < NMBE_LIMIT),
        Check("Baseline R²", f"{model.r2:.3f}", "> 0.70", model.r2 > 0.70),
        Check("Baseline length", f"{model.n} days", ">= 60 days", model.n >= 60),
        Check(
            "Savings uncertainty (90%)",
            f"±{fsu:.0%} of savings" if np.isfinite(fsu) else "no savings",
            "< 50% of savings",
            bool(np.isfinite(fsu) and fsu < 0.5),
        ),
        Check("ADEETIE 10% demonstrated savings", f"{fraction:.1%}", ">= 10%", fraction >= ADEETIE_TARGET),
    ]
    return SavingsResult(
        model=model,
        baseline=baseline,
        reporting=reporting,
        daily=daily,
        monthly=monthly,
        adjusted_baseline_kwh=adj_total,
        actual_kwh=act_total,
        avoided_kwh=avoided,
        fsu90=fsu,
        m=len(data),
        n_eff=n_eff,
        rs_per_kwh=rs_per_kwh,
        grid_kg_per_kwh=grid_kg_per_kwh,
        checks=checks,
    )
