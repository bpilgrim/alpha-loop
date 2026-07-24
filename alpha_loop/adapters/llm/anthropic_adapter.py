"""AnthropicAdapter — LLMPort implementation using the Anthropic API."""

import json
import logging
import re

from ...ports.llm_port import LLMPort

logger = logging.getLogger(__name__)

_MODEL = "claude-sonnet-4-6"
_MAX_TOKENS = 2048

_SYSTEM_PROMPT = """\
You are a systematic trading strategy analyst. You receive a structured report
of a paper trading session and return a single concrete hypothesis for improving
the strategy. You must respond with ONLY a valid JSON object — no markdown fences,
no preamble, no explanation outside the JSON.

The JSON must have exactly these keys:
  hypothesis        – one-sentence statement of what change to test
  rationale         – 2–4 sentences explaining what the data shows and why
  expected_effect   – specific, measurable prediction (e.g. "win rate +5–10%")
  change_type       – one of: filter | entry | exit | risk
  fields_needed     – list of feature/column names needed to test this hypothesis
  proposed_config_diff – partial YAML config dict showing the specific parameter change
  risk              – one sentence describing the main failure mode of this hypothesis
"""


class AnthropicAdapter(LLMPort):
    def __init__(self, api_key: str, model: str = _MODEL) -> None:
        import anthropic
        self._client = anthropic.AsyncAnthropic(api_key=api_key)
        self._model = model

    async def review_session(self, prompt: dict) -> dict:
        """
        Send session metrics to Claude and parse a StrategyHypothesis JSON response.
        `prompt` is the metrics dict from ReviewEngine.compute_metrics().
        """
        user_message = _build_user_message(prompt)

        logger.info("Sending session review to Claude (%s)", self._model)
        response = await self._client.messages.create(
            model=self._model,
            max_tokens=_MAX_TOKENS,
            system=_SYSTEM_PROMPT,
            messages=[{"role": "user", "content": user_message}],
        )

        raw = response.content[0].text.strip()
        logger.debug("LLM raw response: %s", raw[:200])

        return _parse_hypothesis(raw)


# ---------------------------------------------------------------------------
# Prompt builder
# ---------------------------------------------------------------------------

def _build_user_message(metrics: dict) -> str:
    ss = metrics.get("signal_stats", {})
    ts = metrics.get("trade_stats", {})
    rd = metrics.get("r_distribution", {})
    mm = metrics.get("mae_mfe", {})
    eb = metrics.get("exit_breakdown", {})
    ht = metrics.get("hold_time", {})
    best = metrics.get("best_trades", [])
    worst = metrics.get("worst_trades", [])

    lines = [
        "## Session Metrics Report",
        "",
        "### Signal Funnel",
        f"- Total signals evaluated: {ss.get('total', 0)}",
        f"- Passed filters: {ss.get('passed', 0)} ({ss.get('pass_rate_pct', 0)}%)",
        f"- Rejected: {ss.get('rejected', 0)}",
        "- Rejection breakdown:",
    ]
    for reason, count in ss.get("rejection_breakdown", {}).items():
        lines.append(f"  - {reason}: {count}")

    lines += [
        "",
        "### Trade Performance",
        f"- Total closed trades: {ts.get('total', 0)}",
        f"- Wins / Losses: {ts.get('wins', 0)} / {ts.get('losses', 0)}",
        f"- Win rate: {ts.get('win_rate_pct', 0)}%",
        f"- Avg win: ${ts.get('avg_win_usd', 0):.4f}",
        f"- Avg loss: ${ts.get('avg_loss_usd', 0):.4f}",
        f"- Expectancy: ${ts.get('expectancy_usd', 0):.4f} per trade",
        f"- Total PnL: ${ts.get('total_pnl_usd', 0):.4f}",
        "",
        "### R-Multiple Distribution",
        f"- Avg R: {rd.get('avg')}",
        f"- Median R: {rd.get('median')}",
        f"- Best: {rd.get('best')}R  |  Worst: {rd.get('worst')}R",
        f"- Positive R: {rd.get('positive_pct')}%",
        "",
        "### Stop Quality (MAE/MFE on winners)",
        f"- Avg MAE/MFE ratio: {mm.get('avg_mae_mfe_ratio')} (lower = better stop placement)",
        f"- Avg adverse excursion: {mm.get('avg_mae_pct')}%",
        f"- Avg favorable excursion: {mm.get('avg_mfe_pct')}%",
        "",
        "### Exit Reason Breakdown",
    ]
    for reason, stats in eb.items():
        lines.append(
            f"  - {reason}: {stats['count']} trades ({stats['pct']}%), "
            f"win rate {stats['win_rate_pct']}%"
        )

    lines += [
        "",
        "### Hold Time",
        f"- Avg: {ht.get('avg_minutes')} min  |  Median: {ht.get('median_minutes')} min",
        f"- Range: {ht.get('min_minutes')}–{ht.get('max_minutes')} min",
    ]

    if best:
        lines += ["", "### Best Trades (by R-multiple)"]
        for t in best:
            lines.append(
                f"  - {t['symbol']} | R={t['r_multiple']} | exit={t['exit_reason']} | "
                f"hold={t['hold_time_minutes']}min | MAE={t['mae_pct']}% MFE={t['mfe_pct']}%"
            )

    if worst:
        lines += ["", "### Worst Trades (by R-multiple)"]
        for t in worst:
            lines.append(
                f"  - {t['symbol']} | R={t['r_multiple']} | exit={t['exit_reason']} | "
                f"hold={t['hold_time_minutes']}min | MAE={t['mae_pct']}% MFE={t['mfe_pct']}%"
            )

    lines += [
        "",
        "Based on this data, provide one concrete, testable hypothesis for improving "
        "the strategy. Focus on what the data most clearly suggests, not what sounds "
        "sophisticated. Be specific about the parameter change.",
    ]

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Response parser
# ---------------------------------------------------------------------------

def _parse_hypothesis(raw: str) -> dict:
    """Extract JSON from LLM response, tolerating minor formatting variations."""
    # Strip markdown fences if present
    text = re.sub(r"```(?:json)?\s*", "", raw).strip().rstrip("`").strip()

    try:
        return json.loads(text)
    except json.JSONDecodeError:
        # Try extracting the first {...} block
        match = re.search(r"\{.*\}", text, re.DOTALL)
        if match:
            try:
                return json.loads(match.group())
            except json.JSONDecodeError:
                pass
        logger.error("Failed to parse LLM response as JSON:\n%s", raw[:500])
        # Return a fallback so the runner doesn't crash
        return {
            "hypothesis": "Parse error — see raw response in logs",
            "rationale": raw[:500],
            "expected_effect": "unknown",
            "change_type": "filter",
            "fields_needed": [],
            "proposed_config_diff": {},
            "risk": "LLM response was not valid JSON",
        }
