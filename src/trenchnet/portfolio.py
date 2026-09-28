"""The bot's own holdings (as opposed to the tracked wallets' positions)."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone

from .brokers.base import Fill


def utc_day(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d")


@dataclass
class Holding:
    mint: str
    tokens: float
    original_tokens: float
    cost_sol: float
    entry_price: float
    entry_ts: float
    peak_price: float
    entry_reason: str
    tp_hit: set[int] = field(default_factory=set)
    realized_sol: float = 0.0


class Portfolio:
    def __init__(self, cash_sol: float) -> None:
        self.starting_cash = cash_sol
        self.cash_sol = cash_sol
        self.holdings: dict[str, Holding] = {}
        self.closed: list[dict] = []
        self.realized_pnl_sol = 0.0
        self.daily_realized: dict[str, float] = {}
        self.traded_mints: set[str] = set()
        self.fills: list[Fill] = []

    def apply(self, f: Fill, tp_index: int | None = None) -> float:
        """Apply a fill; returns realized PnL of this fill (0 for buys)."""
        self.fills.append(f)
        self.traded_mints.add(f.mint)
        if f.side == "buy":
            self.cash_sol -= f.sol_amount
            h = self.holdings.get(f.mint)
            if h is None:
                h = Holding(mint=f.mint, tokens=0.0, original_tokens=0.0, cost_sol=0.0, entry_price=f.price_sol,
                            entry_ts=f.ts, peak_price=f.price_sol, entry_reason=f.reason)
                self.holdings[f.mint] = h
            h.tokens += f.token_amount
            h.original_tokens += f.token_amount
            h.cost_sol += f.sol_amount
            h.entry_price = h.cost_sol / h.tokens if h.tokens else f.price_sol
            return 0.0

        h = self.holdings.get(f.mint)
        if h is None or h.tokens <= 0:
            return 0.0
        q = min(f.token_amount, h.tokens)
        basis = h.cost_sol * q / h.tokens
        pnl = f.sol_amount - basis
        self.cash_sol += f.sol_amount
        self.realized_pnl_sol += pnl
        day = utc_day(f.ts)
        self.daily_realized[day] = self.daily_realized.get(day, 0.0) + pnl
        h.realized_sol += pnl
        h.tokens -= q
        h.cost_sol -= basis
        if tp_index is not None:
            h.tp_hit.add(tp_index)
        if h.tokens <= h.original_tokens * 1e-6:
            self.closed.append({
                "mint": f.mint, "entry_ts": h.entry_ts, "exit_ts": f.ts, "realized_sol": h.realized_sol,
                "entry_reason": h.entry_reason, "exit_reason": f.reason,
            })
            del self.holdings[f.mint]
        return pnl

    def exposure_sol(self) -> float:
        return sum(h.cost_sol for h in self.holdings.values())

    def daily_pnl(self, ts: float) -> float:
        return self.daily_realized.get(utc_day(ts), 0.0)

    def equity(self, prices: dict[str, float]) -> float:
        marked = sum(h.tokens * prices.get(h.mint, h.entry_price) for h in self.holdings.values())
        return self.cash_sol + marked

    def summary(self, prices: dict[str, float]) -> dict:
        wins = sum(1 for c in self.closed if c["realized_sol"] > 0)
        return {
            "starting_cash_sol": round(self.starting_cash, 4),
            "cash_sol": round(self.cash_sol, 4),
            "equity_sol": round(self.equity(prices), 4),
            "realized_pnl_sol": round(self.realized_pnl_sol, 4),
            "open_positions": len(self.holdings),
            "closed_positions": len(self.closed),
            "win_rate": round(wins / len(self.closed), 3) if self.closed else None,
            "fills": len(self.fills),
        }
