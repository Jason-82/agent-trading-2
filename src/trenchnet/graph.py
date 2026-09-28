"""Stage 2: the wallet-token graph.

Every observed purchase connects a wallet to a token, carrying time, direction
and size. Comparing those records reveals which wallets repeatedly *arrive
together*. Overlap is normalised by each wallet's total activity (two wallets
that buy everything will overlap often) and restricted to a time window
(buying the same token days apart is not arriving together).

These relationships describe repeated trading patterns. They do not establish
common ownership or private coordination.
"""

from __future__ import annotations

import statistics
from collections import defaultdict
from dataclasses import asdict, dataclass, field
from itertools import combinations
from typing import Callable

from .config import GraphConfig
from .models import Side, Trade


@dataclass
class Entry:
    wallet: str
    mint: str
    first_buy_ts: float
    buy_sol: float = 0.0
    n_buys: int = 0
    bought_tokens: float = 0.0
    sold_tokens: float = 0.0
    last_ts: float = 0.0


@dataclass
class PairStats:
    a: str
    b: str
    tokens_a: int
    tokens_b: int
    shared_tokens: int
    co_entries: int  # shared tokens entered within the co-entry window
    a_led: int
    b_led: int
    median_lag_s: float | None  # positive: b usually enters after a
    jaccard: float
    lift: float
    recurring: bool

    @property
    def usual_leader(self) -> str | None:
        if self.a_led > self.b_led:
            return self.a
        if self.b_led > self.a_led:
            return self.b
        return None


@dataclass
class Group:
    members: list[str]  # ordered by historical tendency to enter first
    lead_ratio: dict[str, float]
    shared_entries: int  # minimum pairwise co-entries inside the group
    order_agreement: float | None  # share of recurring pairs that entered in their usual order this time


@dataclass
class RelationshipSummary:
    mint: str
    participants: list[str]  # current tracked buyers, in entry order
    entry_ts: dict[str, float]
    groups: list[Group] = field(default_factory=list)
    independents: list[str] = field(default_factory=list)
    independent_max_jaccard: dict[str, float] = field(default_factory=dict)
    thin_history: list[str] = field(default_factory=list)
    pairs: list[PairStats] = field(default_factory=list)

    @property
    def cluster_count(self) -> int:
        """Distinct participant clusters: each recurring group counts once,
        each independent wallet counts once."""
        return len(self.groups) + len(self.independents)

    def to_dict(self) -> dict:
        d = asdict(self)
        d["cluster_count"] = self.cluster_count
        return d

    def lines(self, name: Callable[[str], str] = lambda w: w[:6]) -> list[str]:
        out: list[str] = []
        for g in self.groups:
            members = _join([name(w) for w in g.members])
            line = f"{members} have recurring shared entries ({g.shared_entries}+ earlier tokens together)"
            if len(g.members) >= 2 and g.lead_ratio.get(g.members[0], 0) > 0.6:
                line += f"; {name(g.members[0])} usually enters first"
            if g.order_agreement is not None:
                line += f"; this time {g.order_agreement:.0%} of their pairs entered in the usual order"
            out.append(line + ".")
        for w in self.independents:
            j = self.independent_max_jaccard.get(w, 0.0)
            if len(self.participants) > 1:
                out.append(f"{name(w)} rarely overlaps with the other participants in the observed period "
                           f"(max overlap {j:.0%}).")
        if self.groups and self.independents:
            out.append("The current token has attracted both a recurring group and outside participants.")
        elif len(self.groups) > 1:
            out.append("The current token has attracted more than one recurring group.")
        for w in self.thin_history:
            out.append(f"{name(w)} has little observed history, so its overlap figures carry less weight.")
        if not out and self.participants:
            out.append(f"{_join([name(w) for w in self.participants])} entered; no relationship history yet.")
        return out


def _join(xs: list[str]) -> str:
    if len(xs) <= 1:
        return "".join(xs)
    return ", ".join(xs[:-1]) + " and " + xs[-1]


class TradeGraph:
    def __init__(self, cfg: GraphConfig | None = None) -> None:
        self.cfg = cfg or GraphConfig()
        self.entries: dict[str, dict[str, Entry]] = defaultdict(dict)  # mint -> wallet -> entry
        self.wallet_mints: dict[str, set[str]] = defaultdict(set)

    def add_trade(self, t: Trade) -> None:
        if t.side is Side.BUY:
            e = self.entries[t.mint].get(t.wallet)
            if e is None:
                e = Entry(wallet=t.wallet, mint=t.mint, first_buy_ts=t.ts)
                self.entries[t.mint][t.wallet] = e
                self.wallet_mints[t.wallet].add(t.mint)
            e.first_buy_ts = min(e.first_buy_ts, t.ts)
            e.buy_sol += t.sol_amount
            e.bought_tokens += t.token_amount
            e.n_buys += 1
            e.last_ts = max(e.last_ts, t.ts)
        elif t.side is Side.SELL:
            e = self.entries.get(t.mint, {}).get(t.wallet)
            if e is not None:
                e.sold_tokens += t.token_amount
                e.last_ts = max(e.last_ts, t.ts)

    def add_trades(self, trades: list[Trade]) -> None:
        for t in sorted(trades, key=lambda t: t.ts):
            self.add_trade(t)

    def _mints(self, wallet: str, exclude: str | None) -> set[str]:
        ms = self.wallet_mints.get(wallet, set())
        return ms - {exclude} if exclude else set(ms)

    def universe(self, exclude: str | None = None) -> int:
        return len([m for m in self.entries if m != exclude])

    def pair_stats(self, a: str, b: str, exclude_mint: str | None = None) -> PairStats:
        ma, mb = self._mints(a, exclude_mint), self._mints(b, exclude_mint)
        shared = ma & mb
        w = self.cfg.co_entry_window_s
        a_led = b_led = 0
        lags = []
        co = 0
        for m in shared:
            ta, tb = self.entries[m][a].first_buy_ts, self.entries[m][b].first_buy_ts
            if abs(ta - tb) <= w:
                co += 1
                lags.append(tb - ta)
                if ta < tb:
                    a_led += 1
                elif tb < ta:
                    b_led += 1
        union = len(ma | mb)
        jaccard = co / union if union else 0.0
        u = self.universe(exclude_mint)
        lift = (co * u / (len(ma) * len(mb))) if ma and mb else 0.0
        recurring = co >= self.cfg.min_co_entries and jaccard >= self.cfg.min_jaccard and lift >= self.cfg.min_lift
        return PairStats(
            a=a, b=b, tokens_a=len(ma), tokens_b=len(mb), shared_tokens=len(shared), co_entries=co,
            a_led=a_led, b_led=b_led, median_lag_s=statistics.median(lags) if lags else None,
            jaccard=round(jaccard, 4), lift=round(lift, 3), recurring=recurring,
        )

    def relationship_summary(self, mint: str, tracked: set[str] | None = None) -> RelationshipSummary:
        """Relationships among the tracked wallets currently in `mint`, using
        their history on *other* tokens only."""
        es = [e for e in self.entries.get(mint, {}).values() if tracked is None or e.wallet in tracked]
        es.sort(key=lambda e: e.first_buy_ts)
        participants = [e.wallet for e in es]
        entry_ts = {e.wallet: e.first_buy_ts for e in es}
        summary = RelationshipSummary(mint=mint, participants=participants, entry_ts=entry_ts)

        pairs = {frozenset((a, b)): self.pair_stats(a, b, exclude_mint=mint) for a, b in combinations(participants, 2)}
        summary.pairs = sorted(pairs.values(), key=lambda p: p.co_entries, reverse=True)

        # Union-find over recurring edges.
        parent = {w: w for w in participants}

        def find(x: str) -> str:
            while parent[x] != x:
                parent[x] = parent[parent[x]]
                x = parent[x]
            return x

        for p in pairs.values():
            if p.recurring:
                parent[find(p.a)] = find(p.b)
        comps: dict[str, list[str]] = defaultdict(list)
        for w in participants:
            comps[find(w)].append(w)

        for members in comps.values():
            if len(members) == 1:
                w = members[0]
                summary.independents.append(w)
                others = [pairs[frozenset((w, o))].jaccard for o in participants if o != w]
                summary.independent_max_jaccard[w] = max(others) if others else 0.0
                continue
            led = defaultdict(int)
            total = defaultdict(int)
            agree = n_ord = 0
            min_shared = None
            for a, b in combinations(members, 2):
                p = pairs[frozenset((a, b))]
                led[p.a] += p.a_led
                led[p.b] += p.b_led
                total[p.a] += p.a_led + p.b_led
                total[p.b] += p.a_led + p.b_led
                if p.recurring:
                    min_shared = p.co_entries if min_shared is None else min(min_shared, p.co_entries)
                    leader = p.usual_leader
                    if leader is not None:
                        n_ord += 1
                        follower = p.b if leader == p.a else p.a
                        if entry_ts[leader] <= entry_ts[follower]:
                            agree += 1
            ratio = {w: (led[w] / total[w] if total[w] else 0.5) for w in members}
            ordered = sorted(members, key=lambda w: (-ratio[w], entry_ts[w]))
            summary.groups.append(Group(
                members=ordered, lead_ratio={w: round(r, 3) for w, r in ratio.items()},
                shared_entries=min_shared or 0, order_agreement=(agree / n_ord) if n_ord else None,
            ))

        summary.thin_history = [w for w in participants
                                if len(self._mints(w, mint)) < self.cfg.min_history_tokens]
        return summary
