"""Find wallets that trade through the Fomo app, from public blockchain data.

Fomo doesn't show its traders' wallet addresses, but every Fomo trade is an
ordinary Solana transaction. Fomo pays the network fee for its users from
one sponsor wallet and takes its app fee in USDC into one token account, so
listing that fee account's recent transactions gives a sample of Fomo
trades. In each one, the trader is the owner of the USDC account the fee
was paid from (or, failing that, whoever signed besides the sponsor).

Only public on-chain data is read, through your own RPC. Nothing is fetched
from Fomo's app or website, whose terms forbid scraping and looking up other
users. Names are not available this way: the result is a list of wallet
addresses ranked by how much they traded in the sample. Whether they trade
*well* is decided later by `trenchnet profile`, from their full history.

The addresses below are the ones DefiLlama's open-source Fomo volume adapter
uses (dexs/fomo). If Fomo changes them, pass another account with --account.
"""

from __future__ import annotations

import logging
from collections import Counter
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Callable, Iterable, Iterator

from .models import Side
from .solana_rpc import RpcError, RpcHTTPError, SolanaRpc
from .sources.rpc_backfill import (BREAKER, QUOTE_MINTS, _list_address, _reason, account_keys, analyze_wallet_tx,
                                   fetch_transaction)
from .textfile import read_text_any

if TYPE_CHECKING:
    from .prices import SolUsdPrices

log = logging.getLogger(__name__)

FOMO_GAS_SPONSOR = "AgmLJBMDCqWynYnQiPCuj9ewsNNsBJXyzoUhD9LJzN51"  # pays the network fee of every Fomo transaction
FOMO_FEE_WALLET = "R4rNJHaffSUotNmqSKNEfDcJE8A7zJUkaoM5Jkd7cYX"  # receives Fomo's app fee
FOMO_FEE_ACCOUNT = "HrTf9CzXR1dRH4Sof5QrpmGWwpwAf3qZzwCsEjQpXcSq"  # its USDC account: in every Fomo trade
FOMO_RELAY_VAULT = "7uTT8Xi5RWXzy7h9XL244GRgEycDYDhLjr3ZyNdXi8pZ"  # cross-chain deposits (Relay)
FOMO_ACCOUNTS = {FOMO_GAS_SPONSOR, FOMO_FEE_WALLET, FOMO_FEE_ACCOUNT, FOMO_RELAY_VAULT}


@dataclass
class Trader:
    wallet: str
    trades: int = 0
    buys: int = 0
    sells: int = 0
    usd: float = 0.0  # volume in the sample, in dollars (SOL trades converted when a price is known)
    tokens: set[str] = field(default_factory=set)
    first_ts: float | None = None
    last_ts: float | None = None


@dataclass
class Discovery:
    listed: int = 0  # recent transactions of the fee account(s)
    sampled: int = 0
    read: int = 0
    fomo_trades: int = 0  # transactions with an identified trader who bought or sold
    no_trade: int = 0  # read, but nobody in it bought or sold a token (deposits, withdrawals, ...)
    unreadable: Counter = field(default_factory=Counter)
    found_by: Counter = field(default_factory=Counter)  # "fee" / "signer"
    traders: dict[str, Trader] = field(default_factory=dict)
    span: tuple[float, float] | None = None  # time range of the transactions read
    stopped: RpcHTTPError | None = None

    def ranked(self) -> list[Trader]:
        return sorted(self.traders.values(), key=lambda t: (t.usd, t.trades), reverse=True)


def _instructions(tx: dict) -> Iterator[dict]:
    t = tx.get("transaction")
    if not isinstance(t, dict):
        return
    yield from (t.get("message") or {}).get("instructions") or []
    for group in (tx.get("meta") or {}).get("innerInstructions") or []:
        yield from group.get("instructions") or []


def _signers(tx: dict) -> list[str]:
    t = tx.get("transaction")
    if not isinstance(t, dict):
        return []
    return [k["pubkey"] for k in (t.get("message") or {}).get("accountKeys") or []
            if isinstance(k, dict) and k.get("signer")]


def find_traders(tx: dict, fee_accounts: Iterable[str] = (FOMO_FEE_ACCOUNT,),
                 fee_owners: Iterable[str] = (FOMO_FEE_WALLET,)) -> tuple[list[str], str | None]:
    """The trader(s) in one Fomo transaction and how they were recognised:
    "fee" (owner of the token account Fomo's fee was paid from) or "signer"
    (signed it besides Fomo's sponsor)."""
    keys = account_keys(tx)
    meta = tx.get("meta") or {}
    owner_of: dict[str, str] = {}  # token account -> owner
    for b in (meta.get("preTokenBalances") or []) + (meta.get("postTokenBalances") or []):
        i = b.get("accountIndex")
        if b.get("owner") and isinstance(i, int) and i < len(keys):
            owner_of[keys[i]] = b["owner"]
    fee_owners = set(fee_owners)
    destinations = set(fee_accounts) | {a for a, o in owner_of.items() if o in fee_owners}
    skip = FOMO_ACCOUNTS | fee_owners
    found: list[str] = []
    for ix in _instructions(tx):
        parsed = ix.get("parsed")
        if not isinstance(parsed, dict) or parsed.get("type") not in ("transfer", "transferChecked"):
            continue
        info = parsed.get("info") or {}
        if info.get("destination") not in destinations:
            continue
        payer = owner_of.get(info.get("source", "")) or info.get("authority") or info.get("multisigAuthority")
        if payer and payer not in skip and payer not in found:
            found.append(payer)
    if found:
        return found, "fee"
    signers = [s for s in _signers(tx) if s not in skip]
    return (signers, "signer") if signers else ([], None)


def sample_evenly(items: list, n: int) -> list:
    if n >= len(items):
        return list(items)
    step = len(items) / n
    return [items[int(i * step)] for i in range(n)]


async def discover(rpc: SolanaRpc, accounts: Iterable[str] = (FOMO_FEE_ACCOUNT,), pages: int = 3,
                   sample: int = 300, prices: "SolUsdPrices | None" = None,
                   progress: Callable[[int, int, int], None] | None = None,
                   retry_pause_s: float = 5.0) -> Discovery:
    """List up to `pages` x 1,000 recent transactions of Fomo's fee account(s),
    read `sample` of them spread evenly over that period, and collect who
    traded. Listing failures raise RpcHTTPError / RpcError; a fatal error while
    reading stops early with what was found (`stopped`)."""
    accounts = list(accounts)
    out = Discovery()
    listed: list[dict] = []
    seen: set[str] = set()
    for acc in accounts:
        for s in await _list_address(rpc, acc, pages * 1000, retry_pause_s, successful_only=True):
            if s["signature"] not in seen:
                seen.add(s["signature"])
                listed.append(s)
    out.listed = len(listed)
    chosen = sample_evenly(listed, sample)
    out.sampled = len(chosen)
    times: list[float] = []
    pending_usd: list[tuple[Trader, float, float]] = []  # SOL trades to convert: (trader, SOL, time)
    streak = 0  # HTTP-level failures in a row: stop instead of grinding through an outage for hours
    for n, s in enumerate(chosen, 1):
        sig = s["signature"]
        try:
            tx = await fetch_transaction(rpc, sig)
        except RpcHTTPError as e:
            if e.fatal:
                out.stopped = e
                break
            out.unreadable[_reason(e)] += 1
            streak += 1
            if streak >= BREAKER:
                out.stopped = RpcHTTPError(e.status, f"{BREAKER} transactions in a row failed ({e})")
                break
            if progress:
                progress(n, len(chosen), len(out.traders))
            continue
        except RpcError as e:
            out.unreadable[_reason(e)] += 1
            if progress:
                progress(n, len(chosen), len(out.traders))
            continue
        streak = 0
        out.read += 1
        if tx.get("blockTime"):
            times.append(float(tx["blockTime"]))
        wallets, how = find_traders(tx, fee_accounts={FOMO_FEE_ACCOUNT, *accounts})
        traded = False
        for w in wallets:
            view = analyze_wallet_tx(tx, w, sig)
            t = view.trade or view.pending
            if t is None or not t.side.is_trade:
                continue
            traded = True
            tr = out.traders.setdefault(w, Trader(w))
            tr.trades += 1
            tr.buys += t.side is Side.BUY
            tr.sells += t.side is Side.SELL
            tr.tokens.add(t.mint)
            tr.first_ts = t.ts if tr.first_ts is None else min(tr.first_ts, t.ts)
            tr.last_ts = t.ts if tr.last_ts is None else max(tr.last_ts, t.ts)
            if t.quote_mint in QUOTE_MINTS:
                tr.usd += t.quote_amount or 0.0
            else:
                pending_usd.append((tr, t.sol_amount, t.ts))
        if traded:
            out.fomo_trades += 1
            out.found_by[how] += 1
        else:
            out.no_trade += 1
        if progress:
            progress(n, len(chosen), len(out.traders))
    if times:
        out.span = (min(times), max(times))
    if pending_usd and prices is not None:
        await prices.prefetch(ts for _, _, ts in pending_usd)
        for tr, sol, ts in pending_usd:
            price = prices.get(ts)
            if price:
                tr.usd += sol * price
    return out


def merge_wallet_file(path: str, traders: list[Trader], note: Callable[[Trader], str]) -> tuple[int, int]:
    """Add traders to a wallets file (address,nickname per line), keeping what
    is already there; the file is (re)written as UTF-8. Returns (added,
    already present). Raises OSError if the file can't be read or written."""
    from pathlib import Path

    p = Path(path)
    existing = read_text_any(p) if p.exists() else ""
    have = set()
    for line in existing.splitlines():
        words = line.split("#", 1)[0].replace(",", " ").split()
        if words:
            have.add(words[0].lstrip("\ufeff"))
    new = [t for t in traders if t.wallet not in have]
    if new:
        lines = [f"{t.wallet},fomo-{t.wallet[:4]}  # {note(t)}" for t in new]
        sep = "" if not existing or existing.endswith("\n") else "\n"
        p.write_text(existing + sep + "\n".join(lines) + "\n", encoding="utf-8")
    return len(new), len(traders) - len(new)
