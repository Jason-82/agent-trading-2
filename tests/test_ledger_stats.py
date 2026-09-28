import pytest

from trenchnet.ledger import Ledger
from trenchnet.stats import SelectionCriteria, compute_wallet_stats, select

from .conftest import mk


def test_average_cost_realized_pnl():
    led = Ledger()
    led.apply(mk("w", "m", "buy", 1.0, 1000, 0))
    led.apply(mk("w", "m", "buy", 2.0, 1000, 10))  # avg cost 0.0015
    p = led.apply(mk("w", "m", "sell", 1.5, 500, 20))
    assert p.realized_pnl_sol == pytest.approx(1.5 - 0.75)
    assert p.status == "reduced"
    assert p.sold_fraction == pytest.approx(0.25)
    p = led.apply(mk("w", "m", "sell", 3.0, 1500, 30))
    assert p.realized_pnl_sol == pytest.approx(0.75 + 3.0 - 2.25)
    assert p.status == "exited"
    assert p.hold_seconds(100) == 30


def test_sale_without_observed_buy_is_unattributed():
    led = Ledger()
    p = led.apply(mk("w", "m", "sell", 2.0, 1000, 0))
    assert p.realized_pnl_sol == 0
    assert p.unattributed_proceeds_sol == pytest.approx(2.0)
    assert p.missing_basis


def test_transfer_is_not_a_sale():
    led = Ledger()
    led.apply(mk("w", "m", "buy", 1.0, 1000, 0))
    p = led.apply(mk("w", "m", "transfer_out", 0, 1000, 5))
    assert p.realized_pnl_sol == 0
    assert p.sold_tokens == 0
    assert p.sold_fraction == 0
    assert p.status == "transferred_out"
    assert p.closed_by_transfer


def test_partial_transfer_moves_basis_proportionally():
    led = Ledger()
    led.apply(mk("w", "m", "buy", 1.0, 1000, 0))
    led.apply(mk("w", "m", "transfer_out", 0, 400, 5))
    p = led.apply(mk("w", "m", "sell", 1.2, 600, 10))
    assert p.realized_pnl_sol == pytest.approx(1.2 - 0.6)
    assert p.status == "exited"


def _round_trips(wallet, n, hold, mult, start=0.0, size=1.0, mint_prefix="m"):
    out = []
    for i in range(n):
        t = start + i * 10_000
        out.append(mk(wallet, f"{mint_prefix}{i}", "buy", size, 1000, t))
        out.append(mk(wallet, f"{mint_prefix}{i}", "sell", size * mult(i), 1000, t + hold))
    return out


def test_stats_and_selection_patient_holder():
    trades = _round_trips("w", 8, hold=4 * 3600, mult=lambda i: 1.5 if i % 4 else 0.8)
    s = compute_wallet_stats("w", trades)
    assert s.closed_positions == 8
    assert s.win_rate == pytest.approx(6 / 8)
    assert s.median_hold_s == 4 * 3600
    assert s.quick_flip_share == 0
    assert s.median_entry_sol == 1.0
    assert s.top_token_profit_share == pytest.approx(1 / 6)
    sel = select(s, SelectionCriteria())
    assert sel.eligible and sel.score > 0


def test_selection_rejects_scalper_and_concentrated_winner():
    scalper = compute_wallet_stats("s", _round_trips("s", 10, hold=60, mult=lambda i: 1.3))
    assert not select(scalper, SelectionCriteria()).eligible
    lucky = compute_wallet_stats("l", _round_trips("l", 8, hold=7200, mult=lambda i: 15 if i == 0 else 0.7))
    sel = select(lucky, SelectionCriteria())
    assert not sel.eligible
    assert any("from one token" in r for r in sel.reasons)


def test_recent_open_positions_are_reported():
    trades = _round_trips("w", 6, hold=3600, mult=lambda i: 1.5)
    trades += [mk("w", f"open{i}", "buy", 1.0, 1000, 100_000 + i) for i in range(6)]
    s = compute_wallet_stats("w", trades)
    assert s.open_positions == 6
    assert s.recent_open_share == pytest.approx(0.6)
