"""The tool surface: one implementation shared by the agent loop and the MCP server.

Seven read-only functions and one write. The write, ``propose_adjustment``, appends a draft to a
pending queue in a separate file. Nothing in this package can approve or post a draft, and the
ledger connection itself is read-only.
"""

from __future__ import annotations

import hashlib
import json
from collections import defaultdict
from collections.abc import Callable
from dataclasses import asdict, dataclass
from datetime import date
from functools import cached_property
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from ledger_sleuth.matcher import MatchResult, OpenItem, reference_similarity_pct, run_matcher
from ledger_sleuth.models import BankRow, LedgerRow, format_minor
from ledger_sleuth.rules import Finding, run_all_rules
from ledger_sleuth.rules.detectors import normalize_address, normalize_iban
from ledger_sleuth.store import LedgerStore

MAX_PAGE = 200
DEFAULT_PAGE = 50
PENDING_QUEUE_FILENAME = "pending_adjustments.jsonl"

AdjustmentType = Literal[
    "reverse_duplicate_payment",
    "recover_overpayment",
    "write_off_fx_difference",
    "add_missing_bank_fee",
    "correct_reference",
    "hold_payment",
]


class ToolError(Exception):
    """A tool could not satisfy a well-formed request; the message goes back to the caller."""


class _Input(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ListOpenItemsInput(_Input):
    company_id: str | None = Field(default=None, description="Filter by company, e.g. CO-A.")
    kind: str | None = Field(default=None, description="Filter by open-item kind.")
    counterparty_id: str | None = Field(default=None, description="Filter by counterparty.")
    limit: int = Field(default=DEFAULT_PAGE, ge=1, le=MAX_PAGE)
    offset: int = Field(default=0, ge=0)


class GetLedgerRowsInput(_Input):
    row_ids: list[str] | None = Field(default=None, description="Exact ledger row IDs.")
    company_id: str | None = None
    counterparty_id: str | None = None
    doc_type: str | None = Field(default=None, description="INVOICE, PAYMENT, REFUND, ...")
    posted_by: str | None = None
    date_from: date | None = Field(default=None, description="Inclusive value date.")
    date_to: date | None = Field(default=None, description="Inclusive value date.")
    reference_contains: str | None = None
    limit: int = Field(default=DEFAULT_PAGE, ge=1, le=MAX_PAGE)
    offset: int = Field(default=0, ge=0)


class GetBankRowsInput(_Input):
    bank_row_ids: list[str] | None = Field(default=None, description="Exact bank row IDs.")
    company_id: str | None = None
    counterparty_iban: str | None = None
    date_from: date | None = None
    date_to: date | None = None
    reference_contains: str | None = None
    limit: int = Field(default=DEFAULT_PAGE, ge=1, le=MAX_PAGE)
    offset: int = Field(default=0, ge=0)


class RunMatcherInput(_Input):
    ledger_row_id: str | None = Field(
        default=None,
        description="Explain one ledger row: its match or open item, and candidate bank rows.",
    )


class GetRuleFindingsInput(_Input):
    finding_ids: list[str] | None = None
    typology: str | None = None
    company_id: str | None = None
    counterparty_id: str | None = None
    limit: int = Field(default=DEFAULT_PAGE, ge=1, le=MAX_PAGE)
    offset: int = Field(default=0, ge=0)


class GetCounterpartyProfileInput(_Input):
    counterparty_id: str


class GetApprovalMatrixInput(_Input):
    company_id: str | None = None


class ProposeAdjustmentInput(_Input):
    target_row_id: str = Field(description="Ledger row the adjustment would apply to.")
    adjustment_type: AdjustmentType
    amount_minor: int = Field(gt=0, description="Minor units, in the target row's currency.")
    currency: Literal["TRY", "EUR", "USD"]
    reason: str = Field(min_length=10, max_length=1000)
    evidence_row_ids: list[str] = Field(min_length=1)


def ledger_row_to_dict(row: LedgerRow) -> dict[str, Any]:
    return {
        "row_id": row.row_id,
        "company_id": row.company_id,
        "doc_no": row.doc_no,
        "doc_type": row.doc_type,
        "direction": row.direction,
        "counterparty_id": row.counterparty_id,
        "posting_ts": row.posting_ts.isoformat(),
        "value_date": row.value_date.isoformat(),
        "currency": row.currency,
        "amount_minor": row.amount_minor,
        "amount": format_minor(row.amount_minor),
        "amount_try_minor": row.amount_try_minor,
        "fx_rate_micro": row.fx_rate_micro,
        "reference": row.reference,
        "description": row.description,
        "account": row.account,
        "posted_by": row.posted_by,
        "approved_by": row.approved_by,
    }


def bank_row_to_dict(row: BankRow) -> dict[str, Any]:
    return {
        "bank_row_id": row.bank_row_id,
        "company_id": row.company_id,
        "account_id": row.account_id,
        "value_date": row.value_date.isoformat(),
        "currency": row.currency,
        "amount_minor": row.amount_minor,
        "amount": format_minor(row.amount_minor),
        "counterparty_iban": row.counterparty_iban,
        "channel": row.channel,
        "reference": row.reference,
    }


def _page(items: list[Any], limit: int, offset: int) -> tuple[list[Any], dict[str, Any]]:
    window = items[offset : offset + limit]
    return window, {
        "total": len(items),
        "offset": offset,
        "returned": len(window),
        "truncated": offset + len(window) < len(items),
    }


def _ledger_totals(rows: list[LedgerRow]) -> dict[str, Any]:
    """Sums a caller would otherwise compute. They cover every filtered row, not one page."""
    by_currency: dict[str, int] = defaultdict(int)
    try_total = 0
    for row in rows:
        by_currency[row.currency] += row.amount_minor
        try_total += row.amount_try_minor
    return {
        "amount_minor_by_currency": dict(sorted(by_currency.items())),
        "amount_try_minor": try_total,
    }


@dataclass(frozen=True, slots=True)
class AdjustmentDraft:
    draft_id: str
    status: str
    target_row_id: str
    adjustment_type: str
    amount_minor: int
    currency: str
    amount_try_minor: int
    required_approver_role: str
    reason: str
    evidence_row_ids: tuple[str, ...]


class PendingQueue:
    """Append-only draft queue, kept apart from the ledger. A draft is a request, never an entry."""

    def __init__(self, path: Path) -> None:
        self.path = path

    def _events(self) -> list[dict[str, Any]]:
        if not self.path.exists():
            return []
        return [json.loads(line) for line in self.path.read_text().splitlines() if line]

    def _append(self, event: dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a") as handle:
            handle.write(json.dumps(event, sort_keys=True) + "\n")

    def submit(self, draft: AdjustmentDraft) -> AdjustmentDraft:
        existing = self.get(draft.draft_id)
        if existing is None:
            self._append({"event": "submitted", **asdict(draft)})
            return draft
        return existing

    def withdraw(self, draft_id: str, reason: str) -> None:
        """Called by the orchestrator when the case behind a draft fails review."""
        self._append({"event": "withdrawn", "draft_id": draft_id, "reason": reason})

    def get(self, draft_id: str) -> AdjustmentDraft | None:
        return next((d for d in self.all_drafts() if d.draft_id == draft_id), None)

    def all_drafts(self) -> list[AdjustmentDraft]:
        drafts: dict[str, AdjustmentDraft] = {}
        for event in self._events():
            if event["event"] == "submitted":
                fields = {k: v for k, v in event.items() if k != "event"}
                fields["evidence_row_ids"] = tuple(fields["evidence_row_ids"])
                drafts[event["draft_id"]] = AdjustmentDraft(**fields)
            else:
                withdrawn = drafts[event["draft_id"]]
                drafts[event["draft_id"]] = AdjustmentDraft(
                    **{**asdict(withdrawn), "status": "withdrawn"}
                )
        return list(drafts.values())


class LedgerTools:
    """Tool functions over one dataset. Each takes a validated input model and returns JSON data."""

    def __init__(self, store: LedgerStore, queue: PendingQueue) -> None:
        self.store = store
        self.queue = queue

    @cached_property
    def _matches(self) -> MatchResult:
        return run_matcher(self.store)

    @cached_property
    def _findings(self) -> list[Finding]:
        return run_all_rules(self.store)

    @cached_property
    def _findings_by_id(self) -> dict[str, Finding]:
        return {f.finding_id: f for f in self._findings}

    @property
    def findings(self) -> list[Finding]:
        return self._findings

    @property
    def open_items(self) -> list[OpenItem]:
        return self._matches.open_items

    def finding(self, finding_id: str) -> Finding | None:
        return self._findings_by_id.get(finding_id)

    def list_open_items(self, args: ListOpenItemsInput) -> dict[str, Any]:
        items = [
            item
            for item in self._matches.open_items
            if (args.company_id is None or item.company_id == args.company_id)
            and (args.kind is None or item.kind == args.kind)
            and (args.counterparty_id is None or item.counterparty_id == args.counterparty_id)
        ]
        window, page = _page(items, args.limit, args.offset)
        return {"open_items": [asdict(i) for i in window], **page}

    def get_ledger_rows(self, args: GetLedgerRowsInput) -> dict[str, Any]:
        if args.row_ids is not None:
            found = [
                self.store.ledger_by_id[r] for r in args.row_ids if r in self.store.ledger_by_id
            ]
            missing = [r for r in args.row_ids if r not in self.store.ledger_by_id]
        else:
            missing = []
            source = (
                self.store.rows_by_counterparty.get(args.counterparty_id, [])
                if args.counterparty_id is not None
                else self.store.ledger_rows
            )
            found = [row for row in source if self._ledger_filter(row, args)]
        window, page = _page(found, args.limit, args.offset)
        return {
            "rows": [ledger_row_to_dict(r) for r in window],
            "missing_row_ids": missing,
            "totals": _ledger_totals(found),
            **page,
        }

    @staticmethod
    def _ledger_filter(row: LedgerRow, args: GetLedgerRowsInput) -> bool:
        return (
            (args.company_id is None or row.company_id == args.company_id)
            and (args.doc_type is None or row.doc_type == args.doc_type)
            and (args.posted_by is None or row.posted_by == args.posted_by)
            and (args.date_from is None or row.value_date >= args.date_from)
            and (args.date_to is None or row.value_date <= args.date_to)
            and (args.reference_contains is None or args.reference_contains in row.reference)
        )

    def get_bank_rows(self, args: GetBankRowsInput) -> dict[str, Any]:
        if args.bank_row_ids is not None:
            by_id = self.store.bank_by_id
            found = [by_id[r] for r in args.bank_row_ids if r in by_id]
            missing = [r for r in args.bank_row_ids if r not in by_id]
        else:
            missing = []
            iban = normalize_iban(args.counterparty_iban) if args.counterparty_iban else None
            found = [
                row
                for row in self.store.bank_rows
                if (args.company_id is None or row.company_id == args.company_id)
                and (iban is None or row.counterparty_iban == iban)
                and (args.date_from is None or row.value_date >= args.date_from)
                and (args.date_to is None or row.value_date <= args.date_to)
                and (args.reference_contains is None or args.reference_contains in row.reference)
            ]
        window, page = _page(found, args.limit, args.offset)
        return {"rows": [bank_row_to_dict(r) for r in window], "missing_row_ids": missing, **page}

    def run_matcher(self, args: RunMatcherInput) -> dict[str, Any]:
        result = self._matches
        if args.ledger_row_id is None:
            return {"counts": dict(sorted(result.counts.items()))}
        row = self.store.ledger_by_id.get(args.ledger_row_id)
        if row is None:
            raise ToolError(f"unknown ledger row {args.ledger_row_id}")
        open_item = next(
            (i for i in result.open_items if i.ledger_row_id == row.row_id),
            None,
        )
        candidates = sorted(
            (
                (reference_similarity_pct(row.reference, bank.reference), bank)
                for bank in self.store.bank_rows
                if bank.company_id == row.company_id
                and abs((bank.value_date - row.value_date).days) <= 30
                and abs(abs(bank.amount_minor) - row.amount_minor) * 100 <= row.amount_minor
            ),
            key=lambda pair: (-pair[0], pair[1].bank_row_id),
        )[:10]
        return {
            "ledger_row_id": row.row_id,
            "matched_bank_row_ids": list(result.matched.get(row.row_id, ())),
            "open_item": asdict(open_item) if open_item else None,
            "candidates": [
                {
                    "bank_row_id": bank.bank_row_id,
                    "reference_similarity_pct": similarity,
                    "gap_days": abs((bank.value_date - row.value_date).days),
                    "amount_minor": bank.amount_minor,
                    "currency": bank.currency,
                }
                for similarity, bank in candidates
            ],
        }

    def get_rule_findings(self, args: GetRuleFindingsInput) -> dict[str, Any]:
        wanted = set(args.finding_ids) if args.finding_ids is not None else None
        findings = [
            f
            for f in self._findings
            if (wanted is None or f.finding_id in wanted)
            and (args.typology is None or f.typology == args.typology)
            and (args.company_id is None or f.company_id == args.company_id)
            and (args.counterparty_id is None or f.counterparty_id == args.counterparty_id)
        ]
        window, page = _page(findings, args.limit, args.offset)
        return {"findings": [asdict(f) for f in window], **page}

    def get_counterparty_profile(self, args: GetCounterpartyProfileInput) -> dict[str, Any]:
        cp = self.store.counterparties.get(args.counterparty_id)
        if cp is None:
            raise ToolError(f"unknown counterparty {args.counterparty_id}")
        rows = self.store.rows_by_counterparty.get(cp.counterparty_id, [])
        payments = [r for r in rows if r.doc_type == "PAYMENT"]
        invoices = [r for r in rows if r.doc_type == "INVOICE"]
        return {
            "counterparty_id": cp.counterparty_id,
            "name": cp.name,
            "kind": cp.kind,
            "vendor_type": cp.vendor_type,
            "currency": cp.currency,
            "created_on": cp.created_on.isoformat(),
            "iban": cp.iban,
            "address": cp.address,
            "company_ids": list(cp.company_ids),
            "iban_changes": [
                {
                    "change_id": c.change_id,
                    "changed_on": c.changed_on.isoformat(),
                    "old_iban": c.old_iban,
                    "new_iban": c.new_iban,
                    "changed_by": c.changed_by,
                    "verified_by": c.verified_by,
                }
                for c in self.store.changes_by_counterparty.get(cp.counterparty_id, [])
            ],
            "activity": {
                "invoice_count": len(invoices),
                "payment_count": len(payments),
                "invoice_total_try_minor": sum(r.amount_try_minor for r in invoices),
                "payment_total_try_minor": sum(r.amount_try_minor for r in payments),
                "first_value_date": min(
                    (r.value_date for r in rows), default=cp.created_on
                ).isoformat(),
                "last_value_date": max(
                    (r.value_date for r in rows), default=cp.created_on
                ).isoformat(),
            },
            "employee_links": self._employee_links(cp.iban, cp.address, cp.counterparty_id),
        }

    def _employee_links(
        self, iban: str, address: str, counterparty_id: str
    ) -> list[dict[str, Any]]:
        history = {normalize_iban(iban)}
        for change in self.store.changes_by_counterparty.get(counterparty_id, []):
            history.update((normalize_iban(change.old_iban), normalize_iban(change.new_iban)))
        links = []
        target_address = normalize_address(address)
        for employee in self.store.employees:
            similarity = reference_similarity_pct(
                target_address, normalize_address(employee.address)
            )
            iban_match = normalize_iban(employee.iban) in history
            if iban_match or similarity >= 70:
                links.append(
                    {
                        "employee_id": employee.employee_id,
                        "role": employee.role,
                        "company_id": employee.company_id,
                        "iban_match_including_history": iban_match,
                        "address_similarity_pct": similarity,
                    }
                )
        return links

    def get_approval_matrix(self, args: GetApprovalMatrixInput) -> dict[str, Any]:
        tiers = [
            asdict(t)
            for t in self.store.approval_matrix
            if args.company_id is None or t.company_id == args.company_id
        ]
        return {"tiers": tiers}

    def propose_adjustment(self, args: ProposeAdjustmentInput) -> dict[str, Any]:
        """Create a draft for human approval. The only write; it never touches the ledger."""
        target = self.store.ledger_by_id.get(args.target_row_id)
        if target is None:
            raise ToolError(f"unknown target row {args.target_row_id}")
        unknown = [
            r
            for r in args.evidence_row_ids
            if r not in self.store.ledger_by_id and r not in self.store.bank_by_id
        ]
        if unknown:
            raise ToolError(f"unknown evidence rows: {', '.join(unknown)}")
        if args.currency != target.currency:
            raise ToolError(f"currency must match target row ({target.currency})")
        if args.amount_minor > target.amount_minor:
            raise ToolError(
                f"amount {args.amount_minor} exceeds target row amount {target.amount_minor}"
            )
        amount_try = args.amount_minor * target.fx_rate_micro // 1_000_000
        key = f"{args.target_row_id}|{args.adjustment_type}|{args.amount_minor}"
        draft = AdjustmentDraft(
            draft_id=f"ADJ-{hashlib.sha256(key.encode()).hexdigest()[:10]}",
            status="pending_human_approval",
            target_row_id=args.target_row_id,
            adjustment_type=args.adjustment_type,
            amount_minor=args.amount_minor,
            currency=args.currency,
            amount_try_minor=amount_try,
            required_approver_role=self._approver_role(target.company_id, amount_try),
            reason=args.reason,
            evidence_row_ids=tuple(args.evidence_row_ids),
        )
        return asdict(self.queue.submit(draft))

    def _approver_role(self, company_id: str, amount_try_minor: int) -> str:
        for tier in self.store.approval_matrix:
            if tier.company_id != company_id:
                continue
            if tier.max_amount_try_minor is None or amount_try_minor <= tier.max_amount_try_minor:
                return tier.approver_role
        raise ToolError(f"no approval tier for {company_id}")


@dataclass(frozen=True, slots=True)
class ToolSpec:
    name: str
    description: str
    input_model: type[_Input]
    handler_name: str
    read_only: bool = True


TOOL_SPECS: tuple[ToolSpec, ...] = (
    ToolSpec(
        "get_approval_matrix",
        "Approval tiers per company: the limit in TRY minor units and the approving role. "
        "Use it before reasoning about whether an amount sits near a limit.",
        GetApprovalMatrixInput,
        "get_approval_matrix",
    ),
    ToolSpec(
        "get_bank_rows",
        "Bank statement rows by ID or by filters. Signed amounts: negative leaves the account.",
        GetBankRowsInput,
        "get_bank_rows",
    ),
    ToolSpec(
        "get_counterparty_profile",
        "Master data for one counterparty: age, IBAN change history with who changed and verified "
        "it, activity totals computed in code, and links to employees by IBAN or similar address.",
        GetCounterpartyProfileInput,
        "get_counterparty_profile",
    ),
    ToolSpec(
        "get_ledger_rows",
        "Ledger rows by ID or by filters. Returns `totals` computed in code over all rows the "
        "filter matched, so never add amounts yourself. Copy a total from here instead.",
        GetLedgerRowsInput,
        "get_ledger_rows",
    ),
    ToolSpec(
        "get_rule_findings",
        "Findings of the deterministic rules, with the numbers behind each one in `metrics`.",
        GetRuleFindingsInput,
        "get_rule_findings",
    ),
    ToolSpec(
        "list_open_items",
        "Reconciliation exceptions the matcher could not resolve, with gaps and differences "
        "already computed in `metrics`.",
        ListOpenItemsInput,
        "list_open_items",
    ),
    ToolSpec(
        "propose_adjustment",
        "Create a draft adjustment in the human approval queue. It does not change the ledger and "
        "nothing you can call will post it. Use only for a concrete, evidenced correction.",
        ProposeAdjustmentInput,
        "propose_adjustment",
        read_only=False,
    ),
    ToolSpec(
        "run_matcher",
        "Without arguments: matcher totals by class. With ledger_row_id: how that row was "
        "resolved, plus the nearest bank rows and their reference similarity.",
        RunMatcherInput,
        "run_matcher",
    ),
)
SPECS_BY_NAME = {spec.name: spec for spec in TOOL_SPECS}


def call_tool(tools: LedgerTools, name: str, raw_input: dict[str, Any]) -> dict[str, Any]:
    """Validate and run one tool call. Raises ToolError for anything the caller should see."""
    spec = SPECS_BY_NAME.get(name)
    if spec is None:
        raise ToolError(f"unknown tool {name}")
    try:
        args = spec.input_model.model_validate(raw_input)
    except ValidationError as exc:
        details = "; ".join(
            f"{'.'.join(str(p) for p in e['loc']) or 'input'}: {e['msg']}" for e in exc.errors()
        )
        raise ToolError(f"invalid input for {name}: {details}") from exc
    handler: Callable[[Any], dict[str, Any]] = getattr(tools, spec.handler_name)
    return handler(args)
