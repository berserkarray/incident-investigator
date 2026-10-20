"""Confidence_Policy: one uniform penalty per distinct Data_Gap (Req 17.4, 17.5, D12).

Known limitation: the penalty is applied to every Hypothesis whatever the gap's relevance to it.
"""

from collections.abc import Sequence

from ..models.gaps import DataGap
from ..models.hypothesis import ConfidenceAdjustment, HypothesisRevision


def distinct_gap_count(gaps: Sequence[DataGap]) -> int:
    return len({gap.dedupe_key() for gap in gaps if not gap.after_verdict})


def apply_confidence_policy(
    active: Sequence[HypothesisRevision],
    superseded: Sequence[HypothesisRevision],
    gaps: Sequence[DataGap],
    penalty: float,
) -> tuple[ConfidenceAdjustment, ...]:
    """Adjusted Confidence = max(0, C - penalty * N). A uniform shift keeps the rank order."""
    n = distinct_gap_count(gaps)
    return tuple(
        ConfidenceAdjustment(
            hypothesis_id=h.hypothesis_id,
            revision=h.revision,
            raw_confidence=h.confidence,
            adjusted_confidence=max(0.0, h.confidence - penalty * n),
            penalty=penalty,
            gap_count=n,
        )
        for h in (*active, *superseded)
    )
