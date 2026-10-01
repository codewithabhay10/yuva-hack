"""The fixed bill schema every bill is turned into, and its arithmetic checks (P1).

Whether a bill arrives as a PDF, a scan, a phone photo or from the tariff simulator, it ends
up as a ``BillDocument``. A misread bill corrupts every downstream number, so every bill
goes through ``validate_bill`` and a human confirmation step before it is saved.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Literal

from pydantic import BaseModel, Field


class ZoneReading(BaseModel):
    zone: str = Field(description="ToD zone name as printed on the bill, e.g. peak, normal, solar, off-peak")
    units: float = Field(description="Units billed in this zone, in the bill's energy basis (kWh or kVAh)")
    charges: float | None = Field(default=None, description="Energy charges for this zone in rupees, if printed")


class BillDocument(BaseModel):
    """One monthly electricity bill, in the shape the rest of the pipeline expects."""

    consumer_number: str | None = Field(default=None, description="Consumer or account number")
    discom: str | None = Field(default=None, description="Distribution company name")
    tariff_category: str | None = Field(default=None, description="Tariff category printed on the bill")
    period_start: date = Field(description="First day of the billing period")
    period_end: date = Field(description="Last day of the billing period")
    energy_basis: Literal["kWh", "kVAh"] = Field(description="Whether energy charges are on kWh or kVAh")
    kwh_total: float = Field(description="Total active energy in kWh")
    kvah_total: float | None = Field(default=None, description="Total apparent energy in kVAh, if printed")
    zones: list[ZoneReading] = Field(default_factory=list, description="Zone-wise ToD units")
    max_demand_kva: float = Field(description="Recorded maximum demand in kVA")
    contract_demand_kva: float = Field(description="Contract (sanctioned) demand in kVA")
    billing_demand_kva: float | None = Field(default=None, description="Demand actually billed in kVA")
    power_factor: float | None = Field(default=None, description="Average power factor for the period")
    energy_charges: float = Field(description="Total energy charges in rupees")
    demand_charges: float = Field(description="Demand (fixed) charges in rupees, excluding excess-demand surcharge")
    excess_demand_charges: float = Field(default=0.0, description="Excess-demand penalty in rupees")
    pf_adjustment: float = Field(default=0.0, description="Power-factor penalty (+) or incentive (-) in rupees")
    fixed_charges: float = Field(default=0.0, description="Meter rent and other fixed charges in rupees")
    electricity_duty: float = Field(default=0.0, description="Electricity duty and taxes in rupees")
    other_charges: float = Field(default=0.0, description="Any other line items in rupees (+ or -)")
    total_amount: float = Field(description="Total bill amount in rupees for the period")

    @property
    def billing_units(self) -> float:
        if self.energy_basis == "kVAh" and self.kvah_total is not None:
            return self.kvah_total
        return self.kwh_total

    @property
    def line_items_total(self) -> float:
        return (
            self.energy_charges
            + self.demand_charges
            + self.excess_demand_charges
            + self.pf_adjustment
            + self.fixed_charges
            + self.electricity_duty
            + self.other_charges
        )

    def zone_units(self) -> dict[str, float]:
        return {z.zone: z.units for z in self.zones}


@dataclass(frozen=True)
class Issue:
    severity: Literal["error", "warning"]
    field: str
    message: str


def validate_bill(bill: BillDocument, rel_tol: float = 0.005) -> list[Issue]:
    """Arithmetic cross-checks that catch most OCR and extraction mistakes."""
    issues: list[Issue] = []

    days = (bill.period_end - bill.period_start).days + 1
    if days <= 0:
        issues.append(Issue("error", "period_end", "Billing period ends before it starts."))
    elif not 25 <= days <= 35:
        issues.append(Issue("warning", "period_end", f"Billing period is {days} days; expected about a month."))

    if bill.zones:
        zone_sum = sum(z.units for z in bill.zones)
        if abs(zone_sum - bill.billing_units) > rel_tol * max(bill.billing_units, 1.0):
            issues.append(
                Issue(
                    "error",
                    "zones",
                    f"Zone units add up to {zone_sum:,.0f} but the bill total is "
                    f"{bill.billing_units:,.0f} {bill.energy_basis}.",
                )
            )

    if bill.kvah_total is not None:
        if bill.kvah_total + 1e-6 < bill.kwh_total:
            issues.append(Issue("error", "kvah_total", "kVAh is lower than kWh, which is physically impossible."))
        elif bill.kvah_total > 0 and bill.power_factor is not None:
            implied = bill.kwh_total / bill.kvah_total
            if abs(implied - bill.power_factor) > 0.015:
                issues.append(
                    Issue(
                        "warning",
                        "power_factor",
                        f"Printed PF {bill.power_factor:.3f} differs from kWh/kVAh = {implied:.3f}.",
                    )
                )

    if bill.power_factor is not None and not 0.3 <= bill.power_factor <= 1.0:
        issues.append(Issue("error", "power_factor", f"Power factor {bill.power_factor} is outside 0.3 to 1.0."))

    if bill.max_demand_kva <= 0:
        issues.append(Issue("error", "max_demand_kva", "Maximum demand must be positive."))
    if bill.contract_demand_kva <= 0:
        issues.append(Issue("error", "contract_demand_kva", "Contract demand must be positive."))
    if bill.billing_demand_kva is not None and bill.billing_demand_kva + 1e-6 < min(
        bill.max_demand_kva, bill.contract_demand_kva
    ):
        issues.append(
            Issue("warning", "billing_demand_kva", "Billing demand is below the recorded maximum demand.")
        )

    tolerance = max(2.0, 0.001 * abs(bill.total_amount))
    if abs(bill.line_items_total - bill.total_amount) > tolerance:
        issues.append(
            Issue(
                "error",
                "total_amount",
                f"Line items add up to Rs {bill.line_items_total:,.0f} but the bill total is "
                f"Rs {bill.total_amount:,.0f}.",
            )
        )

    zone_charges = [z.charges for z in bill.zones if z.charges is not None]
    if zone_charges and len(zone_charges) == len(bill.zones):
        if abs(sum(zone_charges) - bill.energy_charges) > max(2.0, 0.001 * bill.energy_charges):
            issues.append(
                Issue("error", "energy_charges", "Zone-wise energy charges do not add up to total energy charges.")
            )
    return issues


def has_errors(issues: list[Issue]) -> bool:
    return any(i.severity == "error" for i in issues)
