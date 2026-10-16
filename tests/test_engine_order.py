"""Live trades of one wallet arriving out of order from two sources."""

import pytest

from trenchnet.brokers.paper import PaperBroker
from trenchnet.config import Config
from trenchnet.engine import Engine, tracked_from_roster
from trenchnet.judge import HeuristicJudge
from trenchnet.models import Side, Trade
from trenchnet.reporter import Reporter
from trenchnet.store import Store

W, COIN, T0 = "WalletA", "CoinX", 1_760_000_000.0


def engine():
    cfg = Config()
    cfg.llm.use_llm_profiles = cfg.llm.use_llm_judge = cfg.llm.use_llm_reports = False
    cfg.strategy.enabled = False
    store = Store()
    store.upsert_roster(wallet=W, alias="A", included=True, score=1.0, reasons=[], stats={}, profile=None,
                        profile_source=None, ts=0)
    return Engine(cfg, store, tracked_from_roster(store.roster()), HeuristicJudge(cfg), Reporter(cfg),
                  PaperBroker(cfg.paper), out=lambda line: None)


@pytest.mark.parametrize("clock_error", [0.0, -3.0, 3.0])  # this PC's clock vs the chain
async def test_sell_reported_first_by_pumpportal_still_follows_the_rpc_buy(clock_error):
    e = engine()
    pp_sell = Trade("sell1", W, COIN, Side.SELL, 0.6, 1000, T0 + 1 + 0.8 + clock_error, source="pumpportal:pump")
    rpc_buy = Trade("buy1", W, COIN, Side.BUY, 0.5, 1000, T0, source="rpc")  # only the RPC saw it (aggregator)
    rpc_sell = Trade("sell1", W, COIN, Side.SELL, 0.6, 1000, T0 + 1, source="rpc")  # chain copy of the sell
    await e.handle(pp_sell)
    await e.handle(rpc_buy)
    if clock_error >= 0:  # the buy is visibly older than the sell: put in order at once
        assert e.ledger.get(W, COIN).status == "exited"
    await e.handle(rpc_sell)  # the chain copy settles every case
    pos = e.ledger.get(W, COIN)
    assert pos.status == "exited" and pos.realized_pnl_sol == pytest.approx(0.1)
    stored = e.store.trades(wallets=[W])
    assert [(t.signature, t.ts, t.source) for t in stored] == [("buy1", T0, "rpc"), ("sell1", T0 + 1, "rpc")]


async def test_buy_reported_first_by_pumpportal_with_a_fast_clock_settles_in_chain_order():
    e = engine()
    for ev in (Trade("buy1", W, COIN, Side.BUY, 0.5, 1000, T0 + 21, source="pumpportal:pump"),  # clock 20 s ahead
               Trade("buy1", W, COIN, Side.BUY, 0.5, 1000, T0, source="rpc"),
               Trade("sell1", W, COIN, Side.SELL, 0.6, 1000, T0 + 5, source="rpc")):
        await e.handle(ev)
    assert e.ledger.get(W, COIN).status == "exited"


async def test_in_order_trades_are_applied_without_a_rebuild(monkeypatch):
    e = engine()
    monkeypatch.setattr(e.ledger, "rebuild", lambda *a: pytest.fail("rebuilt"))
    await e.handle(Trade("b", W, COIN, Side.BUY, 0.5, 1000, T0, source="pumpportal:pump"))
    await e.handle(Trade("s", W, COIN, Side.SELL, 0.6, 1000, T0 + 2, source="pumpportal:pump"))
    assert e.ledger.get(W, COIN).status == "exited"
