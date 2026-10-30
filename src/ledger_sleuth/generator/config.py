"""Generator parameters. Chosen before the baseline was ever run; see README on tuning."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

TRAIN_SEED = 1729
HELDOUT_SEED = 271828

TYPOLOGIES: tuple[str, ...] = (
    "duplicate_payment",
    "invoice_split",
    "bank_detail_change",
    "just_below_threshold",
    "off_hours_posting",
    "new_vendor_high_value",
    "benford_deviation",
    "employee_vendor_overlap",
)

BENIGN_KINDS: tuple[str, ...] = (
    "timing_difference",
    "fx_difference",
    "partial_payment",
    "refund",
    "typo_reference",
)

# Incident variants and weights. Some variants sit outside what the baseline rule looks for, on
# purpose: they are how the eval measures where the rules are blind.
INCIDENT_PLAN: dict[str, tuple[int, dict[str, float]]] = {
    "duplicate_payment": (
        24,
        {
            "same_ref_same_amount": 0.40,
            "ref_variant_same_amount": 0.25,
            "cross_company": 0.15,
            "late_resubmission": 0.10,
            "net_of_fee": 0.10,
        },
    ),
    "invoice_split": (16, {"tight_window": 0.50, "spread_window": 0.25, "across_companies": 0.25}),
    "bank_detail_change": (
        12,
        {"unverified_fast": 0.50, "self_verified_fast": 0.25, "slow_burn": 0.25},
    ),
    "just_below_threshold": (
        24,
        {"tight_band": 0.60, "repeated_series": 0.20, "wide_band": 0.20},
    ),
    "off_hours_posting": (
        20,
        {"night_manual": 0.50, "weekend_manual": 0.30, "holiday_business_hours": 0.20},
    ),
    "new_vendor_high_value": (
        12,
        {"fast_high": 0.60, "structured_below": 0.20, "aged_shell": 0.20},
    ),
    "benford_deviation": (6, {"strong_skew": 0.50, "blended": 0.34, "low_volume": 0.16}),
    "employee_vendor_overlap": (
        8,
        {
            "iban_exact": 0.375,
            "address_exact": 0.25,
            "address_abbreviated": 0.25,
            "historical_iban": 0.125,
        },
    ),
}

# Benign noise rates over cash rows.
TIMING_RATE = 0.015
FX_RATE = 0.010
PARTIAL_RATE = 0.010
TYPO_RATE = 0.012
OVERPAYMENT_REFUNDS = 40
CUSTOMER_REFUNDS = 60

N_TRADE_VENDORS = 105
N_HIGH_FREQ_VENDORS = 12
N_MONTHLY_VENDORS = 14
N_WEEKLY_VENDORS = 6
N_CAPEX_VENDORS = 5
N_EXPENSE_VENDORS = 8
N_NEW_LEGIT_HIGH_VALUE = 10
N_LEGIT_IBAN_CHANGES = 15
N_CUSTOMERS = 25

TRADE_INVOICES = 17_500
SALES_INVOICES = 8_000
JOURNALS = 7_000


@dataclass(frozen=True)
class GeneratorConfig:
    seed: int
    year: int = 2025
    scale: float = 1.0  # scales legitimate volume; incident counts stay fixed

    @property
    def start(self) -> date:
        return date(self.year, 1, 1)

    @property
    def end(self) -> date:
        return date(self.year, 12, 31)
