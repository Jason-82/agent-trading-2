"""Backfill a wallet's trading history from any Solana RPC endpoint.

Venue-agnostic: instead of decoding each DEX program, it reads the wallet's
own balance changes in each transaction.

* one token up and SOL down  -> buy
* one token down and SOL up  -> sell
* the same with USDC/USDT/USD1 instead of SOL -> a buy or sell priced in that
  stablecoin, valued in SOL at the SOL/USD price of its time (see prices.py)
* token moves with no matching payment  -> transfer (never a sale)

Wrapped-SOL balance changes are counted as SOL. The network fee is excluded
when the wallet paid it. Token-account rent (paid when one of the wallet's
token accounts is created, refunded when it is closed) is excluded using that
account's actual lamports, so SPL and Token-2022 accounts are both handled.
SOL moves smaller than MIN_SOL_MOVE are treated as noise, so a transfer that
also pays a small tip is still a transfer. Transactions touching several
tokens at once are skipped rather than guessed. Tips paid alongside a real
trade remain inside its SOL amount.

Some apps (Fomo, for one) send their users' trades with a sponsor wallet that
pays the network fee. The trader still co-signs, so the trade is in the
wallet's own history; the sponsor's fee simply isn't part of the trade.

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
from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING, Any, Callable, Iterable

from ..models import Side, Trade
from ..solana_rpc import VERSION_UNSUPPORTED, LAMPORTS_PER_SOL, RpcError, RpcHTTPError, SolanaRpc

if TYPE_CHECKING:
    from ..prices import SolUsdPrices

log = logging.getLogger(__name__)

WSOL = "So11111111111111111111111111111111111111112"
MIN_SOL_MOVE = 0.001  # a buy must spend at least this much SOL (else it's a transfer in)
MIN_SELL_PROCEEDS = 0.0001  # without a known swap program, smaller "proceeds" are rent noise on a transfer
MAX_SIGNATURE_PAGES = 10  # pages listed for --limit 1000 (more for larger limits); failed ones are skipped

# Stablecoins used as the price currency instead of SOL (Fomo, pump.fun USDC pairs since May 2026,
# USD1 pairs, ...). Each counts as $1.
USDC = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
QUOTE_MINTS = {
    USDC: "USDC",
    "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB": "USDT",
    "USD1ttGY1N17NEEHLmELoaybftRBUSErhqYiQzvEmuB": "USD1",
}
QUOTE_DUST = 1.0  # a stablecoin change under $1 beside a real SOL payment is leftover change, not the price
MIN_QUOTE_MOVE = 0.1  # without a known swap program, a smaller stablecoin payment doesn't make a purchase

# Swap programs: a token decrease in a transaction that calls one of these is a sale even when the
# proceeds are nil (rugged coin) or eaten by tips; without them it would look like a transfer.
# Concentrated-liquidity pools (Meteora DLMM, Raydium CLMM) are left out on purpose: adding
# one-sided liquidity to them also moves only the coin, and that is not a sale.
SWAP_PROGRAMS = {
    "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P",  # pump.fun bonding curve
    "pAMMBay6oceH9fJKBRHGP5D4bD4sWpmSwMn52FMfXEA",  # PumpSwap
    "JUP6LkbZbjS1jKKwapdHNy74zcZ3tLUZoi5QNyVTaV4",  # Jupiter v6
    "675kPX9MHTjS2zt1qfr1NYHuzeLXfQM9H24wFSUt1Mp8",  # Raydium AMM v4
    "CPMMoo8L3F4NbTegBCKVNunggL7H1ZpdTHKxQB5qKP1C",  # Raydium CPMM
    "LanMV9sAd7wArD4vJFi2qDdfnVhFxYSUg6eADduJ3uj",  # Raydium LaunchLab (bonk.fun)
    "dbcij3LWUppWqq96dh6gJWwBifmcGfLSB5D4DuSMaqN",  # Meteora Dynamic Bonding Curve
    "cpamdpZCGKUy5JxQXB4dcpGPiikHawvSWAd6mEn1sGG",  # Meteora DAMM v2
    "Eo7WjKq67rjJQSZxS6z3YkapzY3eMj6Xy8X5EQVn5UaB",  # Meteora DAMM v1
    "DF1ow4tspfHX9JwWJsAb9epbkA8hmpSEAtxXy1V27QBH",  # DFlow aggregator (routes Fomo's trades)
}


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


def _owned_accounts(balances: list[dict] | None, owner: str) -> dict[int, tuple[str, float]]:
    """accountIndex -> (mint, token amount) for the owner's token accounts."""
    out: dict[int, tuple[str, float]] = {}
    for b in balances or []:
        if b.get("owner") == owner and "accountIndex" in b:
            amt = b.get("uiTokenAmount") or {}
            out[b["accountIndex"]] = (b["mint"], float(amt.get("uiAmountString") or amt.get("uiAmount") or 0))
    return out


def _rent(lamports: int, account: tuple[str, float]) -> float:
    """Rent part of a token account's lamports. A wrapped-SOL account's
    lamports also hold the wrapped SOL itself, which is counted separately."""
    mint, amount = account
    sol = lamports / LAMPORTS_PER_SOL
    return max(0.0, sol - amount) if mint == WSOL else sol


# Why a transaction produced no trade, in plain words (used in summaries and `trenchnet inspect`).
SKIP_REASONS = {
    "failed": "transaction failed",
    "not_in_tx": "wallet not in the transaction",
    "no_token_change": "no token balance change (SOL transfers, fees, other activity)",
    "several_tokens": "several tokens changed at once (multi-token swap or batch)",
    "no_time": "no block time",
    "unrelated_transfer": "tokens received or sent that the wallet never bought or sold (usually spam airdrops)",
    "quote_conversion": "only SOL or stablecoins moved (swaps between them, deposits, withdrawals)",
    "other_quote": "token bought or sold with USDC/USDT/USD1, but no SOL/USD price for that time could be fetched",
}


def drop_unrelated_transfers(res: "BackfillResult", known_traded: Iterable[str] = ()) -> None:
    """Keep transfers only for tokens the wallet also bought or sold, in this
    download or in history saved earlier (`known_traded`). Well-known wallets
    receive a steady stream of spam tokens; those aren't trading activity."""
    traded = {t.mint for t in res.trades if t.side.is_trade} | set(known_traded)
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
    # A trade priced in a stablecoin, waiting to be valued in SOL (status "other_quote"): see value_in_sol().
    pending: Trade | None = None


def value_in_sol(view: TxView, sol_usd: float) -> Trade:
    """The stablecoin-priced trade of `view`, valued in SOL at `sol_usd`
    dollars per SOL. SOL that moved alongside (tips, bot fees) counts the same
    way it does in a SOL trade: added to a buy's cost, taken off a sale."""
    t = view.pending
    assert t is not None and t.quote_amount is not None
    leg = -view.sol_delta if t.side is Side.BUY else view.sol_delta
    return replace(t, sol_amount=max(0.0, t.quote_amount / sol_usd + leg))


def analyze_wallet_tx(tx: dict | None, wallet: str, signature: str) -> TxView:
    if not tx or not tx.get("meta") or tx["meta"].get("err") is not None:
        return TxView("failed")
    meta = tx["meta"]
    keys = account_keys(tx)
    pre = _token_amounts(meta.get("preTokenBalances"), wallet)
    post = _token_amounts(meta.get("postTokenBalances"), wallet)
    if wallet in keys:
        i = keys.index(wallet)
        native = (meta["postBalances"][i] - meta["preBalances"][i]) / LAMPORTS_PER_SOL
        if i == 0:
            native += meta.get("fee", 0) / LAMPORTS_PER_SOL
    elif pre or post:
        native = 0.0  # only the wallet's token accounts took part (another signer moved them)
    else:
        return TxView("not_in_tx")
    sol_delta = native
    sol_delta += post.pop(WSOL, 0.0) - pre.pop(WSOL, 0.0)

    # Rent for the wallet's token accounts created or closed in this tx is not
    # part of the trade. Only rent the wallet itself paid or got back counts:
    # spam senders and app sponsors often pay to create the account. Opening
    # one account and closing another in the same tx nets out.
    pre_accts = _owned_accounts(meta.get("preTokenBalances"), wallet)
    post_accts = _owned_accounts(meta.get("postTokenBalances"), wallet)
    created = sum(_rent(meta["postBalances"][a], post_accts[a]) for a in post_accts.keys() - pre_accts.keys())
    closed = sum(_rent(meta["preBalances"][a], pre_accts[a]) for a in pre_accts.keys() - post_accts.keys())
    net_rent = created - closed
    if net_rent > 1e-9 and native <= -net_rent + 1e-9:  # the wallet paid to open its token account
        sol_delta += net_rent
    elif net_rent < -1e-9 and native > 1e-9:  # the refund from closing one came back (net of any tips)
        sol_delta += net_rent

    deltas = {m: post.get(m, 0.0) - pre.get(m, 0.0) for m in set(pre) | set(post)}
    deltas = {m: d for m, d in deltas.items() if abs(d) > 0}
    if not deltas:
        return TxView("no_token_change", sol_delta)
    coins = {m: d for m, d in deltas.items() if m not in QUOTE_MINTS}
    quotes = {m: d for m, d in deltas.items() if m in QUOTE_MINTS}
    if not coins:  # SOL <-> stablecoin, deposits, withdrawals: not a token trade, never a position
        return TxView("quote_conversion", sol_delta, deltas)
    if len(coins) > 1:
        return TxView("several_tokens", sol_delta, deltas)
    mint, d = next(iter(coins.items()))
    swap = not SWAP_PROGRAMS.isdisjoint(keys)
    ts = tx.get("blockTime")

    # Coin and stablecoin both received, or both given (adding/removing liquidity): not a trade.
    if any(q * d > 0 and abs(q) >= QUOTE_DUST for q in quotes.values()):
        return TxView("several_tokens", sol_delta, deltas)
    # Paid for / sold for a stablecoin. A small stablecoin change next to a real SOL payment is
    # leftover change from a route through it (SOL -> USD1 -> coin), not the price.
    sol_leg = (d > 0 and sol_delta < -MIN_SOL_MOVE) or (d < 0 and sol_delta > MIN_SOL_MOVE)
    priced = {m: q for m, q in quotes.items()
              if q * d < 0 and (abs(q) >= QUOTE_DUST or (not sol_leg and (swap or abs(q) >= MIN_QUOTE_MOVE)))}
    if len(priced) > 1:
        return TxView("several_tokens", sol_delta, deltas)
    if priced:
        if ts is None:
            return TxView("no_time", sol_delta, deltas)
        qm, q = next(iter(priced.items()))
        pending = Trade(signature=signature, wallet=wallet, mint=mint, side=Side.BUY if d > 0 else Side.SELL,
                        sol_amount=0.0, token_amount=abs(d), ts=float(ts), source="rpc",
                        quote_mint=qm, quote_amount=abs(q))
        return TxView("other_quote", sol_delta, deltas, pending=pending)

    paid_sol = d > 0 and (sol_delta < -MIN_SOL_MOVE or (swap and sol_delta < -1e-6))
    # With a swap program, a sale can net nothing or even a little less than nothing (tips);
    # putting real SOL in alongside the token (e.g. adding liquidity) is not a sale.
    got_sol = d < 0 and (sol_delta > MIN_SELL_PROCEEDS or (swap and sol_delta > -0.01))
    if paid_sol:
        side, sol = Side.BUY, -sol_delta
    elif got_sol:
        side, sol = Side.SELL, max(sol_delta, 0.0)  # a rugged coin can sell for nothing
    elif d > 0:
        side, sol = Side.TRANSFER_IN, 0.0
    else:
        side, sol = Side.TRANSFER_OUT, 0.0
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
    # Signatures whose reading is final (trade or no trade): saved trades for exactly these are replaced.
    read: set[str] = field(default_factory=set)
    held_back: int = 0  # trades not saved yet: an unread transaction from the same second could precede them
    order: dict[str, int] = field(default_factory=dict)  # every listed signature -> position on chain (higher = later)
    unpriced: int = 0  # stablecoin-priced trades left out for lack of a SOL/USD price


class TxNotFound(RpcError):
    """The RPC answered, but without the transaction (too recent, or pruned)."""

    def __init__(self) -> None:
        super().__init__(None, "the RPC returned no transaction for this signature")


async def fetch_transaction(rpc: SolanaRpc, signature: str) -> dict:
    """jsonParsed when the node can produce it; otherwise the raw base64 form
    (needed for large v1 transactions)."""
    try:
        tx = await rpc.get_transaction(signature)
    except RpcError as e:
        if e.code == VERSION_UNSUPPORTED:
            raise
        tx = await rpc.get_transaction(signature, encoding="base64")
    if tx is None:
        raise TxNotFound()
    return tx


def _reason(e: Exception) -> str:
    if isinstance(e, RpcHTTPError):
        return f"HTTP {e.status}" if e.status else "network error"
    if isinstance(e, TxNotFound):
        return "not returned by the RPC"
    if isinstance(e, RpcError):
        return f"RPC error {e.code}"
    return type(e).__name__


def _retryable(e: Exception) -> bool:
    return isinstance(e, (RpcHTTPError, TxNotFound))


async def _list_signatures(rpc: SolanaRpc, address: str, limit: int, before: str | None,
                           retry_pause_s: float) -> list[dict]:
    try:
        return await rpc.get_signatures(address, limit=limit, before=before)
    except RpcHTTPError as e:
        if e.fatal:
            raise
        await asyncio.sleep(retry_pause_s)  # one more try after a pause, like transactions get
        return await rpc.get_signatures(address, limit=limit, before=before)


async def _list_address(rpc: SolanaRpc, address: str, max_signatures: int, retry_pause_s: float,
                        successful_only: bool) -> list[dict]:
    """Newest first, up to `max_signatures` entries (successful ones only, if asked)."""
    out: list[dict] = []
    before = None
    # One page per 1,000 wanted, plus 9 more for wallets where most transactions failed.
    max_pages = -(-max_signatures // 1000) + MAX_SIGNATURE_PAGES - 1
    for _ in range(max_pages):
        if len(out) >= max_signatures:
            break
        # A full page when skipping failures: asking for "what's left" would end the
        # listing early on wallets with many failed transactions.
        want = 1000 if successful_only else min(1000, max_signatures - len(out))
        page = await _list_signatures(rpc, address, want, before, retry_pause_s)
        if not page:
            break
        out += [s for s in page if not successful_only or s.get("err") is None]
        before = page[-1]["signature"]
        if len(page) < want:  # end of the address's history
            break
    else:
        if len(out) < max_signatures:
            log.warning("%s: stopped listing after %d pages of mostly failed transactions; "
                        "read %d instead of %d", address, max_pages, len(out), max_signatures)
    return out[:max_signatures]


async def list_wallet_signatures(rpc: SolanaRpc, wallet: str, max_signatures: int, retry_pause_s: float = 10.0,
                                 successful_only: bool = True) -> list[dict]:
    """The wallet's most recent transactions, newest first."""
    return await _list_address(rpc, wallet, max_signatures, retry_pause_s, successful_only)


async def backfill_wallet(rpc: SolanaRpc, wallet: str, max_signatures: int = 500,
                          progress: Callable[[int, int, int], None] | None = None,
                          retry_pause_s: float = 10.0, result: BackfillResult | None = None,
                          prices: "SolUsdPrices | None" = None, known_traded: Iterable[str] = ()) -> BackfillResult:
    """Read up to `max_signatures` of the wallet's most recent successful
    transactions.

    Transactions that fail temporarily (rate limits, network) are retried
    once at the end. The backfill stops early, keeping what it has read, on
    a fatal error (rejected key/URL, credits used up) or after BREAKER
    transactions in a row fail at the HTTP level; `result.stopped` says why.
    Pass `result` to keep partial trades if the caller is cancelled: they are
    in chain order even then, and any trade sharing its second with a
    transaction not read yet is held back (it will be read next time).
    Trades priced in stablecoins are valued with `prices` (left out without).
    Failing to list the wallet's transactions at all raises RpcHTTPError or
    RpcError."""
    res = result if result is not None else BackfillResult()
    listed = await list_wallet_signatures(rpc, wallet, max_signatures, retry_pause_s)
    sigs = [s["signature"] for s in listed]
    block_time = {s["signature"]: s.get("blockTime") for s in listed}
    newest_first = {s: i for i, s in enumerate(sigs)}
    res.order = {s: len(sigs) - i for i, s in enumerate(sigs)}
    pending: list[TxView] = []  # stablecoin-priced, waiting for a SOL/USD price
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
                    raw = await rpc.get_transaction(sig, encoding="base64")
                    if raw is None:
                        raise TxNotFound() from None
                    view = analyze_wallet_tx(raw, wallet, sig)
                res.transactions += 1
                res.read.add(sig)
                if view.trade is not None:
                    res.trades.append(view.trade)
                elif view.pending is not None:
                    pending.append(view)
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
                progress(n, len(batch), len(res.trades) + len(pending))
        return failed

    finished = False
    try:
        failed = await read(sigs, show_progress=True)
        transient = [s for s, e in failed.items() if _retryable(e)]
        if transient and res.stopped is None:
            log.info("retrying %d transactions after a pause", len(transient))
            await asyncio.sleep(retry_pause_s)
            for s in transient:
                failed.pop(s)
            failed.update(await read(transient, show_progress=False))
        res.unreadable = Counter(_reason(e) for e in failed.values())
        if pending:
            if prices is not None:
                await prices.prefetch(v.pending.ts for v in pending if v.pending is not None)
            for v in pending:
                assert v.pending is not None
                price = prices.get(v.pending.ts) if prices is not None else None
                if price:
                    res.trades.append(value_in_sol(v, price))
                else:
                    res.ignored["other_quote"] += 1
                    res.unpriced += 1
            pending.clear()
        finished = True
    finally:
        for v in pending:  # interrupted before they could be valued: read them again next time
            if v.pending is not None:
                res.read.discard(v.pending.signature)
        if not finished or res.stopped is not None:
            # Partial result: a transaction not read yet may share a second with a saved trade
            # and precede it on-chain; saving that trade now would replay them in the wrong order.
            unread = {block_time.get(s) for s in sigs if s not in res.read} - {None}
            held = [t for t in res.trades if t.ts in unread]
            if held:
                res.trades = [t for t in res.trades if t.ts not in unread]
                res.read.difference_update(t.signature for t in held)
                res.held_back += len(held)
        # Oldest first, keeping on-chain order within the same second (a buy and a sell
        # in one second must replay buy-then-sell).
        res.trades.sort(key=lambda t: (t.ts, -newest_first.get(t.signature, 0)))
        drop_unrelated_transfers(res, known_traded)
    return res
