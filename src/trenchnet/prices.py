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

import logging
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
                 sources: Iterable[PriceSource] | None = None, now: Callable[[], float] = time.time) -> None:
        self.store = store
        self._http = http
        self.sources = list(sources if sources is not None else SOURCES)
        self.now = now
        self._cache: dict[int, float] = {}
        self._asked: set[int] = set()  # buckets already requested this run (incl. ones with no candle)
        self._loaded: set[int] = set()  # days already loaded from the database
        self._failures: dict[str, list[str]] = {}
        self._off: set[str] = set()  # sources given up on for this run
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

    async def prefetch(self, timestamps: Iterable[float]) -> None:
        """Download the candles needed for these times (in as few requests as
        possible). Never raises: if no source works, `problem` says why."""
        need = sorted({bucket(t) for t in timestamps})
        if not need:
            return
        self._load(need[0] - NEAREST_S, need[-1] + NEAREST_S)
        now = bucket(self.now())
        missing = [b for b in need if b not in self._cache and b not in self._asked and b <= now]
        while missing:
            source = next((s for s in self.sources if s.name not in self._off), None)
            if source is None:
                tried = "; ".join(f"{n}: {why[-1]}" for n, why in self._failures.items())
                if self.problem is None:
                    log.warning("no SOL/USD price source could be reached (%s)", tried)
                self.problem = f"no SOL/USD price source could be reached ({tried})"
                return
            start = missing[0]
            end = min(start + (source.max_candles - 1) * BUCKET_S, now)
            try:
                rows = await source.fetch(self.http, start, end)
            except Exception as e:  # noqa: BLE001 - any failure: try the next source
                self._failed(source, e)
                continue
            got = [(b, p, source.name) for b, p in rows if p > 0 and b % BUCKET_S == 0]
            self._cache.update((b, p) for b, p, _ in got)
            if self.store is not None and got:
                self.store.save_sol_usd(got)
            settled = bucket(self.now() - SETTLED_S)
            self._asked.update(b for b in range(start, end + 1, BUCKET_S) if b <= settled)
            missing = [b for b in missing if b > end]

    def _failed(self, source: PriceSource, e: Exception) -> None:
        why = _why(e)
        self._failures.setdefault(source.name, []).append(why)
        # A refusal (blocked, bad request) won't change during this run; network trouble gets 3 tries.
        refused = isinstance(e, httpx.HTTPStatusError) and 400 <= e.response.status_code < 500 \
            and e.response.status_code != 429
        if refused or len(self._failures[source.name]) >= 3 or not isinstance(e, httpx.HTTPError):
            self._off.add(source.name)
            log.info("SOL/USD prices: not using %s (%s)", source.name, why)
