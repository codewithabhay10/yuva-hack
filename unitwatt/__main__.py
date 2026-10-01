"""Run the demo pipeline from the command line and write its outputs.

    python -m unitwatt [output_dir]
"""

import sys
from pathlib import Path

from unitwatt.messages import format_inr, format_lakh, owner_message
from unitwatt.pipeline import run_demo
from unitwatt.report import emissions_statement_html, savings_report_html


def main() -> None:
    out = Path(sys.argv[1] if len(sys.argv) > 1 else "data/generated")
    r = run_demo(workdir=out / "inputs")
    (out / "unitwatt_savings_report.html").write_text(
        savings_report_html(r, ["Idle-load switch-off and air-leak repair", "Billet heating moved into solar hours"]), encoding="utf-8"
    )
    (out / "unitwatt_emissions_statement.html").write_text(emissions_statement_html(r), encoding="utf-8")
    r.ledger.to_csv(out / "daily_ledger.csv")

    s, m = r.savings, r.baseline_model
    print(f"{r.factory.name}: {r.ledger.index.min()} to {r.ledger.index.max()}, data quality {r.quality.score:.1f}/100")
    print("Energy model: " + ", ".join(f"{p} {m.coef[p]:.0f} kWh/t" for p in m.products) + f" (R² {m.r2:.3f}, CV {m.cv_rmse:.1%})")
    print(f"Savings: {format_inr(s.avoided_kwh)} kWh ({s.savings_pct:.1%}, ±{s.fsu90:.0%}), {format_lakh(s.rs_saved)}, {s.tco2_avoided:.1f} tCO2")
    if r.drift.alarm_date:
        print(f"Drift alarm since {r.drift.alarm_date}")
    print("Top actions:")
    for o in r.opportunities_latest[:3]:
        print(f"  - {o.title}: {format_lakh(o.rupees_per_year)}/year ({o.confidence})")
    print()
    print(owner_message(r.owner_facts, r.factory.language))
    print(f"\nInputs, reports and ledger written to {out}/")


if __name__ == "__main__":
    main()
