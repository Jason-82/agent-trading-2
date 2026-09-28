"""PumpPortalSource against a local websocket server."""

import asyncio
import json

import websockets

from trenchnet.config import PumpPortalConfig
from trenchnet.models import PriceTick, Trade
from trenchnet.sources.pumpportal_ws import PumpPortalSource

W = "Wallet111111111111111111111111111111111111"


async def test_subscribes_streams_and_watches_tokens():
    received: list[dict] = []
    ready = asyncio.Event()

    async def handler(ws):
        received.append(json.loads(await ws.recv()))
        await ws.send(json.dumps({"message": "Successfully subscribed to keys."}))
        await ws.send(json.dumps({"signature": "S1", "mint": "M1", "traderPublicKey": W, "txType": "buy",
                                  "solAmount": 1.0, "tokenAmount": 2e7, "marketCapSol": 30.0}))
        received.append(json.loads(await ws.recv()))  # watch_tokens from the client
        ready.set()
        await ws.wait_closed()

    async with websockets.serve(handler, "127.0.0.1", 0) as server:
        port = server.sockets[0].getsockname()[1]
        src = PumpPortalSource(PumpPortalConfig(ws_url=f"ws://127.0.0.1:{port}"), [W], api_key=None)
        events = []
        async for ev in src.events():
            events.append(ev)
            if isinstance(ev, Trade):
                await src.watch_tokens(["M1"])
                await asyncio.wait_for(ready.wait(), 5)
                src.stop()

    assert received[0] == {"method": "subscribeAccountTrade", "keys": [W]}
    assert received[1] == {"method": "subscribeTokenTrade", "keys": ["M1"]}
    assert isinstance(events[0], PriceTick) and isinstance(events[1], Trade)
    assert events[1].signature == "S1"
