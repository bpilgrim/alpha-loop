"""LLMPort — abstract interface for AI analysis. NOT callable from live/paper trading path."""

from abc import ABC, abstractmethod


class LLMPort(ABC):

    @abstractmethod
    async def review_session(self, prompt: dict) -> dict:
        """
        Send a structured session review prompt to the LLM.
        Returns a StrategyHypothesis dict:
          hypothesis, rationale, expected_effect, change_type,
          fields_needed, proposed_config_diff, risk
        Only callable from trade_reviewer and research_runner apps.
        """
