"""Citation_Validator: remove absent citations, label Facts and Assumptions (Req 1.6, 1.7, 4).

A pure function of its inputs, so it treats Investigator and Baseline_Agent outputs identically
(Req 4.7) and a second pass changes nothing (Req 4.8).
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Literal

from ..models.claims import ClaimBody
from ..models.evidence import LedgerView
from ..models.incident import ClaimStatusUpdate, IncidentClaim
from ..models.outputs import HypothesisDraft, RecommendedActionDraft
from ..models.vocab import ActionCategory, Caller, ClaimLabel, ClaimStatus

TargetKind = Literal["claim", "hypothesis", "recommended_action", "claim_status"]

EVIDENCE_BACKED_CATEGORIES: frozenset[ActionCategory] = frozenset(
    {ActionCategory.MITIGATION, ActionCategory.FIX, ActionCategory.PREVENTION}
)
"""Recommended_Action categories that must cite evidence (Req 12.4)."""


def unsupported_action_problems(
    actions: Sequence[RecommendedActionDraft], ledger: LedgerView, path: str = "recommended_actions"
) -> list[str]:
    """Req 12.4, checked on what the Citation_Validator will keep.

    The Citation_Validator removes every cited ID that is absent from the Run_Ledger, so a mitigation,
    fix or prevention action is checked on its *remaining* IDs: it needs at least one Evidence_ID that
    is in the ledger. Used as a repairable content rule before the output is committed, so an action
    can no longer pass with only absent IDs and then reach the report with none.
    """
    problems = []
    for index, action in enumerate(actions):
        if action.category in EVIDENCE_BACKED_CATEGORIES and not any(ledger.has(e) for e in action.evidence_ids):
            cited = ", ".join(action.evidence_ids) or "none"
            problems.append(
                f"{path}.{index}.evidence_ids: a {action.category} action must cite at least one evidence ID "
                f"(E0001, E0002, ...) from the context; cited: {cited}, none of which is an evidence ID in the ledger"
            )
    return problems


@dataclass(frozen=True)
class CitableOutput:
    claims: tuple[ClaimBody, ...] = ()
    hypotheses: tuple[HypothesisDraft, ...] = ()
    actions: tuple[RecommendedActionDraft, ...] = ()
    claim_updates: tuple[ClaimStatusUpdate, ...] = ()


@dataclass(frozen=True)
class CitationViolation:
    producer: Caller
    target_kind: TargetKind
    target_id: str
    removed_evidence_id: str | None = None
    removed_hypothesis_id: str | None = None
    kept_prior_status: bool = False

    def trace_fields(self) -> dict:
        return {
            "producer": self.producer,
            "target_kind": self.target_kind,
            "target_id": self.target_id,
            "removed_evidence_id": self.removed_evidence_id,
            "removed_hypothesis_id": self.removed_hypothesis_id,
            "kept_prior_status": self.kept_prior_status,
        }


@dataclass(frozen=True)
class CitationResult:
    output: CitableOutput
    labels: tuple[ClaimLabel, ...]  # one per claim, in order
    violations: tuple[CitationViolation, ...] = field(default_factory=tuple)


def _keep_present(
    ids: Sequence[str],
    ledger: LedgerView,
    producer: Caller,
    kind: TargetKind,
    target: str,
    violations: list[CitationViolation],
) -> tuple[str, ...]:
    kept = []
    for evidence_id in ids:
        if ledger.has(evidence_id):
            kept.append(evidence_id)
        else:
            violations.append(CitationViolation(producer, kind, target, removed_evidence_id=evidence_id))
    return tuple(kept)


def _hypothesis_target(draft: HypothesisDraft, index: int) -> str:
    return draft.hypothesis_id or f"hypothesis[{index}]"


class CitationValidator:
    def validate(
        self,
        output: CitableOutput,
        ledger: LedgerView,
        run_hypothesis_ids: frozenset[str],
        prior_claims: Mapping[str, IncidentClaim],
        producer: Caller,
    ) -> CitationResult:
        violations: list[CitationViolation] = []

        claims = []
        labels = []
        for index, body in enumerate(output.claims):
            kept = _keep_present(body.evidence_ids, ledger, producer, "claim", f"claim[{index}]", violations)
            claims.append(body.model_copy(update={"evidence_ids": kept}))
            labels.append(ClaimLabel.FACT if kept else ClaimLabel.ASSUMPTION)

        hypotheses = []
        for index, draft in enumerate(output.hypotheses):
            target = _hypothesis_target(draft, index)
            supporting = _keep_present(
                draft.supporting_evidence_ids, ledger, producer, "hypothesis", target, violations
            )
            contradicting = _keep_present(
                draft.contradicting_evidence_ids, ledger, producer, "hypothesis", target, violations
            )
            hypotheses.append(
                draft.model_copy(
                    update={"supporting_evidence_ids": supporting, "contradicting_evidence_ids": contradicting}
                )
            )

        actions = []
        for index, action in enumerate(output.actions):
            target = f"action[{index}]"
            if action.hypothesis_id not in run_hypothesis_ids:
                violations.append(
                    CitationViolation(
                        producer, "recommended_action", target, removed_hypothesis_id=action.hypothesis_id
                    )
                )
                continue
            kept = _keep_present(action.evidence_ids, ledger, producer, "recommended_action", target, violations)
            actions.append(action.model_copy(update={"evidence_ids": kept}))

        updates = []
        for update in output.claim_updates:
            prior = prior_claims.get(update.claim_id)
            if prior is None:
                violations.append(CitationViolation(producer, "claim_status", update.claim_id, kept_prior_status=False))
                continue
            corroborating = _keep_present(
                update.corroborating_ids, ledger, producer, "claim_status", update.claim_id, violations
            )
            contradicting = _keep_present(
                update.contradicting_ids, ledger, producer, "claim_status", update.claim_id, violations
            )
            needs = {
                ClaimStatus.VERIFIED: corroborating,
                ClaimStatus.CONTRADICTED: contradicting,
            }.get(update.status)
            if needs is not None and not any(not ledger.is_historical(e) for e in needs):
                # Req 1.6, 1.7: keep the whole prior record, which makes a second pass a no-op.
                violations.append(CitationViolation(producer, "claim_status", update.claim_id, kept_prior_status=True))
                updates.append(
                    ClaimStatusUpdate(
                        claim_id=prior.claim_id,
                        status=prior.status,
                        corroborating_ids=prior.corroborating_ids,
                        contradicting_ids=prior.contradicting_ids,
                    )
                )
                continue
            updates.append(
                update.model_copy(update={"corroborating_ids": corroborating, "contradicting_ids": contradicting})
            )

        cleaned = CitableOutput(
            claims=tuple(claims), hypotheses=tuple(hypotheses), actions=tuple(actions), claim_updates=tuple(updates)
        )
        return CitationResult(output=cleaned, labels=tuple(labels), violations=tuple(violations))


def apply_claim_updates(
    prior_claims: Mapping[str, IncidentClaim], updates: Sequence[ClaimStatusUpdate]
) -> tuple[IncidentClaim, ...]:
    """New claim records from validated updates. A claim citing any evidence is labelled a Fact."""
    result = []
    for update in updates:
        prior = prior_claims.get(update.claim_id)
        if prior is None:
            continue
        cited = bool(update.corroborating_ids or update.contradicting_ids)
        result.append(
            prior.model_copy(
                update={
                    "status": update.status,
                    "corroborating_ids": update.corroborating_ids,
                    "contradicting_ids": update.contradicting_ids,
                    "label": ClaimLabel.FACT if cited else ClaimLabel.ASSUMPTION,
                }
            )
        )
    return tuple(result)
