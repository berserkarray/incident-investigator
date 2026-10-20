"""Citation_Validator: removals, labels, claim-status rule, producer independence, idempotence (Req 1.6, 1.7, 4)."""

from hypothesis import given
from hypothesis import strategies as st

from incident_investigator.models.claims import ClaimDraft, LogSignal, ObservabilityClaim
from incident_investigator.models.evidence import LedgerEntry, LedgerView
from incident_investigator.models.incident import ClaimStatusUpdate, IncidentClaim, IncidentClaimKind
from incident_investigator.models.outputs import HypothesisDraft, RecommendedActionDraft
from incident_investigator.models.vocab import AgentName, ClaimLabel, ClaimStatus, Reliability, ToolName
from incident_investigator.state.citations import CitableOutput, CitationValidator, apply_claim_updates

VALIDATOR = CitationValidator()
ID_POOL = [f"E{i}" for i in range(1, 9)]
CLAIM_IDS = ["IC1", "IC2", "IC3"]


def ledger(**entries: tuple[ToolName, Reliability]) -> LedgerView:
    return LedgerView(items={k: LedgerEntry(tool=t, reliability=r) for k, (t, r) in entries.items()})


LEDGER = ledger(
    E1=(ToolName.QUERY_LOGS, Reliability.COMPLETE),
    E2=(ToolName.SEARCH_INCIDENTS, Reliability.COMPLETE),
    E3=(ToolName.QUERY_METRICS, Reliability.PARTIAL),
)
PRIOR = {
    "IC1": IncidentClaim(claim_id="IC1", kind=IncidentClaimKind.SYMPTOM, text="errors"),
    "IC2": IncidentClaim(
        claim_id="IC2",
        kind=IncidentClaimKind.AFFECTED_SERVICE,
        text="checkout",
        status=ClaimStatus.VERIFIED,
        corroborating_ids=("E1",),
        label=ClaimLabel.FACT,
    ),
}


def hypothesis(supporting=(), contradicting=(), rank=1, confidence=0.5, component="checkout-a1b") -> HypothesisDraft:
    return HypothesisDraft(
        component=component,
        cause_type="config_error",
        statement="s",
        supporting_evidence_ids=tuple(supporting),
        contradicting_evidence_ids=tuple(contradicting),
        confidence=confidence,
        rank=rank,
        evidence_sufficient=False,
    )


# example tests


def test_absent_ids_are_removed_and_order_is_kept():
    output = CitableOutput(
        claims=(ClaimDraft(text="t", evidence_ids=("E9", "E3", "E8", "E1")),),
        hypotheses=(hypothesis(supporting=("E3", "E7", "E1"), contradicting=("E6",)),),
    )
    result = VALIDATOR.validate(output, LEDGER, frozenset(), {}, AgentName.REASONING)
    assert result.output.claims[0].evidence_ids == ("E3", "E1")
    assert result.output.hypotheses[0].supporting_evidence_ids == ("E3", "E1")
    assert result.output.hypotheses[0].contradicting_evidence_ids == ()
    removed = [(v.target_kind, v.removed_evidence_id) for v in result.violations]
    assert removed == [("claim", "E9"), ("claim", "E8"), ("hypothesis", "E7"), ("hypothesis", "E6")]
    assert all(v.producer == AgentName.REASONING for v in result.violations)


def test_orphan_recommended_action_is_removed():
    actions = (
        RecommendedActionDraft(text="roll back", category="mitigation", hypothesis_id="H1", evidence_ids=("E1", "E9")),
        RecommendedActionDraft(text="look", category="further_investigation", hypothesis_id="H7"),
    )
    result = VALIDATOR.validate(CitableOutput(actions=actions), LEDGER, frozenset({"H1"}), {}, AgentName.REPORTING)
    assert [a.hypothesis_id for a in result.output.actions] == ["H1"]
    assert result.output.actions[0].evidence_ids == ("E1",)
    kinds = [(v.target_kind, v.removed_evidence_id, v.removed_hypothesis_id) for v in result.violations]
    assert kinds == [("recommended_action", "E9", None), ("recommended_action", None, "H7")]


def test_fact_and_assumption_labels_ignore_reliability_and_history():
    claims = (
        ClaimDraft(text="historical only", evidence_ids=("E2",)),
        ClaimDraft(text="partial only", evidence_ids=("E3",)),
        ClaimDraft(text="nothing cited"),
        ClaimDraft(text="only absent", evidence_ids=("E9",)),
        ObservabilityClaim(
            text="log errors", service="checkout-a1b", signal=LogSignal(level="error", pattern="x"), no_onset=True, evidence_ids=("E1",)
        ),
    )
    result = VALIDATOR.validate(CitableOutput(claims=claims), LEDGER, frozenset(), {}, AgentName.OBSERVABILITY)
    assert result.labels == (ClaimLabel.FACT, ClaimLabel.FACT, ClaimLabel.ASSUMPTION, ClaimLabel.ASSUMPTION, ClaimLabel.FACT)


def test_claim_status_needs_non_historical_support_of_the_matching_kind():
    updates = (
        ClaimStatusUpdate(claim_id="IC1", status="verified", corroborating_ids=("E2",)),  # historical only
        ClaimStatusUpdate(claim_id="IC2", status="contradicted", corroborating_ids=("E1",)),  # wrong kind
    )
    result = VALIDATOR.validate(CitableOutput(claim_updates=updates), LEDGER, frozenset(), PRIOR, AgentName.REASONING)
    first, second = result.output.claim_updates
    assert (first.status, first.corroborating_ids) == (ClaimStatus.UNVERIFIED, ())
    # the whole prior record comes back, citations included (Req 1.6, 1.7)
    assert (second.status, second.corroborating_ids) == (ClaimStatus.VERIFIED, ("E1",))
    assert [v.kept_prior_status for v in result.violations] == [True, True]
    claims = {c.claim_id: c for c in apply_claim_updates(PRIOR, result.output.claim_updates)}
    assert claims["IC2"].label is ClaimLabel.FACT and claims["IC1"].label is ClaimLabel.ASSUMPTION

    accepted = VALIDATOR.validate(
        CitableOutput(claim_updates=(ClaimStatusUpdate(claim_id="IC1", status="verified", corroborating_ids=("E2", "E3")),)),
        LEDGER,
        frozenset(),
        PRIOR,
        AgentName.REASONING,
    )
    assert accepted.output.claim_updates[0].status is ClaimStatus.VERIFIED and accepted.violations == ()


def test_investigator_and_baseline_outputs_are_treated_identically():
    output = CitableOutput(
        claims=(ClaimDraft(text="t", evidence_ids=("E1", "E9")),),
        hypotheses=(hypothesis(supporting=("E5",)),),
        actions=(RecommendedActionDraft(text="x", category="further_investigation", hypothesis_id="H9"),),
        claim_updates=(ClaimStatusUpdate(claim_id="IC1", status="verified", corroborating_ids=("E2",)),),
    )
    multi = VALIDATOR.validate(output, LEDGER, frozenset({"H1"}), PRIOR, AgentName.REASONING)
    base = VALIDATOR.validate(output, LEDGER, frozenset({"H1"}), PRIOR, "baseline")
    assert multi.output == base.output and multi.labels == base.labels

    def strip(result):
        return [{**v.trace_fields(), "producer": None} for v in result.violations]

    assert strip(multi) == strip(base) and len(multi.violations) == 4


# Property 13


@st.composite
def ledgers(draw) -> LedgerView:
    present = draw(st.lists(st.sampled_from(ID_POOL), unique=True, min_size=1, max_size=6))
    tools = st.sampled_from(list(ToolName))
    return LedgerView(
        items={i: LedgerEntry(tool=draw(tools), reliability=draw(st.sampled_from(list(Reliability)))) for i in present}
    )


def id_lists(max_size: int = 4):
    return st.lists(st.sampled_from(ID_POOL), unique=True, max_size=max_size)


@st.composite
def hypotheses(draw) -> HypothesisDraft:
    ids = draw(id_lists(6))
    cut = draw(st.integers(0, len(ids)))
    return hypothesis(supporting=ids[:cut], contradicting=ids[cut:])


@st.composite
def actions(draw) -> RecommendedActionDraft:
    category = draw(st.sampled_from(["mitigation", "fix", "prevention", "further_investigation"]))
    evidence = draw(id_lists().filter(lambda ids: bool(ids) or category == "further_investigation"))
    return RecommendedActionDraft(
        text="act", category=category, hypothesis_id=draw(st.sampled_from(["H1", "H2", "H3", "H4"])), evidence_ids=tuple(evidence)
    )


@st.composite
def updates(draw) -> ClaimStatusUpdate:
    return ClaimStatusUpdate(
        claim_id=draw(st.sampled_from([*CLAIM_IDS, "IC9"])),
        status=draw(st.sampled_from([ClaimStatus.VERIFIED, ClaimStatus.CONTRADICTED, ClaimStatus.UNVERIFIED])),
        corroborating_ids=tuple(draw(id_lists(2))),
        contradicting_ids=tuple(draw(id_lists(2))),
    )


@st.composite
def priors(draw, ledger_view: LedgerView) -> dict[str, IncidentClaim]:
    """Prior claim records as the Blackboard holds them: each was validated against the ledger, which
    only grows, so a held verified or contradicted status always has current, in-ledger support.
    Held statuses are mostly verified or contradicted, so later reverts restore non-trivial records."""
    current = sorted(e for e in ledger_view.items if not ledger_view.is_historical(e))
    held = {}
    for claim_id in CLAIM_IDS:
        status = draw(st.sampled_from([ClaimStatus.VERIFIED, ClaimStatus.CONTRADICTED, ClaimStatus.UNVERIFIED]))
        cited = tuple(draw(st.lists(st.sampled_from(current), unique=True, min_size=1, max_size=2))) if current else ()
        if status is ClaimStatus.UNVERIFIED or not cited:
            status, cited = ClaimStatus.UNVERIFIED, ()
        raw = ClaimStatusUpdate(
            claim_id=claim_id,
            status=status,
            corroborating_ids=cited if status is ClaimStatus.VERIFIED else (),
            contradicting_ids=cited if status is ClaimStatus.CONTRADICTED else (),
        )
        unverified = IncidentClaim(claim_id=claim_id, kind=IncidentClaimKind.SYMPTOM, text="claim")
        result = VALIDATOR.validate(CitableOutput(claim_updates=(raw,)), ledger_view, frozenset(), {claim_id: unverified}, "baseline")
        assert result.violations == ()
        (record,) = apply_claim_updates({claim_id: unverified}, result.output.claim_updates)
        held[claim_id] = record
    return held


@st.composite
def cases(draw):
    ledger_view = draw(ledgers())
    output = CitableOutput(
        claims=tuple(ClaimDraft(text="c", evidence_ids=tuple(ids)) for ids in draw(st.lists(id_lists(), max_size=4))),
        hypotheses=tuple(draw(st.lists(hypotheses(), max_size=4))),
        actions=tuple(draw(st.lists(actions(), max_size=4))),
        claim_updates=tuple(draw(st.lists(updates(), min_size=1, max_size=5))),
    )
    run_ids = frozenset(draw(st.lists(st.sampled_from(["H1", "H2", "H3"]), unique=True)))
    producer = draw(st.sampled_from([AgentName.REASONING, AgentName.REPORTING, "baseline"]))
    return output, ledger_view, run_ids, draw(priors(ledger_view)), producer


# Feature: incident-investigator, Property 13: Citation validation is idempotent
@given(cases())
def test_property_13_citation_validation_is_idempotent(case):
    """Req 4.8: validate(validate(x)) == validate(x), and the second pass records no violation."""
    output, ledger_view, run_ids, prior, producer = case
    first = VALIDATOR.validate(output, ledger_view, run_ids, prior, producer)
    second = VALIDATOR.validate(first.output, ledger_view, run_ids, prior, producer)
    assert second.output == first.output
    assert second.labels == first.labels
    assert second.violations == ()
    # and every citation left after the first pass is in the ledger
    for body in first.output.claims:
        assert all(ledger_view.has(e) for e in body.evidence_ids)
    for draft in first.output.hypotheses:
        assert all(ledger_view.has(e) for e in (*draft.supporting_evidence_ids, *draft.contradicting_evidence_ids))
    assert all(a.hypothesis_id in run_ids for a in first.output.actions)
