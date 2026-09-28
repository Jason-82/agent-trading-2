import pytest

from trenchnet.brokers.paper import PaperBroker
from trenchnet.cases import Case
from trenchnet.config import GraphConfig, PaperConfig, StrategyConfig
from trenchnet.graph import TradeGraph
from trenchnet.ledger import Ledger
from trenchnet.portfolio import Portfolio
from trenchnet.strategy import Strategy

from .conftest import mk

FIT = {"behavior_fit": "fits", "position_intent": "standard"}


def setup(wallets, independent=True, price=1e-6):
    """A case where `wallets` bought at t=0..; history makes A,B,C a group and
    D independent when `independent`."""
    g = TradeGraph(GraphConfig(min_history_tokens=1))
    hist = []
    for i in range(3):
        hist += [mk("A", f"h{i}", "buy", 1, 1, i * 1e5), mk("B", f"h{i}", "buy", 1, 1, i * 1e5 + 60),
                 mk("C", f"h{i}", "buy", 1, 1, i * 1e5 + 120)]
        hist += [mk("D" if independent else "A", f"d{i}", "buy", 1, 1, i * 1e5 + 5e4)]
    hist += [mk("X", f"bg{i}", "buy", 1, 1, 9e5 + i) for i in range(10)]
    g.add_trades(hist)
    led = Ledger()
    case = Case(mint="M", opened_ts=1e6)
    for k, w in enumerate(wallets):
        t = mk(w, "M", "buy", 1.0, 1.0 / price, 1e6 + k * 60)
        led.apply(t)
        g.add_trade(t)
        case.add_event(t)
        case.assessments.append({"side": "buy", "assessment": FIT})
    rel = g.relationship_summary("M", set("ABCD"))
    return g, led, case, rel


def test_entry_requires_independent_cluster():
    pf = Portfolio(5.0)
    s = Strategy(StrategyConfig(min_tracked_buyers=3), pf)
    _, led, case, rel = setup("ABC")
    order, why = s.entry(case, led, set("ABCD"), rel, 1e6 + 300)
    assert order is None and "cluster" in why[0]
    _, led, case, rel = setup("ABCD")
    order, why = s.entry(case, led, set("ABCD"), rel, 1e6 + 300)
    assert order is not None and order.sol_amount == pytest.approx(0.1)


def test_entry_blocked_by_run_up_and_staleness():
    pf = Portfolio(5.0)
    s = Strategy(StrategyConfig(min_tracked_buyers=3, max_run_up=2.0, max_entry_delay_s=600), pf)
    _, led, case, rel = setup("ABCD")
    case.update_price(case.first_entry_price * 2.5, 1e6 + 200)
    order, why = s.entry(case, led, set("ABCD"), rel, 1e6 + 300)
    assert order is None and "price already" in why[0]
    _, led, case, rel = setup("ABCD")
    order, why = s.entry(case, led, set("ABCD"), rel, 1e6 + 2000)
    assert order is None and "stale" in why[0]


def test_entry_requires_pattern_fit():
    s = Strategy(StrategyConfig(min_tracked_buyers=3), Portfolio(5.0))
    _, led, case, rel = setup("ABCD")
    for a in case.assessments:
        a["assessment"] = {"behavior_fit": "fits", "position_intent": "exploratory"}
    order, why = s.entry(case, led, set("ABCD"), rel, 1e6 + 300)
    assert order is None and "fit" in why[0]


def test_daily_loss_limit_blocks_entries():
    pf = Portfolio(5.0)
    pf.daily_realized = {"1970-01-12": -1.0}  # ts 1e6 is 1970-01-12 UTC
    s = Strategy(StrategyConfig(min_tracked_buyers=3, daily_loss_limit_sol=0.3), pf)
    _, led, case, rel = setup("ABCD")
    order, why = s.entry(case, led, set("ABCD"), rel, 1e6 + 300)
    assert order is None and "daily loss" in why[0]


async def enter(s, pf, led, case, rel):
    order, _ = s.entry(case, led, set("ABCD"), rel, 1e6 + 300)
    fill = await PaperBroker(PaperConfig(fee_bps=0, slippage_bps=0, latency_penalty_bps=0)).buy(
        order.mint, order.sol_amount, order.ref_price, 1e6 + 300, order.reason)
    pf.apply(fill)
    return fill


async def test_stop_loss_and_take_profit():
    pf = Portfolio(5.0)
    s = Strategy(StrategyConfig(min_tracked_buyers=3, stop_loss_pct=0.3, take_profit=[[2.0, 0.5]]), pf)
    _, led, case, rel = setup("ABCD")
    fill = await enter(s, pf, led, case, rel)
    case.update_price(fill.price_sol * 2.1, 1e6 + 400)
    [o] = s.exits(case, led, set("ABCD"), 1e6 + 400)
    assert o.tp_index == 0 and o.token_amount == pytest.approx(fill.token_amount / 2)
    pf.apply((await PaperBroker(PaperConfig()).sell(o.mint, o.token_amount, o.ref_price, 1e6 + 400, "tp")),
             tp_index=0)
    case.update_price(fill.price_sol * 1.3, 1e6 + 500)  # >30% below the 2.1x peak -> trailing stop
    [o] = s.exits(case, led, set("ABCD"), 1e6 + 500)
    assert "trailing" in o.reason

    pf2 = Portfolio(5.0)
    s2 = Strategy(StrategyConfig(min_tracked_buyers=3, stop_loss_pct=0.3), pf2)
    _, led, case, rel = setup("ABCD")
    fill = await enter(s2, pf2, led, case, rel)
    case.update_price(fill.price_sol * 0.6, 1e6 + 400)
    [o] = s2.exits(case, led, set("ABCD"), 1e6 + 400)
    assert "stop-loss" in o.reason and o.token_amount == pytest.approx(fill.token_amount)


async def test_follow_the_cohort_out():
    pf = Portfolio(5.0)
    s = Strategy(StrategyConfig(min_tracked_buyers=3, exit_on_cohort_sold_fraction=0.5,
                                exit_on_leader_exit=True), pf)
    _, led, case, rel = setup("ABCD")
    await enter(s, pf, led, case, rel)
    assert s.exits(case, led, set("ABCD"), 1e6 + 400) == []
    led.apply(mk("A", "M", "sell", 1.0, 1e6, 1e6 + 500))  # leader exits fully
    [o] = s.exits(case, led, set("ABCD"), 1e6 + 500)
    assert o.reason == "first tracked entrant exited"


async def test_transfers_do_not_trigger_cohort_exit():
    pf = Portfolio(5.0)
    s = Strategy(StrategyConfig(min_tracked_buyers=3, exit_on_cohort_sold_fraction=0.25,
                                exit_on_leader_exit=True), pf)
    _, led, case, rel = setup("ABCD")
    await enter(s, pf, led, case, rel)
    for w in "ABC":
        led.apply(mk(w, "M", "transfer_out", 0, 1e6, 1e6 + 500))
    assert s.exits(case, led, set("ABCD"), 1e6 + 500) == []


async def test_paper_broker_costs_and_portfolio_pnl():
    b = PaperBroker(PaperConfig(fee_bps=100, slippage_bps=100, latency_penalty_bps=100))
    buy = await b.buy("M", 1.0, 1e-6, 0, "x")
    assert buy.price_sol == pytest.approx(1.02e-6)
    assert buy.token_amount == pytest.approx(0.99 / 1.02e-6)
    sell = await b.sell("M", buy.token_amount, 1e-6, 1, "y")
    assert sell.sol_amount < 1.0
    pf = Portfolio(5.0)
    pf.apply(buy)
    pnl = pf.apply(sell)
    assert pnl == pytest.approx(sell.sol_amount - 1.0)
    assert pf.cash_sol == pytest.approx(5.0 + pnl)
    assert pf.closed and not pf.holdings
