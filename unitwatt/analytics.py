"""Stage 3: understanding where energy goes (P8, P9, P11).

P8  Energy per product without sub-meters: regress daily electricity on daily output of each
    product line, with non-negative coefficients, and a benchmark prior when data is short.
P9  Idle waste: load in non-production hours (nights, Sundays, holidays) against an
    essential-load floor, in rupees.
P11 Slow efficiency loss: compare each day with the model's prediction and run a CUSUM and
    an EWMA on the gap. Explainable alerts, not a black box.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date

import numpy as np
import pandas as pd
from scipy.optimize import nnls
from scipy.stats import t as student_t

from unitwatt.profiles import Factory, hhmm_to_minutes
from unitwatt.tariff import SLOT_HOURS, Tariff

# --------------------------------------------------------------------------------------
# P8: energy model
# --------------------------------------------------------------------------------------

PRIOR_UNTIL_DAYS = 30  # with 30 to 60 days of varied product mix the regression stands on its own


@dataclass
class EnergyModel:
    products: list[str]
    coef: dict[str, float]
    ci90: dict[str, tuple[float, float]]
    n: int
    r2: float
    rmse: float
    cv_rmse: float
    nmbe: float
    mean_y: float
    rho: float
    period: tuple[date, date]
    method: str
    target: str = "elec_kwh"
    warnings: list[str] = field(default_factory=list)
    fitted: pd.DataFrame = field(default_factory=pd.DataFrame, repr=False)

    @property
    def terms(self) -> list[str]:
        return ["base", "production_day"] + self.products

    @property
    def n_params(self) -> int:
        return len(self.terms)

    @property
    def sec(self) -> dict[str, float]:
        return {p: self.coef[p] for p in self.products}

    def predict(self, ledger: pd.DataFrame) -> pd.Series:
        x = design_matrix(ledger, self.products)
        beta = np.array([self.coef[t] for t in self.terms])
        return pd.Series(x @ beta, index=ledger.index, name="predicted")

    def summary(self) -> pd.DataFrame:
        labels = {"base": "Always-on base load (kWh/day)", "production_day": "Production-day fixed load (kWh/day)"}
        rows = []
        for term in self.terms:
            lo, hi = self.ci90.get(term, (np.nan, np.nan))
            rows.append(
                {"Term": labels.get(term, f"{term} (kWh per tonne)"), "Estimate": self.coef[term], "90% low": lo, "90% high": hi}
            )
        return pd.DataFrame(rows)


def design_matrix(ledger: pd.DataFrame, products: list[str]) -> np.ndarray:
    cols = [np.ones(len(ledger)), ledger["is_production_day"].astype(float).to_numpy()]
    cols += [ledger[f"t_{p}"].fillna(0.0).astype(float).to_numpy() for p in products]
    return np.column_stack(cols)


def model_rows(ledger: pd.DataFrame, start: date | None = None, end: date | None = None, exclude_estimated: bool = True) -> pd.Series:
    mask = ledger["production_recorded"] & ~ledger["outlier"]
    if exclude_estimated:
        mask &= ~ledger["estimated"]
    if start is not None:
        mask &= ledger.index >= start
    if end is not None:
        mask &= ledger.index <= end
    return mask


def _nnls_with_prior(x: np.ndarray, y: np.ndarray, prior: np.ndarray | None, prior_rows: np.ndarray | None) -> np.ndarray:
    if prior is None or prior_rows is None:
        return nnls(x, y)[0]
    return nnls(np.vstack([x, prior_rows]), np.concatenate([y, prior_rows @ prior]))[0]


def fit_energy_model(
    ledger: pd.DataFrame,
    products: list[str],
    start: date | None = None,
    end: date | None = None,
    prior_sec: dict[str, float] | None = None,
    prior_days: float = 15.0,
    target: str = "elec_kwh",
    bootstrap: int = 200,
    seed: int = 0,
) -> EnergyModel:
    """energy = base + production_day + sum(SEC_i x tonnes_i), all coefficients >= 0."""
    rows = model_rows(ledger, start, end)
    data = ledger[rows]
    warnings: list[str] = []
    x = design_matrix(data, products)
    y = data[target].astype(float).to_numpy()
    n, p = x.shape

    prior, prior_rows, method = None, None, "regression"
    if prior_sec is not None and n < PRIOR_UNTIL_DAYS:
        # A benchmark (or machine-list) prior, worth `prior_days` days of data, fading as data grows.
        weight = prior_days * (PRIOR_UNTIL_DAYS - n) / PRIOR_UNTIL_DAYS
        prior = np.concatenate([[0.0, 0.0], [prior_sec[q] for q in products]])
        scale = np.sqrt(weight * np.mean(x[:, 2:] ** 2, axis=0)) if n else np.ones(len(products))
        prior_rows = np.zeros((len(products), p))
        prior_rows[np.arange(len(products)), np.arange(2, p)] = scale
        method = "regression with benchmark prior"
        warnings.append(
            f"Only {n} usable days: SEC is pulled towards the cluster benchmark until {PRIOR_UNTIL_DAYS} days exist."
        )

    shares = data[[f"t_{q}" for q in products]].div(data["total_t"].replace(0, np.nan), axis=0)
    if n >= 10 and shares.std().min() < 0.03:
        warnings.append("Product mix barely varies, so per-product SEC is weakly identified.")
    if n < max(p + 5, 20) and prior is None:
        return _engineering_estimate(ledger, products, rows, prior_sec, target, warnings)

    beta = _nnls_with_prior(x, y, prior, prior_rows)
    fitted = x @ beta
    resid = y - fitted
    dof = max(n - p, 1)
    rmse = float(np.sqrt(np.sum(resid**2) / dof))
    mean_y = float(np.mean(y))
    sst = float(np.sum((y - mean_y) ** 2))
    r2 = 1 - float(np.sum(resid**2)) / sst if sst > 0 else 0.0
    rho = float(np.corrcoef(resid[:-1], resid[1:])[0, 1]) if n > 3 else 0.0

    # Moving-block bootstrap (weekly blocks) for 90% intervals that respect autocorrelation.
    rng = np.random.default_rng(seed)
    block = 7
    n_blocks = int(np.ceil(n / block))
    draws = []
    for _ in range(bootstrap):
        starts = rng.integers(0, max(n - block, 1), n_blocks)
        idx = np.concatenate([np.arange(s, min(s + block, n)) for s in starts])[:n]
        draws.append(_nnls_with_prior(x[idx], y[idx], prior, prior_rows))
    draws = np.array(draws) if draws else np.empty((0, p))
    terms = ["base", "production_day"] + products
    ci90 = {
        term: (float(np.percentile(draws[:, i], 5)), float(np.percentile(draws[:, i], 95))) if len(draws) else (np.nan, np.nan)
        for i, term in enumerate(terms)
    }
    fitted_df = pd.DataFrame({"actual": y, "fitted": fitted, "residual": resid}, index=data.index)
    return EnergyModel(
        products=products,
        coef={term: float(b) for term, b in zip(terms, beta)},
        ci90=ci90,
        n=n,
        r2=r2,
        rmse=rmse,
        cv_rmse=rmse / mean_y if mean_y else np.nan,
        nmbe=float(np.sum(resid)) / (dof * mean_y) if mean_y else np.nan,
        mean_y=mean_y,
        rho=rho,
        period=(data.index.min(), data.index.max()),
        method=method,
        target=target,
        warnings=warnings,
        fitted=fitted_df,
    )


def _engineering_estimate(
    ledger: pd.DataFrame,
    products: list[str],
    rows: pd.Series,
    prior_sec: dict[str, float] | None,
    target: str,
    warnings: list[str],
) -> EnergyModel:
    """Fallback when the regression cannot separate products: benchmark SEC scaled to the meter."""
    data = ledger[rows]
    idle_days = data[~data["is_production_day"]]
    base = float(idle_days[target].median()) if len(idle_days) else 0.0
    prior_sec = prior_sec or {q: 1.0 for q in products}
    prod = data[data["is_production_day"]]
    variable = sum(prior_sec[q] * prod[f"t_{q}"].sum() for q in products)
    scale = float((prod[target].sum() - base * len(prod)) / variable) if variable > 0 else 1.0
    coef = {"base": base, "production_day": 0.0, **{q: prior_sec[q] * max(scale, 0.0) for q in products}}
    warnings = warnings + ["Not enough varied data for a regression: engineering estimate from benchmarks, scaled to the meter."]
    return EnergyModel(
        products=products, coef=coef, ci90={}, n=len(data), r2=np.nan, rmse=np.nan, cv_rmse=np.nan, nmbe=np.nan,
        mean_y=float(data[target].mean()) if len(data) else np.nan, rho=0.0,
        period=(data.index.min(), data.index.max()) if len(data) else (None, None),
        method="engineering estimate", target=target, warnings=warnings,
    )


# --------------------------------------------------------------------------------------
# P9: idle waste
# --------------------------------------------------------------------------------------


@dataclass
class IdleResult:
    essential_kw: float
    base_kw: float
    idle_kwh: float
    total_kwh: float
    avoidable_kwh: float
    avoidable_rs: float
    nights: pd.DataFrame
    by_month: pd.DataFrame
    profile: pd.DataFrame

    @property
    def idle_pct(self) -> float:
        return self.idle_kwh / self.total_kwh if self.total_kwh else 0.0


def non_production_mask(index: pd.DatetimeIndex, ledger: pd.DataFrame, factory: Factory) -> np.ndarray:
    minutes = index.hour * 60 + index.minute
    in_shift = (minutes >= hhmm_to_minutes(factory.shift_start)) & (minutes < hhmm_to_minutes(factory.shift_end))
    prod_day = ledger["is_production_day"].reindex(index.date).fillna(False).to_numpy(dtype=bool)
    return ~(in_shift & prod_day)


def idle_analysis(
    survey_data: pd.DataFrame,
    ledger: pd.DataFrame,
    factory: Factory,
    tariff: Tariff,
    start: date | None = None,
    end: date | None = None,
) -> IdleResult:
    data = survey_data.dropna(subset=["kwh"])
    if start is not None:
        data = data[data.index.date >= start]
    if end is not None:
        data = data[data.index.date <= end]
    idle = non_production_mask(data.index, ledger, factory)
    kw = data["kwh"] / SLOT_HOURS
    essential = factory.essential_night_load_kw
    price = tariff.price_series(data.index)
    avoidable = ((kw - essential).clip(lower=0) * SLOT_HOURS).where(idle, 0.0)
    df = pd.DataFrame({"kwh": data["kwh"], "idle": idle, "avoidable_kwh": avoidable, "rs": avoidable * price, "kw": kw})

    nights = df[df["idle"]].groupby(df.index[df["idle"]].date)["kw"].median().rename("median_idle_kw").to_frame()
    nights.index.name = "date"
    month = pd.Index(df.index.to_period("M").strftime("%b %Y"), name="month")
    by_month = pd.DataFrame(
        {
            "total_kwh": df["kwh"].groupby(month, sort=False).sum(),
            "idle_kwh": df["kwh"].where(df["idle"], 0.0).groupby(month, sort=False).sum(),
            "avoidable_kwh": df["avoidable_kwh"].groupby(month, sort=False).sum(),
            "avoidable_rs": df["rs"].groupby(month, sort=False).sum(),
        }
    )
    by_month["idle_pct"] = by_month["idle_kwh"] / by_month["total_kwh"]

    day_type = np.where(ledger["is_production_day"].reindex(df.index.date).fillna(False).to_numpy(dtype=bool), "Production day", "Non-production day")
    profile = df.groupby([day_type, df.index.strftime("%H:%M")])["kw"].mean().unstack(0)
    return IdleResult(
        essential_kw=essential,
        base_kw=float(df.loc[df["idle"], "kw"].median()) if df["idle"].any() else 0.0,
        idle_kwh=float(df.loc[df["idle"], "kwh"].sum()),
        total_kwh=float(df["kwh"].sum()),
        avoidable_kwh=float(df["avoidable_kwh"].sum()),
        avoidable_rs=float(df["rs"].sum()),
        nights=nights,
        by_month=by_month,
        profile=profile,
    )


# --------------------------------------------------------------------------------------
# P11: drift monitor
# --------------------------------------------------------------------------------------


@dataclass
class DriftResult:
    daily: pd.DataFrame
    alarm_date: date | None
    k: float
    h: float
    reference: tuple[date, date]
    recent_excess_pct: float
    excess_kwh_since_alarm: float


def drift_monitor(
    ledger: pd.DataFrame,
    model: EnergyModel,
    start: date,
    end: date | None = None,
    k: float = 0.5,
    h: float = 5.0,
    ewma_lambda: float = 0.2,
) -> DriftResult:
    """Tabular CUSUM on standardised daily residuals of production days after ``start``."""
    rows = model_rows(ledger, start, end) & ledger["is_production_day"]
    data = ledger[rows]
    predicted = model.predict(data)
    actual = data[model.target]
    resid = actual - predicted
    z = resid / model.rmse
    pct = resid / predicted

    cusum_hi, cusum_lo, ewma = [], [], []
    s_hi = s_lo = 0.0
    e = 0.0
    for value, p in zip(z.to_numpy(), pct.to_numpy()):
        s_hi = max(0.0, s_hi + value - k)
        s_lo = min(0.0, s_lo + value + k)
        e = ewma_lambda * p + (1 - ewma_lambda) * e
        cusum_hi.append(s_hi)
        cusum_lo.append(s_lo)
        ewma.append(e)
    daily = pd.DataFrame(
        {"actual": actual, "predicted": predicted, "residual": resid, "z": z, "pct": pct,
         "cusum_hi": cusum_hi, "cusum_lo": cusum_lo, "ewma_pct": ewma}
    )
    daily["alarm"] = daily["cusum_hi"] > h
    alarm_date = daily.index[daily["alarm"]].min() if daily["alarm"].any() else None
    recent = daily.tail(14)
    excess = float(daily.loc[daily.index >= alarm_date, "residual"].sum()) if alarm_date else 0.0
    return DriftResult(
        daily=daily,
        alarm_date=alarm_date,
        k=k,
        h=h,
        reference=model.period,
        recent_excess_pct=float(recent["residual"].sum() / recent["predicted"].sum()) if len(recent) else 0.0,
        excess_kwh_since_alarm=excess,
    )


def t_value(dof: int, confidence: float = 0.90) -> float:
    return float(student_t.ppf(0.5 + confidence / 2, max(dof, 1)))
