"""Control-flow exceptions shared across layers. None of them is an Agent_Failure."""


class RunClosed(Exception):
    """The Run has ended (loop terminated or wall-clock timeout); no new Tool_Call or LLM call may start."""


class BudgetBlocked(Exception):
    """The token Budget (or the part left after the Reporting_Agent reserve) is used up (Req 18.3, 18.4)."""


class ImmutableEvidenceError(Exception):
    """Evidence_Items cannot be modified or deleted after insertion (Req 3.7)."""
