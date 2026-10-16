"""SOL/USD at a moment in the past, for trades paid in stablecoins.

Fomo trades, and pump.fun coins paired with USDC, are bought and sold for
USDC rather than SOL. Everything else here counts in SOL, so such a trade is
valued in SOL at the SOL/USD price of its time. Stablecoins count as exactly
$1.

Prices are 5-minute candles from public exchange APIs that need no key,
tried in order (Coinbase, then Binance, then Binance.US) so a site that is
down or blocked in your country is skipped. Every candle fetched is kept in
the database, so each period is downloaded only once.
"""

from __future__ import annotations

import asyncio
import logging
import math
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Awaitable, Callable, Iterable

import httpx

if TYPE_CHECKING:
    from .store import Store

log = logging.getLogger(__name__)

BUCKET_S = 300  # 5-minute candles
NEAREST_S = 1800  # if a candle is missing, use one up to 30 minutes away
SETTLED_S = 900  # candles older than this won't appear later: don't ask for them again

Fetch = Callable[[httpx.AsyncClient, int, int], Awaitable[list[tuple[int, float]]]]


def bucket(ts: float) -> int:
    return int(ts // BUCKET_S) * BUCKET_S


def _iso(ts: int) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _mid(open_: object, close: object) -> float:
    return (float(open_) + float(close)) / 2  # type: ignore[arg-type]


async def _coinbase(http: httpx.AsyncClient, start: int, end: int) -> list[tuple[int, float]]:
    r = await http.get("https://api.exchange.coinbase.com/products/SOL-USD/candles",
                       params={"granularity": BUCKET_S, "start": _iso(start), "end": _iso(end)})
    r.raise_for_status()
    # [[time, low, high, open, close, volume], ...], newest first
    return [(int(c[0]), _mid(c[3], c[4])) for c in r.json()]


async def _coinbase_advanced(http: httpx.AsyncClient, start: int, end: int) -> list[tuple[int, float]]:
    r = await http.get("https://api.coinbase.com/api/v3/brokerage/market/products/SOL-USD/candles",
                       params={"granularity": "FIVE_MINUTE", "start": str(start), "end": str(end)})
    r.raise_for_status()
    return [(int(c["start"]), _mid(c["open"], c["close"])) for c in r.json()["candles"]]


def _binance(base: str, symbol: str) -> Fetch:
    async def fetch(http: httpx.AsyncClient, start: int, end: int) -> list[tuple[int, float]]:
        r = await http.get(f"{base}/api/v3/klines", params={
            "symbol": symbol, "interval": "5m", "startTime": start * 1000, "endTime": end * 1000 + 999,
            "limit": 1000})
        r.raise_for_status()
        # [[open time ms, open, high, low, close, ...], ...]
        return [(int(k[0]) // 1000, _mid(k[1], k[4])) for k in r.json()]
    return fetch


@dataclass(frozen=True)
class PriceSource:
    name: str
    max_candles: int  # per request
    fetch: Fetch


SOURCES = (
    PriceSource("coinbase", 300, _coinbase),
    PriceSource("coinbase-advanced", 350, _coinbase_advanced),
    PriceSource("binance", 1000, _binance("https://data-api.binance.vision", "SOLUSDT")),
    PriceSource("binance.us", 1000, _binance("https://api.binance.us", "SOLUSDT")),
)


def _refused(e: Exception) -> bool:
    """An answer that won't change during this run: blocked in this country (403/451) or a bad request."""
    if not isinstance(e, httpx.HTTPStatusError):
        return False
    code = e.response.status_code
    return 400 <= code < 500 and code not in (408, 425, 429)


def _why(e: Exception) -> str:
    if isinstance(e, httpx.HTTPStatusError):
        code = e.response.status_code
        return f"HTTP {code}" + (" (not available from your location)" if code in (403, 451) else "")
    if isinstance(e, httpx.TransportError):
        return f"could not connect ({type(e).__name__})"
    return f"unexpected answer ({type(e).__name__})"


class SolUsdPrices:
    """Look up SOL/USD for past times; fetch what's needed first with prefetch()."""

    def __init__(self, store: "Store | None" = None, http: httpx.AsyncClient | None = None,
                 sources: Iterable[PriceSource] | None = None, now: Callable[[], float] = time.time,
                 monotonic: Callable[[], float] = time.monotonic, retry_pause_s: float = 2.0) -> None:
        self.store = store
        self._http = http
        self.sources = list(sources if sources is not None else SOURCES)
        self.now = now
        self.monotonic = monotonic
        self.retry_pause_s = retry_pause_s
        self._cache: dict[int, float] = {}
        self._asked: set[int] = set()  # buckets already requested this run (incl. ones with no candle)
        self._loaded: set[int] = set()  # days already loaded from the database
        self._fails: dict[str, int] = {}  # failures in a row, per source
        self._until: dict[str, float] = {}  # source resting until this monotonic time (inf: not this run)
        self._why: dict[str, str] = {}  # last failure, per source
        self.problem: str | None = None  # why prices are missing, for the user

    @property
    def http(self) -> httpx.AsyncClient:
        if self._http is None:
            self._http = httpx.AsyncClient(timeout=15.0, headers={"User-Agent": "trenchnet"})
        return self._http

    async def aclose(self) -> None:
        if self._http is not None:
            await self._http.aclose()

    def _load(self, lo: int, hi: int) -> None:
        if self.store is None:
            return
        days = range(lo // 86400, hi // 86400 + 1)
        if all(d in self._loaded for d in days):
            return
        self._cache.update(self.store.sol_usd(days[0] * 86400, (days[-1] + 1) * 86400))
        self._loaded.update(days)

    def get(self, ts: float) -> float | None:
        """SOL/USD at `ts` from what is already cached, or None."""
        b = bucket(ts)
        self._load(b - NEAREST_S, b + NEAREST_S)
        for k in range(NEAREST_S // BUCKET_S + 1):
            for c in (b - k * BUCKET_S, b + k * BUCKET_S):
                p = self._cache.get(c)
                if p:
                    return p
        return None

    async def price_at(self, ts: float) -> float | None:
        await self.prefetch([ts])
        return self.get(ts)

    def _usable(self) -> list[PriceSource]:
        t = self.monotonic()
        return [s for s in self.sources if self._until.get(s.name, 0.0) <= t]

    async def prefetch(self, timestamps: Iterable[float]) -> None:
        """Download the candles needed for these times (in as few requests as
        possible). Never raises: if no source works, `problem` says why, and
        the next call tries again (a source that failed rests a while first)."""
        need = sorted({bucket(t) for t in timestamps})
        if not need:
            return
        self._load(need[0] - NEAREST_S, need[-1] + NEAREST_S)
        now = max(bucket(self.now()), need[-1])  # a block's time can be ahead of this PC's clock
        missing = [b for b in need if b not in self._cache and b not in self._asked and b <= now]
        while missing:
            start = missing[0]
            if start > bucket(self.now() - SETTLED_S):
                # A recent trade: its candle may not be published yet, so also fetch the half hour
                # before it for get()'s nearest-candle fallback (still one request).
                start -= NEAREST_S
            end = await self._fetch_window(start, now)
            if end is None:
                why = "; ".join(f"{n}: {w}" for n, w in self._why.items())
                if self.problem is None:
                    log.warning("no SOL/USD price source could be reached (%s)", why)
                self.problem = f"no SOL/USD price source could be reached ({why})"
                return
            missing = [b for b in missing if b > end]

    async def _fetch_window(self, start: int, now: int) -> int | None:
        """One window of candles from the first source that answers (two rounds, a short pause
        between). Returns the window's last bucket, or None if no source answered."""
        for round_ in range(2):
            for source in self._usable():
                end = min(start + (source.max_candles - 1) * BUCKET_S, now)
                try:
                    rows = await source.fetch(self.http, start, end)
                except Exception as e:  # noqa: BLE001 - any failure: try the next source
                    self._failed(source, e)
                    continue
                self._fails.pop(source.name, None)
                self._until.pop(source.name, None)
                self._why.pop(source.name, None)
                self.problem = None
                got = [(b, p, source.name) for b, p in rows if p > 0 and b % BUCKET_S == 0]
                self._cache.update((b, p) for b, p, _ in got)
                if self.store is not None and got:
                    self.store.save_sol_usd(got)
                settled = bucket(self.now() - SETTLED_S)
                self._asked.update(b for b in range(start, end + 1, BUCKET_S) if b <= settled)
                return end
            resting = [s for s in self.sources if self._until.get(s.name, 0.0) < math.inf]
            if round_ == 0 and resting:
                await asyncio.sleep(self.retry_pause_s)
        return None

    def _failed(self, source: PriceSource, e: Exception) -> None:
        why = self._why[source.name] = _why(e)
        if _refused(e):
            self._until[source.name] = math.inf
            log.info("SOL/USD prices: not using %s this run (%s)", source.name, why)
            return
        # Network trouble, rate limits, server errors, odd answers (a Wi-Fi login page): temporary.
        # After two in a row the source rests 1, 2, 4 ... up to 15 minutes before it is asked again.
        n = self._fails[source.name] = self._fails.get(source.name, 0) + 1
        if n >= 2:
            self._until[source.name] = self.monotonic() + min(60.0 * 2 ** (n - 2), 900.0)
