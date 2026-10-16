"""The event loop that runs the pipeline.

    trade from a followed wallet
      -> ledger + graph update                         (code)
      -> snapshot: positions, profiles, relationships   (code)
      -> narrow judgments                               (judge)
      -> route: observe / analyze / insufficient        (explicit rules)
      -> situation report when routed to analysis       (reporter)
      -> entry / exit decisions                         (strategy, deterministic)
      -> paper or live fill                             (broker)
"""

from __future__ import annotations

import asyncio
import logging
import re
import sqlite3
from dataclasses import fields
from typing import Awaitable, Callable, Iterable

from .brokers.base import Broker, Fill
from .cases import Case, TrackedWallet, build_snapshot, iso
from .config import Config
from .graph import TradeGraph
from .judge import Judge, route
from .ledger import Ledger
from .models import PriceTick, Trade, short
from .portfolio import Portfolio
from .reporter import Reporter, format_report
from .store import Store
from .strategy import Order, Strategy

log = logging.getLogger(__name__)

_TP_REASON = re.compile(r"^take-profit (\d+):")


def tracked_from_roster(roster: list[dict]) -> dict[str, TrackedWallet]:
    return {r["wallet"]: TrackedWallet(r["wallet"], r["alias"] or short(r["wallet"]), r["profile"], r["stats"])
            for r in roster}


class Engine:
    def __init__(self, cfg: Config, store: Store, tracked: dict[str, TrackedWallet], judge: Judge,
                 reporter: Reporter, broker: Broker, *, history: Iterable[Trade] = (),
                 out: Callable[[str], None] = print, background_reports: bool = False,
                 on_hold: Callable[[str], Awaitable[None]] | None = None,
                 on_release: Callable[[str], Awaitable[None]] | None = None) -> None:
        self.cfg = cfg
        self.store = store
        self.tracked = tracked
        self.tracked_set = set(tracked)
        self.judge = judge
        self.reporter = reporter
        self.broker = broker
        self.out = out
        self.background_reports = background_reports
        self.on_hold = on_hold  # e.g. subscribe to price updates for a held token
        self.on_release = on_release
        self.ledger = Ledger()
        self.graph = TradeGraph(cfg.graph)
        self.cases: dict[str, Case] = {}
        self.portfolio = Portfolio(cfg.paper.starting_balance_sol)
        self.strategy = Strategy(cfg.strategy, self.portfolio)
        self.prices: dict[str, float] = {}
        self.seen: set[tuple] = set()
        self.now = 0.0
        self._tasks: set[asyncio.Task] = set()
        # One order at a time per token: the ticker and the event loop both run
        # exits, and live orders await confirmation.
        self._locks: dict[str, asyncio.Lock] = {}
        self.skips: dict[str, str] = {}  # mint -> last entry-blocking reason
        for t in sorted(history, key=lambda t: t.ts):
            if t.wallet in self.tracked_set:
                self.seen.add(t.key)
                self.ledger.apply(t)
                self.graph.add_trade(t)

    def name(self, wallet: str) -> str:
        return self.tracked[wallet].alias if wallet in self.tracked else short(wallet)

    def _lock(self, mint: str) -> asyncio.Lock:
        lock = self._locks.get(mint)
        if lock is None:
            lock = self._locks[mint] = asyncio.Lock()
        return lock

    def restore(self, fills: list[dict]) -> None:
        """Rebuild the bot's holdings from stored fills after a restart, and
        reopen a case for each token still held so exits keep running."""
        names = {f.name for f in fields(Fill)}
        for row in fills:
            fill = Fill(**{k: row[k] for k in names})
            m = _TP_REASON.match(fill.reason) if fill.side == "sell" else None
            self.portfolio.apply(fill, tp_index=int(m.group(1)) - 1 if m else None)
        for mint, h in self.portfolio.holdings.items():
            case = self.cases[mint] = Case(mint=mint, opened_ts=h.entry_ts)
            for t in self.store.trades(wallets=list(self.tracked_set), mint=mint):
                case.add_event(t)
            if mint not in self.prices and case.last_price is not None:
                self.prices[mint] = case.last_price

    # -- events -----------------------------------------------------------

    async def handle(self, ev: Trade | PriceTick) -> None:
        self.now = max(self.now, ev.ts)
        if isinstance(ev, PriceTick):
            await self.on_price(ev)
        else:
            await self.on_trade(ev)

    async def on_price(self, tick: PriceTick) -> None:
        self.prices[tick.mint] = tick.price_sol
        case = self.cases.get(tick.mint)
        if case is not None:
            case.update_price(tick.price_sol, tick.ts, tick.market_cap_sol)
            await self._exits(case)

    def _rebuild(self, t: Trade) -> None:
        self.ledger.rebuild(t.wallet, t.mint, self.store.trades(wallets=[t.wallet], mint=t.mint, until=self.now))

    async def _confirm(self, t: Trade) -> None:
        """The on-chain copy (from the RPC watcher) of a trade first reported
        live by PumpPortal, which stamps trades with this PC's clock: store it
        with its chain time and replay the position in chain order."""
        stored = self.store.trades(wallets=[t.wallet], mint=t.mint)
        old = next((s for s in stored if s.key == t.key), None)
        if old is None or old.source == t.source or old.ts == t.ts:
            return
        self.store.replace_trades(t.wallet, [t], {t.signature}, source=t.source)
        self._rebuild(t)
        case = self.cases.get(t.mint)
        if case is not None:
            await self._exits(case)

    async def on_trade(self, t: Trade) -> None:
        if t.key in self.seen:
            if t.source == "rpc" and t.wallet in self.tracked_set:
                await self._confirm(t)
            return
        self.seen.add(t.key)
        self.store.add_trade(t)
        if t.price is not None:
            self.prices[t.mint] = t.price
        if t.wallet not in self.tracked_set:
            case = self.cases.get(t.mint)
            if case is not None:
                case.update_price(t.price, t.ts, t.market_cap_sol)
                await self._exits(case)
            return

        pos = self.ledger.get(t.wallet, t.mint)
        if pos is not None and pos.last_ts is not None and t.ts < pos.last_ts:
            # Arrived after a later trade of the same wallet and token (the two live sources
            # don't wait for each other): replay the position in time order instead.
            # Two trades in the same second from different sources can't be ordered this way.
            self._rebuild(t)
        else:
            self.ledger.apply(t)
        self.graph.add_trade(t)
        case = self.cases.get(t.mint)
        if case is None:
            case = self.cases[t.mint] = Case(mint=t.mint, opened_ts=t.ts)
        case.add_event(t)

        rel = self.graph.relationship_summary(t.mint, self.tracked_set)
        snap = build_snapshot(case, t, self.ledger, self.tracked, rel, self.now)
        assessment, source = await self.judge.assess(snap)
        decision = route(snap, assessment, self.cfg.router)
        a = assessment.model_dump()
        case.assessments.append({
            "ref": snap["event"]["ref"], "alias": self.name(t.wallet), "wallet": t.wallet,
            "signature": t.signature, "side": t.side.value, "assessment": a, "route": decision.route,
        })
        self.store.add_assessment(mint=t.mint, signature=t.signature, wallet=t.wallet, ts=t.ts,
                                  route=decision.route, reasons=decision.reasons, assessment=a, source=source)
        self.out(
            f"[{iso(t.ts)}] {short(t.mint)} {snap['event']['ref']} {self.name(t.wallet)} {t.side.value} "
            f"{t.sol_amount:.3f} SOL"
            + (f" (${t.quote_amount:,.2f} in stablecoin)" if t.quote_mint and t.quote_amount is not None else "")
            + f" | fit={a['behavior_fit']} intent={a['position_intent']} "
            f"history={a['history']} -> {decision.route}"
            + (" (uncertain)" if decision.uncertain else "")
        )

        if decision.route == "analyze":
            await self._report(case, snap, t)

        lock = self._lock(case.mint)
        if lock.locked():
            return  # an order for this token is in flight; the next event re-evaluates
        async with lock:
            order, reasons = self.strategy.entry(case, self.ledger, self.tracked_set, rel, self.now)
            if order is not None:
                await self._execute(order)
        if (order is None and reasons and reasons[0] not in ("already holding", "already traded this token")
                and self.skips.get(case.mint) != reasons[0]):
            self.skips[case.mint] = reasons[0]
            if len(case.buyers()) >= 2:
                self.out(f"    no entry: {reasons[0]}")
        await self._exits(case)

    async def tick(self, now: float) -> None:
        """Time-based checks (max hold) when no events arrive."""
        self.now = max(self.now, now)
        for mint in list(self.portfolio.holdings):
            if mint in self.cases:
                await self._exits(self.cases[mint])
        self.save_marks()

    def save_marks(self) -> None:
        """Persist the latest price of each held token so `trenchnet pnl` can
        value open positions while the bot is stopped."""
        try:
            for mint in self.portfolio.holdings:
                case = self.cases.get(mint)
                if case is not None and case.last_price is not None:
                    self.store.save_price(mint, case.last_price, case.last_price_ts or self.now)
        except sqlite3.Error as e:  # e.g. the database is locked by another program; try again next tick
            self.store.db.rollback()
            log.warning("could not save last prices (%s)", e)

    # -- reports ----------------------------------------------------------

    async def _report(self, case: Case, snap: dict, trigger: Trade) -> None:
        if case.report_pending:
            return
        case.report_pending = True
        if self.background_reports:
            task = asyncio.create_task(self._write_report(case, snap, trigger))
            self._tasks.add(task)
            task.add_done_callback(self._tasks.discard)
        else:
            await self._write_report(case, snap, trigger)

    async def _write_report(self, case: Case, snap: dict, trigger: Trade) -> None:
        try:
            prev = self.store.reports(case.mint, limit=1)
            previous = prev[0]["report"] | {"time": iso(prev[0]["ts"])} if prev else None
            report, source, unsupported = await self.reporter.write(case, snap, self.tracked, previous)
            rid = self.store.add_report(
                mint=case.mint, ts=trigger.ts, trigger_signature=trigger.signature,
                previous_id=prev[0]["id"] if prev else None, source=source, report=report,
                evidence_signatures=[t.signature for t in case.events], unsupported_citations=unsupported,
            )
            case.last_report = {"id": rid, "ts": trigger.ts, "headline": report["headline"],
                                "what_changed": report["what_changed"]}
            self.out(format_report(report, trigger.ts, source))
            if unsupported:
                self.out(f"  ! {len(unsupported)} statement(s) cited no supplied event: {unsupported}")
        except Exception:  # noqa: BLE001 - a failed report must not stop the loop
            log.exception("report for %s failed", case.mint)
        finally:
            case.report_pending = False

    async def drain(self) -> None:
        if self._tasks:
            await asyncio.gather(*list(self._tasks), return_exceptions=True)

    # -- trading ----------------------------------------------------------

    async def _exits(self, case: Case) -> None:
        lock = self._lock(case.mint)
        if lock.locked():
            return  # an order for this token is in flight; the next event or tick re-evaluates
        async with lock:
            for order in self.strategy.exits(case, self.ledger, self.tracked_set, self.now):
                await self._execute(order)

    async def _execute(self, order: Order) -> None:
        """Callers hold the token's lock."""
        if order.side == "buy":
            # Mark before submitting: an unclear outcome must never lead to a second buy.
            self.portfolio.traded_mints.add(order.mint)
            fill = await self.broker.buy(order.mint, order.sol_amount, order.ref_price, self.now, order.reason)
        else:
            fill = await self.broker.sell(order.mint, order.token_amount, order.ref_price, self.now, order.reason)
        if fill is None:
            self.out(f"    ✗ {order.side} {short(order.mint)} not filled ({order.reason})")
            return
        pnl = self.portfolio.apply(fill, tp_index=order.tp_index)
        self.store.add_fill(mint=fill.mint, side=fill.side, sol_amount=fill.sol_amount,
                            token_amount=fill.token_amount, price_sol=fill.price_sol, fee_sol=fill.fee_sol,
                            ts=fill.ts, mode=fill.mode, reason=fill.reason, tx_signature=fill.tx_signature)
        tag = "BUY " if fill.side == "buy" else "SELL"
        extra = "" if fill.side == "buy" else f" pnl={pnl:+.4f} SOL"
        self.out(f"    ▶ {fill.mode.upper()} {tag} {short(fill.mint)} {fill.sol_amount:.4f} SOL @ "
                 f"{fill.price_sol:.3e}{extra} — {fill.reason}")
        try:
            if fill.side == "buy" and self.on_hold:
                await self.on_hold(fill.mint)
            if fill.side == "sell" and fill.mint not in self.portfolio.holdings and self.on_release:
                await self.on_release(fill.mint)
        except Exception:  # noqa: BLE001 - a subscription hiccup must not stop the loop
            log.exception("price subscription update for %s failed", fill.mint)

    def summary(self) -> dict:
        return self.portfolio.summary(self.prices)
