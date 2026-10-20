"""Budgets and usage (Req 18)."""

from pydantic import Field

from .base import StrictModel


class TokenCount(StrictModel):
    input_tokens: int = Field(0, ge=0)
    output_tokens: int = Field(0, ge=0)

    @property
    def total(self) -> int:
        return self.input_tokens + self.output_tokens

    def plus(self, other: "TokenCount") -> "TokenCount":
        return TokenCount(
            input_tokens=self.input_tokens + other.input_tokens,
            output_tokens=self.output_tokens + other.output_tokens,
        )


class BudgetLimits(StrictModel):
    critic_rounds: int = Field(ge=1, le=3)
    tool_calls: int = Field(ge=1)
    tokens: int = Field(ge=1)
    wall_clock_s: float = Field(gt=0)
    reporting_reserve: int = Field(ge=0)


class BudgetUsage(StrictModel):
    critic_rounds: int = Field(0, ge=0)
    tool_calls: int = Field(0, ge=0)
    tokens: TokenCount = TokenCount()
    per_caller: dict[str, TokenCount] = {}
    elapsed_s: float = Field(0.0, ge=0)

    @property
    def total_tokens(self) -> int:
        """Token_Usage: input plus output tokens of every LLM call so far."""
        return self.tokens.total
