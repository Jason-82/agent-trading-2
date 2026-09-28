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

This makes one getTransaction call per signature; use a paid RPC for large
backfills.
"""

from __future__ import annotations

import logging
from typing import Any

from ..models import Side, Trade
from ..solana_rpc import LAMPORTS_PER_SOL, SolanaRpc

log = logging.getLogger(__name__)

WSOL = "So11111111111111111111111111111111111111112"
MIN_SOL_MOVE = 0.001


def _key(k: Any) -> str:
    return k["pubkey"] if isinstance(k, dict) else str(k)


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


def parse_wallet_tx(tx: dict | None, wallet: str, signature: str) -> list[Trade]:
    if not tx or not tx.get("meta") or tx["meta"].get("err") is not None:
        return []
    meta = tx["meta"]
    keys = [_key(k) for k in tx["transaction"]["message"]["accountKeys"]]
    if wallet not in keys:
        return []
    i = keys.index(wallet)
    sol_delta = (meta["postBalances"][i] - meta["preBalances"][i]) / LAMPORTS_PER_SOL
    if i == 0:
        sol_delta += meta.get("fee", 0) / LAMPORTS_PER_SOL

    pre = _token_amounts(meta.get("preTokenBalances"), wallet)
    post = _token_amounts(meta.get("postTokenBalances"), wallet)
    sol_delta += post.pop(WSOL, 0.0) - pre.pop(WSOL, 0.0)

    # Rent for the wallet's token accounts created or closed in this tx is not
    # part of the trade.
    pre_accts = _owned_accounts(meta.get("preTokenBalances"), wallet)
    post_accts = _owned_accounts(meta.get("postTokenBalances"), wallet)
    for idx in post_accts - pre_accts:  # created: rent now sits in the account
        sol_delta += meta["postBalances"][idx] / LAMPORTS_PER_SOL
    for idx in pre_accts - post_accts:  # closed: rent refunded to the wallet
        sol_delta -= meta["preBalances"][idx] / LAMPORTS_PER_SOL

    deltas = {m: post.get(m, 0.0) - pre.get(m, 0.0) for m in set(pre) | set(post)}
    deltas = {m: d for m, d in deltas.items() if abs(d) > 0}
    if len(deltas) != 1:
        return []
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
        return []
    return [Trade(signature=signature, wallet=wallet, mint=mint, side=side, sol_amount=sol,
                  token_amount=abs(d), ts=float(ts), source="rpc")]


async def backfill_wallet(rpc: SolanaRpc, wallet: str, max_signatures: int = 500) -> list[Trade]:
    sigs: list[str] = []
    before = None
    while len(sigs) < max_signatures:
        page = await rpc.get_signatures(wallet, limit=min(1000, max_signatures - len(sigs)), before=before)
        if not page:
            break
        sigs += [s["signature"] for s in page if s.get("err") is None]
        before = page[-1]["signature"]
        if len(page) < 1000:
            break
    trades: list[Trade] = []
    for n, sig in enumerate(sigs, 1):
        try:
            trades += parse_wallet_tx(await rpc.get_transaction(sig), wallet, sig)
        except Exception as e:  # noqa: BLE001 - one bad transaction should not stop a backfill
            log.warning("skip %s: %s", sig, e)
        if n % 100 == 0:
            log.info("%s: %d/%d transactions, %d trades", wallet[:6], n, len(sigs), len(trades))
    return trades
