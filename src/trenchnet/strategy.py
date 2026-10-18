"""Trading rules layered on top of the TRENCHNET analysis.

The article's pipeline stops at analysis: its routes are explicitly *not*
trading orders. This module is the extra step that turns a case into orders,
and it is deliberately deterministic. Entries require confluence (several
tracked buyers, more than one independent cluster, buys that fit the buyers'
own patterns) or, when enabled, a conviction buy (one wallet with a strong
record buying far more than it usually does); exits follow the tracked
wallets out and enforce hard risk limits. The language model's reports
explain; they never place trades.
"""

from __future__ import annotations

from dataclasses import dataclass

from .cases import Case, TrackedWallet
from .config import StrategyConfig
from .graph import RelationshipSummary
from .ledger import Ledger
from .models import Side, short
from .portfolio import Portfolio


@dataclass
class Order:
    mint: str
    side: str  # "buy" | "sell"
    ref_price: float
    reason: str
    sol_amount: float = 0.0  # for buys
    token_amount: float = 0.0  # for sells
    tp_index: int | None = None
    leader: str | None = None  # conviction entries: the wallet the position follows out


class Strategy:
    def __init__(self, cfg: StrategyConfig, portfolio: Portfolio) -> None:
        self.cfg = cfg
        self.pf = portfolio

    # -- cohort -----------------------------------------------------------

    def cohort(self, case: Case, ledger: Ledger, tracked: set[str]) -> list[str]:
        """Tracked wallets whose first buy landed inside the entry window."""
        if case.first_entry_ts is None:
            return []
        end = case.first_entry_ts + self.cfg.entry_window_s
        out = []
        for w, p in ledger.for_mint(case.mint).items():
            if w in tracked and p.first_buy_ts is not None and case.first_entry_ts <= p.first_buy_ts <= end:
                out.append(w)
        return sorted(out, key=lambda w: ledger.for_mint(case.mint)[w].first_buy_ts)

    def cohort_sold_fraction(self, case: Case, ledger: Ledger, cohort: list[str]) -> float:
        acquired = sold = 0.0
        for w in cohort:
            p = ledger.get(w, case.mint)
            if p is None:
                continue
            acquired += p.acquired_tokens
            sold += p.sold_tokens  # transfers are not sales
        return min(1.0, sold / acquired) if acquired > 0 else 0.0

    # -- entry ------------------------------------------------------------

    def entry(self, case: Case, ledger: Ledger, tracked: set[str], rel: RelationshipSummary,
              now: float, wallets: dict[str, TrackedWallet] | None = None) -> tuple[Order | None, list[str]]:
        """Returns (order or None, reasons). Reasons explain either the entry
        or the first rule that blocked it. `wallets` (the followed wallets'
        records) is needed for conviction entries."""
        c = self.cfg
        if not c.enabled:
            return None, ["strategy disabled"]
        if case.mint in self.pf.holdings:
            return None, ["already holding"]
        if case.mint in self.pf.traded_mints and not c.reenter_same_mint:
            return None, ["already traded this token"]
        price = case.last_price
        if price is None or case.first_entry_ts is None:
            return None, ["no price yet"]

        ok, reasons = self._confluence(case, ledger, tracked, rel, now, price)
        leader = None
        if not ok and c.conviction_entry:
            leader, why = self._conviction(case, ledger, tracked, wallets or {}, now, price)
            if leader is not None:
                ok, reasons = True, why
            elif why:  # a big buy was seen: say why it isn't followed
                reasons = why
        if not ok:
            return None, reasons
        size, blocked = self._size(case, now)
        if blocked:
            return None, [f"{reasons[0]}; not followed: {blocked}" if leader else blocked]
        return Order(case.mint, "buy", price, "; ".join(reasons), sol_amount=size, leader=leader), reasons

    def _size(self, case: Case, now: float) -> tuple[float, str | None]:
        """The position size, or what blocks any entry: market cap and risk limits."""
        c = self.cfg
        mc = case.market_cap_sol
        if mc is not None and c.min_market_cap_sol is not None and mc < c.min_market_cap_sol:
            return 0.0, f"market cap {mc:.0f} SOL below minimum"
        if mc is not None and c.max_market_cap_sol is not None and mc > c.max_market_cap_sol:
            return 0.0, f"market cap {mc:.0f} SOL above maximum"
        if self.pf.daily_pnl(now) <= -c.daily_loss_limit_sol:
            return 0.0, "daily loss limit reached; no new entries today"
        if len(self.pf.holdings) >= c.max_open_positions:
            return 0.0, "max open positions"
        room = c.max_total_exposure_sol - self.pf.exposure_sol()
        size = min(c.position_size_sol, room, self.pf.cash_sol)
        if size < 0.25 * c.position_size_sol:
            return 0.0, "exposure or cash limit"
        return size, None

    def _confluence(self, case: Case, ledger: Ledger, tracked: set[str], rel: RelationshipSummary,
                    now: float, price: float) -> tuple[bool, list[str]]:
        c = self.cfg
        cohort = self.cohort(case, ledger, tracked)
        active = [w for w in cohort
                  if (p := ledger.get(w, case.mint)) is not None and p.is_open
                  and p.sold_fraction <= 1 - c.min_active_holder_fraction]
        if len(active) < c.min_tracked_buyers:
            return False, [f"{len(active)} active tracked buyers in window (< {c.min_tracked_buyers})"]
        if rel.cluster_count < c.min_independent_clusters:
            return False, [f"{rel.cluster_count} independent cluster(s) (< {c.min_independent_clusters}); "
                           "buyers are one recurring group"]
        supportive = [a for a in case.assessments
                      if a["side"] == Side.BUY.value and a["assessment"]["behavior_fit"] == "fits"
                      and a["assessment"]["position_intent"] in ("standard", "conviction")]
        if len(supportive) < c.min_supportive_buys:
            return False, [f"{len(supportive)} buys fit their wallet's pattern (< {c.min_supportive_buys})"]
        thin = len([w for w in cohort if w in rel.thin_history]) / len(cohort) if cohort else 0.0
        if thin > c.max_insufficient_history_share:
            return False, [f"{thin:.0%} of cohort has thin history"]
        if now - case.first_entry_ts > c.max_entry_delay_s:
            return False, [f"{(now - case.first_entry_ts) / 60:.0f} min since first tracked entry (stale)"]
        if case.first_entry_price and price / case.first_entry_price > c.max_run_up:
            return False, [f"price already {price / case.first_entry_price:.1f}x the first tracked entry"]
        reasons = [
            f"{len(active)} active tracked buyers within {c.entry_window_s / 60:.0f} min",
            f"{rel.cluster_count} independent clusters",
            f"{len(supportive)} buys fit their wallet's pattern",
        ]
        if case.first_entry_price:
            reasons.append(f"price {price / case.first_entry_price:.2f}x first tracked entry")
        return True, reasons

    def strong_record(self, tw: TrackedWallet) -> bool:
        """Good enough a record to follow on its own."""
        s = tw.stats
        win_rate = s.get("win_rate")
        return ((s.get("realized_pnl_sol") or 0.0) >= self.cfg.conviction_min_realized_sol
                and win_rate is not None and win_rate >= self.cfg.conviction_min_win_rate)

    def _conviction(self, case: Case, ledger: Ledger, tracked: set[str], wallets: dict[str, TrackedWallet],
                    now: float, price: float) -> tuple[str | None, list[str]]:
        """A followed wallet with a strong record that just bought far more than
        its usual entry, and still holds. Returns (that wallet, reasons), or
        (None, why the biggest such buy isn't followed; empty if there was none)."""
        c = self.cfg
        big = []
        for t in case.events:
            tw = wallets.get(t.wallet)
            if (t.side is not Side.BUY or t.wallet not in tracked or tw is None or now - t.ts > c.conviction_max_delay_s
                    or not tw.typical_entry_sol or not self.strong_record(tw)):
                continue
            ratio = t.sol_amount / tw.typical_entry_sol
            if ratio >= c.conviction_size_multiple:
                big.append((ratio, t, tw))
        blocked = []
        for ratio, t, tw in sorted(big, key=lambda b: b[0], reverse=True):
            head = (f"conviction: {tw.alias} bought {t.sol_amount:.2f} SOL, "
                    f"{ratio:.1f}x its usual {tw.typical_entry_sol:.2f} SOL entry")
            p = ledger.get(t.wallet, case.mint)
            if p is None or not p.is_open or p.sold_fraction > 1 - c.min_active_holder_fraction:
                blocked.append(f"{head}; not followed: it has sold since")
                continue
            run_up = price / t.price if t.price else None
            if run_up is not None and run_up > c.max_run_up:
                blocked.append(f"{head}; not followed: the price is already {run_up:.1f}x its buy")
                continue
            s = tw.stats
            reasons = [head, f"its record: {s['realized_pnl_sol']:.1f} SOL realized, win rate {s['win_rate']:.0%}"]
            if run_up is not None:
                reasons.append(f"price {run_up:.2f}x its buy")
            return t.wallet, reasons
        return None, blocked[:1]

    # -- exits ------------------------------------------------------------

    def exits(self, case: Case, ledger: Ledger, tracked: set[str], now: float,
              wallets: dict[str, TrackedWallet] | None = None) -> list[Order]:
        h = self.pf.holdings.get(case.mint)
        if h is None:
            return []
        c = self.cfg
        price = case.last_price
        full = lambda why: [Order(case.mint, "sell", price or h.entry_price, why, token_amount=h.tokens)]  # noqa: E731

        if price is not None:
            h.peak_price = max(h.peak_price, price)
            if price <= h.entry_price * (1 - c.stop_loss_pct):
                return full(f"stop-loss: price {price / h.entry_price:.2f}x entry")
            if h.tp_hit and price <= h.peak_price * (1 - c.trailing_stop_pct):
                return full(f"trailing stop: {1 - price / h.peak_price:.0%} below peak")

        if h.leader is not None:
            # A conviction entry follows the one wallet it copied, not the token's other buyers.
            lead = ledger.get(h.leader, case.mint)
            who = wallets[h.leader].alias if wallets and h.leader in wallets else short(h.leader)
            if lead is not None and lead.sold_fraction >= c.exit_on_cohort_sold_fraction:
                return full(f"{who} has sold {lead.sold_fraction:.0%} of its tokens")
            if c.exit_on_leader_exit and lead is not None and not lead.is_open:
                return full(f"{who} no longer holds the token")
        else:
            cohort = self.cohort(case, ledger, tracked)
            sold = self.cohort_sold_fraction(case, ledger, cohort)
            if cohort and sold >= c.exit_on_cohort_sold_fraction:
                return full(f"tracked cohort has sold {sold:.0%} of its tokens")
            if c.exit_on_leader_exit and cohort:
                leader = ledger.get(cohort[0], case.mint)
                if leader is not None and leader.status == "exited":
                    return full("first tracked entrant exited")
        if now - h.entry_ts >= c.max_hold_s:
            return full(f"max hold {c.max_hold_s / 3600:.1f}h reached")

        if price is not None:
            for i, (mult, frac) in enumerate(c.take_profit):
                if i not in h.tp_hit and price >= h.entry_price * mult:
                    qty = min(h.tokens, h.original_tokens * frac)
                    if qty > 0:
                        return [Order(case.mint, "sell", price, f"take-profit {i + 1}: {mult:.1f}x entry",
                                      token_amount=qty, tp_index=i)]
        return []
