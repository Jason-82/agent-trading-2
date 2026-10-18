"""Live data from PumpPortal's websocket.

* ``subscribeAccountTrade`` streams every trade by the roster wallets.
* ``subscribeTokenTrade`` streams all trades on tokens the bot holds, used
  only as price ticks for exits.

PumpPortal meters account/token trade subscriptions; they require an API key
(``PUMPPORTAL_API_KEY``) linked to a funded PumpPortal wallet.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import AsyncIterator, Iterable

import websockets

from ..config import PumpPortalConfig
from ..models import PriceTick, Side, Trade

log = logging.getLogger(__name__)


def parse_message(msg: dict, tracked: set[str], now: float) -> list[Trade | PriceTick]:
    tx = msg.get("txType")
    if tx not in ("buy", "sell"):
        return []  # subscription acks, token creations, errors
    try:
        mint = msg["mint"]
        sol = float(msg.get("solAmount") or 0)
        tokens = float(msg.get("tokenAmount") or 0)
    except (KeyError, TypeError, ValueError):
        return []
    mcap = msg.get("marketCapSol")
    mcap = float(mcap) if mcap is not None else None
    v_sol, v_tok = msg.get("vSolInBondingCurve"), msg.get("vTokensInBondingCurve")
    if v_sol and v_tok:
        curve_price = float(v_sol) / float(v_tok)
    elif sol > 0 and tokens > 0:
        curve_price = sol / tokens
    else:
        curve_price = None

    out: list[Trade | PriceTick] = []
    if curve_price:
        out.append(PriceTick(mint=mint, price_sol=curve_price, ts=now, market_cap_sol=mcap))
    wallet = msg.get("traderPublicKey")
    if wallet in tracked and msg.get("signature"):
        out.append(Trade(
            signature=msg["signature"], wallet=wallet, mint=mint, side=Side(tx), sol_amount=sol,
            token_amount=tokens, ts=now, market_cap_sol=mcap, source=f"pumpportal:{msg.get('pool', 'pump')}",
        ))
    return out


class PumpPortalSource:
    def __init__(self, cfg: PumpPortalConfig, wallets: Iterable[str], api_key: str | None = None) -> None:
        self.cfg = cfg
        self.wallets = sorted(set(wallets))
        self.tracked = set(self.wallets)
        self.api_key = api_key
        self._tokens: set[str] = set()
        self._ws = None
        self._stop = False
        self._stopped = asyncio.Event()
        self._reported: set[str] = set()  # PumpPortal errors already explained (reconnects repeat them)

    @property
    def url(self) -> str:
        return f"{self.cfg.ws_url}?api-key={self.api_key}" if self.api_key else self.cfg.ws_url

    async def _send(self, payload: dict) -> None:
        if self._ws is None:
            return
        try:
            await self._ws.send(json.dumps(payload))
        except (OSError, websockets.WebSocketException) as e:
            # _tokens is already updated; the reconnect loop resubscribes.
            log.warning("pumpportal send failed (%s); will resubscribe on reconnect", e)

    async def watch_tokens(self, mints: Iterable[str]) -> None:
        new = set(mints) - self._tokens
        if new:
            self._tokens |= new
            await self._send({"method": "subscribeTokenTrade", "keys": sorted(new)})

    async def unwatch_tokens(self, mints: Iterable[str]) -> None:
        gone = set(mints) & self._tokens
        if gone:
            self._tokens -= gone
            await self._send({"method": "unsubscribeTokenTrade", "keys": sorted(gone)})

    def stop(self) -> None:
        self._stop = True
        self._stopped.set()
        if self._ws is not None:
            asyncio.get_running_loop().create_task(self._ws.close())

    def _redact(self, text: str) -> str:
        return text.replace(self.api_key, "<api key>") if self.api_key else text

    def _report(self, error: object) -> None:
        """Explain a PumpPortal error in plain words, once per kind."""
        text = self._redact(str(error))
        if text in self._reported:
            return
        self._reported.add(text)
        low = text.lower()
        if "balance" in low:
            log.warning("PumpPortal: %s\n  The wallet linked to your PUMPPORTAL_API_KEY holds less than PumpPortal's "
                        "minimum (0.02 SOL), so PumpPortal sends no trades or prices. Send about 0.05 SOL to that "
                        "wallet (the walletPublicKey you saved when you created the key) and restart. Until then "
                        "the bot only sees what your RPC reports (if SOLANA_RPC_URL is set).", text)
        elif "key" in low or "auth" in low:
            log.warning("PumpPortal: %s\n  Check PUMPPORTAL_API_KEY in .env: it should be the apiKey value PumpPortal "
                        "gave you.", text)
        else:
            log.warning("PumpPortal: %s", text)

    async def events(self) -> AsyncIterator[Trade | PriceTick]:
        backoff = 1.0
        while not self._stop:
            try:
                async with websockets.connect(self.url, ping_interval=20, max_size=2**22) as ws:
                    self._ws = ws
                    await self._send({"method": "subscribeAccountTrade", "keys": self.wallets})
                    if self._tokens:
                        await self._send({"method": "subscribeTokenTrade", "keys": sorted(self._tokens)})
                    log.info("pumpportal connected; tracking %d wallets", len(self.wallets))
                    backoff = 1.0
                    async for raw in ws:
                        try:
                            msg = json.loads(raw)
                        except json.JSONDecodeError:
                            continue
                        if not isinstance(msg, dict):
                            continue
                        if "errors" in msg or "error" in msg:
                            self._report(msg.get("errors") or msg.get("error"))
                        for ev in parse_message(msg, self.tracked, time.time()):
                            yield ev
                        if self._stop:
                            return
            except (OSError, websockets.WebSocketException) as e:
                log.warning("pumpportal connection lost (%s); reconnecting in %.0fs", self._redact(str(e)), backoff)
            finally:
                self._ws = None
            if not self._stop:
                try:  # a stop() during the wait ends it at once
                    await asyncio.wait_for(self._stopped.wait(), backoff)
                except asyncio.TimeoutError:
                    pass
                backoff = min(backoff * 2, 60.0)
