"""Live trades of the followed wallets, straight from your Solana RPC.

PumpPortal reports the trades it decodes on pump.fun and PumpSwap, under the
account that called pump.fun. A trade routed through an aggregator, or sent
by an app's sponsor wallet (every Fomo trade), may not show up there under
the trader's wallet at all. This source asks the RPC's websocket for every
transaction that mentions a followed wallet (`logsSubscribe`), reads each one
with the same reader as the backfill, and reports the buys and sells,
including stablecoin-priced ones valued in SOL.

It costs one getTransaction call per transaction of a followed wallet.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections import OrderedDict
from typing import TYPE_CHECKING, AsyncIterator, Iterable
from urllib.parse import urlparse

import websockets

from ..models import Trade
from ..solana_rpc import RpcError, RpcHTTPError, SolanaRpc
from .rpc_backfill import TxNotFound, analyze_wallet_tx, fetch_transaction, value_in_sol

if TYPE_CHECKING:
    from ..prices import SolUsdPrices

log = logging.getLogger(__name__)

_DONE = object()


def ws_url_from(rpc_url: str) -> str:
    """The websocket address of an http(s) RPC URL (same host, path and key)."""
    p = urlparse(rpc_url)
    return p._replace(scheme="wss" if p.scheme == "https" else "ws").geturl()


class RpcWalletSource:
    HEALTHY_S = 30.0  # a connection that lasted this long resets the reconnect delay, however it ended

    def __init__(self, rpc: SolanaRpc, wallets: Iterable[str], ws_url: str | None = None,
                 prices: "SolUsdPrices | None" = None, fetch_attempts: int = 5, fetch_pause_s: float = 1.0) -> None:
        self.rpc = rpc
        self.ws_url = ws_url or ws_url_from(rpc.url)
        self.prices = prices
        self.fetch_attempts = fetch_attempts
        self.fetch_pause_s = fetch_pause_s
        self.watch: dict[str, str] = {w: w for w in sorted(set(wallets))}  # address -> followed wallet
        self._seen: OrderedDict[tuple[str, str], None] = OrderedDict()
        self._tasks: set[asyncio.Task] = set()
        self._last: dict[str, asyncio.Task] = {}  # wallet -> its latest read, which later reads wait for
        self._ws = None
        self._stop = False
        self._stopped = asyncio.Event()

    @property
    def host(self) -> str:
        """Safe to print: the URL itself usually holds the API key."""
        return urlparse(self.ws_url).hostname or "<invalid websocket URL>"

    def stop(self) -> None:
        self._stop = True
        self._stopped.set()
        if self._ws is not None:
            asyncio.get_running_loop().create_task(self._ws.close())

    async def events(self) -> AsyncIterator[Trade]:
        queue: asyncio.Queue = asyncio.Queue()
        runner = asyncio.create_task(self._run(queue))
        try:
            while True:
                item = await queue.get()
                if item is _DONE:
                    return
                yield item
        finally:
            runner.cancel()
            for t in list(self._tasks):
                t.cancel()
            await asyncio.gather(runner, *self._tasks, return_exceptions=True)

    async def _run(self, queue: asyncio.Queue) -> None:
        loop = asyncio.get_running_loop()
        backoff = 1.0
        try:
            while not self._stop:
                connected_at = None
                try:
                    async with websockets.connect(self.ws_url, ping_interval=20, max_size=2**22,
                                                  open_timeout=10) as ws:
                        self._ws = ws
                        connected_at = loop.time()
                        await self._listen(ws, queue)
                except websockets.InvalidURI:
                    log.error("the RPC websocket address for %s is not valid; not watching wallets through it", self.host)
                    return
                except (OSError, asyncio.TimeoutError, websockets.WebSocketException) as e:
                    if connected_at is not None and loop.time() - connected_at >= self.HEALTHY_S:
                        backoff = 1.0
                    log.warning("RPC websocket %s: connection lost (%s); reconnecting in %.0fs",
                                self.host, type(e).__name__, backoff)
                else:
                    backoff = 1.0
                finally:
                    self._ws = None
                if not self._stop:
                    try:  # a stop() during the wait ends it at once
                        await asyncio.wait_for(self._stopped.wait(), backoff)
                    except asyncio.TimeoutError:
                        pass
                    backoff = min(backoff * 2, 60.0)
        finally:
            await queue.put(_DONE)

    async def _listen(self, ws, queue: asyncio.Queue) -> None:
        if self._stop:  # stop() came while connecting
            return
        requests: dict[int, str] = {}
        subscriptions: dict[int, str] = {}  # subscription id -> watched address
        for n, address in enumerate(self.watch, 1):
            requests[n] = address
            await ws.send(json.dumps({"jsonrpc": "2.0", "id": n, "method": "logsSubscribe",
                                      "params": [{"mentions": [address]}, {"commitment": "confirmed"}]}))
        log.info("RPC websocket %s: watching %d wallets", self.host, len(set(self.watch.values())))
        async for raw in ws:
            try:
                msg = json.loads(raw)
            except json.JSONDecodeError:
                continue
            if not isinstance(msg, dict):
                continue
            if msg.get("id") in requests:
                address = requests.pop(msg["id"])
                if "result" in msg:
                    subscriptions[msg["result"]] = address
                else:
                    log.warning("RPC websocket %s refused to watch %s: %s", self.host, address, msg.get("error"))
                continue
            if msg.get("method") != "logsNotification":
                continue
            params = msg.get("params") or {}
            value = ((params.get("result") or {}).get("value")) or {}
            address = subscriptions.get(params.get("subscription"))
            sig = value.get("signature")
            if address is None or not sig or value.get("err") is not None:
                continue
            wallet = self.watch[address]
            if (sig, wallet) in self._seen:  # e.g. notified again after a reconnect
                continue
            self._seen[(sig, wallet)] = None
            if len(self._seen) > 10_000:
                self._seen.popitem(last=False)
            # Reads run in parallel, but each wallet's trades are reported in the order they were
            # notified: a sell that reads faster than the buy before it must not overtake it.
            task = asyncio.create_task(self._read(sig, wallet, queue, self._last.get(wallet)))
            self._last[wallet] = task
            self._tasks.add(task)
            task.add_done_callback(self._tasks.discard)
            task.add_done_callback(lambda t, w=wallet: self._last.get(w) is t and self._last.pop(w))
            if self._stop:
                return

    async def _read(self, sig: str, wallet: str, queue: asyncio.Queue, previous: asyncio.Task | None) -> None:
        trade = None
        try:
            trade = await self._read_one(sig, wallet)
        except Exception:  # noqa: BLE001 - one unreadable transaction must not stop the watcher
            log.exception("could not read %s's transaction %s", wallet, sig)
        finally:
            if previous is not None:  # wait for the wallet's earlier read however it ends
                await asyncio.wait([previous])
        if trade is not None and trade.side.is_trade:  # like PumpPortal: buys and sells only
            await queue.put(trade)

    async def _read_one(self, sig: str, wallet: str) -> Trade | None:
        tx = None
        for attempt in range(self.fetch_attempts):
            try:
                tx = await fetch_transaction(self.rpc, sig)
                break
            except TxNotFound:  # notified a moment before the node serves it
                await asyncio.sleep(self.fetch_pause_s * (attempt + 1))
            except (RpcHTTPError, RpcError) as e:
                log.warning("could not read %s's transaction %s: %s", wallet, sig, e)
                return None
        if tx is None:
            log.warning("transaction %s of %s never became readable", sig, wallet)
            return None
        try:
            view = analyze_wallet_tx(tx, wallet, sig)
        except (KeyError, TypeError, IndexError, ValueError) as e:
            log.warning("could not read transaction %s (%s)", sig, e)
            return None
        if view.trade is None and view.pending is not None:
            price = await self.prices.price_at(view.pending.ts) if self.prices is not None else None
            if not price:
                log.warning("%s traded %s in a stablecoin, but no SOL/USD price is available (%s)",
                            wallet, view.pending.mint, self.prices.problem if self.prices else "no price source")
                return None
            return value_in_sol(view, price)
        return view.trade


async def merge_events(*sources) -> AsyncIterator:
    """Events from several sources as they arrive. A source that fails is
    logged and dropped; the merge ends when every source has ended."""
    queue: asyncio.Queue = asyncio.Queue()

    async def pump(src) -> None:
        try:
            async for ev in src.events():
                await queue.put(ev)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - one broken source must not stop the others
            log.exception("%s stopped", type(src).__name__)
        finally:
            await queue.put(_DONE)

    tasks = [asyncio.create_task(pump(s)) for s in sources]
    running = len(tasks)
    try:
        while running:
            ev = await queue.get()
            if ev is _DONE:
                running -= 1
                continue
            yield ev
    finally:
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
