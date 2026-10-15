"""History-reader cases found by researching real 2026 wallets."""

import pytest

from trenchnet.ledger import Ledger
from trenchnet.models import Side
from trenchnet.sources.rpc_backfill import (QUOTE_MINTS, WSOL, BackfillResult, analyze_wallet_tx,
                                            backfill_wallet)
from trenchnet.stats import compute_wallet_stats

from .conftest import mk

W = "Wallet111111111111111111111111111111111111"
PUMP = "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P"
USDC = next(m for m, n in QUOTE_MINTS.items() if n == "USDC")
COIN = "Coin1111111111111111111111111111111111pump"
RENT = 2_039_280


def tx(native_change, tokens_pre, tokens_post, programs=(), lamports_pre=None, lamports_post=None):
    """tokens_*: {mint: amount}; each mint gets its own wallet-owned account."""
    mints = sorted(set(tokens_pre) | set(tokens_post))
    keys = [W] + [f"Ata{m[:6]}" for m in mints] + list(programs)
    n = len(keys)
    pre, post = [10**10] + [0] * (n - 1), [10**10 + native_change] + [0] * (n - 1)
    for j, m in enumerate(mints, 1):
        pre[j] = (lamports_pre or {}).get(m, RENT if m in tokens_pre else 0)
        post[j] = (lamports_post or {}).get(m, RENT if m in tokens_post else 0)

    def bal(tokens):
        return [{"accountIndex": mints.index(m) + 1, "owner": W, "mint": m,
                 "uiTokenAmount": {"uiAmountString": str(a)}} for m, a in tokens.items()]

    return {"blockTime": 1_760_000_000, "transaction": {"message": {"accountKeys": [{"pubkey": k} for k in keys]}},
            "meta": {"err": None, "fee": 5000, "preBalances": pre, "postBalances": post,
                     "preTokenBalances": bal(tokens_pre), "postTokenBalances": bal(tokens_post)}}


def side(t):
    v = analyze_wallet_tx(t, W, "s")
    return (v.trade.side, round(v.trade.sol_amount, 6)) if v.trade else v.status


def test_sell_closing_a_wsol_account_with_leftover_wsol_is_not_double_counted():
    # Sell for 0.5 SOL into a WSOL account that already held 0.2 WSOL, then close it: the wallet gets 0.7 SOL + rent.
    t = tx(700_000_000 + RENT - 5000, {COIN: 1000, WSOL: 0.2}, {COIN: 0},
           lamports_pre={WSOL: RENT + 200_000_000})
    assert side(t) == (Side.SELL, 0.5)


def test_stablecoin_priced_trades_are_recognised_not_lost():
    assert side(tx(-5000, {COIN: 0, USDC: 150}, {COIN: 1000, USDC: 0})) == "other_quote"  # bought with USDC
    assert side(tx(-5000, {COIN: 1000, USDC: 0}, {COIN: 0, USDC: 140})) == "other_quote"  # sold for USDC


def test_sol_to_stablecoin_swap_is_not_a_position():
    assert side(tx(-1_000_005_000, {USDC: 0}, {USDC: 150})) == "quote_conversion"


def test_small_stablecoin_residual_next_to_a_sol_buy_is_ignored():
    # bonk.fun-style route: SOL -> USD1 -> coin leaves a few cents of USD1 behind.
    # (two token accounts are opened: the coin's and the USD1 residual's)
    assert side(tx(-500_005_000 - 2 * RENT, {}, {COIN: 1000, USDC: 0.03})) == (Side.BUY, 0.5)


def test_rugged_sale_through_a_swap_program_is_a_sale_for_nothing():
    # Tip eats the dust proceeds: SOL change is slightly negative, but pump.fun was called.
    assert side(tx(-5000 - 20_000, {COIN: 1000}, {COIN: 0}, programs=[PUMP])) == (Side.SELL, 0.0)
    # Same balances without a swap program: a transfer.
    assert side(tx(-5000 - 20_000, {COIN: 1000}, {COIN: 0}))[0] is Side.TRANSFER_OUT


def test_adding_liquidity_is_not_a_sale():
    assert side(tx(-2_000_005_000, {COIN: 1000}, {COIN: 0}, programs=[PUMP]))[0] is Side.TRANSFER_OUT


def test_position_with_a_small_leftover_counts_as_closed():
    led = Ledger()
    led.apply(mk(W, "M", "buy", 1.0, 1000, 0))
    p = led.apply(mk(W, "M", "sell", 2.0, 995, 60))  # sold 99.5%
    assert p.status == "exited"


def test_mostly_sold_then_dust_sent_away_is_a_closed_trade():
    led = Ledger()
    led.apply(mk(W, "M", "buy", 1.0, 1000, 0))
    led.apply(mk(W, "M", "sell", 2.0, 950, 60))
    p = led.apply(mk(W, "M", "transfer_out", 0, 50, 120))  # burns/sends the last 5%
    assert p.status == "exited" and not p.closed_by_transfer
    s = compute_wallet_stats(W, [mk(W, "M", "buy", 1.0, 1000, 0), mk(W, "M", "sell", 2.0, 950, 60),
                                 mk(W, "M", "transfer_out", 0, 50, 120)])
    assert s.closed_positions == 1


class PagedRpc:
    """1,500 signatures, 40% failed, served in pages like getSignaturesForAddress."""

    def __init__(self, total=1500, fail_every=5):
        self.sigs = [{"signature": f"s{i}", "err": {"x": 1} if i % fail_every < 2 else None} for i in range(total)]
        self.limits = []

    async def get_signatures(self, address, limit=1000, before=None):
        self.limits.append(limit)
        start = 0 if before is None else int(before[1:]) + 1
        return self.sigs[start:start + limit]

    async def get_transaction(self, sig, encoding="jsonParsed"):
        return tx(-500_005_000 - RENT, {}, {COIN + sig: 10})


async def test_listing_keeps_paging_past_failed_transactions():
    rpc = PagedRpc()
    res = await backfill_wallet(rpc, W, max_signatures=1000, retry_pause_s=0)
    assert res.transactions == 900  # all 900 successful ones, not cut short after page 2
    assert rpc.limits == [1000, 1000]


async def test_same_second_trades_replay_in_chain_order():
    class SameSecond:
        async def get_signatures(self, address, limit=1000, before=None):
            # newest first: the sell (s0) happened after the buy (s1), both in one second
            return [{"signature": "s0", "err": None}, {"signature": "s1", "err": None}] if before is None else []

        async def get_transaction(self, sig, encoding="jsonParsed"):
            if sig == "s1":
                return tx(-500_005_000 - RENT, {}, {COIN: 1000})
            return tx(600_000_000 + RENT - 5000, {COIN: 1000}, {}, programs=[PUMP])

    res = await backfill_wallet(SameSecond(), W, max_signatures=10, retry_pause_s=0)
    assert [t.side for t in res.trades] == [Side.BUY, Side.SELL]


def test_refresh_replaces_downloaded_history(tmp_path, monkeypatch):
    from solders.pubkey import Pubkey

    import trenchnet.cli as cli
    from trenchnet.models import Trade
    from trenchnet.store import Store

    w = str(Pubkey.new_unique())
    db = str(tmp_path / "t.db")
    store = Store(db)
    store.add_trades([
        Trade("old", w, COIN, Side.TRANSFER_OUT, 0.0, 1000, 6, source="rpc"),  # misread by the old reader
        Trade("live", w, COIN, Side.BUY, 0.1, 200, 9, source="pumpportal:pump"),  # seen live: must stay
    ])
    store.close()
    (tmp_path / "w.txt").write_text(f"{w}\n")

    async def fake_backfill(rpc, wallet, max_signatures=500, result=None, **kw):
        result.trades.append(Trade("old", w, COIN, Side.SELL, 0.5, 1000, 6, source="rpc"))
        result.read.add("old")
        result.transactions = 1
        return result

    monkeypatch.setattr("trenchnet.sources.rpc_backfill.backfill_wallet", fake_backfill)
    cli.main(["backfill", str(tmp_path / "w.txt"), "--db", db, "--refresh", "--rpc", "https://rpc.example/?api-key=K"])
    assert sorted((t.signature, t.side.value) for t in Store(db).trades()) == [("live", "buy"), ("old", "sell")]
