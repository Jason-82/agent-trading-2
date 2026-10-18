"""Conviction buys: one followed wallet with a strong record buying far more
than it usually does, copied on its own and followed out by that wallet."""

import pytest

from trenchnet.brokers.base import Fill
from trenchnet.brokers.paper import PaperBroker
from trenchnet.cases import Case, TrackedWallet
from trenchnet.config import Config, StrategyConfig
from trenchnet.engine import Engine, tracked_from_roster
from trenchnet.graph import RelationshipSummary
from trenchnet.judge import HeuristicJudge
from trenchnet.ledger import Ledger
from trenchnet.models import Side, Trade
from trenchnet.portfolio import Portfolio
from trenchnet.reporter import Reporter
from trenchnet.store import Store
from trenchnet.strategy import Strategy

T0 = 1_760_000_000.0
MITCH, FROG, WEAK = "MitchWallet", "FrogWallet", "WeakWallet"
STRONG = {"realized_pnl_sol": 222.8, "win_rate": 0.65, "median_entry_sol": 5.0, "closed_positions": 37}
RECORDS = {MITCH: ("mitch", STRONG),
           FROG: ("mrfrog", {"realized_pnl_sol": 25.2, "win_rate": 0.85, "median_entry_sol": 3.0}),
           WEAK: ("yami", {"realized_pnl_sol": 9.7, "win_rate": 0.56, "median_entry_sol": 0.8})}
WALLETS = {w: TrackedWallet(w, alias, None, stats) for w, (alias, stats) in RECORDS.items()}
NO_GROUP = RelationshipSummary(mint="Coin", participants=[], entry_ts={})

_n = iter(range(10**6))


def trade(wallet, side, sol, at, tokens=1e6):
    return Trade(f"t{next(_n)}", wallet, "Coin", Side(side), sol, tokens, T0 + at)


def case_with(*trades):
    led, case = Ledger(), Case(mint="Coin", opened_ts=T0)
    for t in trades:
        led.apply(t)
        case.add_event(t)
    return led, case


def strategy(**kw):
    return Strategy(StrategyConfig(**{"conviction_entry": True, **kw}), Portfolio(5.0))


def test_a_strong_wallet_buying_far_more_than_usual_is_followed():
    led, case = case_with(trade(MITCH, "buy", 19.6, 0))
    order, why = strategy().entry(case, led, set(WALLETS), NO_GROUP, T0 + 5, WALLETS)
    assert order is not None and order.leader == MITCH and order.sol_amount == pytest.approx(0.1)
    assert why[0] == "conviction: mitch bought 19.60 SOL, 3.9x its usual 5.00 SOL entry"
    assert "222.8 SOL realized, win rate 65%" in order.reason


def test_off_unless_enabled():
    led, case = case_with(trade(MITCH, "buy", 19.6, 0))
    s = Strategy(StrategyConfig(), Portfolio(5.0))
    order, why = s.entry(case, led, set(WALLETS), NO_GROUP, T0 + 5, WALLETS)
    assert order is None and "active tracked buyers" in why[0]


@pytest.mark.parametrize("wallet, sol, now, followed", [
    (MITCH, 12.0, 5, set(WALLETS)),  # 2.4x its usual: an ordinary big buy
    (WEAK, 4.0, 5, set(WALLETS)),  # 5x its usual, but a 9.7 SOL record
    (MITCH, 19.6, 400, set(WALLETS)),  # seen too late (over 5 minutes ago)
    (MITCH, 19.6, 5, {FROG, WEAK}),  # not a followed wallet
])
def test_not_a_conviction_buy(wallet, sol, now, followed):
    led, case = case_with(trade(wallet, "buy", sol, 0))
    order, why = strategy().entry(case, led, followed, NO_GROUP, T0 + now, WALLETS)
    assert order is None and "active tracked buyers" in why[0]


def test_a_win_rate_below_the_minimum_is_not_followed():
    wallets = {MITCH: TrackedWallet(MITCH, "mitch", None, {**STRONG, "win_rate": 0.32})}
    led, case = case_with(trade(MITCH, "buy", 19.6, 0))
    order, _ = strategy().entry(case, led, {MITCH}, NO_GROUP, T0 + 5, wallets)
    assert order is None


def test_says_why_a_big_buy_is_not_followed():
    led, case = case_with(trade(MITCH, "buy", 19.6, 0), trade(MITCH, "sell", 15.0, 30, tokens=8e5))
    order, why = strategy().entry(case, led, set(WALLETS), NO_GROUP, T0 + 40, WALLETS)
    assert order is None and why == ["conviction: mitch bought 19.60 SOL, 3.9x its usual 5.00 SOL entry; "
                                     "not followed: it has sold since"]
    led, case = case_with(trade(MITCH, "buy", 19.6, 0), trade(FROG, "buy", 1.0, 30, tokens=1e4))
    order, why = strategy().entry(case, led, set(WALLETS), NO_GROUP, T0 + 40, WALLETS)
    assert order is None and why[0].endswith("; not followed: the price is already 5.1x its buy")


def test_the_biggest_qualifying_buy_leads_and_risk_limits_still_apply():
    led, case = case_with(trade(FROG, "buy", 9.5, 0), trade(MITCH, "buy", 25.0, 20))
    order, why = strategy().entry(case, led, set(WALLETS), NO_GROUP, T0 + 30, WALLETS)
    assert order.leader == MITCH and why[0].startswith("conviction: mitch bought 25.00 SOL, 5.0x")
    s = strategy(max_open_positions=0)
    order, why = s.entry(case, led, set(WALLETS), NO_GROUP, T0 + 30, WALLETS)
    assert order is None and why == ["conviction: mitch bought 25.00 SOL, 5.0x its usual 5.00 SOL entry; "
                                     "not followed: max open positions"]


def held(s, case, leader=MITCH):
    s.pf.apply(Fill("Coin", "buy", 0.1, 0.1 / case.last_price, case.last_price, 0.0, T0 + 5, "paper", "conviction"),
               leader=leader)
    return s.pf.holdings["Coin"]


def test_a_conviction_position_follows_its_wallet_out_not_the_other_buyers():
    # yami bought first and left: for a confluence entry that's "first tracked entrant exited".
    led, case = case_with(trade(WEAK, "buy", 0.8, -60), trade(WEAK, "sell", 0.9, -30),
                          trade(MITCH, "buy", 19.6, 0))
    s = strategy()
    held(s, case)
    assert s.exits(case, led, set(WALLETS), T0 + 60, WALLETS) == []
    t = trade(MITCH, "sell", 12.0, 120, tokens=6e5)  # mitch sells 60%
    led.apply(t)
    case.add_event(t)
    [o] = s.exits(case, led, set(WALLETS), T0 + 120, WALLETS)
    assert o.side == "sell" and o.reason == "mitch has sold 60% of its tokens"


def test_a_conviction_position_exits_when_its_wallet_moves_the_tokens_away():
    led, case = case_with(trade(MITCH, "buy", 19.6, 0))
    s = strategy()
    held(s, case)
    led.apply(trade(MITCH, "transfer_out", 0, 60))
    [o] = s.exits(case, led, set(WALLETS), T0 + 60, WALLETS)
    assert o.reason == "mitch no longer holds the token"


def engine():
    cfg = Config()
    cfg.llm.use_llm_profiles = cfg.llm.use_llm_judge = cfg.llm.use_llm_reports = False
    cfg.strategy.conviction_entry = True
    store = Store()
    for w, (alias, stats) in RECORDS.items():
        store.upsert_roster(wallet=w, alias=alias, included=True, score=1.0, reasons=[], stats=stats,
                            profile={"style": "patient_holder", "summary": "test"}, profile_source=None, ts=0)
    lines = []
    e = Engine(cfg, store, tracked_from_roster(store.roster()), HeuristicJudge(cfg), Reporter(cfg),
               PaperBroker(cfg.paper), out=lines.append)
    return e, lines


async def test_engine_copies_a_conviction_buy_and_keeps_its_leader_after_a_restart():
    e, lines = engine()
    await e.handle(trade(MITCH, "buy", 19.6, 0))
    h = e.portfolio.holdings["Coin"]
    assert h.leader == MITCH and h.entry_reason.startswith("conviction: mitch bought 19.60 SOL")
    assert any("PAPER BUY" in line for line in lines)

    restarted, _ = engine()
    restarted.store = e.store
    restarted.restore(e.store.fills())
    assert restarted.portfolio.holdings["Coin"].leader == MITCH

    same_price = 1e6 / 19.6
    await e.handle(trade(FROG, "buy", 1.0, 30, tokens=same_price))  # another wallet's trades don't move it
    await e.handle(trade(FROG, "sell", 1.0, 40, tokens=same_price))
    assert "Coin" in e.portfolio.holdings
    await e.handle(trade(MITCH, "sell", 19.0, 60, tokens=1e6))
    assert "Coin" not in e.portfolio.holdings
    assert e.portfolio.closed[-1]["exit_reason"] == "mitch has sold 100% of its tokens"


async def test_engine_says_why_a_big_buy_was_not_copied():
    e, lines = engine()
    e.cfg.strategy.max_open_positions = 0
    await e.handle(trade(MITCH, "buy", 19.6, 0))
    assert lines[-1] == ("    no entry: conviction: mitch bought 19.60 SOL, 3.9x its usual 5.00 SOL entry; "
                         "not followed: max open positions")
    await e.handle(trade(MITCH, "sell", 15.0, 30, tokens=8e5))
    assert lines[-1] == ("    no entry: conviction: mitch bought 19.60 SOL, 3.9x its usual 5.00 SOL entry; "
                         "not followed: it has sold since")
    await e.handle(trade(FROG, "buy", 0.5, 40))  # the same reason again: not repeated
    assert "no entry" not in lines[-1]


def test_run_lists_the_wallets_whose_big_buys_it_would_copy():
    from trenchnet.cli import _entry_rules

    e, _ = engine()
    assert _entry_rules(e.cfg, e) == ("buys when 3+ followed wallets buy within 15 min (from 2+ separate groups), "
                                      "or when one of mitch, mrfrog buys 3x its usual entry or more")
    e.cfg.strategy.conviction_min_win_rate = 0.7
    e.cfg.strategy.min_independent_clusters = 1
    assert _entry_rules(e.cfg, e).endswith("within 15 min, or when mrfrog buys 3x its usual entry or more")
    e.cfg.strategy.conviction_min_realized_sol = 1000
    assert "no followed wallet has the record for them" in _entry_rules(e.cfg, e)
