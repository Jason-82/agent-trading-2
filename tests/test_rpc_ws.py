"""Watching followed wallets through the RPC's websocket."""

import asyncio
import json

import websockets
from solders.pubkey import Pubkey

from trenchnet.models import PriceTick, Side, Trade
from trenchnet.sources.rpc_backfill import USDC, usdc_account
from trenchnet.sources.rpc_ws import RpcWalletSource, merge_events, ws_url_from

from .test_stablecoin_and_rereads import COIN, FEE, PUMP, RENT, W, FakeRpc, fixed_prices, tx


def test_websocket_url_keeps_host_path_and_key():
    assert ws_url_from("https://mainnet.helius-rpc.com/?api-key=K") == "wss://mainnet.helius-rpc.com/?api-key=K"
    assert ws_url_from("http://127.0.0.1:8899") == "ws://127.0.0.1:8899"


async def test_reports_buys_and_sells_of_the_wallet_and_its_usdc_account():
    ata = usdc_account(W)
    subscribed: list[str] = []

    async def handler(ws):
        for i in range(2):
            req = json.loads(await ws.recv())
            subscribed.append(req["params"][0]["mentions"][0])
            await ws.send(json.dumps({"jsonrpc": "2.0", "id": req["id"], "result": 100 + i}))

        def note(sub, sig, err=None):
            return json.dumps({"jsonrpc": "2.0", "method": "logsNotification", "params": {
                "subscription": sub, "result": {"context": {"slot": 1}, "value": {"signature": sig, "err": err, "logs": []}}}})

        await ws.send(note(100, "failedTx", err={"InstructionError": [0, "x"]}))
        await ws.send(note(100, "solBuy"))
        await ws.send(note(101, "solBuy"))  # same transaction seen through the USDC account: once only
        await ws.send(note(101, "usdcSell"))
        await ws.send(note(100, "transfer"))
        await ws.wait_closed()

    rpc = FakeRpc({}, {
        "solBuy": tx(-FEE - 500_000_000 - RENT, {}, {COIN: 1000}, programs=[PUMP]),
        "usdcSell": [None, tx(0, {COIN: 1000, USDC: 0}, {COIN: 0, USDC: 300}, programs=[PUMP], wallet_in_tx=False)],
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
    assert sorted(subscribed) == sorted([W, ata])
    assert sorted((t.signature, t.side, round(t.sol_amount, 6)) for t in got) == [
        ("solBuy", Side.BUY, 0.5), ("usdcSell", Side.SELL, 2.0)]


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
    rpc = FakeRpc({}, {"fomoBuy": tx(0, {COIN: 0, USDC: 150}, {COIN: 1000, USDC: 0}, programs=[PUMP], wallet_in_tx=False)})
    monkeypatch.setattr(cli, "_rpc_from", lambda url, rps=None: rpc)
    monkeypatch.setattr("trenchnet.prices.SOURCES", fixed_prices(150.0).sources)

    async def pumpportal(ws):
        await ws.recv()  # subscribeAccountTrade: nothing to report for this wallet
        await ws.wait_closed()

    async def rpc_ws(ws):
        req = json.loads(await ws.recv())
        await ws.send(json.dumps({"jsonrpc": "2.0", "id": req["id"], "result": 7}))
        async for raw in ws:  # the USDC account's subscription, too
            r = json.loads(raw)
            await ws.send(json.dumps({"jsonrpc": "2.0", "id": r["id"], "result": 8}))
            break
        await ws.send(json.dumps({"jsonrpc": "2.0", "method": "logsNotification", "params": {
            "subscription": 8, "result": {"value": {"signature": "fomoBuy", "err": None}}}}))
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
