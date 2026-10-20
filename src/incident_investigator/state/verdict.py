"""Verdict rules for both systems (Req 16.4, 19.1, 19.2, 25.5). Pure functions."""

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Protocol

from ..models.evidence import LedgerView
from ..models.vocab import TerminationReason, Verdict


class RankedHypothesis(Protocol):
    hypothesis_id: str
    rank: int
    confidence: float  # after the Confidence_Policy
    supporting_evidence_ids: tuple[str, ...]


@dataclass(frozen=True)
class VerdictDecision:
    verdict: Verdict
    conditions: dict[str, bool]


def _rank1(ranked: Sequence[RankedHypothesis]) -> RankedHypothesis | None:
    return min(ranked, key=lambda h: h.rank) if ranked else None


def _evidence_conditions(rank1: RankedHypothesis | None, ledger: LedgerView, threshold: float) -> dict[str, bool]:
    return {
        "has_hypotheses": rank1 is not None,
        "confidence_at_threshold": rank1 is not None and rank1.confidence >= threshold,
        "complete_current_support": rank1 is not None
        and any(ledger.is_complete_current(e) for e in rank1.supporting_evidence_ids),
    }


def _decide(conditions: dict[str, bool]) -> VerdictDecision:
    verdict = Verdict.CONCLUSIVE if all(conditions.values()) else Verdict.INCONCLUSIVE
    return VerdictDecision(verdict=verdict, conditions=conditions)


def investigator_verdict(
    termination: TerminationReason | None,
    acceptance_id: str | None,
    ranked: Sequence[RankedHypothesis],
    ledger: LedgerView,
    threshold: float,
) -> VerdictDecision:
    """Conclusive only if the Critic accepted rank 1, its adjusted Confidence clears the threshold,
    and it cites at least one in-ledger, ``complete``, non-historical supporting Evidence_ID."""
    rank1 = _rank1(ranked)
    conditions = {
        "critic_accepted": termination is TerminationReason.CRITIC_ACCEPTED,
        "acceptance_names_rank1": rank1 is not None and acceptance_id == rank1.hypothesis_id,
        **_evidence_conditions(rank1, ledger, threshold),
    }
    return _decide(conditions)


def baseline_verdict(
    declared: Verdict, ranked: Sequence[RankedHypothesis], ledger: LedgerView, threshold: float
) -> VerdictDecision:
    """The Baseline_Agent's declared outcome stands in for the Critic's acceptance."""
    rank1 = _rank1(ranked)
    conditions = {"declared_conclusive": declared is Verdict.CONCLUSIVE, **_evidence_conditions(rank1, ledger, threshold)}
    return _decide(conditions)
