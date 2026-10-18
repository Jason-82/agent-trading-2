"""Live feed noise: spam pushed into followed wallets, old leftovers sold for
nothing, and a wallet flipping in and out of a token."""

import pytest

from trenchnet.models import Side, Trade
from trenchnet.store import Store

from .test_engine_order import COIN, T0, W, W2
from .test_engine_order import engine as bare_engine


def engine():
    """Two followed wallets with a real trading history (so events get analyzed)."""
    e = bare_engine()
    for w in e.tracked.values():
        w.profile = {"style": "mixed", "summary": "test wallet"}
        w.stats.update(closed_positions=40, n_tokens=60, median_entry_sol=1.0, median_buy_sol=1.0,
                       p25_hold_s=30, p75_hold_s=600)
    return e


def buy(sig, wallet, sol, at, mint=COIN, src="pumpportal:pump"):
    return Trade(sig, wallet, mint, Side.BUY, sol, 12.8 if sol < 0.005 else 1000, T0 + at, source=src)


def sell(sig, wallet, sol, at, mint=COIN, tokens=1000):
    return Trade(sig, wallet, mint, Side.SELL, sol, tokens, T0 + at, source="pumpportal:pump")


async def test_spam_buys_for_nothing_are_not_trades():
    e = engine()
    lines = []
    e.out = lines.append
    for i, w in enumerate((W, W2)):
        await e.handle(buy(f"spam{i}", w, 0.0000012, i))
    assert lines == [] and COIN not in e.cases  # nothing judged, no case, no "group is accumulating"
    stored = e.store.trades()
    assert {t.side for t in stored} == {Side.TRANSFER_IN} and all(t.sol_amount == 0 for t in stored)
    assert e.ledger.get(W, COIN).tokens == pytest.approx(12.8) and not e.ledger.get(W, COIN).traded


async def test_old_leftovers_sold_for_nothing_are_quiet_but_tracked():
    e = engine()
    lines = []
    e.out = lines.append
    await e.handle(sell("cleanup", W, 0.0002, 0, mint="OldCoin", tokens=50))
    assert lines == [] and "OldCoin" not in e.cases
    assert e.ledger.get(W, "OldCoin").n_sells == 1
    # A sale for nothing in a token the bot follows (a rug) is still judged and shown.
    await e.handle(buy("b1", W, 0.5, 10))
    await e.handle(sell("rug", W, 0.0001, 20))
    assert any("sell 0.000 SOL" in line for line in lines)


async def test_a_wallet_flipping_in_and_out_does_not_get_a_report_every_time():
    e = engine()
    await e.handle(buy("a1", W2, 5.0, 0))
    await e.handle(buy("b1", W, 1.0, 10))  # second wallet joins: first report
    for k in range(6):  # W flips in and out every minute while W2 holds
        await e.handle(sell(f"s{k}", W, 1.1, 60 + 120 * k))
        await e.handle(buy(f"b{k + 2}", W, 1.0, 120 + 120 * k))
    assert len(e.store.reports(COIN, limit=100)) == 1
    await e.handle(sell("s_late", W, 1.1, 60 + 120 * 6 + 900))  # 15 minutes on: worth a fresh look
    assert len(e.store.reports(COIN, limit=100)) == 2
    await e.handle(sell("a_out", W2, 6.0, 2000))  # the last holder leaves: reported at once
    assert len(e.store.reports(COIN, limit=100)) == 3


def test_next_backfill_replaces_a_spam_buy_saved_from_the_live_feed():
    store = Store()
    store.add_trades([Trade("spam", W, COIN, Side.BUY, 0.0, 12.8, T0, source="pumpportal:pump")])
    store.replace_trades(W, [Trade("spam", W, COIN, Side.TRANSFER_IN, 0.0, 12.8, T0, source="rpc")], {"spam"})
    assert [(t.side, t.source) for t in store.trades()] == [(Side.TRANSFER_IN, "rpc")]
