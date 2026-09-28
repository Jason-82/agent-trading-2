"""Stage 3: narrow judgments about incoming events (the article's "Jev").

Rather than ask "is this token good?", the assessment is split into narrow
questions with clear criteria. Independent questions share one request; code
combines the answers with explicit rules (see `route`). Counting ("do four
buyers exceed three?") is never delegated to a model.

Any decision model that returns the `Assessment` shape can be plugged in by
implementing the `Judge` protocol. `ClaudeJudge` uses a small, fast Claude
model with a constrained output schema; `HeuristicJudge` is rule-based and
runs offline.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Literal, Protocol

from pydantic import BaseModel

from .config import Config, RouterConfig
from .llm import LLM, LLMError, dumps

log = logging.getLogger(__name__)

BehaviorFit = Literal["fits", "partial", "does_not_fit", "unknown"]
Intent = Literal["exploratory", "standard", "conviction", "partial_exit", "full_exit", "transfer", "unclear"]
History = Literal["sufficient", "insufficient"]
Attention = Literal["no_change", "update_report", "first_analysis"]


class Assessment(BaseModel):
    behavior_fit: BehaviorFit
    behavior_fit_confidence: float
    position_intent: Intent
    position_intent_confidence: float
    history: History
    attention: Attention
    attention_confidence: float

    def clamped(self) -> "Assessment":
        c = lambda x: max(0.0, min(1.0, float(x)))  # noqa: E731
        return self.model_copy(update={
            "behavior_fit_confidence": c(self.behavior_fit_confidence),
            "position_intent_confidence": c(self.position_intent_confidence),
            "attention_confidence": c(self.attention_confidence),
        })


class Judge(Protocol):
    name: str

    async def assess(self, snapshot: dict) -> tuple[Assessment, str]:
        """Returns (assessment, source)."""
        ...


class HeuristicJudge:
    name = "heuristic"

    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg

    async def assess(self, snapshot: dict) -> tuple[Assessment, str]:
        return self.assess_sync(snapshot), self.name

    def assess_sync(self, snap: dict) -> Assessment:
        ev, prof = snap["event"], snap.get("wallet_profile")
        hist = (prof or {}).get("history", {})
        sufficient = (
            hist.get("closed_positions", 0) >= self.cfg.selection.min_closed_positions
            and hist.get("n_tokens", 0) >= self.cfg.graph.min_history_tokens
        )
        side = ev["side"]
        fit: BehaviorFit = "unknown"
        fit_c = 0.3
        intent: Intent = "unclear"
        intent_c = 0.3

        if side == "buy":
            r = ev.get("size_vs_typical_entry")
            style = (prof or {}).get("style")
            if r is not None:
                if 0.33 <= r <= 3.0:
                    fit, fit_c = "fits", 0.8
                elif 0.15 <= r < 0.33 or 3.0 < r <= 6.0:
                    fit, fit_c = "partial", 0.6
                else:
                    fit, fit_c = "does_not_fit", 0.7
                if r < 0.5:
                    # A small first buy is the normal way in for a wallet that scales in.
                    if style == "accumulator" and ev.get("is_first_buy_in_token"):
                        intent, intent_c = "standard", 0.6
                    else:
                        intent, intent_c = "exploratory", 0.7
                elif r <= 2.0:
                    intent, intent_c = "standard", 0.7
                else:
                    intent, intent_c = "conviction", 0.75
        elif side == "sell":
            done = ev.get("position_status_after") == "exited"
            intent, intent_c = ("full_exit", 0.9) if done else ("partial_exit", 0.9)
            held = ev.get("minutes_since_wallet_first_buy")
            p25, p75 = hist.get("p25_hold_s"), hist.get("p75_hold_s")
            if held is not None and p25 is not None and p75 is not None:
                held_s = held * 60
                if p25 / 3 <= held_s <= p75 * 3:
                    fit, fit_c = "fits", 0.7
                elif p25 / 10 <= held_s <= p75 * 10:
                    fit, fit_c = "partial", 0.6
                else:
                    fit, fit_c = "does_not_fit", 0.7
        else:
            intent, intent_c = "transfer", 0.9

        buyers = snap["token"]["tracked_buyers"]
        if snap.get("last_report") is None:
            if buyers >= self.cfg.router.min_buyers_for_analysis:
                attention, att_c = "first_analysis", 0.8
            else:
                attention, att_c = "no_change", 0.8
        else:
            material = (
                side == "sell"
                or (side == "buy" and ev.get("is_first_buy_in_token"))
                or intent == "conviction"
                # adding while other followed wallets reduce: the participants diverge
                or (side == "buy" and bool(ev.get("others_reduced_or_exited")))
            )
            attention, att_c = ("update_report", 0.75) if material else ("no_change", 0.7)

        return Assessment(
            behavior_fit=fit, behavior_fit_confidence=fit_c, position_intent=intent,
            position_intent_confidence=intent_c, history="sufficient" if sufficient else "insufficient",
            attention=attention, attention_confidence=att_c,
        )


JUDGE_SYSTEM = """\
You answer narrow, independent questions about one event in a pump.fun token that a followed wallet just \
traded. Use only the supplied snapshot. Answer each question separately; give a confidence in [0, 1] that \
reflects how clearly the snapshot supports the answer. Low confidence is a valid answer — do not round it up.

behavior_fit — does this action match the wallet's established pattern in wallet_profile (entry size vs. \
typical entry, holding time vs. its usual holding time, its attention_triggers)? "unknown" if the profile \
is missing or silent on this.

position_intent — for buys: "exploratory" (small test position), "standard", or "conviction" (substantially \
larger than usual). A small first buy by a wallet that normally scales in is "standard", not exploratory. \
For sells: "partial_exit" or "full_exit". For transfers: "transfer" (a transfer is never a sale). "unclear" \
otherwise.

history — is the supplied history sufficient to interpret this event? "insufficient" if the wallet has few \
closed positions or thin relationship history.

attention — given last_report: "first_analysis" if there is no report yet and this event makes the \
situation worth a first analysis; "update_report" if the event makes the existing report outdated (for \
example an early buyer reducing, a new kind of participant arriving, a much larger addition); \
"no_change" if the current report remains valid."""


class ClaudeJudge:
    """Narrow-question decision model backed by a small Claude model."""

    def __init__(self, cfg: Config, llm: LLM) -> None:
        self.cfg = cfg
        self.llm = llm
        self.name = cfg.llm.judge_model
        self.fallback = HeuristicJudge(cfg)

    async def assess(self, snapshot: dict) -> tuple[Assessment, str]:
        try:
            a = await self.llm.parse(
                model=self.cfg.llm.judge_model, system=JUDGE_SYSTEM, user=dumps(snapshot),
                schema=Assessment, max_tokens=1024,
            )
            return a.clamped(), self.name
        except LLMError as e:
            log.warning("judge fell back to heuristics: %s", e)
            return self.fallback.assess_sync(snapshot), "heuristic-fallback"


Route = Literal["observe", "analyze", "insufficient_evidence"]


@dataclass
class RouteDecision:
    route: Route
    reasons: list[str] = field(default_factory=list)
    uncertain: bool = False


def route(snapshot: dict, a: Assessment, cfg: RouterConfig) -> RouteDecision:
    """Combine the narrow answers with explicit rules. These routes describe
    what happens to the information; they are not trading orders."""
    buyers = snapshot["token"]["tracked_buyers"]
    rel = snapshot["relationships"]

    if a.attention == "no_change":
        return RouteDecision("observe", ["current reading still valid"])
    if a.attention_confidence < cfg.min_confidence:
        return RouteDecision(
            "observe",
            [f"attention={a.attention} at confidence {a.attention_confidence:.2f} < {cfg.min_confidence}; left uncertain"],
            uncertain=True,
        )
    if buyers < cfg.min_buyers_for_analysis and snapshot.get("last_report") is None:
        return RouteDecision("observe", [f"{buyers} tracked buyer(s) < {cfg.min_buyers_for_analysis} for a first analysis"])
    if a.history == "insufficient" and rel.get("thin_share", 0) >= 0.5:
        return RouteDecision("insufficient_evidence", [
            "event wallet history judged insufficient and most participants have thin relationship history",
        ])
    reason = "first analysis" if snapshot.get("last_report") is None else "existing report needs an update"
    return RouteDecision("analyze", [f"{reason} (attention={a.attention}, confidence {a.attention_confidence:.2f})"])
