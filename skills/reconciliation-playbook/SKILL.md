---
name: reconciliation-playbook
description: How to resolve ledger-to-bank exceptions from the matcher, and when an exception can be closed as benign.
---

# Reconciliation playbook

Start from the open item's `metrics`; the matcher has already computed the gap or difference. Then read the rows.

| Open item | Benign when | Do not close when |
|---|---|---|
| `timing_difference` | Same reference, same amount, the bank value date simply lags. Any gap can be benign if the rows agree. | The payee IBAN on the bank row differs from the vendor's IBAN in force on that date. |
| `fx_difference` | Foreign-currency row settled from a TRY account and `diff_bps` is plausible for the rate movement. Larger differences are not automatically wrong: compare with other settlements of that counterparty in the period. | The difference repeats in one direction for one vendor, or the row also carries a rule finding. |
| `partial_payment` | Tranches share the reference and `shortfall_minor` is 0, or the shortfall is small and looks like a fee. | A material shortfall has no explanation in the data. Keep it open. |
| `reference_mismatch` | Amount is exact, dates are close, and the bank text is a damaged or reformatted form of the ledger reference. Look at `similarity_pct` and read both strings. Refund references legitimately differ from the original (`RF-`, `CR-`, `REFUND ...`). | The bank reference points to a different invoice, or the same bank row could explain another ledger row. |
| `amount_mismatch` | Rarely. Needs a documented cause. | Default: keep open. |
| `unmatched_ledger` / `unmatched_bank` | Only with a concrete counterpart found by other means. | Default: keep open. |

Before closing anything, check that the row is not also in a rule finding. A tolerance is a reason to look less hard, not evidence that nothing happened. A timing difference on a duplicate payment is still a duplicate payment.

What closure must show: a claim citing the ledger row and the bank row(s), with an amount or date assertion where the statement relies on one.

Adjustments you may draft from reconciliation work: `write_off_fx_difference`, `add_missing_bank_fee`, `correct_reference`. Each stays a draft for a human.
