"""Stage 1: trader profiles.

Code computes the numbers (stats.py); the model explains what makes a wallet
worth following and which of its behaviors deserve attention. A heuristic
profiler produces the same shape offline or when the model is unavailable.
"""

from __future__ import annotations

import logging
import string
import time
from datetime import datetime, timezone
from typing import Literal

from pydantic import BaseModel, Field

from .config import Config
from .llm import LLM, LLMError, dumps
from .models import Trade
from .stats import WalletStats, compute_wallet_stats, select
from .store import Store

log = logging.getLogger(__name__)

Style = Literal[
    "patient_holder", "scalper", "sniper", "accumulator", "concentrated_winner", "mixed", "insufficient_history"
]


class Evidence(BaseModel):
    claim: str
    trade_refs: list[str] = Field(description="Trade ids from the table, e.g. ['t12', 't40']")


class TraderProfile(BaseModel):
    style: Style
    summary: str = Field(description="2-4 sentences on how this wallet trades")
    inclusion_evidence: list[Evidence] = Field(description="Why this wallet is worth following, each claim backed by trades")
    attention_triggers: list[str] = Field(description="Specific future behaviors that deserve examination")
    typical_entry: str
    holding_behavior: str
    profit_concentration: str
    missing_history: list[str] = Field(description="Gaps in the supplied history that change the interpretation")
    recommend_include: bool


PROFILE_SYSTEM = """\
You profile pump.fun (Solana memecoin) wallets for a system that follows a small group of traders.

Describe how this wallet trades using the supplied history. Identify repeatable behavior, profit \
concentration, typical holding time, and unusual position changes. Support each conclusion with trades by \
citing trade ids from the table (t1, t2, ...). Mark missing history where it changes the interpretation.

The computed statistics are authoritative; interpret them rather than recomputing them. A wallet whose \
profit mostly comes from one exceptional winner, or that finishes trades within minutes, means something \
different from one that repeatedly realizes gains while holding for hours — say which this is. Open \
positions are unrealized: do not treat them as gains. "Unattributed proceeds" are sales of tokens bought \
before the observed history began.

attention_triggers should be concrete and checkable against future trades, e.g. "a buy larger than 2x its \
usual 0.5 SOL entry" or "selling within 10 minutes, which it rarely does".

Describe trading behavior only. Do not guess identities, ownership, or motives."""


def _fmt_ts(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def render_trade_table(trades: list[Trade], limit: int) -> tuple[str, dict[str, str], int]:
    """Compact trade table with short ids. Returns (table, ref->signature, omitted count)."""
    trades = sorted(trades, key=lambda t: t.ts)
    omitted = max(0, len(trades) - limit)
    shown = trades[omitted:]
    mint_labels: dict[str, str] = {}
    refs: dict[str, str] = {}
    lines = ["id | time_utc | token | side | sol | tokens"]
    for i, t in enumerate(shown, start=omitted + 1):
        ref = f"t{i}"
        refs[ref] = t.signature
        label = mint_labels.setdefault(t.mint, f"M{len(mint_labels) + 1}")
        lines.append(f"{ref} | {_fmt_ts(t.ts)} | {label} | {t.side.value} | {t.sol_amount:.4f} | {t.token_amount:.0f}")
    return "\n".join(lines), refs, omitted


def heuristic_profile(stats: WalletStats) -> TraderProfile:
    """Rule-based profile with the same shape as the model's output."""
    s = stats
    missing: list[str] = []
    if s.positions_missing_basis:
        missing.append(
            f"{s.positions_missing_basis} positions were sold without an observed buy "
            f"({s.unattributed_proceeds_sol:.2f} SOL unattributed); realized PnL excludes them."
        )
    if s.recent_open_share is not None and s.recent_open_share >= 0.5:
        missing.append(f"{s.recent_open_share:.0%} of the most recent positions are still open; recent gains are unrealized.")

    if s.closed_positions < 3:
        style: Style = "insufficient_history"
    elif s.quick_flip_share is not None and s.quick_flip_share >= 0.6:
        style = "scalper"
    elif s.top_token_profit_share is not None and s.top_token_profit_share >= 0.6:
        style = "concentrated_winner"
    elif s.avg_buys_per_position is not None and s.avg_buys_per_position >= 2.0:
        style = "accumulator"
    elif s.median_hold_s is not None and s.median_hold_s >= 3600:
        style = "patient_holder"
    else:
        style = "mixed"

    entry = s.median_entry_sol or 0.0
    hold_h = (s.median_hold_s or 0.0) / 3600
    triggers = []
    if entry:
        triggers.append(f"a buy larger than {2 * entry:.2f} SOL (2x its median {entry:.2f} SOL entry)")
    if style in ("patient_holder", "accumulator"):
        triggers.append("a full exit within 10 minutes of entry, which is unusual for this wallet")
    if style == "accumulator":
        triggers.append("a single large entry instead of its usual scaling in")
    triggers.append("any sale while other followed wallets still hold the same token")

    evidence = []
    if s.closed_positions:
        evidence.append(Evidence(
            claim=f"Realized {s.realized_pnl_sol:.2f} SOL across {s.closed_positions} closed positions"
                  + (f" (win rate {s.win_rate:.0%})" if s.win_rate is not None else ""),
            trade_refs=[],
        ))
    return TraderProfile(
        style=style,
        summary=(
            f"{style.replace('_', ' ').capitalize()}: {s.n_trades} trades over {s.n_tokens} tokens, "
            f"median hold {hold_h:.1f}h, median entry {entry:.2f} SOL, "
            f"{s.avg_buys_per_position or 0:.1f} buys per position."
        ),
        inclusion_evidence=evidence,
        attention_triggers=triggers,
        typical_entry=f"median first buy {entry:.2f} SOL; 90th percentile buy {s.p90_buy_sol or 0:.2f} SOL",
        holding_behavior=(
            f"median hold {hold_h:.1f}h; {s.quick_flip_share or 0:.0%} of closed positions held under 5 minutes"
        ),
        profit_concentration=(
            f"top token = {s.top_token_profit_share:.0%} of gross profit, top 3 = {s.top3_profit_share:.0%}"
            if s.top_token_profit_share is not None else "no realized profit to concentrate"
        ),
        missing_history=missing,
        recommend_include=style not in ("insufficient_history", "scalper"),
    )


class Profiler:
    def __init__(self, cfg: Config, llm: LLM | None = None) -> None:
        self.cfg = cfg
        self.llm = llm if cfg.llm.use_llm_profiles else None

    async def profile(self, stats: WalletStats, trades: list[Trade]) -> tuple[dict, str]:
        """Returns (profile dict, source) where source is the model id or 'heuristic'."""
        if self.llm is not None:
            try:
                table, refs, omitted = render_trade_table(trades, self.cfg.llm.max_trades_in_prompt)
                note = (f"\n{omitted} older trades are omitted from the table; the statistics cover the full history."
                        if omitted else "")
                user = (
                    f"Wallet: {stats.wallet}\n\nComputed statistics (authoritative):\n{dumps(stats.to_dict())}\n\n"
                    f"Trades:{note}\n{table}"
                )
                prof = await self.llm.parse(
                    model=self.cfg.llm.profile_model, system=PROFILE_SYSTEM, user=user,
                    schema=TraderProfile, effort=self.cfg.llm.profile_effort,
                )
                out = prof.model_dump()
                unknown = []
                for ev in out["inclusion_evidence"]:
                    ev["signatures"] = [refs[r] for r in ev["trade_refs"] if r in refs]
                    unknown += [r for r in ev["trade_refs"] if r not in refs]
                out["unresolved_refs"] = unknown
                return out, self.cfg.llm.profile_model
            except LLMError as e:
                log.warning("profile for %s fell back to heuristics: %s", stats.wallet, e)
        return heuristic_profile(stats).model_dump(), "heuristic"


def _alias(i: int) -> str:
    letters = string.ascii_uppercase
    return letters[i] if i < 26 else f"{letters[i // 26 - 1]}{letters[i % 26]}"


async def build_roster(store: Store, cfg: Config, llm: LLM | None, wallets: list[str] | None = None,
                       aliases: dict[str, str] | None = None, profile_all: bool = False,
                       now: float | None = None) -> list[dict]:
    """Compute stats for candidates, apply explicit selection criteria,
    profile the selected wallets, and persist the ordered roster."""
    wallets = wallets if wallets is not None else store.wallets()
    aliases = dict(aliases or {})
    profiler = Profiler(cfg, llm)
    rows = []
    for w in wallets:
        trades = store.trades(wallets=[w])
        stats = compute_wallet_stats(w, trades, now=now)
        sel = select(stats, cfg.selection)
        rows.append((sel, stats, trades))
    rows.sort(key=lambda r: r[0].score, reverse=True)

    used = set(aliases.values())
    next_i = 0
    out = []
    for sel, stats, trades in rows:
        if sel.wallet not in aliases:
            while _alias(next_i) in used:
                next_i += 1
            aliases[sel.wallet] = _alias(next_i)
            used.add(aliases[sel.wallet])
        profile, source = (None, None)
        if sel.eligible or profile_all:
            profile, source = await profiler.profile(stats, trades)
        included = sel.eligible and (profile is None or profile.get("recommend_include", True))
        reasons = list(sel.reasons)
        if sel.eligible and not included:
            reasons.append(f"profile recommends exclusion (style: {profile['style']})")
        store.upsert_roster(
            wallet=sel.wallet, alias=aliases[sel.wallet], included=included, score=sel.score,
            reasons=reasons, stats=stats.to_dict(), profile=profile, profile_source=source,
            ts=now if now is not None else time.time(),
        )
        out.append({"wallet": sel.wallet, "alias": aliases[sel.wallet], "included": included,
                    "score": sel.score, "reasons": reasons, "profile": profile, "profile_source": source})
    return out
