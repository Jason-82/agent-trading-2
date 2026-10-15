"""Backfill a wallet's trading history from any Solana RPC endpoint.

Venue-agnostic: instead of decoding each DEX program, it reads the wallet's
own balance changes in each transaction.

* one non-SOL token up and SOL down  -> buy
* one non-SOL token down and SOL up  -> sell
* token moves with no matching SOL   -> transfer (never a sale)

Wrapped-SOL balance changes are counted as SOL. The network fee is excluded
when the wallet paid it. Token-account rent (paid when one of the wallet's
token accounts is created, refunded when it is closed) is excluded using that
account's actual lamports, so SPL and Token-2022 accounts are both handled.
SOL moves smaller than MIN_SOL_MOVE are treated as noise, so a transfer that
also pays a small tip is still a transfer. Transactions touching several
tokens at once are skipped rather than guessed. Tips paid alongside a real
trade remain inside its SOL amount.

Transactions of every version (legacy, v0, and v1 from Sept 2026) are read.
If a node can't return a transaction in readable form, or returns a layout
this parser doesn't know, the raw base64 form is fetched and decoded locally
with solders.

This makes one getTransaction call per signature, paced to the RPC's rate
limit; a free Helius key handles a 1,000-transaction wallet in a few minutes.
"""

from __future__ import annotations

import asyncio
import base64
import logging
from collections import Counter
from dataclasses import dataclass, field
from typing import Any, Callable

from ..models import Side, Trade
from ..solana_rpc import VERSION_UNSUPPORTED, LAMPORTS_PER_SOL, RpcError, RpcHTTPError, SolanaRpc

log = logging.getLogger(__name__)

WSOL = "So11111111111111111111111111111111111111112"
MIN_SOL_MOVE = 0.001


def _key(k: Any) -> str:
    return k["pubkey"] if isinstance(k, dict) else str(k)


def account_keys(tx: dict) -> list[str]:
    """All account addresses in index order, for any encoding.

    jsonParsed lists lookup-table addresses inside accountKeys already; the
    json and base64 encodings list only the message's own keys, and the
    loaded addresses (v0 lookup tables) follow them: writable, then read-only."""
    t = tx["transaction"]
    if isinstance(t, list):  # ["<base64>", "base64"]
        from solders.transaction import VersionedTransaction

        msg = VersionedTransaction.from_bytes(base64.b64decode(t[0])).message
        keys = [str(k) for k in msg.account_keys]
        parsed = False
    else:
        raw = t["message"]["accountKeys"]
        keys = [_key(k) for k in raw]
        parsed = bool(raw) and isinstance(raw[0], dict)
    if not parsed:
        loaded = (tx.get("meta") or {}).get("loadedAddresses") or {}
        keys += list(loaded.get("writable") or []) + list(loaded.get("readonly") or [])
    return keys


def _token_amounts(balances: list[dict] | None, owner: str) -> dict[str, float]:
    out: dict[str, float] = {}
    for b in balances or []:
        if b.get("owner") != owner:
            continue
        amt = b.get("uiTokenAmount") or {}
        v = amt.get("uiAmountString") or amt.get("uiAmount") or 0
        out[b["mint"]] = out.get(b["mint"], 0.0) + float(v)
    return out


def _owned_accounts(balances: list[dict] | None, owner: str) -> set[int]:
    return {b["accountIndex"] for b in balances or [] if b.get("owner") == owner and "accountIndex" in b}


# Why a transaction produced no trade, in plain words (used in summaries and `trenchnet inspect`).
SKIP_REASONS = {
    "failed": "transaction failed",
    "not_in_tx": "wallet not in the transaction",
    "no_token_change": "no token balance change (SOL transfers, fees, other activity)",
    "several_tokens": "several tokens changed at once (multi-token swap or batch)",
    "no_time": "no block time",
    "unrelated_transfer": "tokens received or sent that the wallet never bought or sold (usually spam airdrops)",
}


def drop_unrelated_transfers(res: "BackfillResult") -> None:
    """Keep transfers only for tokens the wallet also bought or sold in the
    downloaded history. Well-known wallets receive a steady stream of spam
    tokens; those aren't trading activity."""
    traded = {t.mint for t in res.trades if t.side.is_trade}
    kept = [t for t in res.trades if t.side.is_trade or t.mint in traded]
    dropped = len(res.trades) - len(kept)
    if dropped:
        res.trades = kept
        res.ignored["unrelated_transfer"] += dropped


@dataclass
class TxView:
    """What one transaction did to one wallet, as the parser sees it."""

    status: str  # "trade" or a key of SKIP_REASONS
    sol_delta: float = 0.0  # SOL the wallet gained (+) or spent (-), excluding fee and token-account rent
    token_deltas: dict[str, float] = field(default_factory=dict)  # mint -> change, wallet-owned accounts, excl. WSOL
    trade: Trade | None = None


def analyze_wallet_tx(tx: dict | None, wallet: str, signature: str) -> TxView:
    if not tx or not tx.get("meta") or tx["meta"].get("err") is not None:
        return TxView("failed")
    meta = tx["meta"]
    keys = account_keys(tx)
    if wallet not in keys:
        return TxView("not_in_tx")
    i = keys.index(wallet)
    native = (meta["postBalances"][i] - meta["preBalances"][i]) / LAMPORTS_PER_SOL
    if i == 0:
        native += meta.get("fee", 0) / LAMPORTS_PER_SOL
    sol_delta = native

    pre = _token_amounts(meta.get("preTokenBalances"), wallet)
    post = _token_amounts(meta.get("postTokenBalances"), wallet)
    sol_delta += post.pop(WSOL, 0.0) - pre.pop(WSOL, 0.0)

    # Rent for the wallet's token accounts created or closed in this tx is not
    # part of the trade.
    # Only rent the wallet itself paid or got back counts: spam senders often
    # pay to create the token account they drop tokens into.
    pre_accts = _owned_accounts(meta.get("preTokenBalances"), wallet)
    post_accts = _owned_accounts(meta.get("postTokenBalances"), wallet)
    created = sum(meta["postBalances"][idx] for idx in post_accts - pre_accts) / LAMPORTS_PER_SOL
    closed = sum(meta["preBalances"][idx] for idx in pre_accts - post_accts) / LAMPORTS_PER_SOL
    if created and native <= -created + 1e-9:  # the wallet paid to open its token account
        sol_delta += created
    if closed and native >= closed - 1e-9:  # the refund from closing it came back to the wallet
        sol_delta -= closed

    deltas = {m: post.get(m, 0.0) - pre.get(m, 0.0) for m in set(pre) | set(post)}
    deltas = {m: d for m, d in deltas.items() if abs(d) > 0}
    if not deltas:
        return TxView("no_token_change", sol_delta)
    if len(deltas) > 1:
        return TxView("several_tokens", sol_delta, deltas)
    mint, d = next(iter(deltas.items()))

    if d > 0 and sol_delta < -MIN_SOL_MOVE:
        side, sol = Side.BUY, -sol_delta
    elif d < 0 and sol_delta > MIN_SOL_MOVE:
        side, sol = Side.SELL, sol_delta
    elif d > 0:
        side, sol = Side.TRANSFER_IN, 0.0
    else:
        side, sol = Side.TRANSFER_OUT, 0.0
    ts = tx.get("blockTime")
    if ts is None:
        return TxView("no_time", sol_delta, deltas)
    trade = Trade(signature=signature, wallet=wallet, mint=mint, side=side, sol_amount=sol,
                  token_amount=abs(d), ts=float(ts), source="rpc")
    return TxView("trade", sol_delta, deltas, trade)


def parse_wallet_tx(tx: dict | None, wallet: str, signature: str) -> list[Trade]:
    view = analyze_wallet_tx(tx, wallet, signature)
    return [view.trade] if view.trade is not None else []


BREAKER = 10  # this many transactions in a row failing at the HTTP level: stop instead of grinding for hours


@dataclass
class BackfillResult:
    trades: list[Trade] = field(default_factory=list)  # kept even when the wallet stops early
    transactions: int = 0  # successful transactions examined
    unreadable: Counter = field(default_factory=Counter)  # reason -> count, after retries
    not_attempted: int = 0  # signatures left unread because the backfill stopped early
    ignored: Counter = field(default_factory=Counter)  # read fine but no trade: SKIP_REASONS key -> count
    stopped: RpcHTTPError | None = None  # why it stopped early (fatal error or circuit breaker)


async def fetch_transaction(rpc: SolanaRpc, signature: str) -> dict | None:
    """jsonParsed when the node can produce it; otherwise the raw base64 form
    (needed for large v1 transactions)."""
    try:
        return await rpc.get_transaction(signature)
    except RpcError as e:
        if e.code == VERSION_UNSUPPORTED:
            raise
        return await rpc.get_transaction(signature, encoding="base64")


def _reason(e: Exception) -> str:
    if isinstance(e, RpcHTTPError):
        return f"HTTP {e.status}" if e.status else "network error"
    if isinstance(e, RpcError):
        return f"RPC error {e.code}"
    return type(e).__name__


async def _list_signatures(rpc: SolanaRpc, wallet: str, limit: int, before: str | None,
                           retry_pause_s: float) -> list[dict]:
    try:
        return await rpc.get_signatures(wallet, limit=limit, before=before)
    except RpcHTTPError as e:
        if e.fatal:
            raise
        await asyncio.sleep(retry_pause_s)  # one more try after a pause, like transactions get
        return await rpc.get_signatures(wallet, limit=limit, before=before)


async def backfill_wallet(rpc: SolanaRpc, wallet: str, max_signatures: int = 500,
                          progress: Callable[[int, int, int], None] | None = None,
                          retry_pause_s: float = 10.0, result: BackfillResult | None = None) -> BackfillResult:
    """Read up to `max_signatures` of the wallet's most recent successful
    transactions.

    Transactions that fail temporarily (rate limits, network) are retried
    once at the end. The backfill stops early, keeping what it has read, on
    a fatal error (rejected key/URL, credits used up) or after BREAKER
    transactions in a row fail at the HTTP level; `result.stopped` says why.
    Pass `result` to keep partial trades if the caller is cancelled.
    Failing to list the wallet's transactions at all raises RpcHTTPError or
    RpcError."""
    res = result if result is not None else BackfillResult()
    sigs: list[str] = []
    before = None
    while len(sigs) < max_signatures:
        page = await _list_signatures(rpc, wallet, min(1000, max_signatures - len(sigs)), before, retry_pause_s)
        if not page:
            break
        sigs += [s["signature"] for s in page if s.get("err") is None]
        before = page[-1]["signature"]
        if len(page) < 1000:
            break
    sigs = sigs[:max_signatures]
    warned: set[str] = set()

    async def read(batch: list[str], show_progress: bool) -> dict[str, Exception]:
        failed: dict[str, Exception] = {}
        streak = 0
        for n, sig in enumerate(batch, 1):
            try:
                tx = await fetch_transaction(rpc, sig)
                try:
                    view = analyze_wallet_tx(tx, wallet, sig)
                except (KeyError, TypeError, IndexError, ValueError):
                    # Unfamiliar readable layout (a new transaction format?): decode the raw form instead.
                    view = analyze_wallet_tx(await rpc.get_transaction(sig, encoding="base64"), wallet, sig)
                res.transactions += 1
                if view.trade is not None:
                    res.trades.append(view.trade)
                else:
                    res.ignored[view.status] += 1
            except RpcHTTPError as e:
                if e.fatal:
                    res.stopped = e
                    res.not_attempted += len(batch) - n + 1
                    return failed
                failed[sig] = e
                streak += 1
                if streak >= BREAKER:
                    res.stopped = RpcHTTPError(e.status, f"{BREAKER} transactions in a row failed ({e})")
                    res.not_attempted += len(batch) - n
                    return failed
            except Exception as e:  # noqa: BLE001 - one bad transaction should not stop a backfill
                failed[sig] = e
            else:
                streak = 0
            if sig in failed:
                reason = _reason(failed[sig])
                if reason not in warned:  # one line per kind of problem, not one per transaction
                    warned.add(reason)
                    log.warning("some transactions could not be read (%s), e.g. %s: %s", reason, sig, failed[sig])
            if progress and show_progress:
                progress(n, len(batch), len(res.trades))
        return failed

    failed = await read(sigs, show_progress=True)
    transient = [s for s, e in failed.items() if isinstance(e, RpcHTTPError)]
    if transient and res.stopped is None:
        log.info("retrying %d transactions after a pause", len(transient))
        await asyncio.sleep(retry_pause_s)
        for s in transient:
            failed.pop(s)
        failed.update(await read(transient, show_progress=False))
    res.unreadable = Counter(_reason(e) for e in failed.values())
    drop_unrelated_transfers(res)
    return res
