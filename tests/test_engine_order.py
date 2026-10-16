"""Live trades from two sources (PumpPortal and the RPC watcher): the bot
uses one clock, the time each trade reaches it, and applies trades in the
order they arrive."""

import pytest

from trenchnet.brokers.paper import PaperBroker
from trenchnet.config import Config
from trenchnet.engine import Engine, tracked_from_roster
from trenchnet.judge import HeuristicJudge
from trenchnet.models import Side, Trade
from trenchnet.reporter import Reporter
from trenchnet.store import Store

W, W2, COIN, T0 = "WalletA", "WalletB", "CoinX", 1_760_000_000.0


def engine():
    cfg = Config()
    cfg.llm.use_llm_profiles = cfg.llm.use_llm_judge = cfg.llm.use_llm_reports = False
    cfg.strategy.enabled = False
    store = Store()
    for w in (W, W2):
        store.upsert_roster(wallet=w, alias=w[-1], included=True, score=1.0, reasons=[], stats={}, profile=None,
                            profile_source=None, ts=0)
    return Engine(cfg, store, tracked_from_roster(store.roster()), HeuristicJudge(cfg), Reporter(cfg),
                  PaperBroker(cfg.paper), out=lambda line: None)


def pp(sig, side, at, wallet=W):
    return Trade(sig, wallet, COIN, Side(side), 0.5 if side == "buy" else 0.6, 1000, at, source="pumpportal:pump")


def rpc(sig, side, at, wallet=W):
    return Trade(sig, wallet, COIN, Side(side), 0.5 if side == "buy" else 0.6, 1000, at, source="rpc")


async def test_the_rpc_copy_of_a_trade_pumpportal_reported_changes_nothing():
    e = engine()
    await e.handle(pp("buy1", "buy", T0 + 0.6))
    await e.handle(rpc("buy1", "buy", T0 + 1.4))  # the same trade, a moment later through the RPC
    case = e.cases[COIN]
    assert case.first_entry_ts == T0 + 0.6 and len(case.events) == 1
    assert e.strategy.cohort(case, e.ledger, e.tracked_set) == [W]  # the first buyer stays in the cohort
    assert len(e.store.trades(wallets=[W])) == 1


@pytest.mark.parametrize("arrivals", [
    # (source, signature, side, arrival) - a quick buy then sell; the buy's RPC read is lost
    [("pp", "buy1", "buy", 0.9), ("pp", "sell1", "sell", 1.3), ("rpc", "sell1", "sell", 2.0)],
    [("pp", "buy1", "buy", 0.9), ("rpc", "sell1", "sell", 1.2), ("pp", "sell1", "sell", 1.3)],
    # PC clock wrong: arrival order still decides
    [("pp", "buy1", "buy", 5.0), ("rpc", "buy1", "buy", 5.5), ("pp", "sell1", "sell", 6.0)],
    # Fomo/aggregator buy only the RPC sees, then a pump.fun sell PumpPortal reports
    [("rpc", "buy1", "buy", 1.5), ("pp", "sell1", "sell", 3.0), ("rpc", "sell1", "sell", 3.6)],
])
async def test_trades_are_applied_in_the_order_they_arrive(arrivals):
    e = engine()
    for src, sig, side, at in arrivals:
        await e.handle((pp if src == "pp" else rpc)(sig, side, T0 + at))
    pos = e.ledger.get(W, COIN)
    assert pos.status == "exited" and pos.realized_pnl_sol == pytest.approx(0.1)
    # A restart replays the stored history the same way.
    restarted = Engine(e.cfg, e.store, e.tracked, e.judge, e.reporter, e.broker,
                       history=e.store.trades(wallets=[W]), out=lambda line: None)
    assert restarted.ledger.get(W, COIN).status == "exited"


async def test_first_entry_is_the_earliest_buy_even_if_it_arrives_later():
    e = engine()
    await e.handle(pp("b2", "buy", T0 + 5, wallet=W2))
    await e.handle(rpc("b1", "buy", T0 + 2))  # e.g. a replay file out of order
    assert e.cases[COIN].first_entry_ts == T0 + 2
    assert e.strategy.cohort(e.cases[COIN], e.ledger, e.tracked_set) == [W, W2]


async def test_a_late_trade_does_not_replace_a_newer_price():
    from trenchnet.models import PriceTick

    e = engine()
    await e.handle(pp("b0", "buy", T0, wallet=W2))  # opens the case
    await e.handle(PriceTick(COIN, 1.05e-6, T0 + 2))  # live price update
    late = Trade("fomo1", W, COIN, Side.BUY, 0.00062, 1000, T0 + 1, source="rpc")  # announced at +1, read at +3
    await e.handle(late)
    assert e.cases[COIN].last_price == pytest.approx(1.05e-6) and e.prices[COIN] == pytest.approx(1.05e-6)
