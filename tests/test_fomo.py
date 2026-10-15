"""`trenchnet fomo`: finding Fomo traders from public transactions."""

from solders.pubkey import Pubkey

from trenchnet.fomo import (FOMO_FEE_ACCOUNT, FOMO_FEE_WALLET, FOMO_GAS_SPONSOR, discover, find_traders,
                            merge_wallet_file, sample_evenly)
from trenchnet.sources.rpc_backfill import USDC

from .test_stablecoin_and_rereads import FakeRpc

PUMP = "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P"
POOL = str(Pubkey.new_unique())  # pool authority (a program-derived address in reality)
T0 = 1_760_000_000


def fomo_tx(user, coin, usdc_paid=150.0, fee=1.5, buy=True, user_signs=False, fee_transfer=True, time=T0):
    """A Fomo trade as jsonParsed returns it: the sponsor signs and pays, the
    user's USDC pays the pool and Fomo's fee (on a buy) or receives the
    proceeds and pays the fee from them (on a sale)."""
    u_usdc, u_coin, pool_usdc, pool_coin = (str(Pubkey.new_unique()) for _ in range(4))
    keys = [FOMO_GAS_SPONSOR] + ([user] if user_signs else []) + [u_usdc, u_coin, FOMO_FEE_ACCOUNT, pool_usdc,
                                                                    pool_coin, PUMP]
    signers = {FOMO_GAS_SPONSOR, user}
    idx = {k: i for i, k in enumerate(keys)}

    def bal(acct, owner, mint, amount):
        return {"accountIndex": idx[acct], "owner": owner, "mint": mint, "uiTokenAmount": {"uiAmountString": str(amount)}}

    if buy:
        pre = [bal(u_usdc, user, USDC, 500), bal(u_coin, user, coin, 0), bal(FOMO_FEE_ACCOUNT, FOMO_FEE_WALLET, USDC, 10),
               bal(pool_usdc, POOL, USDC, 1000), bal(pool_coin, POOL, coin, 10**6)]
        post = [bal(u_usdc, user, USDC, 500 - usdc_paid), bal(u_coin, user, coin, 1000),
                bal(FOMO_FEE_ACCOUNT, FOMO_FEE_WALLET, USDC, 10 + fee), bal(pool_usdc, POOL, USDC, 1000 + usdc_paid - fee),
                bal(pool_coin, POOL, coin, 10**6 - 1000)]
    else:
        pre = [bal(u_usdc, user, USDC, 0), bal(u_coin, user, coin, 1000), bal(FOMO_FEE_ACCOUNT, FOMO_FEE_WALLET, USDC, 10),
               bal(pool_usdc, POOL, USDC, 1000), bal(pool_coin, POOL, coin, 10**6)]
        post = [bal(u_usdc, user, USDC, usdc_paid - fee), bal(u_coin, user, coin, 0),
                bal(FOMO_FEE_ACCOUNT, FOMO_FEE_WALLET, USDC, 10 + fee), bal(pool_usdc, POOL, USDC, 1000 - usdc_paid),
                bal(pool_coin, POOL, coin, 10**6 + 1000)]
    inner = [{"program": "spl-token", "parsed": {"type": "transferChecked", "info": {
        "source": u_usdc, "destination": FOMO_FEE_ACCOUNT, "authority": user if user_signs else FOMO_GAS_SPONSOR,
        "mint": USDC, "tokenAmount": {"uiAmountString": str(fee)}}}}] if fee_transfer else []
    n = len(keys)
    return {"blockTime": time,
            "transaction": {"message": {"accountKeys": [{"pubkey": k, "signer": k in signers} for k in keys],
                                        "instructions": []}},
            "meta": {"err": None, "fee": 5000, "preBalances": [10**10] * n, "postBalances": [10**10 - 5000] + [10**10] * (n - 1),
                     "preTokenBalances": pre, "postTokenBalances": post,
                     "innerInstructions": [{"index": 0, "instructions": inner}]}}


def test_the_trader_is_whoever_paid_fomos_fee():
    user = str(Pubkey.new_unique())
    assert find_traders(fomo_tx(user, "CoinA")) == ([user], "fee")
    assert find_traders(fomo_tx(user, "CoinA", user_signs=True)) == ([user], "fee")
    # No parsed fee transfer: fall back to the signer that isn't Fomo's sponsor.
    assert find_traders(fomo_tx(user, "CoinA", user_signs=True, fee_transfer=False)) == ([user], "signer")
    assert find_traders(fomo_tx(user, "CoinA", fee_transfer=False)) == ([], None)


def test_sample_is_spread_over_the_whole_period():
    assert sample_evenly(list(range(10)), 5) == [0, 2, 4, 6, 8]
    assert sample_evenly([1, 2], 5) == [1, 2]


async def test_discover_ranks_wallets_by_volume():
    big, small = str(Pubkey.new_unique()), str(Pubkey.new_unique())
    txs = {
        "t1": fomo_tx(big, "CoinA", usdc_paid=900),
        "t2": fomo_tx(big, "CoinA", usdc_paid=1200, buy=False, user_signs=True),
        "t3": fomo_tx(small, "CoinB", usdc_paid=20),
        "t4": fomo_tx(str(Pubkey.new_unique()), "CoinC", fee_transfer=False),  # trader not identifiable
    }
    listing = [{"signature": s, "err": None, "blockTime": T0} for s in txs]
    found = await discover(FakeRpc({FOMO_FEE_ACCOUNT: listing}, txs), sample=10, retry_pause_s=0)
    assert [t.wallet for t in found.ranked()] == [big, small]
    b = found.traders[big]
    assert (b.trades, b.buys, b.sells, b.usd, b.own_address_in_tx) == (2, 1, 1, 900 + 1200 - 1.5, 1)  # sale proceeds are net of the fee
    assert (found.read, found.fomo_trades, found.no_trade) == (4, 3, 1)


def test_wallet_file_is_added_to_not_overwritten(tmp_path):
    from trenchnet.cli import _read_wallets
    from trenchnet.fomo import Trader

    a, b = str(Pubkey.new_unique()), str(Pubkey.new_unique())
    out = tmp_path / "fomo.txt"
    out.write_text(f"{a},mine\n")
    assert merge_wallet_file(str(out), [Trader(a), Trader(b, trades=3)], lambda t: f"{t.trades} trades") == (1, 1)
    wallets, aliases = _read_wallets(str(out))
    assert wallets == [a, b] and aliases == {a: "mine", b: f"fomo-{b[:4]}"}


def test_fomo_command_writes_the_top_wallets(tmp_path, monkeypatch, capsys):
    import trenchnet.cli as cli

    whale, other = str(Pubkey.new_unique()), str(Pubkey.new_unique())
    txs = {"t1": fomo_tx(whale, "CoinA", usdc_paid=900), "t2": fomo_tx(whale, "CoinB", usdc_paid=50),
           "t3": fomo_tx(other, "CoinA", usdc_paid=30)}
    listing = [{"signature": s, "err": None, "blockTime": T0} for s in txs]
    monkeypatch.setattr(cli, "_rpc_from", lambda url, rps=None: FakeRpc({FOMO_FEE_ACCOUNT: listing}, txs))
    out = tmp_path / "fomo.txt"
    cli.main(["fomo", "--rpc", "https://x/", "--db", str(tmp_path / "t.db"), "--out", str(out)])
    text = capsys.readouterr().out
    assert "3 trades by 2 wallets" in text and whale in text
    assert "wasn't in the transaction" in text  # the user's address only appears through token accounts
    lines = out.read_text().splitlines()
    assert len(lines) == 1 and lines[0].startswith(f"{whale},fomo-{whale[:4]}")  # `other` made only 1 trade
