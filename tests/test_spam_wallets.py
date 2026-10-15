"""Spam airdrops, inactive wallets, and who paid the token-account rent."""

import pytest

from trenchnet.models import Side
from trenchnet.sources.rpc_backfill import BackfillResult, analyze_wallet_tx, drop_unrelated_transfers
from trenchnet.stats import SelectionCriteria, compute_wallet_stats, select

from .conftest import mk

W, SPAMMER, ATA = "Wallet111111111111111111111111111111111111", "Spammer", "AtaW"
RENT = 2_039_280


def _tx(native_pre, native_post, keys, pre_tok, post_tok, fee_payer_is_wallet=True):
    n = len(keys)
    pre, post = [0] * n, [0] * n
    w = keys.index(W)
    pre[w], post[w] = native_pre, native_post
    ata = keys.index(ATA)
    if pre_tok:
        pre[ata] = RENT
    if post_tok:
        post[ata] = RENT

    def bal(v):
        return [{"accountIndex": ata, "owner": W, "mint": "SpamMint", "uiTokenAmount": {"uiAmountString": str(v)}}] if v is not None else []

    return {"blockTime": 1, "transaction": {"message": {"accountKeys": [{"pubkey": k} for k in keys]}},
            "meta": {"err": None, "fee": 5000, "preBalances": pre, "postBalances": post,
                     "preTokenBalances": bal(pre_tok), "postTokenBalances": bal(post_tok)}}


def test_spam_drop_paid_by_sender_shows_no_sol_change():
    # The spammer pays the fee and the new account's rent; the wallet's SOL doesn't move.
    v = analyze_wallet_tx(_tx(10**9, 10**9, [SPAMMER, W, ATA], None, 10), W, "s")
    assert v.trade.side is Side.TRANSFER_IN and v.sol_delta == 0.0


def test_buy_that_opens_the_token_account_still_excludes_rent():
    v = analyze_wallet_tx(_tx(10**9, 10**9 - 500_000_000 - 5000 - RENT, [W, ATA], None, 1000), W, "s")
    assert v.trade.side is Side.BUY and v.trade.sol_amount == pytest.approx(0.5)


def test_sell_that_closes_the_token_account_excludes_the_refund():
    v = analyze_wallet_tx(_tx(10**9, 10**9 + 800_000_000 - 5000 + RENT, [W, ATA], 1000, None), W, "s")
    assert v.trade.side is Side.SELL and v.trade.sol_amount == pytest.approx(0.8)


def test_unrelated_transfers_are_dropped_related_ones_kept():
    res = BackfillResult(trades=[
        mk(W, "Spam1", "transfer_in", 0, 10, 1), mk(W, "Spam2", "transfer_in", 0, 1, 2),
        mk(W, "Real", "buy", 1.0, 1000, 3), mk(W, "Real", "transfer_out", 0, 1000, 4),
    ])
    drop_unrelated_transfers(res)
    assert [(t.mint, t.side.value) for t in res.trades] == [("Real", "buy"), ("Real", "transfer_out")]
    assert res.ignored["unrelated_transfer"] == 2


def test_received_only_tokens_are_not_positions():
    trades = [mk(W, f"Spam{i}", "transfer_in", 0, 10, i) for i in range(50)]
    trades += [mk(W, "M1", "buy", 1.0, 1000, 100), mk(W, "M1", "sell", 2.0, 1000, 5000)]
    s = compute_wallet_stats(W, trades)
    assert s.n_tokens == 1 and s.open_positions == 0 and s.closed_positions == 1


def test_wallet_with_no_buys_or_sells_gets_a_plain_reason():
    s = compute_wallet_stats(W, [mk(W, f"Spam{i}", "transfer_in", 0, 10, i) for i in range(380)])
    sel = select(s, SelectionCriteria())
    assert not sel.eligible
    assert sel.reasons == ["no buys or sells in the downloaded history (only 380 token transfers): "
                           "inactive, a bot, or it trades from another wallet"]
