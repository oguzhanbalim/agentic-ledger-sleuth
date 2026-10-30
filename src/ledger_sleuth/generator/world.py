"""Master data and legitimate activity.

Draft records use internal ``uid`` values. Public identifiers are assigned only after every row
exists, in time order, so an ID can never reveal that a row was injected.
"""

from __future__ import annotations

import math
import random
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta

from ledger_sleuth.generator import config as cfg
from ledger_sleuth.models import SYSTEM_USER

COMPANIES: tuple[tuple[str, str, str], ...] = (
    ("CO-A", "Synthetic Company A", "TRY"),
    ("CO-B", "Synthetic Company B", "EUR"),
    ("CO-C", "Synthetic Company C", "USD"),
)
APPROVAL_LIMITS_TRY: dict[str, tuple[int, int, int]] = {
    "CO-A": (100_000, 500_000, 2_000_000),
    "CO-B": (75_000, 400_000, 1_500_000),
    "CO-C": (150_000, 750_000, 3_000_000),
}
APPROVER_ROLES = ("AP_SUPERVISOR", "FINANCE_MANAGER", "CFO", "BOARD")
# Weekdays in the synthetic calendar treated as public holidays. Not exposed to the rules.
HOLIDAYS_2025 = (
    date(2025, 1, 1),
    date(2025, 4, 23),
    date(2025, 5, 1),
    date(2025, 5, 19),
    date(2025, 7, 15),
    date(2025, 10, 29),
)
_STREETS = (
    "Ardic",
    "Ihlamur",
    "Menekse",
    "Palamut",
    "Kestane",
    "Zambak",
    "Defne",
    "Lale",
    "Akasya",
)
_DISTRICTS = ("Bati", "Dogu", "Merkez", "Liman", "Tepe", "Sahil", "Vadi")
_CITIES = ("Istanbul", "Izmir", "Ankara", "Bursa")
_DESCRIPTIONS = (
    "Services rendered",
    "Goods received",
    "Consulting fee",
    "Maintenance contract",
    "Logistics charge",
    "Software licence",
    "Raw materials",
    "Subcontracted work",
    "Office supplies",
)
_CHANNELS = {"TRY": ("EFT", "HAVALE"), "EUR": ("SEPA", "SWIFT"), "USD": ("SWIFT",)}


@dataclass
class DraftRow:
    uid: int
    company_id: str
    doc_type: str
    direction: str
    counterparty_id: str | None
    posting_ts: datetime
    value_date: date
    currency: str
    amount_minor: int
    fx_rate_micro: int
    amount_try_minor: int
    reference: str
    description: str
    account: str
    posted_by: str
    approved_by: str | None


@dataclass
class DraftBank:
    uid: int
    ledger_uid: int | None
    company_id: str
    account_id: str
    value_date: date
    currency: str
    amount_minor: int
    counterparty_iban: str
    channel: str
    reference: str


@dataclass
class DraftCounterparty:
    counterparty_id: str
    name: str
    kind: str
    vendor_type: str
    currency: str
    created_on: date
    iban: str
    address: str
    company_ids: list[str]
    number: int
    profile: str = "trade"
    median_try: float = 0.0
    sigma: float = 1.0
    used_refs: set[str] = field(default_factory=set)


@dataclass
class DraftIbanChange:
    counterparty_id: str
    changed_on: date
    old_iban: str
    new_iban: str
    changed_by: str
    verified_by: str | None


@dataclass
class DraftEmployee:
    employee_id: str
    name: str
    role: str
    company_id: str
    iban: str
    address: str


@dataclass
class DraftIncident:
    typology: str
    variant: str
    row_uids: list[int]
    counterparty_id: str | None
    company_id: str


@dataclass
class DraftBenign:
    kind: str
    variant: str
    row_uids: list[int]
    bank_uids: list[int]


@dataclass
class DraftDecoy:
    kind: str
    counterparty_id: str | None
    row_uids: list[int]


def make_iban(rng: random.Random) -> str:
    """Valid mod-97 structure under the unassigned country code XX: cannot be a real account."""
    body = "".join(rng.choice("0123456789") for _ in range(22))
    check = 98 - int(f"{body}333300") % 97
    return f"XX{check:02d}{body}"


def spaced_iban(iban: str) -> str:
    return " ".join(iban[i : i + 4] for i in range(0, len(iban), 4))


def make_address(rng: random.Random) -> str:
    street = rng.choice(_STREETS)
    kind = rng.choice(("Caddesi", "Sokak"))
    return (
        f"{street} {kind} No:{rng.randint(1, 180)}/{rng.randint(1, 30)}, "
        f"{rng.choice(_DISTRICTS)}, {rng.choice(_CITIES)}"
    )


def is_close_window(day: date) -> bool:
    """Last two and first two calendar days of a month."""
    next_day = day + timedelta(days=2)
    return day.day <= 2 or next_day.month != day.month


class World:
    def __init__(self, config: cfg.GeneratorConfig) -> None:
        self.config = config
        self._uid = 0
        self.rows: list[DraftRow] = []
        self.bank: list[DraftBank] = []
        self.vendors: list[DraftCounterparty] = []
        self.customers: list[DraftCounterparty] = []
        self.employees: list[DraftEmployee] = []
        self.iban_changes: list[DraftIbanChange] = []
        self.incidents: list[DraftIncident] = []
        self.benign: list[DraftBenign] = []
        self.decoys: list[DraftDecoy] = []
        self._cp_index: dict[str, DraftCounterparty] = {}
        pool_rng = self.rng("ids")
        self._cp_numbers = pool_rng.sample(range(1000, 9999), 600)
        self._fx = self._build_fx()
        self._business_posters: dict[str, list[str]] = {}
        self._build_employees()

    def rng(self, name: str) -> random.Random:
        # Seeding from a string hashes with SHA-512, so this is stable across runs and versions.
        return random.Random(f"{self.config.seed}:{name}")

    def uid(self) -> int:
        self._uid += 1
        return self._uid

    # -- reference data -------------------------------------------------------------------------

    def _build_fx(self) -> dict[str, list[int]]:
        table: dict[str, list[int]] = {"TRY": [1_000_000] * 520}
        for currency, start in (("USD", 35.0), ("EUR", 37.0)):
            rng = self.rng(f"fx:{currency}")
            rate = start
            series = []
            for _ in range(520):
                rate *= 1 + rng.gauss(0.0004, 0.002)
                series.append(round(rate * 1_000_000))
            table[currency] = series
        return table

    def fx_rate(self, day: date, currency: str) -> int:
        return self._fx[currency][(day - self.config.start).days]

    def _build_employees(self) -> None:
        rng = self.rng("employees")
        numbers = rng.sample(range(100, 999), 60)
        roster = (
            ("AP_CLERK", 5),
            ("AP_SUPERVISOR", 2),
            ("FINANCE_MANAGER", 1),
            ("CFO", 1),
            ("BOARD", 1),
            ("OPERATIONS", 4),
            ("SALES", 4),
        )
        index = 0
        for company_id, _, _ in COMPANIES:
            for role, count in roster:
                for _ in range(count):
                    number = numbers[index]
                    index += 1
                    self.employees.append(
                        DraftEmployee(
                            employee_id=f"E-{number}",
                            name=f"Synthetic Employee {number}",
                            role=role,
                            company_id=company_id,
                            iban=make_iban(rng),
                            address=make_address(rng),
                        )
                    )

    def employees_with(self, company_id: str, role: str) -> list[DraftEmployee]:
        return [e for e in self.employees if e.company_id == company_id and e.role == role]

    def approver(self, company_id: str, amount_try_minor: int, rng: random.Random) -> str:
        limits = APPROVAL_LIMITS_TRY[company_id]
        tier = next(
            (i for i, limit in enumerate(limits) if amount_try_minor <= limit * 100),
            len(limits),
        )
        return rng.choice(self.employees_with(company_id, APPROVER_ROLES[tier])).employee_id

    def clerk(self, company_id: str, rng: random.Random) -> str:
        return rng.choice(self.employees_with(company_id, "AP_CLERK")).employee_id

    def new_counterparty(
        self,
        rng: random.Random,
        *,
        kind: str,
        vendor_type: str,
        created_on: date,
        currency: str | None = None,
        company_ids: list[str] | None = None,
        profile: str = "trade",
    ) -> DraftCounterparty:
        number = self._cp_numbers.pop()
        prefix = "V" if kind == "vendor" else "C"
        chosen_companies = company_ids or self._pick_companies(rng)
        cp = DraftCounterparty(
            counterparty_id=f"{prefix}-{number}",
            name=f"Synthetic {'Vendor' if kind == 'vendor' else 'Customer'} {number}",
            kind=kind,
            vendor_type=vendor_type,
            currency=currency or rng.choices(("TRY", "EUR", "USD"), (0.70, 0.15, 0.15))[0],
            created_on=created_on,
            iban=make_iban(rng),
            address=make_address(rng),
            company_ids=chosen_companies,
            number=number,
            profile=profile,
        )
        (self.vendors if kind == "vendor" else self.customers).append(cp)
        self._cp_index[cp.counterparty_id] = cp
        return cp

    def _pick_companies(self, rng: random.Random) -> list[str]:
        ids = [c[0] for c in COMPANIES]
        return sorted(rng.sample(ids, 2 if rng.random() < 0.25 else 1))

    def iban_at(self, cp: DraftCounterparty, day: date) -> str:
        """IBAN in force on ``day``, from the change history."""
        changes = sorted(
            (c for c in self.iban_changes if c.counterparty_id == cp.counterparty_id),
            key=lambda c: c.changed_on,
        )
        if not changes:
            return cp.iban
        iban = changes[0].old_iban
        for change in changes:
            if change.changed_on <= day:
                iban = change.new_iban
        return iban

    # -- primitives ---------------------------------------------------------------------------

    def new_reference(self, cp: DraftCounterparty, rng: random.Random, prefix: str = "INV") -> str:
        while True:
            # Vendor numbering is not monotonic, so a reference cannot reveal when it was created.
            ref = f"{prefix}-{cp.number}-{rng.randint(100_000, 999_999)}"
            if ref not in cp.used_refs:
                cp.used_refs.add(ref)
                return ref

    def business_ts(self, day: date, rng: random.Random) -> datetime:
        """A timestamp in working hours, on a working day."""
        while day.weekday() >= 5:
            day += timedelta(days=1)
        return datetime.combine(
            day, time(rng.randint(8, 18), rng.randint(0, 59), rng.randint(0, 59))
        )

    def legit_posting_ts(self, day: date, rng: random.Random) -> datetime:
        """Working hours, plus the out-of-hours work real closes produce."""
        roll = rng.random()
        if is_close_window(day) and day.weekday() >= 5 and roll < 0.5:
            return datetime.combine(day, time(rng.randint(10, 17), rng.randint(0, 59), 0))
        if is_close_window(day) and roll < 0.08:
            hour = rng.choice((20, 21, 22, 23))
            return datetime.combine(day, time(hour, rng.randint(0, 59), rng.randint(0, 59)))
        if roll < 0.002:
            hour = rng.choice((20, 21))
            return datetime.combine(day, time(hour, rng.randint(0, 59), rng.randint(0, 59)))
        return self.business_ts(day, rng)

    def _add_row(self, **fields: object) -> DraftRow:
        row = DraftRow(uid=self.uid(), **fields)  # type: ignore[arg-type]
        self.rows.append(row)
        return row

    def add_invoice(
        self,
        cp: DraftCounterparty,
        company_id: str,
        day: date,
        amount_minor: int,
        rng: random.Random,
        *,
        reference: str | None = None,
        posted_by: str | None = None,
        posting_ts: datetime | None = None,
        doc_type: str = "INVOICE",
    ) -> DraftRow:
        rate = self.fx_rate(day, cp.currency)
        amount_try = amount_minor * rate // 1_000_000
        use_batch = posted_by is None and posting_ts is None and rng.random() < 0.15
        if use_batch:
            poster = SYSTEM_USER
            ts = datetime.combine(day, time(2, rng.randint(0, 59), rng.randint(0, 59)))
        else:
            poster = posted_by or self.clerk(company_id, rng)
            ts = posting_ts or self.legit_posting_ts(day, rng)
        return self._add_row(
            company_id=company_id,
            doc_type=doc_type,
            direction="none",
            counterparty_id=cp.counterparty_id,
            posting_ts=ts,
            value_date=day,
            currency=cp.currency,
            amount_minor=amount_minor,
            fx_rate_micro=rate,
            amount_try_minor=amount_try,
            reference=reference or self.new_reference(cp, rng),
            description=rng.choice(_DESCRIPTIONS),
            account="320-AP" if doc_type == "INVOICE" else "120-AR",
            posted_by=poster,
            approved_by=self.approver(company_id, amount_try, rng),
        )

    def add_cash(
        self,
        cp: DraftCounterparty,
        company_id: str,
        day: date,
        amount_minor: int,
        reference: str,
        rng: random.Random,
        *,
        doc_type: str = "PAYMENT",
        direction: str = "out",
        posted_by: str | None = None,
        posting_ts: datetime | None = None,
        iban: str | None = None,
        bank_reference: str | None = None,
        description: str | None = None,
    ) -> tuple[DraftRow, DraftBank]:
        rate = self.fx_rate(day, cp.currency)
        amount_try = amount_minor * rate // 1_000_000
        row = self._add_row(
            company_id=company_id,
            doc_type=doc_type,
            direction=direction,
            counterparty_id=cp.counterparty_id,
            posting_ts=posting_ts or self.legit_posting_ts(day, rng),
            value_date=day,
            currency=cp.currency,
            amount_minor=amount_minor,
            fx_rate_micro=rate,
            amount_try_minor=amount_try,
            reference=reference,
            description=description or rng.choice(_DESCRIPTIONS),
            account=f"102-BANK-{cp.currency}",
            posted_by=posted_by or self.clerk(company_id, rng),
            approved_by=self.approver(company_id, amount_try, rng),
        )
        bank = DraftBank(
            uid=self.uid(),
            ledger_uid=row.uid,
            company_id=company_id,
            account_id=f"BA-{company_id[-1]}-{cp.currency}",
            value_date=day + timedelta(days=rng.choice((0, 0, 0, 1, 1, 2))),
            currency=cp.currency,
            amount_minor=-amount_minor if direction == "out" else amount_minor,
            counterparty_iban=(iban or self.iban_at(cp, day)).replace(" ", ""),
            channel=rng.choice(_CHANNELS[cp.currency]),
            reference=bank_reference or reference,
        )
        self.bank.append(bank)
        return row, bank

    def amount_from_try(self, cp: DraftCounterparty, day: date, target_try_minor: int) -> int:
        return max(100, target_try_minor * 1_000_000 // self.fx_rate(day, cp.currency))

    def sample_amount(self, cp: DraftCounterparty, day: date, rng: random.Random) -> int:
        target = rng.lognormvariate(math.log(cp.median_try), cp.sigma)
        target = min(target, 5_000_000.0)
        return self.amount_from_try(cp, day, round(target * 100))

    def random_day(self, rng: random.Random) -> date:
        return self.config.start + timedelta(days=rng.randint(0, 364))

    def pay_invoice(
        self,
        cp: DraftCounterparty,
        invoice: DraftRow,
        rng: random.Random,
        *,
        days: int | None = None,
    ) -> tuple[DraftRow, DraftBank] | None:
        terms = days if days is not None else rng.choice((30, 45, 60)) + rng.randint(-3, 7)
        day = invoice.value_date + timedelta(days=terms)
        if day > self.config.end:
            return None  # remains open AP at year end
        return self.add_cash(
            cp, invoice.company_id, day, invoice.amount_minor, invoice.reference, rng
        )

    # -- legitimate activity --------------------------------------------------------------------

    def build_legit_activity(self) -> None:
        self._build_vendors()
        self._build_legit_iban_changes()
        self._build_vendor_flows()
        self._build_customer_flows()
        self._build_journals()

    def _scaled(self, n: int) -> int:
        return max(1, round(n * self.config.scale))

    def _build_vendors(self) -> None:
        rng = self.rng("vendors")
        for _ in range(cfg.N_TRADE_VENDORS):
            cp = self.new_counterparty(
                rng, kind="vendor", vendor_type="trade", created_on=self._old_date(rng)
            )
            cp.median_try = rng.uniform(5_000, 80_000)
            cp.sigma = rng.uniform(0.8, 1.4)
        for _ in range(cfg.N_HIGH_FREQ_VENDORS):
            cp = self.new_counterparty(
                rng,
                kind="vendor",
                vendor_type="trade",
                created_on=self._old_date(rng),
                profile="high_freq",
            )
            cp.median_try = rng.uniform(25_000, 45_000)
            cp.sigma = rng.uniform(0.3, 0.6)
        for profile, count in (
            ("monthly", cfg.N_MONTHLY_VENDORS),
            ("weekly", cfg.N_WEEKLY_VENDORS),
        ):
            for _ in range(count):
                cp = self.new_counterparty(
                    rng,
                    kind="vendor",
                    vendor_type="trade",
                    created_on=self._old_date(rng),
                    profile=profile,
                )
                cp.median_try = rng.uniform(2_000, 60_000)
        for _ in range(cfg.N_CAPEX_VENDORS):
            cp = self.new_counterparty(
                rng,
                kind="vendor",
                vendor_type="trade",
                created_on=self._old_date(rng),
                profile="capex",
            )
            cp.median_try = rng.uniform(300_000, 1_500_000)
            cp.sigma = 0.4
        for _ in range(cfg.N_EXPENSE_VENDORS):
            employee = rng.choice(self.employees)
            cp = self.new_counterparty(
                rng,
                kind="vendor",
                vendor_type="employee_expense",
                created_on=self._old_date(rng),
                currency="TRY",
                company_ids=[employee.company_id],
                profile="expense",
            )
            cp.iban = employee.iban
            cp.address = employee.address
            cp.median_try = rng.uniform(800, 4_000)
            cp.sigma = 0.7
            self.decoys.append(DraftDecoy("employee_expense_vendor", cp.counterparty_id, []))
        for _ in range(cfg.N_NEW_LEGIT_HIGH_VALUE):
            created = self.config.start + timedelta(days=rng.randint(10, 300))
            cp = self.new_counterparty(
                rng,
                kind="vendor",
                vendor_type="trade",
                created_on=created,
                profile="new_legit",
            )
            cp.median_try = rng.uniform(300_000, 1_800_000)
            cp.sigma = 0.3

    def _old_date(self, rng: random.Random) -> date:
        return date(self.config.year - rng.randint(1, 7), rng.randint(1, 12), rng.randint(1, 28))

    def _build_legit_iban_changes(self) -> None:
        rng = self.rng("legit-iban")
        candidates = [v for v in self.vendors if v.profile == "trade"]
        for cp in rng.sample(candidates, cfg.N_LEGIT_IBAN_CHANGES):
            company = cp.company_ids[0]
            clerk, supervisor = (
                self.clerk(company, rng),
                rng.choice(self.employees_with(company, "AP_SUPERVISOR")).employee_id,
            )
            new_iban = make_iban(rng)
            self.iban_changes.append(
                DraftIbanChange(
                    counterparty_id=cp.counterparty_id,
                    changed_on=self.random_day(rng),
                    old_iban=cp.iban,
                    new_iban=new_iban,
                    changed_by=clerk,
                    verified_by=supervisor,
                )
            )
            cp.iban = new_iban
            self.decoys.append(DraftDecoy("verified_iban_change", cp.counterparty_id, []))

    def _build_vendor_flows(self) -> None:
        rng = self.rng("vendor-flows")
        trade = [v for v in self.vendors if v.profile == "trade"]
        rng.shuffle(trade)
        weights = [1 / (rank + 1) ** 0.8 for rank in range(len(trade))]
        total_weight = sum(weights)
        trade_total = self._scaled(cfg.TRADE_INVOICES)
        for cp, weight in zip(trade, weights, strict=True):
            count = max(4, round(trade_total * weight / total_weight))
            for _ in range(count):
                self._legit_invoice_and_payment(cp, self.random_day(rng), rng)
        for cp in self.vendors:
            if cp.profile == "high_freq":
                for month in range(1, 13):
                    for _ in range(self._scaled(rng.randint(8, 20))):
                        self._legit_invoice_and_payment(cp, self._day_in(month, rng), rng)
            elif cp.profile == "monthly":
                amount = self.amount_from_try(cp, self.config.start, round(cp.median_try * 100))
                for month in range(1, 13):
                    day = date(self.config.year, month, rng.randint(1, 5))
                    self._legit_invoice_and_payment(cp, day, rng, amount=amount, terms=20)
            elif cp.profile == "weekly":
                amount = self.amount_from_try(cp, self.config.start, round(cp.median_try * 100))
                start = self.config.start + timedelta(days=rng.randint(0, 6))
                for week in range(52):
                    day = start + timedelta(days=7 * week)
                    self._legit_invoice_and_payment(cp, day, rng, amount=amount, terms=14)
            elif cp.profile == "capex":
                for _ in range(rng.randint(1, 4)):
                    self._legit_invoice_and_payment(cp, self.random_day(rng), rng)
            elif cp.profile == "expense":
                for _ in range(rng.randint(6, 24)):
                    self._legit_invoice_and_payment(cp, self.random_day(rng), rng, terms=7)
            elif cp.profile == "new_legit":
                self._new_legit_flow(cp, rng)

    def _new_legit_flow(self, cp: DraftCounterparty, rng: random.Random) -> None:
        day = cp.created_on + timedelta(days=rng.randint(2, 20))
        invoice_rows = []
        for _ in range(rng.randint(1, 3)):
            pay = self._legit_invoice_and_payment(cp, day, rng, terms=rng.randint(2, 8))
            if pay is not None:
                invoice_rows.append(pay.uid)
        self.decoys.append(
            DraftDecoy("new_vendor_legit_high_value", cp.counterparty_id, invoice_rows)
        )

    def _legit_invoice_and_payment(
        self,
        cp: DraftCounterparty,
        day: date,
        rng: random.Random,
        *,
        amount: int | None = None,
        terms: int | None = None,
    ) -> DraftRow | None:
        company = rng.choice(cp.company_ids)
        value = amount if amount is not None else self.sample_amount(cp, day, rng)
        invoice = self.add_invoice(cp, company, day, value, rng)
        paid = self.pay_invoice(cp, invoice, rng, days=terms)
        return paid[0] if paid else None

    def _day_in(self, month: int, rng: random.Random) -> date:
        return date(self.config.year, month, rng.randint(1, 28))

    def _build_customer_flows(self) -> None:
        rng = self.rng("customer-flows")
        for _ in range(cfg.N_CUSTOMERS):
            cp = self.new_counterparty(
                rng,
                kind="customer",
                vendor_type="customer",
                created_on=self._old_date(rng),
            )
            cp.median_try = rng.uniform(8_000, 120_000)
            cp.sigma = rng.uniform(0.8, 1.3)
        weights = [1 / (rank + 1) ** 0.7 for rank in range(len(self.customers))]
        total = sum(weights)
        for cp, weight in zip(self.customers, weights, strict=True):
            for _ in range(max(4, round(self._scaled(cfg.SALES_INVOICES) * weight / total))):
                day = self.random_day(rng)
                company = rng.choice(cp.company_ids)
                invoice = self.add_invoice(
                    cp,
                    company,
                    day,
                    self.sample_amount(cp, day, rng),
                    rng,
                    reference=self.new_reference(cp, rng, prefix="SI"),
                    doc_type="SALES_INVOICE",
                )
                receipt_day = day + timedelta(
                    days=rng.choice((30, 45, 60, 90)) + rng.randint(-5, 10)
                )
                if receipt_day <= self.config.end:
                    self.add_cash(
                        cp,
                        company,
                        receipt_day,
                        invoice.amount_minor,
                        invoice.reference,
                        rng,
                        doc_type="RECEIPT",
                        direction="in",
                    )

    def _build_journals(self) -> None:
        rng = self.rng("journals")
        accounts = ("770-OPEX", "720-COGS", "193-PREPAID", "381-ACCRUAL", "335-PAYROLL")
        for _ in range(self._scaled(cfg.JOURNALS)):
            day = self.random_day(rng)
            company = rng.choice(COMPANIES)[0]
            amount = round(rng.lognormvariate(math.log(15_000), 1.1) * 100)
            batch = rng.random() < 0.2
            self._add_row(
                company_id=company,
                doc_type="JOURNAL",
                direction="none",
                counterparty_id=None,
                posting_ts=(
                    datetime.combine(day, time(3, rng.randint(0, 59), rng.randint(0, 59)))
                    if batch
                    else self.legit_posting_ts(day, rng)
                ),
                value_date=day,
                currency="TRY",
                amount_minor=amount,
                fx_rate_micro=1_000_000,
                amount_try_minor=amount,
                reference=f"JV-{rng.randint(100_000, 999_999)}",
                description=rng.choice(
                    ("Accrual", "Reclass", "Payroll journal", "Prepaid release")
                ),
                account=rng.choice(accounts),
                posted_by=SYSTEM_USER if batch else self.clerk(company, rng),
                approved_by=None if batch else self.approver(company, amount, rng),
            )

    # -- used by anomalies and noise ------------------------------------------------------------

    def cash_rows(self) -> list[DraftRow]:
        return [r for r in self.rows if r.doc_type in ("PAYMENT", "RECEIPT")]

    def counterparty(self, counterparty_id: str) -> DraftCounterparty:
        return self._cp_index[counterparty_id]

    def restamp_bank_ibans(self, cp: DraftCounterparty) -> None:
        """Make bank rows agree with an IBAN history added after those rows were created."""
        dates = {r.uid: r.value_date for r in self.rows if r.counterparty_id == cp.counterparty_id}
        for bank in self.bank:
            if bank.ledger_uid in dates:
                bank.counterparty_iban = self.iban_at(cp, dates[bank.ledger_uid]).replace(" ", "")
