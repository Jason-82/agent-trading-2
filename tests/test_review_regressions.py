"""Regression tests for concurrency and failure handling in the live path."""

import asyncio

import pydantic
import pytest
import websockets

from trenchnet.brokers.base import Fill
from trenchnet.cases import Case
from trenchnet.config import Config, PumpPortalConfig
from trenchnet.engine import Engine
from trenchnet.judge import ClaudeJudge, HeuristicJudge
from trenchnet.llm import LLM, LLMError
from trenchnet.profiler import _alias
from trenchnet.reporter import Reporter
from trenchnet.solana_rpc import SolanaRpc
from trenchnet.sources.pumpportal_ws import PumpPortalSource
from trenchnet.store import Store

from .test_llm import SNAP, fake_llm


class SlowBroker:
    mode = "paper"

    def __init__(self, fail_buys=False):
        self.buys = self.sells = 0
        self.fail_buys = fail_buys

    async def buy(self, mint, sol_amount, ref_price, ts, reason):
        self.buys += 1
        await asyncio.sleep(0.01)
        if self.fail_buys:
            return None
        return Fill(mint, "buy", sol_amount, sol_amount / ref_price, ref_price, 0.0, ts, self.mode, reason)

    async def sell(self, mint, token_amount, ref_price, ts, reason):
        self.sells += 1
        await asyncio.sleep(0.05)
        return Fill(mint, "sell", token_amount * ref_price, token_amount, ref_price, 0.0, ts, self.mode, reason)


def engine_with(broker):
    cfg = Config()
    return Engine(cfg, Store(), {}, HeuristicJudge(cfg), Reporter(cfg), broker, out=lambda s: None)


async def test_concurrent_exit_checks_sell_once():
    broker = SlowBroker()
    eng = engine_with(broker)
    eng.portfolio.apply(Fill("M", "buy", 0.1, 1e5, 1e-6, 0, 0, "paper", "x"))
    case = eng.cases["M"] = Case(mint="M", opened_ts=0)
    case.update_price(0.5e-6, 10)  # below the stop-loss
    eng.now = 10
    await asyncio.gather(eng._exits(case), eng._exits(case), eng.tick(10))
    assert broker.sells == 1
    assert "M" not in eng.portfolio.holdings


async def test_failed_buy_is_not_retried():
    from trenchnet.strategy import Order

    broker = SlowBroker(fail_buys=True)
    eng = engine_with(broker)
    async with eng._lock("M"):
        await eng._execute(Order("M", "buy", 1e-6, "entry", sol_amount=0.1))
    order, why = eng.strategy.entry(Case(mint="M", opened_ts=0), eng.ledger, set(), None, 0)
    assert order is None and why == ["already traded this token"]


async def test_restore_rebuilds_holdings_and_take_profit_state():
    store = Store()
    for f in (Fill("M", "buy", 0.1, 1e5, 1e-6, 0, 0, "live", "entry", "sig1"),
              Fill("M", "sell", 0.1, 5e4, 2e-6, 0, 60, "live", "take-profit 1: 2.0x entry", "sig2"),
              Fill("N", "buy", 0.1, 1e5, 1e-6, 0, 0, "live", "entry", "sig3"),
              Fill("N", "sell", 0.05, 1e5, 5e-7, 0, 90, "live", "stop-loss: 0.50x entry", "sig4")):
        store.add_fill(**f.to_dict())
    eng = engine_with(SlowBroker())
    eng.store = store
    eng.restore(store.fills(mode="live"))
    assert set(eng.portfolio.holdings) == {"M"}
    assert eng.portfolio.holdings["M"].tp_hit == {0}
    assert {"M", "N"} <= eng.portfolio.traded_mints
    assert "M" in eng.cases


async def test_send_on_dropped_socket_does_not_raise():
    async def handler(ws):
        await ws.close()

    async with websockets.serve(handler, "127.0.0.1", 0) as server:
        port = server.sockets[0].getsockname()[1]
        src = PumpPortalSource(PumpPortalConfig(ws_url=f"ws://127.0.0.1:{port}"), ["W"])
        async with websockets.connect(src.url) as ws:
            src._ws = ws
            await asyncio.sleep(0.05)
            await src.watch_tokens(["M"])  # socket already closed by the server
    assert src._tokens == {"M"}


async def test_off_schema_output_falls_back():
    class Tiny(pydantic.BaseModel):
        x: int

    try:
        Tiny.model_validate({"x": "not an int"})
    except pydantic.ValidationError as e:
        err = e
    llm, _ = fake_llm(err, err)
    with pytest.raises(LLMError):
        await llm.parse(model="m", system="s", user="u", schema=Tiny)
    _, source = await ClaudeJudge(Config(), llm).assess(SNAP)
    assert source == "heuristic-fallback"


async def test_processed_status_is_not_final():
    rpc = SolanaRpc("http://unused")

    async def call(method, params=None):
        return {"value": [{"err": None, "confirmationStatus": "processed"}]}

    rpc.call = call
    assert await rpc.signature_confirmed("sig") is None
    await rpc.aclose()


def test_alias_unbounded():
    assert [_alias(i) for i in (0, 25, 26, 701, 702)] == ["A", "Z", "AA", "ZZ", "AAA"]
