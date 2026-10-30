"""Groups findings and open items into units of investigation. Deterministic; no model involved."""

from __future__ import annotations

import hashlib
from collections import defaultdict
from dataclasses import dataclass

from ledger_sleuth.matcher import OpenItem
from ledger_sleuth.rules import Finding
from ledger_sleuth.store import LedgerStore

MAX_CLUSTER_SIZE = 40


@dataclass(frozen=True, slots=True)
class Cluster:
    cluster_id: str
    group_key: str
    counterparty_id: str | None
    typologies: tuple[str, ...]
    finding_ids: tuple[str, ...]
    open_item_ids: tuple[str, ...]
    exposure_try_minor: int
    row_ids: tuple[str, ...]


def _group_key(finding: Finding) -> str:
    if finding.counterparty_id:
        return f"counterparty:{finding.counterparty_id}"
    poster = finding.metrics.get("posted_by")
    if poster:
        return f"poster:{poster}"
    return f"company:{finding.company_id}"


def _item_key(item: OpenItem) -> str:
    if item.counterparty_id:
        return f"counterparty:{item.counterparty_id}"
    return f"company:{item.company_id}"


def build_clusters(
    findings: list[Finding],
    open_items: list[OpenItem],
    store: LedgerStore,
    *,
    max_size: int = MAX_CLUSTER_SIZE,
) -> list[Cluster]:
    """One cluster per counterparty (or poster, for rows with no counterparty), largest risk first.

    Very large groups are split by size so that one recurring vendor cannot produce a single
    conversation too big to finish.
    """
    grouped: dict[str, list[Finding | OpenItem]] = defaultdict(list)
    for finding in findings:
        grouped[_group_key(finding)].append(finding)
    for item in open_items:
        grouped[_item_key(item)].append(item)

    clusters = []
    for key in sorted(grouped):
        members = sorted(
            grouped[key],
            key=lambda m: m.finding_id if isinstance(m, Finding) else m.item_id,
        )
        for start in range(0, len(members), max_size):
            clusters.append(
                _make_cluster(key, start // max_size, members[start : start + max_size], store)
            )
    return sorted(clusters, key=lambda c: (-c.exposure_try_minor, c.cluster_id))


def _make_cluster(
    key: str, part: int, members: list[Finding | OpenItem], store: LedgerStore
) -> Cluster:
    finding_ids, item_ids, typologies = [], [], set()
    rows: set[str] = set()
    for member in members:
        if isinstance(member, Finding):
            finding_ids.append(member.finding_id)
            typologies.add(member.typology)
            rows.update(member.row_ids)
        else:
            item_ids.append(member.item_id)
            if member.ledger_row_id:
                rows.add(member.ledger_row_id)
    exposure = sum(
        store.ledger_by_id[row].amount_try_minor for row in rows if row in store.ledger_by_id
    )
    digest = hashlib.sha256(f"{key}#{part}".encode()).hexdigest()[:10]
    return Cluster(
        cluster_id=f"CL-{digest}",
        group_key=key,
        counterparty_id=key.split(":", 1)[1] if key.startswith("counterparty:") else None,
        typologies=tuple(sorted(typologies)),
        finding_ids=tuple(finding_ids),
        open_item_ids=tuple(item_ids),
        exposure_try_minor=exposure,
        row_ids=tuple(sorted(rows)),
    )
