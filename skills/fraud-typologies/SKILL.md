---
name: fraud-typologies
description: What each rule typology means, the innocent explanations to rule out, and the facts that move a finding toward suspicious.
---

# Fraud typologies

A rule finding is a reason to look, not a conclusion. For each typology, try to establish the innocent explanation first. If you can show it, close the finding. If you cannot, say what is missing.

**duplicate_payment** — same vendor, same amount, close in time. Innocent: recurring fixed fees with distinct invoice references on a regular cycle; a double payment the vendor already returned (look for a `REFUND` row of the same amount). Suspicious: same or near-same reference, no refund, or the second payment from a sibling company.

**invoice_split** — invoices each under the lowest approval limit, over it together. Innocent: a high-volume vendor whose ordinary invoices happen to fall close together (compare with `activity` and the vendor's normal pace). Suspicious: an unusual burst for this vendor, similar amounts, one poster, one approver.

**bank_detail_change** — payment soon after an IBAN change. Innocent: `verified_by` is a different person from `changed_by`, the new IBAN stays in use. Suspicious: unverified or self-verified change, change reversed shortly after the payment, payment larger than the vendor's norm.

**just_below_threshold** — invoice in a narrow band under a limit. Isolated hits occur naturally. Suspicious: repeats for one vendor or poster, or the invoice is out of line with the vendor's usual amounts.

**off_hours_posting** — manual posting outside working hours. Innocent: `in_close_window` is true and the poster works at these times often (`user_off_hours_share_bps`). Suspicious: outside a close window, a payment rather than a journal, a poster who rarely does this, a large amount.

**new_vendor_high_value** — large payment within days of vendor creation. Innocent: planned capital purchases with a verifiable invoice trail and normal approval. Suspicious: no invoice history, created and paid by related users, payment split to stay low.

**benford_deviation** — leading digits off Benford for one vendor. Innocent: fixed-price or recurring vendors, where `distinct_amounts` is small; a narrow price range. Suspicious: many distinct amounts with digits concentrated high, whole-number amounts, no business reason for the pattern.

**employee_vendor_overlap** — vendor shares an IBAN or address with an employee. Innocent: `vendor_type` is `employee_expense`, an expense-reimbursement account. Suspicious: a trade vendor with the overlap, especially when that employee can post or approve for it. `employee_links` also reports overlaps through IBAN history and similar addresses, which the rule itself does not catch.

Patterns the rules cannot see are worth one look when you have the rows open: a near-duplicate net of a fee, a vendor just older than the new-vendor window, payments structured under the high-value line. Report them in the case file.

Adjustments you may draft from fraud work: `reverse_duplicate_payment`, `recover_overpayment`, `hold_payment`. A suspected fraud case goes to a person either way.
