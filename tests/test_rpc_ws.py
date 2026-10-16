"""Watching followed wallets through the RPC's websocket."""

import asyncio
import json
import time

import websockets
from solders.pubkey import Pubkey

from trenchnet.models import PriceTick, Side, Trade
from trenchnet.solana_rpc import RpcError
from trenchnet.sources.rpc_backfill import USDC
from trenchnet.sources.rpc_ws import RpcWalletSource, merge_events, ws_url_from

from .test_stablecoin_and_rereads import COIN, FEE, JUP, PUMP, RENT, T0, W, FakeRpc, fixed_prices, tx


def test_websocket_url_keeps_host_path_and_key():
    assert ws_url_from("https://mainnet.helius-rpc.com/?api-key=K") == "wss://mainnet.helius-rpc.com/?api-key=K"
    assert ws_url_from("http://127.0.0.1:8899") == "ws://127.0.0.1:8899"


def _note(sub, sig, err=None):
    return json.dumps({"jsonrpc": "2.0", "method": "logsNotification", "params": {
        "subscription": sub, "result": {"context": {"slot": 1}, "value": {"signature": sig, "err": err, "logs": []}}}})


async def _ack_all(ws, n):
    for i in range(n):
        req = json.loads(await ws.recv())
        await ws.send(json.dumps({"jsonrpc": "2.0", "id": req["id"], "result": 100 + i}))


async def test_reports_the_wallets_buys_and_sells():
    subscribed: list[str] = []

    async def handler(ws):
        req = json.loads(await ws.recv())
        subscribed.append(req["params"][0]["mentions"][0])
        await ws.send(json.dumps({"jsonrpc": "2.0", "id": req["id"], "result": 100}))
        await ws.send(_note(100, "failedTx", err={"InstructionError": [0, "x"]}))
        await ws.send(_note(100, "solBuy"))
        await ws.send(_note(100, "solBuy"))  # notified twice: reported once
        await ws.send(_note(100, "usdcSell"))
        await ws.send(_note(100, "transfer"))
        await ws.wait_closed()

    rpc = FakeRpc({}, {
        "solBuy": tx(-FEE - 500_000_000 - RENT, {}, {COIN: 1000}, programs=[PUMP]),
        "usdcSell": [None, tx(0, {COIN: 1000, USDC: 0}, {COIN: 0, USDC: 300}, programs=[PUMP], sponsor=True)],
        "transfer": tx(-FEE, {COIN: 5}, {COIN: 0}),
    })
    async with websockets.serve(handler, "127.0.0.1", 0) as server:
        port = server.sockets[0].getsockname()[1]
        src = RpcWalletSource(rpc, [W], ws_url=f"ws://127.0.0.1:{port}", prices=fixed_prices(150.0), fetch_pause_s=0)
        got = []
        async for ev in src.events():
            got.append(ev)
            if len(got) == 2:
                src.stop()
    assert subscribed == [W]
    assert [(t.signature, t.side, round(t.sol_amount, 6)) for t in got] == [
        ("solBuy", Side.BUY, 0.5), ("usdcSell", Side.SELL, 2.0)]  # valued at the price of its block time
    # Stamped when they reach the bot (one clock with PumpPortal), not with the block time.
    assert all(t.ts > T0 + 86400 for t in got) and got[0].ts <= got[1].ts


async def test_a_wallets_trades_come_out_in_chain_order_even_when_reads_finish_out_of_order():
    async def handler(ws):
        await _ack_all(ws, 1)
        await ws.send(_note(100, "buy1"))  # its first read comes back empty: retried after a pause
        await asyncio.sleep(0.05)
        await ws.send(_note(100, "mid1"))  # can't be read at all
        await asyncio.sleep(0.05)
        await ws.send(_note(100, "sell1"))  # reads at once
        await ws.wait_closed()

    rpc = FakeRpc({}, {
        "buy1": [None, tx(-FEE - 500_000_000 - RENT, {}, {COIN: 1000}, programs=[JUP])],
        "mid1": RpcError(-32603, "internal error"),
        "sell1": tx(-FEE + 600_000_000 + RENT, {COIN: 1000}, {}, programs=[JUP], time=T0 + 1),
    })
    async with websockets.serve(handler, "127.0.0.1", 0) as server:
        port = server.sockets[0].getsockname()[1]
        src = RpcWalletSource(rpc, [W], ws_url=f"ws://127.0.0.1:{port}", fetch_pause_s=0.3)
        got = []
        async for ev in src.events():
            got.append(ev.signature)
            if len(got) == 2:
                src.stop()
    assert got == ["buy1", "sell1"]


async def test_stop_ends_a_reconnect_wait_at_once():
    import socket

    with socket.socket() as sock:  # a port nothing listens on
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    src = RpcWalletSource(FakeRpc({}, {}), [W], ws_url=f"ws://127.0.0.1:{port}")
    loop = asyncio.get_running_loop()

    async def consume():
        async for _ in src.events():
            pass

    task = asyncio.create_task(consume())
    await asyncio.sleep(0.3)  # connection refused: now waiting to reconnect
    started = loop.time()
    src.stop()
    await asyncio.wait_for(task, 2)
    assert loop.time() - started < 0.5


async def test_reconnect_delay_resets_after_a_healthy_connection(monkeypatch):
    waits = []
    sessions = 0

    async def handler(ws):
        nonlocal sessions
        sessions += 1
        await _ack_all(ws, 1)
        ws.transport.abort()  # an abnormal drop (Wi-Fi, sleep), not a clean close

    real_wait_for = asyncio.wait_for

    async def fast_wait_for(aw, timeout):
        waits.append(timeout)
        if len(waits) >= 4:
            src.stop()
        return await real_wait_for(aw, 0.01)

    monkeypatch.setattr("trenchnet.sources.rpc_ws.asyncio.wait_for", fast_wait_for)
    monkeypatch.setattr(RpcWalletSource, "HEALTHY_S", 0.0)  # every session counts as healthy
    async with websockets.serve(handler, "127.0.0.1", 0) as server:
        port = server.sockets[0].getsockname()[1]
        src = RpcWalletSource(FakeRpc({}, {}), [W], ws_url=f"ws://127.0.0.1:{port}")
        async for _ in src.events():
            pass
    assert waits == [1.0, 1.0, 1.0, 1.0]


async def test_merge_keeps_going_when_one_source_breaks():
    class Good:
        async def events(self):
            for i in range(3):
                await asyncio.sleep(0)
                yield PriceTick("M", float(i + 1), float(i))

    class Broken:
        async def events(self):
            yield Trade("s", W, "M", Side.BUY, 1.0, 1.0, 0.0)
            raise RuntimeError("boom")

    got = [ev async for ev in merge_events(Good(), Broken())]
    assert len(got) == 4 and sum(isinstance(e, Trade) for e in got) == 1


def test_run_sees_a_fomo_trade_only_the_rpc_reports(tmp_path, monkeypatch, capsys):
    """`trenchnet run`: PumpPortal says nothing about the wallet; the RPC
    watcher reports its USDC buy, and the engine records it."""
    import trenchnet.cli as cli
    from trenchnet.store import Store

    db = str(tmp_path / "t.db")
    store = Store(db)
    store.upsert_roster(wallet=W, alias="fomo-W", included=True, score=1.0, reasons=[], stats={}, profile=None,
                        profile_source=None, ts=0)
    store.close()
    stoppers = []
    monkeypatch.setattr(cli, "_install_stop_handlers", lambda loop, stop: stoppers.append(stop) or (lambda: None))
    rpc = FakeRpc({}, {"fomoBuy": tx(0, {COIN: 0, USDC: 150}, {COIN: 1000, USDC: 0}, programs=[PUMP], sponsor=True)})
    monkeypatch.setattr(cli, "_rpc_from", lambda url, rps=None: rpc)
    monkeypatch.setattr("trenchnet.prices.SOURCES", fixed_prices(150.0).sources)

    async def pumpportal(ws):
        await ws.recv()  # subscribeAccountTrade: nothing to report for this wallet
        await ws.wait_closed()

    async def rpc_ws(ws):
        await _ack_all(ws, 1)
        await ws.send(_note(100, "fomoBuy"))
        for _ in range(100):
            await asyncio.sleep(0.02)
            if Store(db).trades(wallets=[W]):
                break
        stoppers[0]()
        await ws.wait_closed()

    async def main():
        async with websockets.serve(pumpportal, "127.0.0.1", 0) as pp, websockets.serve(rpc_ws, "127.0.0.1", 0) as rs:
            (tmp_path / "trenchnet.toml").write_text(
                f'[pumpportal]\nws_url = "ws://127.0.0.1:{pp.sockets[0].getsockname()[1]}"\n')
            monkeypatch.setenv("SOLANA_RPC_URL", "https://rpc.example/?api-key=K")
            monkeypatch.setenv("SOLANA_WS_URL", f"ws://127.0.0.1:{rs.sockets[0].getsockname()[1]}")
            args = cli.argparse.Namespace(no_llm=True, live=False, rpc=None, no_rpc_watch=False)
            cfg = cli.load_config(str(tmp_path / "trenchnet.toml"))
            cfg.db_path = db
            await asyncio.wait_for(cli.cmd_run(args, cfg), 10)

    asyncio.run(main())
    trades = Store(db).trades(wallets=[W])
    assert [(t.side, t.sol_amount, t.quote_mint) for t in trades] == [(Side.BUY, 1.0, USDC)]
    assert "also watching the wallets through 127.0.0.1" in capsys.readouterr().out


def test_first_ctrl_c_drops_events_that_are_waiting(tmp_path, monkeypatch):
    """`trenchnet run`: events that arrived while the bot was busy are not
    acted on after the first Ctrl-C (they could open new positions)."""
    import trenchnet.cli as cli
    from trenchnet.store import Store

    db = str(tmp_path / "t.db")
    store = Store(db)
    store.upsert_roster(wallet=W, alias="A", included=True, score=1.0, reasons=[], stats={}, profile=None,
                        profile_source=None, ts=0)
    store.close()
    stoppers, handled = [], []
    monkeypatch.setattr(cli, "_install_stop_handlers", lambda loop, stop: stoppers.append(stop) or (lambda: None))
    monkeypatch.setattr(cli, "_rpc_from", lambda url, rps=None: FakeRpc({}, {}))

    class SlowEngine(cli.Engine):
        async def handle(self, ev):
            handled.append(ev)
            if len(handled) == 1:
                await asyncio.sleep(0.3)  # busy (a judge call, an order) while more events arrive
                stoppers[0]()

    monkeypatch.setattr(cli, "Engine", SlowEngine)

    async def pumpportal(ws):
        await ws.recv()
        for i in range(10):
            await ws.send(json.dumps({"signature": f"S{i}", "mint": f"M{i}", "traderPublicKey": W, "txType": "buy",
                                      "solAmount": 1.0, "tokenAmount": 1e6}))
        await ws.wait_closed()

    async def rpc_ws(ws):
        await _ack_all(ws, 1)
        await ws.wait_closed()

    async def main():
        async with websockets.serve(pumpportal, "127.0.0.1", 0) as pp, websockets.serve(rpc_ws, "127.0.0.1", 0) as rs:
            (tmp_path / "trenchnet.toml").write_text(
                f'[pumpportal]\nws_url = "ws://127.0.0.1:{pp.sockets[0].getsockname()[1]}"\n')
            monkeypatch.setenv("SOLANA_RPC_URL", "https://rpc.example/?api-key=K")
            monkeypatch.setenv("SOLANA_WS_URL", f"ws://127.0.0.1:{rs.sockets[0].getsockname()[1]}")
            args = cli.argparse.Namespace(no_llm=True, live=False, rpc=None, no_rpc_watch=False)
            cfg = cli.load_config(str(tmp_path / "trenchnet.toml"))
            cfg.db_path = db
            await asyncio.wait_for(cli.cmd_run(args, cfg), 10)

    asyncio.run(main())
    assert len(handled) == 1


async def test_a_slow_read_keeps_the_time_it_was_announced():
    loop_times = {}

    async def handler(ws):
        await _ack_all(ws, 1)
        loop_times["sent"] = time.time()
        await ws.send(_note(100, "slow"))
        await ws.wait_closed()

    rpc = FakeRpc({}, {"slow": [None, tx(-FEE - 500_000_000 - RENT, {}, {COIN: 1000}, programs=[PUMP])]})
    async with websockets.serve(handler, "127.0.0.1", 0) as server:
        port = server.sockets[0].getsockname()[1]
        src = RpcWalletSource(rpc, [W], ws_url=f"ws://127.0.0.1:{port}", fetch_pause_s=0.5)
        async for ev in src.events():
            got_at = time.time()
            src.stop()
    assert got_at - ev.ts >= 0.4  # stamped when announced, not when the read finally succeeded
    assert ev.ts - loop_times["sent"] < 0.3
