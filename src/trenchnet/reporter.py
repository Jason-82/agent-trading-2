"""Stage 4: situation reports.

A report answers: what changed, which traders are involved, how this relates
to their previous behavior, and what would change the interpretation next.
Every observed fact must cite the events that support it; citations that do
not resolve to a supplied event are flagged, not silently kept.
"""

from __future__ import annotations

import logging
from typing import Any

from pydantic import BaseModel, Field

from .cases import Case, TrackedWallet, iso
from .config import Config
from .llm import LLM, LLMError, dumps
from .models import short

log = logging.getLogger(__name__)

MAX_EVENTS_IN_PROMPT = 80


class Fact(BaseModel):
    statement: str
    event_refs: list[str] = Field(description="Event ids that support this statement, e.g. ['e3']")


class SituationReport(BaseModel):
    headline: str
    what_changed: str
    traders_involved: list[str]
    relation_to_history: str
    observed_facts: list[Fact]
    interpretations: list[str]
    missing_information: list[str]
    what_would_change_assessment: list[str]


REPORT_SYSTEM = """\
You write situation reports about a pump.fun token that a followed group of wallets is trading.

Explain the change using the supplied trades and profiles. Distinguish observed facts from interpretations: \
observed_facts are things the events show, each citing the event ids (e1, e2, ...) that support it; \
interpretations are your reading of what those facts mean in light of the profiles and relationships. \
Name the missing information that affects this case. End with the specific participant actions that would \
change the assessment. Do not invent motives or a price target.

Wallet relationships describe repeated trading patterns only; they do not establish common ownership or \
coordination. A transfer is not a sale. When a wallet's history is thin, say its figures carry less weight. \
If a previous report exists, describe what changed since it rather than rewriting it. Refer to wallets by \
their aliases."""


def _evidence(case: Case, tracked: dict[str, TrackedWallet]) -> tuple[list[dict], int]:
    name = lambda w: tracked[w].alias if w in tracked else short(w)  # noqa: E731
    events = case.events
    omitted = max(0, len(events) - MAX_EVENTS_IN_PROMPT)
    rows = [
        {"ref": f"e{i + 1}", "wallet": name(t.wallet), "side": t.side.value, "sol": round(t.sol_amount, 4),
         "tokens": round(t.token_amount, 2), "time": iso(t.ts)}
        for i, t in enumerate(events)
    ][omitted:]
    return rows, omitted


class Reporter:
    def __init__(self, cfg: Config, llm: LLM | None = None) -> None:
        self.cfg = cfg
        self.llm = llm if cfg.llm.use_llm_reports else None

    async def write(self, case: Case, snapshot: dict, tracked: dict[str, TrackedWallet],
                    previous: dict | None) -> tuple[dict, str, list[str]]:
        """Returns (report dict, source, unsupported citations)."""
        refs = case.refs()
        if self.llm is not None:
            try:
                rows, omitted = _evidence(case, tracked)
                participants = {tracked[w].alias: tracked[w].profile_brief() for w in case.buyers() if w in tracked}
                recent_assessments = [
                    {"event": a["ref"], "wallet": a["alias"], "assessment": a["assessment"], "route": a["route"]}
                    for a in case.assessments[-10:]
                ]
                user = dumps({
                    "snapshot": snapshot,
                    "events": rows,
                    "older_events_omitted": omitted,
                    "participant_profiles": participants,
                    "recent_assessments": recent_assessments,
                    "previous_report": previous,
                })
                rep = await self.llm.parse(
                    model=self.cfg.llm.report_model, system=REPORT_SYSTEM, user=user,
                    schema=SituationReport, effort=self.cfg.llm.report_effort,
                )
                out = rep.model_dump()
                unsupported = _attach_signatures(out, refs)
                return out, self.cfg.llm.report_model, unsupported
            except LLMError as e:
                log.warning("report for %s fell back to template: %s", case.mint, e)
        out = template_report(case, snapshot, tracked).model_dump()
        return out, "template", _attach_signatures(out, refs)


def _attach_signatures(report: dict[str, Any], refs: dict[str, str]) -> list[str]:
    unsupported = []
    for f in report["observed_facts"]:
        f["signatures"] = [refs[r] for r in f["event_refs"] if r in refs]
        if not f["signatures"]:
            unsupported.append(f["statement"])
    return unsupported


def template_report(case: Case, snap: dict, tracked: dict[str, TrackedWallet]) -> SituationReport:
    """Deterministic report built only from the snapshot and events."""
    ev = snap["event"]
    rel = snap["relationships"]
    positions = snap["positions"]
    alias = {w: t.alias for w, t in tracked.items()}
    verb = {"buy": "bought", "sell": "sold", "transfer_in": "received", "transfer_out": "transferred out"}[ev["side"]]
    tok = snap["token"]

    facts: list[Fact] = []
    for row in positions:
        w_refs = [f"e{i + 1}" for i, t in enumerate(case.events) if alias.get(t.wallet) == row["wallet"]]
        desc = {
            "holding": "holds its full position",
            "reduced": f"has sold {row['sold_fraction']:.0%} of what it acquired",
            "exited": "has exited its observed position",
            "transferred_out": "transferred its tokens out (not a sale)",
            "partially_transferred": "transferred part of its tokens out (not a sale)",
            "flat": "holds no tokens",
        }.get(row["status"], row["status"])
        facts.append(Fact(statement=f"{row['wallet']} bought {row['bought_sol']} SOL over {row['buys']} buy(s) and {desc}.",
                          event_refs=w_refs))

    interp: list[str] = []
    if rel["groups"] and rel["independents"]:
        interp.append(f"Participation has broadened beyond the usual group ({_and(rel['groups'][0])}) "
                      f"to {_and(rel['independents'])}.")
    holding = [r["wallet"] for r in positions if r["status"] == "holding"]
    reduced = [r["wallet"] for r in positions if r["status"] == "reduced"]
    out = [r["wallet"] for r in positions if r["status"] in ("exited", "transferred_out", "flat")]
    if holding and (reduced or out):
        interp.append(f"The participants are split: {_and(reduced + out)} reduced or exited while "
                      f"{_and(holding)} still {'holds' if len(holding) == 1 else 'hold'}.")
    elif holding:
        interp.append("All tracked buyers are still holding; the group is accumulating.")
    elif reduced:
        interp.append(f"No tracked buyer holds a full position; only {_and(reduced)} "
                      f"{'keeps' if len(reduced) == 1 else 'keep'} part of it.")
    elif positions:
        interp.append("Every tracked participant has left its observed position; the group that drew "
                      "attention has unwound.")
    if ev.get("size_vs_typical_entry") and ev["size_vs_typical_entry"] > 2:
        interp.append(f"{ev['wallet']}'s buy is {ev['size_vs_typical_entry']:.1f}x its typical entry.")

    missing = []
    for w in rel["thin_history"]:
        missing.append(f"{w} has thin observed history; its overlap figures carry less weight.")
    if not missing:
        missing.append("No off-chain context; only the observed trades of tracked wallets are known.")

    change = []
    still_in = {r["wallet"] for r in positions if r["status"] in ("holding", "reduced")}
    for r in positions:
        if r["wallet"] in still_in:
            change.append(f"Any sale by {r['wallet']}.")
    for w in rel["independents"]:
        if w in still_in:
            change.append(f"Further additions from {w}.")
    change = change or ["New tracked buyers entering, or the remaining holders selling."]

    return SituationReport(
        headline=f"{ev['wallet']} {verb} {short(case.mint)}; {tok['tracked_buyers']} tracked buyers, "
                 f"{tok['tracked_holders']} still holding",
        what_changed=f"{ev['wallet']} {verb} {ev['sol']} SOL worth ({ev['tokens']} tokens) at {ev['time']}.",
        traders_involved=[r["wallet"] for r in positions],
        relation_to_history=" ".join(rel["summary"]),
        observed_facts=facts,
        interpretations=interp or ["No change in the overall reading."],
        missing_information=missing,
        what_would_change_assessment=change,
    )


def _and(xs: list[str]) -> str:
    return xs[0] if len(xs) == 1 else ", ".join(xs[:-1]) + " and " + xs[-1]


def format_report(r: dict, ts: float | None = None, source: str | None = None) -> str:
    rep = r
    lines = [f"■ {rep['headline']}" + (f"  [{iso(ts)} · {source}]" if ts else "")]
    lines.append(f"  What changed: {rep['what_changed']}")
    lines.append(f"  History: {rep['relation_to_history']}")
    for f in rep["observed_facts"]:
        lines.append(f"  • {f['statement']} ({', '.join(f['event_refs'])})")
    for i in rep["interpretations"]:
        lines.append(f"  ↳ {i}")
    for m in rep["missing_information"]:
        lines.append(f"  ? {m}")
    for c in rep["what_would_change_assessment"]:
        lines.append(f"  → watch: {c}")
    return "\n".join(lines)

