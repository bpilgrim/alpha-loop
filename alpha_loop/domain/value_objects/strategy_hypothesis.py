"""StrategyHypothesis — structured output from the LLM review runner."""

from dataclasses import dataclass, field
from typing import Optional


@dataclass
class StrategyHypothesis:
    hypothesis: str
    rationale: str
    expected_effect: str
    change_type: str                     # filter | entry | exit | risk
    fields_needed: list[str] = field(default_factory=list)
    proposed_config_diff: dict = field(default_factory=dict)
    risk: str = ""

    def to_dict(self) -> dict:
        return {
            "hypothesis": self.hypothesis,
            "rationale": self.rationale,
            "expected_effect": self.expected_effect,
            "change_type": self.change_type,
            "fields_needed": self.fields_needed,
            "proposed_config_diff": self.proposed_config_diff,
            "risk": self.risk,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "StrategyHypothesis":
        return cls(
            hypothesis=d.get("hypothesis", ""),
            rationale=d.get("rationale", ""),
            expected_effect=d.get("expected_effect", ""),
            change_type=d.get("change_type", "filter"),
            fields_needed=d.get("fields_needed", []),
            proposed_config_diff=d.get("proposed_config_diff", {}),
            risk=d.get("risk", ""),
        )
