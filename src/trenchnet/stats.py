"""Stage 1 (code half): measurable facts about each candidate wallet.

Everything that can be computed is computed here, so the language model only
has to interpret, never to count.
"""

from __future__ import annotations

import math
import statistics
from dataclasses import asdict, dataclass, field
from typing import Iterable

from .ledger import Ledger
from .models import Side, Trade

QUICK_FLIP_S = 5 * 60
RECENT_WINDOW = 10


@dataclass
class WalletStats:
    wallet: str
    n_trades: int = 0
    n_buys: int = 0
    n_sells: int = 0
    n_transfers: int = 0
    n_tokens: int = 0
    first_ts: float | None = None
    last_ts: float | None = None
    active_days: float = 0.0
    trades_per_day: float = 0.0
    realized_pnl_sol: float = 0.0
    gross_profit_sol: float = 0.0
    gross_loss_sol: float = 0.0
    closed_positions: int = 0
    winning_positions: int = 0
    losing_positions: int = 0
    win_rate: float | None = None
    open_positions: int = 0
    open_cost_sol: float = 0.0
    median_hold_s: float | None = None
    p25_hold_s: float | None = None
    p75_hold_s: float | None = None
    quick_flip_share: float | None = None  # closed positions held < 5 min
    median_entry_sol: float | None = None  # first buy per position
    median_buy_sol: float | None = None
    p90_buy_sol: float | None = None
    avg_buys_per_position: float | None = None
    top_token_profit_share: float | None = None  # largest winner / gross profit
    top3_profit_share: float | None = None
    positions_missing_basis: int = 0
    unattributed_proceeds_sol: float = 0.0
    recent_open_share: float | None = None  # of the last N positions, share still open
    top_winners: list[dict] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "WalletStats":
        known = {k: v for k, v in d.items() if k in cls.__dataclass_fields__}
        return cls(**known)


def _pct(xs: list[float], q: float) -> float | None:
    if not xs:
        return None
    xs = sorted(xs)
    k = (len(xs) - 1) * q
    lo, hi = math.floor(k), math.ceil(k)
    if lo == hi:
        return xs[lo]
    return xs[lo] + (xs[hi] - xs[lo]) * (k - lo)


def compute_wallet_stats(wallet: str, trades: Iterable[Trade], now: float | None = None) -> WalletStats:
    trades = sorted((t for t in trades if t.wallet == wallet), key=lambda t: t.ts)
    s = WalletStats(wallet=wallet)
    if not trades:
        return s

    ledger = Ledger()
    buy_sizes: list[float] = []
    first_buy_sol: dict[str, float] = {}
    for t in trades:
        ledger.apply(t)
        if t.side is Side.BUY:
            s.n_buys += 1
            buy_sizes.append(t.sol_amount)
            first_buy_sol.setdefault(t.mint, t.sol_amount)
        elif t.side is Side.SELL:
            s.n_sells += 1
        else:
            s.n_transfers += 1

    now = now if now is not None else trades[-1].ts
    s.n_trades = len(trades)
    s.first_ts, s.last_ts = trades[0].ts, trades[-1].ts
    s.active_days = max((s.last_ts - s.first_ts) / 86400.0, 1 / 24)
    s.trades_per_day = s.n_trades / s.active_days

    positions = list(ledger.for_wallet(wallet).values())
    s.n_tokens = len(positions)

    holds: list[float] = []
    entry_sizes: list[float] = []
    buys_per_pos: list[int] = []
    winners: list[tuple[str, float]] = []
    for p in positions:
        s.realized_pnl_sol += p.realized_pnl_sol
        s.unattributed_proceeds_sol += p.unattributed_proceeds_sol
        if p.missing_basis:
            s.positions_missing_basis += 1
        if p.n_buys:
            buys_per_pos.append(p.n_buys)
        if p.mint in first_buy_sol:
            entry_sizes.append(first_buy_sol[p.mint])
        if p.closed_ts is not None and not p.closed_by_transfer and p.n_buys > 0:
            s.closed_positions += 1
            if p.realized_pnl_sol > 0:
                s.winning_positions += 1
            elif p.realized_pnl_sol < 0:
                s.losing_positions += 1
            h = p.hold_seconds(now)
            if h is not None:
                holds.append(h)
        elif p.is_open:
            s.open_positions += 1
            s.open_cost_sol += p.cost_sol
        if p.realized_pnl_sol > 0:
            s.gross_profit_sol += p.realized_pnl_sol
            winners.append((p.mint, p.realized_pnl_sol))
        elif p.realized_pnl_sol < 0:
            s.gross_loss_sol += -p.realized_pnl_sol

    if s.closed_positions:
        s.win_rate = s.winning_positions / s.closed_positions
    if holds:
        s.median_hold_s = statistics.median(holds)
        s.p25_hold_s = _pct(holds, 0.25)
        s.p75_hold_s = _pct(holds, 0.75)
        s.quick_flip_share = sum(1 for h in holds if h < QUICK_FLIP_S) / len(holds)
    if entry_sizes:
        s.median_entry_sol = statistics.median(entry_sizes)
    if buy_sizes:
        s.median_buy_sol = statistics.median(buy_sizes)
        s.p90_buy_sol = _pct(buy_sizes, 0.9)
    if buys_per_pos:
        s.avg_buys_per_position = sum(buys_per_pos) / len(buys_per_pos)
    if s.gross_profit_sol > 0:
        winners.sort(key=lambda x: x[1], reverse=True)
        s.top_token_profit_share = winners[0][1] / s.gross_profit_sol
        s.top3_profit_share = sum(w for _, w in winners[:3]) / s.gross_profit_sol
        s.top_winners = [{"mint": m, "realized_pnl_sol": round(v, 4)} for m, v in winners[:5]]

    recent = sorted((p for p in positions if p.first_buy_ts is not None), key=lambda p: p.first_buy_ts)[-RECENT_WINDOW:]
    if recent:
        s.recent_open_share = sum(1 for p in recent if p.is_open) / len(recent)
    return s


@dataclass
class SelectionCriteria:
    min_closed_positions: int = 5
    min_realized_pnl_sol: float = 1.0
    min_win_rate: float = 0.3
    max_top_token_share: float = 0.85
    max_quick_flip_share: float = 0.9  # pure scalpers finish before you can follow


@dataclass
class Selection:
    wallet: str
    eligible: bool
    score: float
    reasons: list[str]


def select(stats: WalletStats, c: SelectionCriteria) -> Selection:
    """Explicit, inspectable roster criteria. "Best" means best in the
    observed candidate group, not a ranking of every wallet on Solana."""
    reasons: list[str] = []
    ok = True
    if stats.closed_positions < c.min_closed_positions:
        ok = False
        reasons.append(f"only {stats.closed_positions} closed positions (< {c.min_closed_positions}): "
                       f"{stats.n_buys} buys, {stats.n_sells} sells, {stats.n_transfers} transfers")
    if stats.realized_pnl_sol < c.min_realized_pnl_sol:
        ok = False
        reasons.append(f"realized PnL {stats.realized_pnl_sol:.2f} SOL (< {c.min_realized_pnl_sol})")
    if stats.win_rate is not None and stats.win_rate < c.min_win_rate:
        ok = False
        reasons.append(f"win rate {stats.win_rate:.0%} (< {c.min_win_rate:.0%})")
    if stats.top_token_profit_share is not None and stats.top_token_profit_share > c.max_top_token_share:
        ok = False
        reasons.append(f"{stats.top_token_profit_share:.0%} of profit from one token (> {c.max_top_token_share:.0%})")
    if stats.quick_flip_share is not None and stats.quick_flip_share > c.max_quick_flip_share:
        ok = False
        reasons.append(f"{stats.quick_flip_share:.0%} of positions closed within 5 min")
    if ok:
        reasons.append(
            f"{stats.realized_pnl_sol:.2f} SOL realized over {stats.closed_positions} closed positions"
            + (f", win rate {stats.win_rate:.0%}" if stats.win_rate is not None else "")
        )

    # Score: realized profit, discounted for concentration, rewarded for breadth.
    conc = stats.top_token_profit_share if stats.top_token_profit_share is not None else 1.0
    score = max(stats.realized_pnl_sol, 0.0) * (1.0 - 0.5 * conc) * math.log1p(stats.closed_positions)
    if stats.win_rate is not None:
        score *= 0.5 + stats.win_rate
    return Selection(wallet=stats.wallet, eligible=ok, score=round(score, 4), reasons=reasons)
