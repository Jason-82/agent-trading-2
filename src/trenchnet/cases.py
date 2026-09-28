"""A case is one token that tracked wallets have touched. It holds the
ordered event sequence and builds the compact snapshots handed to the judge
and the reporter. Straightforward calculations happen here, in code."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from .graph import RelationshipSummary
from .ledger import Ledger
from .models import Side, Trade, short

RECENT_EVENTS = 12


@dataclass
class TrackedWallet:
    wallet: str
    alias: str
    profile: dict | None
    stats: dict

    @property
    def typical_entry_sol(self) -> float | None:
        return self.stats.get("median_entry_sol")

    def profile_brief(self) -> dict[str, Any] | None:
        if not self.profile:
            return None
        s = self.stats
        return {
            "style": self.profile.get("style"),
            "summary": self.profile.get("summary"),
            "typical_entry": self.profile.get("typical_entry"),
            "holding_behavior": self.profile.get("holding_behavior"),
            "attention_triggers": self.profile.get("attention_triggers", []),
            "history": {
                "closed_positions": s.get("closed_positions", 0),
                "n_tokens": s.get("n_tokens", 0),
                "median_entry_sol": s.get("median_entry_sol"),
                "median_hold_s": s.get("median_hold_s"),
                "p25_hold_s": s.get("p25_hold_s"),
                "p75_hold_s": s.get("p75_hold_s"),
            },
        }


@dataclass
class Case:
    mint: str
    opened_ts: float
    events: list[Trade] = field(default_factory=list)
    first_entry_ts: float | None = None
    first_entry_price: float | None = None
    last_price: float | None = None
    last_price_ts: float | None = None
    market_cap_sol: float | None = None
    assessments: list[dict] = field(default_factory=list)  # per event: signature, wallet, side, assessment, route
    last_report: dict | None = None  # {id, ts, headline, what_changed}
    report_pending: bool = False
    _index: dict[tuple, int] = field(default_factory=dict, repr=False)

    def add_event(self, t: Trade) -> None:
        self._index[t.key] = len(self.events)
        self.events.append(t)
        if t.side is Side.BUY and self.first_entry_ts is None:
            self.first_entry_ts = t.ts
            self.first_entry_price = t.price
        self.update_price(t.price, t.ts, t.market_cap_sol)

    def update_price(self, price: float | None, ts: float, market_cap_sol: float | None = None) -> None:
        if price is None or price <= 0:
            return
        if self.last_price_ts is None or ts >= self.last_price_ts:
            self.last_price, self.last_price_ts = price, ts
            if market_cap_sol is not None:
                self.market_cap_sol = market_cap_sol

    def ref(self, t: Trade) -> str:
        return f"e{self._index[t.key] + 1}"

    def refs(self) -> dict[str, str]:
        return {f"e{i + 1}": t.signature for i, t in enumerate(self.events)}

    def buyers(self) -> list[str]:
        seen: dict[str, None] = {}
        for t in self.events:
            if t.side is Side.BUY:
                seen.setdefault(t.wallet, None)
        return list(seen)


def iso(ts: float | None) -> str | None:
    if ts is None:
        return None
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def position_rows(case: Case, ledger: Ledger, tracked: dict[str, TrackedWallet], now: float) -> list[dict]:
    rows = []
    for w, p in ledger.for_mint(case.mint).items():
        if w not in tracked:
            continue
        rows.append({
            "wallet": tracked[w].alias,
            "status": p.status,
            "first_buy": iso(p.first_buy_ts),
            "bought_sol": round(p.bought_sol, 4),
            "buys": p.n_buys,
            "sells": p.n_sells,
            "sold_fraction": round(p.sold_fraction, 3),
            "held_fraction_of_peak": round(p.tokens / p.peak_tokens, 3) if p.peak_tokens else 0.0,
            "transferred_out_tokens": round(p.transferred_out, 2),
            "minutes_held": round((p.hold_seconds(now) or 0) / 60, 1),
        })
    rows.sort(key=lambda r: r["first_buy"] or "")
    return rows


def build_snapshot(case: Case, event: Trade, ledger: Ledger, tracked: dict[str, TrackedWallet],
                   rel: RelationshipSummary, now: float) -> dict[str, Any]:
    tw = tracked[event.wallet]
    pos = ledger.get(event.wallet, case.mint)
    buys_in_token = sum(1 for t in case.events if t.wallet == event.wallet and t.side is Side.BUY)
    size_ratio = None
    if event.side is Side.BUY and tw.typical_entry_sol:
        size_ratio = round(event.sol_amount / tw.typical_entry_sol, 3)

    name = lambda w: tracked[w].alias if w in tracked else short(w)  # noqa: E731
    holders = [w for w, p in ledger.for_mint(case.mint).items() if w in tracked and p.is_open]
    price_vs_first = None
    if case.last_price and case.first_entry_price:
        price_vs_first = round(case.last_price / case.first_entry_price, 3)

    recent = case.events[-RECENT_EVENTS:]
    return {
        "now": iso(now),
        "token": {
            "mint": case.mint,
            "first_tracked_entry": iso(case.first_entry_ts),
            "minutes_since_first_entry": round((now - case.first_entry_ts) / 60, 1) if case.first_entry_ts else None,
            "price_sol": case.last_price,
            "price_vs_first_tracked_entry": price_vs_first,
            "market_cap_sol": case.market_cap_sol,
            "tracked_buyers": len(case.buyers()),
            "tracked_holders": len(holders),
        },
        "event": {
            "ref": case.ref(event),
            "wallet": tw.alias,
            "side": event.side.value,
            "sol": round(event.sol_amount, 4),
            "tokens": round(event.token_amount, 2),
            "time": iso(event.ts),
            "is_first_buy_in_token": event.side is Side.BUY and buys_in_token == 1,
            "buy_number_in_token": buys_in_token if event.side is Side.BUY else None,
            "size_vs_typical_entry": size_ratio,
            "others_reduced_or_exited": [
                tracked[w].alias for w, p in ledger.for_mint(case.mint).items()
                if w != event.wallet and w in tracked and p.status in ("reduced", "exited")
            ],
            "position_status_after": pos.status if pos else None,
            "position_sold_fraction_after": round(pos.sold_fraction, 3) if pos else None,
            "minutes_since_wallet_first_buy": round((event.ts - pos.first_buy_ts) / 60, 1)
            if pos and pos.first_buy_ts else None,
        },
        "wallet_profile": tw.profile_brief(),
        "positions": position_rows(case, ledger, tracked, now),
        "relationships": {
            "summary": rel.lines(name),
            "cluster_count": rel.cluster_count,
            "groups": [[name(w) for w in g.members] for g in rel.groups],
            "independents": [name(w) for w in rel.independents],
            "thin_history": [name(w) for w in rel.thin_history],
            "thin_share": round(len(rel.thin_history) / len(rel.participants), 3) if rel.participants else 0.0,
        },
        "recent_events": [
            {"ref": case.ref(t), "wallet": name(t.wallet), "side": t.side.value,
             "sol": round(t.sol_amount, 4), "tokens": round(t.token_amount, 2), "time": iso(t.ts)}
            for t in recent
        ],
        "last_report": (
            {"time": iso(case.last_report["ts"]), "headline": case.last_report["headline"],
             "what_changed": case.last_report["what_changed"]}
            if case.last_report else None
        ),
    }
