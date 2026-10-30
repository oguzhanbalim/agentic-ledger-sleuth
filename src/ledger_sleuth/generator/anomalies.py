"""Injects labelled fraud patterns. Labels go to the ground truth, never to the visible rows."""

from __future__ import annotations

import random
from collections.abc import Callable
from datetime import date, datetime, time, timedelta

from ledger_sleuth.generator import config as cfg
from ledger_sleuth.generator.world import (
    APPROVAL_LIMITS_TRY,
    HOLIDAYS_2025,
    DraftCounterparty,
    DraftIbanChange,
    DraftIncident,
    DraftRow,
    World,
    make_iban,
    spaced_iban,
)


def allocate(total: int, weights: dict[str, float]) -> list[str]:
    """Largest-remainder allocation of ``total`` incidents across variants."""
    exact = {name: total * weight for name, weight in weights.items()}
    counts = {name: int(value) for name, value in exact.items()}
    remainder = total - sum(counts.values())
    by_fraction = sorted(exact, key=lambda n: (counts[n] - exact[n], n))
    for name in by_fraction[:remainder]:
        counts[name] += 1
    return [name for name, count in counts.items() for _ in range(count)]


def _record(
    world: World,
    typology: str,
    variant: str,
    rows: list[DraftRow],
    counterparty_id: str | None,
    company_id: str,
) -> None:
    world.incidents.append(
        DraftIncident(typology, variant, [r.uid for r in rows], counterparty_id, company_id)
    )


def _active_vendor(world: World, rng: random.Random) -> DraftCounterparty:
    trade = [v for v in world.vendors if v.profile == "trade" and v.vendor_type == "trade"]
    return rng.choice(trade)


def _mid_year_day(rng: random.Random, year: int) -> date:
    return date(year, 1, 15) + timedelta(days=rng.randint(0, 260))


def _duplicate_payment(world: World, variant: str, rng: random.Random) -> None:
    payments = [
        r
        for r in world.rows
        if r.doc_type == "PAYMENT"
        and r.amount_try_minor >= 5_000_000
        and world.counterparty(r.counterparty_id or "").profile == "trade"
    ]
    if variant == "cross_company":
        payments = [
            r for r in payments if len(world.counterparty(r.counterparty_id or "").company_ids) > 1
        ]
    if not payments:
        raise ValueError(f"no eligible payment for duplicate variant {variant}")
    for _ in range(100):
        original = rng.choice(payments)
        cp = world.counterparty(original.counterparty_id or "")
        gap = {
            "same_ref_same_amount": rng.randint(1, 10),
            "ref_variant_same_amount": rng.randint(1, 12),
            "cross_company": rng.randint(1, 14),
            "late_resubmission": rng.randint(40, 80),
            "net_of_fee": rng.randint(1, 7),
        }[variant]
        day = original.value_date + timedelta(days=gap)
        if day <= world.config.end:
            break
    else:
        raise ValueError("no in-year slot for duplicate payment")

    company = original.company_id
    amount = original.amount_minor
    reference = original.reference
    if variant == "ref_variant_same_amount":
        reference = f"{original.reference}/2"
    elif variant == "cross_company":
        company = next(c for c in cp.company_ids if c != original.company_id)
    elif variant == "late_resubmission":
        # Same invoice re-entered under a different document format: normalisation cannot help.
        reference = f"RESUB-{cp.number}-{rng.randint(100_000, 999_999)}"
    elif variant == "net_of_fee":
        amount = amount - max(1000, amount // 400)
    duplicate, _ = world.add_cash(cp, company, day, amount, reference, rng)
    _record(world, "duplicate_payment", variant, [original, duplicate], cp.counterparty_id, company)


def _invoice_split(world: World, variant: str, rng: random.Random) -> None:
    candidates = [v for v in world.vendors if v.profile == "trade"]
    if variant == "across_companies":
        candidates = [v for v in candidates if len(v.company_ids) > 1]
    cp = rng.choice(candidates)
    company = cp.company_ids[0]
    limit = APPROVAL_LIMITS_TRY[company][0] * 100
    total_try = round(limit * rng.uniform(1.25, 2.5))
    parts = rng.randint(2, 4)
    each = total_try // parts
    if each >= limit:
        parts = total_try // (limit - limit // 10) + 1
        each = total_try // parts
    start = _mid_year_day(rng, world.config.year)
    spread = {"tight_window": 5, "spread_window": 20, "across_companies": 6}[variant]
    invoices = []
    for index in range(parts):
        offset = (
            rng.randint(8, spread) * index // max(parts - 1, 1)
            if variant == "spread_window"
            else rng.randint(0, spread)
        )
        day = start + timedelta(days=offset)
        target = round(each * rng.uniform(0.85, 1.0))
        book_company = (
            cp.company_ids[index % len(cp.company_ids)]
            if (variant == "across_companies")
            else company
        )
        amount = world.amount_from_try(cp, day, target)
        invoice = world.add_invoice(
            cp, book_company, day, amount, rng, posted_by=world.clerk(book_company, rng)
        )
        world.pay_invoice(cp, invoice, rng)
        invoices.append(invoice)
    _record(world, "invoice_split", variant, invoices, cp.counterparty_id, company)


def _bank_detail_change(world: World, variant: str, rng: random.Random) -> None:
    changed_ids = {c.counterparty_id for c in world.iban_changes}
    cp = rng.choice(
        [v for v in world.vendors if v.profile == "trade" and v.counterparty_id not in changed_ids]
    )
    company = cp.company_ids[0]
    clerk = world.clerk(company, rng)
    change_day = _mid_year_day(rng, world.config.year)
    verified_by = {
        "unverified_fast": None,
        "self_verified_fast": clerk,
        "slow_burn": clerk,
    }[variant]
    gap = {
        "unverified_fast": rng.randint(1, 7),
        "self_verified_fast": rng.randint(2, 10),
        "slow_burn": rng.randint(20, 40),
    }[variant]
    old_iban = cp.iban
    new_iban = make_iban(rng)
    world.iban_changes.append(
        DraftIbanChange(cp.counterparty_id, change_day, old_iban, new_iban, clerk, verified_by)
    )
    cp.iban = new_iban
    pay_day = change_day + timedelta(days=gap)
    if variant != "slow_burn":
        # The classic pattern: details flip, one payment goes out, details flip back.
        revert_day = pay_day + timedelta(days=rng.randint(2, 6))
        world.iban_changes.append(
            DraftIbanChange(cp.counterparty_id, revert_day, new_iban, old_iban, clerk, verified_by)
        )
        cp.iban = old_iban
    target = round(rng.uniform(150_000, 900_000) * 100)
    amount = world.amount_from_try(cp, pay_day, target)
    invoice = world.add_invoice(cp, company, pay_day - timedelta(days=1), amount, rng)
    payment, _ = world.add_cash(
        cp, company, pay_day, amount, invoice.reference, rng, posted_by=clerk, iban=new_iban
    )
    world.restamp_bank_ibans(cp)
    _record(world, "bank_detail_change", variant, [payment], cp.counterparty_id, company)


def _near_limit_invoice(
    world: World,
    cp: DraftCounterparty,
    company: str,
    limit_minor: int,
    low: float,
    high: float,
    rng: random.Random,
) -> DraftRow:
    day = _mid_year_day(rng, world.config.year)
    target = round(limit_minor * rng.uniform(low, high))
    amount = world.amount_from_try(cp, day, target)
    invoice = world.add_invoice(cp, company, day, amount, rng, posted_by=world.clerk(company, rng))
    world.pay_invoice(cp, invoice, rng)
    return invoice


def _just_below_threshold(world: World, variant: str, rng: random.Random) -> None:
    cp = _active_vendor(world, rng)
    company = rng.choice(cp.company_ids)
    limit = rng.choice(APPROVAL_LIMITS_TRY[company][:2]) * 100
    band = {
        "tight_band": (0.982, 0.998),
        "repeated_series": (0.982, 0.998),
        "wide_band": (0.93, 0.97),
    }[variant]
    count = 3 if variant == "repeated_series" else 1
    rows = [_near_limit_invoice(world, cp, company, limit, *band, rng) for _ in range(count)]
    _record(world, "just_below_threshold", variant, rows, cp.counterparty_id, company)


def _off_hours_ts(world: World, variant: str, rng: random.Random) -> datetime:
    if variant == "night_manual":
        day = _mid_year_day(rng, world.config.year)
        while day.weekday() >= 5:
            day += timedelta(days=1)
        return datetime.combine(
            day, time(rng.randint(0, 4), rng.randint(0, 59), rng.randint(0, 59))
        )
    if variant == "weekend_manual":
        day = _mid_year_day(rng, world.config.year)
        while day.weekday() < 5:
            day += timedelta(days=1)
        return datetime.combine(
            day, time(rng.randint(9, 22), rng.randint(0, 59), rng.randint(0, 59))
        )
    holidays = [h.replace(year=world.config.year) for h in HOLIDAYS_2025]
    weekday_holidays = [h for h in holidays if h.weekday() < 5]
    day = rng.choice(weekday_holidays)
    return datetime.combine(day, time(rng.randint(10, 15), rng.randint(0, 59), rng.randint(0, 59)))


def _off_hours_posting(world: World, variant: str, rng: random.Random) -> None:
    cp = _active_vendor(world, rng)
    company = rng.choice(cp.company_ids)
    ts = _off_hours_ts(world, variant, rng)
    day = ts.date()
    target = round(rng.uniform(40_000, 400_000) * 100)
    amount = world.amount_from_try(cp, day, target)
    poster = world.clerk(company, rng)
    invoice = world.add_invoice(
        cp, company, day, amount, rng, posted_by=poster, posting_ts=ts + timedelta(minutes=3)
    )
    payment, _ = world.add_cash(
        cp,
        company,
        day,
        amount,
        invoice.reference,
        rng,
        posted_by=poster,
        posting_ts=ts,
    )
    _record(world, "off_hours_posting", variant, [payment, invoice], cp.counterparty_id, company)


def _new_shell_vendor(world: World, created: date, rng: random.Random) -> DraftCounterparty:
    return world.new_counterparty(
        rng, kind="vendor", vendor_type="trade", created_on=created, profile="shell"
    )


def _new_vendor_high_value(world: World, variant: str, rng: random.Random) -> None:
    created = _mid_year_day(rng, world.config.year)
    cp = _new_shell_vendor(world, created, rng)
    company = cp.company_ids[0]
    offset = {
        "fast_high": rng.randint(3, 25),
        "structured_below": rng.randint(3, 25),
        "aged_shell": rng.randint(40, 70),
    }[variant]
    day = created + timedelta(days=offset)
    payments = []
    if variant == "structured_below":
        for index in range(rng.randint(2, 3)):
            pay_day = day + timedelta(days=index * rng.randint(1, 3))
            amount = world.amount_from_try(cp, pay_day, round(rng.uniform(150_000, 240_000) * 100))
            invoice = world.add_invoice(cp, company, pay_day - timedelta(days=1), amount, rng)
            payment, _ = world.add_cash(cp, company, pay_day, amount, invoice.reference, rng)
            payments.append(payment)
    else:
        amount = world.amount_from_try(cp, day, round(rng.uniform(300_000, 2_000_000) * 100))
        invoice = world.add_invoice(cp, company, day - timedelta(days=1), amount, rng)
        payment, _ = world.add_cash(cp, company, day, amount, invoice.reference, rng)
        payments.append(payment)
    _record(world, "new_vendor_high_value", variant, payments, cp.counterparty_id, company)


def _skewed_amount(world: World, cp: DraftCounterparty, day: date, rng: random.Random) -> int:
    """Fabricated amounts: leading digit from 5-9, the way invented numbers tend to fall."""
    digit = rng.choice((5, 6, 7, 8, 9))
    magnitude = rng.choice((1_000, 10_000))
    target = round((digit + rng.random()) * magnitude * 100)
    # Whole-lira amounts in TRY; foreign amounts keep the leading digit at a tenth of the value.
    return target // 100 * 100 if cp.currency == "TRY" else target // 10


def _benford_deviation(world: World, variant: str, rng: random.Random) -> None:
    created = world.config.start - timedelta(days=rng.randint(30, 900))
    cp = _new_shell_vendor(world, created, rng)
    cp.median_try = rng.uniform(10_000, 60_000)
    cp.sigma = 1.0
    company = cp.company_ids[0]
    fabricated, genuine = {
        "strong_skew": (rng.randint(50, 70), 0),
        "blended": (rng.randint(24, 32), rng.randint(46, 60)),
        "low_volume": (rng.randint(18, 25), 0),
    }[variant]
    rows = []
    for _ in range(fabricated):
        day = world.random_day(rng)
        invoice = world.add_invoice(cp, company, day, _skewed_amount(world, cp, day, rng), rng)
        world.pay_invoice(cp, invoice, rng)
        rows.append(invoice)
    for _ in range(genuine):
        day = world.random_day(rng)
        invoice = world.add_invoice(cp, company, day, world.sample_amount(cp, day, rng), rng)
        world.pay_invoice(cp, invoice, rng)
    _record(world, "benford_deviation", variant, rows, cp.counterparty_id, company)


def _abbreviate(address: str) -> str:
    return address.replace("Caddesi", "Cad.").replace("Sokak", "Sk.").replace("No:", "No ")


def _employee_vendor_overlap(world: World, variant: str, rng: random.Random) -> None:
    employee = rng.choice([e for e in world.employees if e.role in ("AP_CLERK", "OPERATIONS")])
    created = world.config.start - timedelta(days=rng.randint(30, 500))
    cp = _new_shell_vendor(world, created, rng)
    cp.company_ids = [employee.company_id]
    cp.median_try = rng.uniform(20_000, 90_000)
    cp.sigma = 0.8
    if variant == "iban_exact":
        cp.iban = spaced_iban(employee.iban)
    elif variant == "address_exact":
        cp.address = employee.address.upper()
    elif variant == "address_abbreviated":
        cp.address = _abbreviate(employee.address)
    elif variant == "historical_iban":
        new_iban = make_iban(rng)
        world.iban_changes.append(
            DraftIbanChange(
                counterparty_id=cp.counterparty_id,
                changed_on=world.config.start + timedelta(days=rng.randint(100, 200)),
                old_iban=employee.iban,
                new_iban=new_iban,
                changed_by=employee.employee_id,
                verified_by=world.clerk(employee.company_id, rng),
            )
        )
        cp.iban = new_iban
    rows = []
    for _ in range(rng.randint(4, 10)):
        day = world.random_day(rng)
        invoice = world.add_invoice(
            cp, employee.company_id, day, world.sample_amount(cp, day, rng), rng
        )
        world.pay_invoice(cp, invoice, rng)
        rows.append(invoice)
    _record(
        world, "employee_vendor_overlap", variant, rows, cp.counterparty_id, employee.company_id
    )


_INJECTORS: dict[str, Callable[[World, str, random.Random], None]] = {
    "duplicate_payment": _duplicate_payment,
    "invoice_split": _invoice_split,
    "bank_detail_change": _bank_detail_change,
    "just_below_threshold": _just_below_threshold,
    "off_hours_posting": _off_hours_posting,
    "new_vendor_high_value": _new_vendor_high_value,
    "benford_deviation": _benford_deviation,
    "employee_vendor_overlap": _employee_vendor_overlap,
}


def inject_all(world: World) -> None:
    for typology in cfg.TYPOLOGIES:
        rng = world.rng(f"anomaly:{typology}")
        total, weights = cfg.INCIDENT_PLAN[typology]
        variants = allocate(total, weights)
        rng.shuffle(variants)
        for variant in variants:
            _INJECTORS[typology](world, variant, rng)
