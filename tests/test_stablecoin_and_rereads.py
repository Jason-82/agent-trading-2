"""Stablecoin-priced trades, re-reading saved history safely, and the ledger's
leftover handling (review of the 2026 reader-gap fixes)."""

import asyncio
import sqlite3

import httpx
import pytest
from solders.pubkey import Pubkey

from trenchnet.ledger import Ledger
from trenchnet.models import Side, Trade
from trenchnet.prices import BUCKET_S, PriceSource, SolUsdPrices, _binance, _coinbase
from trenchnet.solana_rpc import RpcError, RpcHTTPError
from trenchnet.sources.rpc_backfill import USDC, BackfillResult, analyze_wallet_tx, backfill_wallet, value_in_sol
from trenchnet.stats import compute_wallet_stats
from trenchnet.store import Store

from .conftest import mk

W = str(Pubkey.new_unique())
SPONSOR = str(Pubkey.new_unique())
COIN = "Coin1111111111111111111111111111111111pump"
USD1 = "USD1ttGY1N17NEEHLmELoaybftRBUSErhqYiQzvEmuB"
PUMP = "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P"
JUP = "JUP6LkbZbjS1jKKwapdHNy74zcZ3tLUZoi5QNyVTaV4"
DLMM = "LBUZKhRxPF3XUpBCjp4YzTKgLccjZhTSDM9YuVaPwxo"
CLMM = "CAMMCzo5YL8w4VFF8KVHrK22GGUsp5VTaW7grrKgrWqK"
LAUNCHPADS = {
    "Raydium LaunchLab": "LanMV9sAd7wArD4vJFi2qDdfnVhFxYSUg6eADduJ3uj",
    "Meteora DBC": "dbcij3LWUppWqq96dh6gJWwBifmcGfLSB5D4DuSMaqN",
    "Meteora DAMM v2": "cpamdpZCGKUy5JxQXB4dcpGPiikHawvSWAd6mEn1sGG",
    "Meteora DAMM v1": "Eo7WjKq67rjJQSZxS6z3YkapzY3eMj6Xy8X5EQVn5UaB",
}
RENT = 2_039_280  # SPL token account
RENT22 = 2_074_080  # Token-2022 account (pump.fun coins)
FEE = 5000
T0 = 1_760_000_000


def tx(native_change, tokens_pre, tokens_post, programs=(), rent=None, wallet_in_tx=True, time=T0, sponsor=False):
    """A jsonParsed-like transaction. tokens_*: {mint: amount}; each mint has its
    own wallet-owned token account, open before/after iff the mint is listed.
    native_change: the wallet's lamport change (including the fee if it paid it).
    sponsor=True: another wallet pays the fee and the wallet co-signs, as in a
    Fomo trade. wallet_in_tx=False: only the wallet's token accounts take part."""
    rent = rent or {}
    mints = sorted(set(tokens_pre) | set(tokens_post))
    lead = [SPONSOR] if sponsor or not wallet_in_tx else []
    keys = lead + ([W] if wallet_in_tx else []) + [f"Ata{m[:6]}" for m in mints] + list(programs)
    first_ata = keys.index(f"Ata{mints[0][:6]}") if mints else len(keys)
    n = len(keys)
    pre, post = [10**10] * n, [10**10] * n
    if lead:
        post[0] -= FEE
    if wallet_in_tx:
        post[keys.index(W)] += native_change
    for j, m in enumerate(mints, first_ata):
        pre[j] = rent.get(m, RENT) if m in tokens_pre else 0
        post[j] = rent.get(m, RENT) if m in tokens_post else 0

    def bal(tokens):
        return [{"accountIndex": first_ata + mints.index(m), "owner": W, "mint": m,
                 "uiTokenAmount": {"uiAmountString": str(a)}} for m, a in tokens.items()]

    return {"blockTime": time,
            "transaction": {"message": {"accountKeys": [{"pubkey": k, "signer": k in (W, SPONSOR)} for k in keys]}},
            "meta": {"err": None, "fee": FEE, "preBalances": pre, "postBalances": post,
                     "preTokenBalances": bal(tokens_pre), "postTokenBalances": bal(tokens_post)}}


def read(t):
    v = analyze_wallet_tx(t, W, "s")
    if v.trade:
        return (v.trade.side, round(v.trade.sol_amount, 6))
    if v.pending:
        return (v.pending.side, v.pending.quote_mint, round(v.pending.quote_amount, 6), round(v.sol_delta, 7))
    return v.status


# -- reading stablecoin trades ------------------------------------------------

@pytest.mark.parametrize("tip", [1_000_000, 100_000, 2_000_000])
def test_usdc_buy_with_a_tip_is_a_usdc_buy_not_a_sol_buy_for_the_tip(tip):
    # 150 USDC -> coin through pump.fun, opening the Token-2022 coin account and tipping.
    t = tx(-FEE - RENT22 - tip, {USDC: 150}, {USDC: 0, COIN: 1000}, programs=[PUMP], rent={COIN: RENT22})
    assert read(t) == (Side.BUY, USDC, 150.0, -tip / 1e9)
    view = analyze_wallet_tx(t, W, "s")
    assert value_in_sol(view, 150.0).sol_amount == pytest.approx(1.0 + tip / 1e9)  # the tip is part of the cost


def test_usdc_buy_without_a_known_program_and_usd1_through_jupiter():
    assert read(tx(-FEE - RENT - 2_000_000, {USDC: 150}, {USDC: 0, COIN: 1000}))[:3] == (Side.BUY, USDC, 150.0)
    assert read(tx(-FEE - 1_000_000, {USD1: 80, COIN: 0}, {USD1: 0, COIN: 500}, programs=[JUP]))[:3] == \
        (Side.BUY, USD1, 80.0)


def test_closing_the_usdc_account_while_opening_the_coin_account_nets_out():
    t = tx(-FEE + RENT - RENT22, {USDC: 150}, {COIN: 1000}, programs=[PUMP], rent={COIN: RENT22})
    assert read(t) == (Side.BUY, USDC, 150.0, 0.0)


@pytest.mark.parametrize("tip", [0, 100_000, 500_000, 1_000_000])
def test_full_usdc_sale_that_closes_the_coin_account_is_a_usdc_sale(tip):
    t = tx(-FEE + RENT22 - tip, {COIN: 1000, USDC: 0}, {USDC: 140}, programs=[PUMP], rent={COIN: RENT22})
    side, mint, amount, sol = read(t)
    assert (side, mint, amount) == (Side.SELL, USDC, 140.0)
    assert sol == pytest.approx(-tip / 1e9)  # the refund isn't proceeds; the tip is taken off
    assert value_in_sol(analyze_wallet_tx(t, W, "s"), 140.0).sol_amount == pytest.approx(1.0 - tip / 1e9)


def test_rug_sale_that_closes_the_account_and_tips_sells_for_nothing():
    # Proceeds 0.00002 SOL, 0.001 tip, account closed: the rent refund must not become proceeds.
    t = tx(-FEE + 20_000 + RENT - 1_000_000, {COIN: 1000}, {}, programs=[PUMP])
    assert read(t) == (Side.SELL, 0.0)


@pytest.mark.parametrize("program", LAUNCHPADS.values(), ids=LAUNCHPADS.keys())
def test_rug_sales_on_other_launchpads_are_sales_not_transfers(program):
    t = tx(-FEE + 20_000 - 1_000_000, {COIN: 1000}, {COIN: 0}, programs=[program])
    assert read(t) == (Side.SELL, 0.0)
    assert read(tx(-FEE + 20_000 - 1_000_000, {COIN: 1000}, {COIN: 0}))[0] is Side.TRANSFER_OUT


@pytest.mark.parametrize("program", [DLMM, CLMM], ids=["DLMM", "CLMM"])
def test_one_sided_liquidity_into_concentrated_pools_is_not_a_sale(program):
    assert read(tx(-FEE, {COIN: 1000}, {COIN: 0}, programs=[program]))[0] is Side.TRANSFER_OUT
    assert read(tx(-FEE - 1_000_000, {COIN: 1000}, {COIN: 400}, programs=[program]))[0] is Side.TRANSFER_OUT


def test_coin_and_usdc_received_together_is_not_a_buy():
    # Withdrawing from a USDC pool (coin and USDC both come back), with a tip.
    assert read(tx(-FEE - 2_000_000, {COIN: 0, USDC: 0}, {COIN: 1000, USDC: 50}, programs=[PUMP])) == "several_tokens"


def test_small_usdc_change_beside_a_real_sol_payment_is_leftover():
    assert read(tx(-FEE - 500_000_000, {COIN: 0, USDC: 5}, {COIN: 1000, USDC: 4.7})) == (Side.BUY, 0.5)


def test_tiny_usdc_payment_needs_a_swap_program():
    assert read(tx(-FEE, {COIN: 0, USDC: 5}, {COIN: 1000, USDC: 4.95}))[0] is Side.TRANSFER_IN
    assert read(tx(-FEE, {COIN: 0, USDC: 5}, {COIN: 1000, USDC: 4.95}, programs=[PUMP]))[:3] == \
        (Side.BUY, USDC, pytest.approx(0.05))


def test_trade_paid_for_by_a_sponsor():
    # Fomo: the sponsor pays the fee and the new coin account's rent; the trader co-signs.
    assert read(tx(0, {USDC: 150}, {USDC: 0, COIN: 1000}, programs=[PUMP], sponsor=True)) == (Side.BUY, USDC, 150.0, 0.0)
    # Only the wallet's token accounts in it (another signer moved them): still read by owner.
    assert read(tx(0, {USDC: 150}, {USDC: 0, COIN: 1000}, programs=[PUMP], wallet_in_tx=False)) == \
        (Side.BUY, USDC, 150.0, 0.0)
    assert analyze_wallet_tx(tx(0, {}, {}, wallet_in_tx=False), W, "s").status == "not_in_tx"


# -- backfill ------------------------------------------------------------------

class FakeRpc:
    """getSignaturesForAddress per address (newest first) and getTransaction per
    signature: a dict, None, an exception to raise, or a list of those (one per call)."""

    host, requests_per_second, is_public, rate_limited = "rpc.example", 5.0, False, 0

    def __init__(self, listings, txs):
        self.listings, self.txs, self.listed = listings, txs, []

    async def get_signatures(self, address, limit=1000, before=None):
        self.listed.append(address)
        lst = self.listings.get(address, [])
        start = 0 if before is None else [s["signature"] for s in lst].index(before) + 1
        return lst[start:start + limit]

    async def get_transaction(self, sig, encoding="jsonParsed"):
        v = self.txs[sig]
        if isinstance(v, list):
            v = v.pop(0) if len(v) > 1 else v[0]
        if isinstance(v, BaseException):
            raise v
        return v

    async def aclose(self):
        pass


def listing(*sigs, times=None):
    return [{"signature": s, "err": None, "blockTime": (times or {}).get(s, T0), "slot": 1000 - i}
            for i, s in enumerate(sigs)]


def fixed_prices(price=150.0, calls=None):
    async def fetch(http, start, end):
        if calls is not None:
            calls.append((start, end))
        return [(b, price) for b in range(start, end + 1, BUCKET_S)]
    return SolUsdPrices(sources=[PriceSource("fake", 300, fetch)], now=lambda: T0 + 86400)


async def test_usdc_trades_are_valued_in_sol_at_the_time_of_the_trade():
    rpc = FakeRpc({W: listing("buy", "sell")}, {
        "sell": tx(-FEE, {COIN: 1000, USDC: 0}, {COIN: 0, USDC: 300}, programs=[PUMP], time=T0 + 600),
        "buy": tx(-FEE, {COIN: 0, USDC: 150}, {COIN: 1000, USDC: 0}, programs=[PUMP], time=T0),
    })
    res = await backfill_wallet(rpc, W, max_signatures=10, retry_pause_s=0, prices=fixed_prices(150.0))
    assert [(t.side, t.sol_amount, t.quote_mint, t.quote_amount) for t in res.trades] == [
        (Side.SELL, 2.0, USDC, 300.0), (Side.BUY, 1.0, USDC, 150.0)][::-1]
    s = compute_wallet_stats(W, res.trades)
    assert s.realized_pnl_sol == pytest.approx(1.0) and s.win_rate == 1.0


async def test_without_a_price_usdc_trades_are_left_out_and_counted():
    rpc = FakeRpc({W: listing("buy")}, {"buy": tx(-FEE, {COIN: 0, USDC: 150}, {COIN: 1000, USDC: 0}, programs=[PUMP])})
    res = await backfill_wallet(rpc, W, max_signatures=10, retry_pause_s=0)
    assert res.trades == [] and res.unpriced == 1 and res.ignored["other_quote"] == 1


async def test_fomo_trade_is_read_from_the_wallets_own_history():
    # Fomo's sponsor pays the fee (and the coin account's rent); the trader co-signs.
    rpc = FakeRpc({W: listing("fomoBuy")}, {
        "fomoBuy": tx(0, {COIN: 0, USDC: 150}, {COIN: 1000, USDC: 0}, programs=[PUMP], sponsor=True)})
    res = await backfill_wallet(rpc, W, max_signatures=10, retry_pause_s=0, prices=fixed_prices(150.0))
    assert [(t.side, t.sol_amount, t.quote_amount) for t in res.trades] == [(Side.BUY, 1.0, 150.0)]
    assert rpc.listed == [W]  # nothing else is listed: the wallet's own history has every trade


async def test_transaction_the_rpc_returns_nothing_for_is_unreadable_not_failed():
    rpc = FakeRpc({W: listing("gone", "ok")}, {"gone": None, "ok": tx(-FEE - 500_000_000 - RENT, {}, {COIN: 1})})
    res = await backfill_wallet(rpc, W, max_signatures=10, retry_pause_s=0)
    assert res.unreadable == {"not returned by the RPC": 1} and "failed" not in res.ignored
    assert res.read == {"ok"}


async def test_transaction_found_on_the_retry_is_read():
    rpc = FakeRpc({W: listing("late")}, {"late": [None, tx(-FEE - 500_000_000 - RENT, {}, {COIN: 1})]})
    res = await backfill_wallet(rpc, W, max_signatures=10, retry_pause_s=0)
    assert len(res.trades) == 1 and not res.unreadable


async def test_stopped_result_holds_back_trades_sharing_a_second_with_unread_ones():
    quota = RpcHTTPError(429, "credits used up", kind="quota")
    rpc = FakeRpc({W: listing("s0", "s1", "s2", times={"s2": T0 - 100})}, {
        "s0": tx(-FEE + 600_000_000, {COIN: 1000}, {COIN: 0}, programs=[PUMP]),  # sell, same second as s1
        "s1": quota, "s2": quota,
    })
    res = await backfill_wallet(rpc, W, max_signatures=10, retry_pause_s=0)
    assert res.stopped is quota
    assert res.trades == [] and res.held_back == 1 and res.read == set()  # s0 waits until s1 is read


async def test_cancelled_result_is_in_chain_order():
    rpc = FakeRpc({W: listing("s0", "s1", "s2", times={"s2": T0 - 100})}, {
        "s0": tx(-FEE + 600_000_000 + RENT, {COIN: 1000}, {}, programs=[PUMP]),  # sell
        "s1": tx(-FEE - 500_000_000 - RENT, {}, {COIN: 1000}, programs=[PUMP]),  # buy, same second
        "s2": asyncio.CancelledError(),
    })
    res = BackfillResult()
    with pytest.raises(asyncio.CancelledError):
        await backfill_wallet(rpc, W, max_signatures=10, retry_pause_s=0, result=res)
    assert [(t.signature, t.side) for t in res.trades] == [("s1", Side.BUY), ("s0", Side.SELL)]
    assert res.read == {"s0", "s1"}


async def test_limits_above_ten_thousand_are_listed_in_full():
    sigs = [f"s{i}" for i in range(15_000)]
    lst = [{"signature": s, "err": None, "blockTime": T0} for s in sigs]
    rpc = FakeRpc({W: lst}, {s: tx(-FEE, {}, {}) for s in sigs})
    res = await backfill_wallet(rpc, W, max_signatures=15_000, retry_pause_s=0)
    assert res.transactions == 15_000


# -- saving re-reads -----------------------------------------------------------

def test_replace_trades_only_touches_the_signatures_read():
    store = Store()
    store.add_trades([
        Trade("older", W, COIN, Side.BUY, 1.0, 1000, 1, source="rpc"),
        Trade("reread", W, COIN, Side.TRANSFER_OUT, 0.0, 1000, 5, source="rpc"),
        Trade("reread", W, COIN, Side.SELL, 0.5, 1000, 5, source="pumpportal:pump"),
        Trade("gone", W, "Spam", Side.TRANSFER_IN, 0.0, 9, 6, source="rpc"),
    ])
    new, fixed = store.replace_trades(W, [Trade("reread", W, COIN, Side.SELL, 0.5, 1000, 5, source="rpc"),
                                          Trade("fresh", W, COIN, Side.BUY, 0.1, 10, 7, source="rpc")],
                                      {"reread", "gone", "fresh"})
    rows = sorted((t.signature, t.side.value, t.source) for t in store.trades())
    # The live row with the same key stays; the misread transfer and the spam row are gone.
    assert rows == [("fresh", "buy", "rpc"), ("older", "buy", "rpc"), ("reread", "sell", "pumpportal:pump")]
    assert (new, fixed) == (1, 2)


def test_database_from_an_older_version_gains_the_stablecoin_columns(tmp_path):
    db = tmp_path / "old.db"
    con = sqlite3.connect(db)
    con.execute("CREATE TABLE trades (signature TEXT NOT NULL, wallet TEXT NOT NULL, mint TEXT NOT NULL,"
                " side TEXT NOT NULL, sol_amount REAL NOT NULL, token_amount REAL NOT NULL, ts REAL NOT NULL,"
                " market_cap_sol REAL, price_sol REAL, source TEXT, PRIMARY KEY (signature, wallet, mint, side))")
    con.execute("INSERT INTO trades VALUES ('a', 'w', 'm', 'buy', 1.0, 10, 1, NULL, NULL, 'rpc')")
    con.commit()
    con.close()
    store = Store(db)
    store.add_trades([Trade("b", "w", "m", Side.SELL, 2.0, 10, 2, source="rpc", quote_mint=USDC, quote_amount=300)])
    assert [(t.signature, t.quote_mint, t.quote_amount) for t in store.trades()] == [("a", None, None),
                                                                                   ("b", USDC, 300.0)]


def _run_backfill(monkeypatch, tmp_path, rpc, *extra):
    import trenchnet.cli as cli

    monkeypatch.setattr(cli, "_rpc_from", lambda url, rps=None: rpc)
    (tmp_path / "w.txt").write_text(f"{W}\n")
    cli.main(["backfill", str(tmp_path / "w.txt"), "--db", str(tmp_path / "t.db"), "--rpc", "https://x/", *extra])


def _stored(tmp_path):
    return sorted((t.signature, t.side.value) for t in Store(tmp_path / "t.db").trades())


def test_rerun_keeps_history_older_than_the_new_window(monkeypatch, tmp_path):
    store = Store(tmp_path / "t.db")
    store.add_trades([Trade("o_buy", W, "Old", Side.BUY, 1.0, 10, 1, source="rpc"),
                      Trade("o_sell", W, "Old", Side.SELL, 2.0, 10, 2, source="rpc")])
    store.close()
    rpc = FakeRpc({W: listing("n0")}, {"n0": tx(-FEE - 500_000_000 - RENT, {}, {COIN: 1000}, programs=[PUMP])})
    _run_backfill(monkeypatch, tmp_path, rpc, "--limit", "1", "--refresh")
    assert _stored(tmp_path) == [("n0", "buy"), ("o_buy", "buy"), ("o_sell", "sell")]


def test_rerun_keeps_a_saved_trade_whose_transaction_cant_be_read_now(monkeypatch, tmp_path, capsys):
    store = Store(tmp_path / "t.db")
    store.add_trades([Trade("s1", W, COIN, Side.BUY, 0.5, 1000, T0 - 100, source="rpc")])
    store.close()
    rpc = FakeRpc({W: listing("s0", "s1", times={"s1": T0 - 100})}, {
        "s0": tx(-FEE + 600_000_000, {COIN: 1000}, {COIN: 0}, programs=[PUMP]),
        "s1": RpcError(-32009, "missing in long-term storage"),
    })
    _run_backfill(monkeypatch, tmp_path, rpc)
    assert _stored(tmp_path) == [("s0", "sell"), ("s1", "buy")]
    assert "1 unreadable" in capsys.readouterr().out


def test_stopped_rerun_replaces_a_misread_instead_of_duplicating_it(monkeypatch, tmp_path, capsys):
    store = Store(tmp_path / "t.db")
    store.add_trades([Trade("s2", W, COIN, Side.BUY, 0.5, 1000, T0 - 200, source="rpc"),
                      Trade("s1", W, COIN, Side.TRANSFER_OUT, 0.0, 1000, T0 - 100, source="rpc")])
    store.close()
    rpc = FakeRpc({W: listing("s0", "s1", "s2", times={"s1": T0 - 100, "s2": T0 - 200})}, {
        "s0": tx(-FEE - 300_000_000 - RENT, {}, {"Other": 5}, programs=[PUMP]),
        "s1": tx(-FEE - 20_000, {COIN: 1000}, {COIN: 0}, programs=[PUMP], time=T0 - 100),  # rug sale for nothing
        "s2": RpcHTTPError(429, "max usage reached", kind="quota"),
    })
    with pytest.raises(SystemExit):
        _run_backfill(monkeypatch, tmp_path, rpc)
    assert _stored(tmp_path) == [("s0", "buy"), ("s1", "sell"), ("s2", "buy")]
    s = compute_wallet_stats(W, Store(tmp_path / "t.db").trades(wallets=[W]))
    assert s.realized_pnl_sol == pytest.approx(-0.5) and s.losing_positions == 1


def test_rerun_keeps_transfers_of_tokens_traded_in_saved_history(monkeypatch, tmp_path):
    store = Store(tmp_path / "t.db")
    store.add_trades([Trade("b", W, COIN, Side.BUY, 0.5, 1000, T0 - 500, source="rpc")])
    store.close()
    rpc = FakeRpc({W: listing("send")}, {"send": tx(-FEE, {COIN: 1000}, {COIN: 0})})
    _run_backfill(monkeypatch, tmp_path, rpc, "--limit", "1")
    assert _stored(tmp_path) == [("b", "buy"), ("send", "transfer_out")]


# -- ledger: leftovers after a sale ---------------------------------------------

def test_selling_the_leftover_after_a_near_full_exit_is_profit_not_unattributed():
    trades = [mk(W, "M", "buy", 1.0, 1_000_000, 0), mk(W, "M", "sell", 3.0, 993_000, 60),
              mk(W, "M", "sell", 0.5, 7_000, 86_400)]
    s = compute_wallet_stats(W, trades)
    assert s.realized_pnl_sol == pytest.approx(2.5)
    assert s.positions_missing_basis == 0 and s.closed_positions == 1


def test_rebuy_after_a_near_full_exit_tracks_the_whole_balance():
    led = Ledger()
    for t in [mk(W, "M", "buy", 1.0, 1_000_000, 0), mk(W, "M", "sell", 2.0, 991_000, 60),
              mk(W, "M", "buy", 0.05, 10_000, 120)]:
        p = led.apply(t)
    p = led.apply(mk(W, "M", "sell", 0.05, 9_950, 180))
    assert p.status == "reduced" and p.tokens == pytest.approx(9_050) and p.is_open


def test_burning_the_last_tenth_after_selling_counts_its_cost():
    trades = [mk(W, "M", "buy", 1.0, 100, 0), mk(W, "M", "sell", 0.95, 90, 60), mk(W, "M", "transfer_out", 0, 10, 120)]
    s = compute_wallet_stats(W, trades)
    assert s.realized_pnl_sol == pytest.approx(-0.05)
    assert s.closed_positions == 1 and s.losing_positions == 1 and s.winning_positions == 0


# -- SOL/USD prices -----------------------------------------------------------

def _refuse(status):
    async def fetch(http, start, end):
        req = httpx.Request("GET", "https://x")
        raise httpx.HTTPStatusError("no", request=req, response=httpx.Response(status, request=req))
    return fetch


async def test_prices_fall_back_to_the_next_source_and_are_cached(tmp_path):
    store = Store(tmp_path / "p.db")
    calls = []
    good = fixed_prices(151.0, calls).sources[0]
    prices = SolUsdPrices(store, sources=[PriceSource("blocked", 300, _refuse(451)), good], now=lambda: T0 + 86400)
    assert await prices.price_at(T0 + 10) == 151.0 and prices.problem is None
    assert len(calls) == 1
    later = SolUsdPrices(store, sources=[PriceSource("down", 300, _refuse(503))], now=lambda: T0 + 86400)
    assert await later.price_at(T0 + 20) == 151.0  # from the database, nothing fetched


async def test_one_request_covers_many_trades():
    calls = []
    prices = fixed_prices(150.0, calls)
    await prices.prefetch([T0 + k * 600 for k in range(50)])
    assert len(calls) == 1 and prices.get(T0 + 49 * 600) == 150.0


async def test_no_price_source_reachable_is_explained():
    prices = SolUsdPrices(sources=[PriceSource("coinbase", 300, _refuse(451)), PriceSource("binance", 1000, _refuse(403))],
                          now=lambda: T0 + 86400)
    assert await prices.price_at(T0) is None
    assert "coinbase: HTTP 451" in prices.problem and "binance: HTTP 403" in prices.problem


async def test_a_missing_candle_uses_a_neighbour():
    async def gappy(http, start, end):
        return [(b, 140.0) for b in range(start, end + 1, BUCKET_S) if b != (T0 // BUCKET_S) * BUCKET_S]
    prices = SolUsdPrices(sources=[PriceSource("x", 300, gappy)], now=lambda: T0 + 86400)
    assert await prices.price_at(T0) == 140.0


async def test_exchange_answers_are_parsed():
    def handler(req):
        if "coinbase" in req.url.host:
            assert req.url.params["granularity"] == "300"
            return httpx.Response(200, json=[[T0 + 300, 1, 2, 150.0, 152.0, 9], [T0, 1, 2, 148.0, 150.0, 9]])
        assert req.url.params["interval"] == "5m"
        return httpx.Response(200, json=[[T0 * 1000, "148.0", "151", "147", "150.0", "1", 0, "0", 1, "0", "0", "0"]])

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        assert await _coinbase(http, T0, T0 + 300) == [(T0 + 300, 151.0), (T0, 149.0)]
        assert await _binance("https://data-api.binance.vision", "SOLUSDT")(http, T0, T0) == [(T0, 149.0)]


# -- review round 4 ----------------------------------------------------------------

def test_rerun_with_an_unreadable_neighbour_keeps_same_second_order(monkeypatch, tmp_path):
    txs = {
        "s0": tx(-FEE + 600_000_000 + RENT, {COIN: 1000}, {}, programs=[PUMP]),  # sell, after s1, same second
        "s1": tx(-FEE - 500_000_000 - RENT, {}, {COIN: 1000}, programs=[PUMP]),  # buy
    }
    _run_backfill(monkeypatch, tmp_path, FakeRpc({W: listing("s0", "s1")}, dict(txs)))
    assert [t.signature for t in Store(tmp_path / "t.db").trades()] == ["s1", "s0"]
    txs["s0"] = RpcError(-32009, "missing in long-term storage")  # this time the sell can't be read
    _run_backfill(monkeypatch, tmp_path, FakeRpc({W: listing("s0", "s1")}, txs))
    trades = Store(tmp_path / "t.db").trades()
    assert [t.signature for t in trades] == ["s1", "s0"]
    assert compute_wallet_stats(W, trades).realized_pnl_sol == pytest.approx(0.1)


def test_rerun_that_reads_the_whole_second_puts_it_back_in_chain_order(tmp_path):
    store = Store(tmp_path / "t.db")
    store.add_trades([Trade("s0", W, COIN, Side.SELL, 0.6, 1000, T0, source="rpc"),  # saved out of order
                      Trade("s1", W, COIN, Side.BUY, 0.5, 1000, T0, source="rpc")])
    store.replace_trades(W, [Trade("s1", W, COIN, Side.BUY, 0.5, 1000, T0, source="rpc"),
                             Trade("s0", W, COIN, Side.SELL, 0.6, 1000, T0, source="rpc")], {"s0", "s1"})
    assert [t.signature for t in store.trades()] == ["s1", "s0"]


def test_upgrading_an_old_database_says_to_restart_a_running_bot(tmp_path, caplog):
    db = tmp_path / "old.db"
    con = sqlite3.connect(db)
    con.execute("CREATE TABLE trades (signature TEXT NOT NULL, wallet TEXT NOT NULL, mint TEXT NOT NULL,"
                " side TEXT NOT NULL, sol_amount REAL NOT NULL, token_amount REAL NOT NULL, ts REAL NOT NULL,"
                " market_cap_sol REAL, price_sol REAL, source TEXT, PRIMARY KEY (signature, wallet, mint, side))")
    con.execute("INSERT INTO trades VALUES ('a', 'w', 'm', 'buy', 1.0, 10, 1, NULL, NULL, 'rpc')")
    con.commit()
    con.close()
    Store(db)
    assert "stop it and start it again" in caplog.text
    caplog.clear()
    Store(db)
    assert caplog.text == ""  # only once


class Clock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t


def _flaky(script, price=150.0):
    """A source whose answers follow `script`: 'ok', 'down' (no connection), 'html' (a login page)."""
    calls = []

    async def fetch(http, start, end):
        what = script[len(calls)] if len(calls) < len(script) else "ok"
        calls.append(what)
        if what == "down":
            raise httpx.ConnectError("getaddrinfo failed")
        if what == "html":
            return httpx.Response(200, text="<html>log in to Wi-Fi</html>").json()
        return [(b, price) for b in range(start, end + 1, BUCKET_S)]
    return fetch, calls


async def test_prices_recover_after_a_network_blip():
    clock = Clock()
    a, a_calls = _flaky(["down", "down"])
    b, b_calls = _flaky(["down", "down"])
    prices = SolUsdPrices(sources=[PriceSource("a", 300, a), PriceSource("b", 300, b)], now=lambda: T0 + 86400,
                          monotonic=clock, retry_pause_s=0)
    assert await prices.price_at(T0) is None and "could not connect" in prices.problem
    assert len(a_calls) == len(b_calls) == 2  # two rounds, not a burst of retries
    clock.t += 30
    assert await prices.price_at(T0) is None  # both resting: no requests
    assert len(a_calls) == 2
    clock.t += 31  # rest over
    assert await prices.price_at(T0) == 150.0 and prices.problem is None


async def test_occasional_failures_never_switch_a_source_off():
    clock = Clock()
    fetch, calls = _flaky(["down", "ok", "down", "ok", "down", "ok"])
    prices = SolUsdPrices(sources=[PriceSource("a", 1, fetch)], now=lambda: T0 + 86400, monotonic=clock,
                          retry_pause_s=0)
    for k in range(3):  # three lookups, days apart, each hitting one failure first
        assert await prices.price_at(T0 + k * 2 * 86400 - 86400 * 4) == 150.0


async def test_a_wifi_login_page_is_not_a_permanent_refusal():
    clock = Clock()
    fetch, calls = _flaky(["html", "html"])
    prices = SolUsdPrices(sources=[PriceSource("a", 300, fetch)], now=lambda: T0 + 86400, monotonic=clock,
                          retry_pause_s=0)
    assert await prices.price_at(T0) is None
    clock.t += 61
    assert await prices.price_at(T0) == 150.0


async def test_live_trade_is_priced_when_the_pc_clock_is_behind():
    asked = []

    async def fetch(http, start, end):
        asked.append((start, end))
        return [(b, 150.0) for b in range(start, end + 1, BUCKET_S) if b < (T0 // BUCKET_S) * BUCKET_S + BUCKET_S]
    trade_ts = (T0 // BUCKET_S + 1) * BUCKET_S + 3  # 3 s into a new candle the exchange hasn't published yet
    prices = SolUsdPrices(sources=[PriceSource("a", 300, fetch)], now=lambda: trade_ts - 20)  # PC 20 s behind
    assert await prices.price_at(trade_ts) == 150.0
    assert asked and asked[0][1] >= trade_ts // BUCKET_S * BUCKET_S
