"""Benign reconciliation noise: real differences a controller closes without calling anyone."""

from __future__ import annotations

import random
from datetime import timedelta

from ledger_sleuth.generator import config as cfg
from ledger_sleuth.generator.world import DraftBank, DraftBenign, DraftDecoy, DraftRow, World


def _typo(reference: str, rng: random.Random) -> tuple[str, str]:
    """Return (typo_reference, variant)."""
    roll = rng.random()
    digits = [i for i, c in enumerate(reference) if c.isdigit()]
    transposable = [
        i for i in digits[:-1] if reference[i + 1].isdigit() and reference[i] != reference[i + 1]
    ]
    if roll < 0.4 and transposable:
        index = rng.choice(transposable)
        chars = list(reference)
        chars[index], chars[index + 1] = chars[index + 1], chars[index]
        return "".join(chars), "transposed_digits"
    if roll < 0.7:
        index = rng.choice(digits)
        return reference[:index] + reference[index + 1 :], "dropped_digit"
    if roll < 0.9:
        index = rng.choice(digits)
        return f"{reference[:index]}#{reference[index + 1 :]}", "unreadable_digit"
    return reference.split("-")[-1], "truncated"


def _timing(world: World, row: DraftRow, bank: DraftBank, rng: random.Random) -> None:
    gap = rng.randint(4, 14)
    bank.value_date = row.value_date + timedelta(days=gap)
    variant = "within_7d" if gap <= 7 else "over_7d"
    world.benign.append(DraftBenign("timing_difference", variant, [row.uid], [bank.uid]))


def _fx(world: World, row: DraftRow, bank: DraftBank, rng: random.Random) -> None:
    drift_bps = rng.randint(10, 250) * rng.choice((1, -1))
    settled = row.amount_try_minor + row.amount_try_minor * drift_bps // 10_000
    bank.currency = "TRY"
    bank.account_id = f"BA-{row.company_id[-1]}-TRY"
    bank.amount_minor = -settled if row.direction == "out" else settled
    bank.channel = "EFT"
    variant = "within_1pct" if abs(drift_bps) <= 100 else "over_1pct"
    world.benign.append(DraftBenign("fx_difference", variant, [row.uid], [bank.uid]))


def _partial(world: World, row: DraftRow, bank: DraftBank, rng: random.Random) -> None:
    sign = -1 if row.direction == "out" else 1
    total = abs(bank.amount_minor)
    parts = rng.randint(2, 3)
    shortfall = rng.random() < 0.25
    remaining = total - (max(100, total // 200) if shortfall else 0)
    first = remaining
    uids = [bank.uid]
    day = bank.value_date
    for _ in range(parts - 1):
        tranche = remaining // parts
        first -= tranche
        day += timedelta(days=rng.randint(1, 10))
        extra = DraftBank(
            uid=world.uid(),
            ledger_uid=row.uid,
            company_id=bank.company_id,
            account_id=bank.account_id,
            value_date=day,
            currency=bank.currency,
            amount_minor=sign * tranche,
            counterparty_iban=bank.counterparty_iban,
            channel=bank.channel,
            reference=bank.reference,
        )
        world.bank.append(extra)
        uids.append(extra.uid)
    bank.amount_minor = sign * first
    variant = "with_shortfall" if shortfall else "full_sum"
    world.benign.append(DraftBenign("partial_payment", variant, [row.uid], uids))


def _typo_reference(world: World, row: DraftRow, bank: DraftBank, rng: random.Random) -> None:
    bank.reference, variant = _typo(row.reference, rng)
    world.benign.append(DraftBenign("typo_reference", variant, [row.uid], [bank.uid]))


def _overpayment_refund(world: World, rng: random.Random) -> None:
    """A real double payment the vendor already returned: looks like fraud, nets to zero."""
    payments = [
        r
        for r in world.rows
        if r.doc_type == "PAYMENT"
        and world.counterparty(r.counterparty_id or "").profile == "trade"
    ]
    for _ in range(100):
        original = rng.choice(payments)
        second_day = original.value_date + timedelta(days=rng.randint(2, 10))
        refund_day = second_day + timedelta(days=rng.randint(5, 25))
        if refund_day <= world.config.end:
            break
    else:
        return
    cp = world.counterparty(original.counterparty_id or "")
    repeat, _ = world.add_cash(
        cp, original.company_id, second_day, original.amount_minor, original.reference, rng
    )
    refund, refund_bank = world.add_cash(
        cp,
        original.company_id,
        refund_day,
        original.amount_minor,
        f"RF-{original.reference}",
        rng,
        doc_type="REFUND",
        direction="in",
        bank_reference=f"REFUND {original.reference}",
    )
    world.benign.append(
        DraftBenign("refund", "overpayment_returned", [refund.uid, repeat.uid], [refund_bank.uid])
    )
    world.decoys.append(
        DraftDecoy("refunded_double_payment", cp.counterparty_id, [original.uid, repeat.uid])
    )


def _customer_refund(world: World, rng: random.Random) -> None:
    receipts = [r for r in world.rows if r.doc_type == "RECEIPT"]
    for _ in range(100):
        receipt = rng.choice(receipts)
        day = receipt.value_date + timedelta(days=rng.randint(3, 40))
        if day <= world.config.end:
            break
    else:
        return
    cp = world.counterparty(receipt.counterparty_id or "")
    amount = max(100, receipt.amount_minor // rng.choice((1, 2, 4, 10)))
    refund, refund_bank = world.add_cash(
        cp,
        receipt.company_id,
        day,
        amount,
        f"CR-{receipt.reference}",
        rng,
        doc_type="REFUND",
        direction="out",
        bank_reference=f"CUST REFUND {receipt.reference}",
    )
    world.benign.append(DraftBenign("refund", "customer_credit", [refund.uid], [refund_bank.uid]))


def add_noise(world: World) -> None:
    """Applied after injection, over all cash rows, so noise can land on anomalous rows too.

    That overlap is deliberate: it is how a tolerance rule ends up closing an exception that sits
    on a fraudulent payment, which the eval reports as ``wrong closures``.
    """
    rng = world.rng("noise")
    bank_by_ledger: dict[int, DraftBank] = {}
    for bank in world.bank:
        if bank.ledger_uid is not None:
            bank_by_ledger.setdefault(bank.ledger_uid, bank)

    rows = [r for r in world.cash_rows() if r.uid in bank_by_ledger]
    rng.shuffle(rows)
    plans = (
        (cfg.TIMING_RATE, _timing, False),
        (cfg.FX_RATE, _fx, True),
        (cfg.PARTIAL_RATE, _partial, False),
        (cfg.TYPO_RATE, _typo_reference, False),
    )
    cursor = 0
    for rate, apply, foreign_only in plans:
        wanted = round(len(rows) * rate)
        applied = 0
        while applied < wanted and cursor < len(rows):
            row = rows[cursor]
            cursor += 1
            if foreign_only and row.currency == "TRY":
                continue
            apply(world, row, bank_by_ledger[row.uid], rng)
            applied += 1

    for _ in range(cfg.OVERPAYMENT_REFUNDS):
        _overpayment_refund(world, rng)
    for _ in range(cfg.CUSTOMER_REFUNDS):
        _customer_refund(world, rng)
