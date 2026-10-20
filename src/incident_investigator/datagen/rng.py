"""Seeded randomness and opaque IDs. Everything the generator produces derives from (Seed, Scenario_Spec)."""

import hashlib
import random


def digest(*parts: object) -> str:
    return hashlib.sha256("|".join(str(p) for p in parts).encode("utf-8")).hexdigest()


def stream(seed: int, scenario: str, purpose: str) -> random.Random:
    """An independent RNG stream per purpose, so adding draws to one stream never shifts another."""
    return random.Random(int(digest(seed, scenario, purpose)[:16], 16))


def incident_id(scenario: str, seed: int) -> str:
    return f"inc-{digest(scenario, seed)[:12]}"


def canary(scenario: str, seed: int) -> str:
    return f"CANARY-{digest('canary', scenario, seed)[:48]}"


def record_id(seed: int, scenario: str, provisional: str) -> str:
    """Opaque final record ID for a provisional ID such as ``log:fault:3`` (prefix kept: log, met, chg, dep, pin)."""
    prefix = provisional.split(":", 1)[0]
    return f"{prefix}-{digest(seed, scenario, provisional)[:12]}"
