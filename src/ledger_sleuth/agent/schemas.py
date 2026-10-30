"""Structured output. The schema fixes the shape; the evidence gate decides whether it is true."""

from __future__ import annotations

from datetime import date as date_type
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

Disposition = Literal["suspicious", "benign", "needs_human"]


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class AmountAssertion(_Strict):
    """An amount the rows cited by the claim must support, checked in code."""

    amount_minor: int = Field(description="Positive integer minor units, copied from tool output.")
    currency: Literal["TRY", "EUR", "USD"]
    basis: Literal["each", "any", "sum"] = Field(
        description="each: every cited row equals it. any: at least one does. "
        "sum: the cited rows add up to it, so cite only the rows being summed."
    )
    currency_basis: Literal["transaction", "try_equivalent"] = Field(
        description="transaction: compare with the rows' own amounts in `currency`. "
        "try_equivalent: compare with ledger amount_try_minor; currency must be TRY."
    )


class DateAssertion(_Strict):
    date: date_type
    basis: Literal["each", "any"] = Field(
        description="Must be a value or posting date of every (each) or some (any) cited row."
    )


class Claim(_Strict):
    statement: str = Field(
        description="One factual sentence. Any figure in it must come from the citations."
    )
    row_ids: list[str] = Field(description="Ledger (L-) and bank (B-) rows supporting the claim.")
    finding_ids: list[str] = Field(description="Rule findings this claim relies on. May be empty.")
    amount: AmountAssertion | None
    date: DateAssertion | None


class FindingDisposition(_Strict):
    finding_ids: list[str]
    disposition: Disposition
    reason: str


class OpenItemDisposition(_Strict):
    item_ids: list[str]
    disposition: Literal["close_benign", "keep_open"]
    reason: str


class CaseFile(_Strict):
    cluster_id: str
    disposition: Disposition = Field(description="Overall call on the cluster.")
    summary: str
    claims: list[Claim]
    finding_dispositions: list[FindingDisposition] = Field(
        description="Every finding in the cluster exactly once. Group those with the same reason."
    )
    open_item_dispositions: list[OpenItemDisposition] = Field(
        description="Every open item in the cluster exactly once."
    )
    adjustment_draft_ids: list[str] = Field(
        description="draft_id values returned by propose_adjustment. Empty if none."
    )
    recommended_action: str


class ReviewVerdict(_Strict):
    verdict: Literal["accept", "send_back", "escalate"]
    objections: list[str] = Field(description="Specific and checkable. Empty when accepting.")
