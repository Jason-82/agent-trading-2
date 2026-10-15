"""Regression tests for the review of the pnl / Windows / --db change."""

import asyncio
import logging
import signal
import sqlite3

import pytest

from trenchnet.brokers.base import Fill
from trenchnet.cli import _cmd, _install_stop_handlers, main
from trenchnet.config import Config
from trenchnet.pnl import build_pnl, format_pnl, parse_since
from trenchnet.portfolio import Portfolio
from trenchnet.sources.replay import write_events
from trenchnet.sources.synthetic import article_scenario
from trenchnet.store import Store

T0 = 1_760_000_000.0


def F(mint, side, sol, tokens, ts, reason="r", mode="paper", fee=0.0):
    price = sol / tokens if tokens and sol else 0.0
    return Fill(mint, side, sol, tokens, price, fee, ts, mode, reason).to_dict()


# -- backtests never count as paper trading ---------------------------------

def test_replay_and_demo_fills_are_backtest_and_replaced(tmp_path, capsys):
    db = str(tmp_path / "t.db")
    main(["demo", "--db", db])
    capsys.readouterr()
    modes = {f["mode"] for f in Store(db).fills()}
    assert modes == {"backtest"}
    with pytest.raises(SystemExit) as e:
        main(["pnl", "--db", db])
    assert "--mode backtest" in str(e.value.code)

    events = tmp_path / "events.jsonl"
    write_events(events, article_scenario().live)
    for _ in range(2):  # re-running a backtest replaces it instead of stacking
        main(["--db", db, "replay", str(events), "--no-llm"])
    capsys.readouterr()
    main(["pnl", "--db", db, "--mode", "backtest"])
    out = capsys.readouterr().out
    assert "BACKTEST trading P&L" in out and "Buys                1," in out


# -- reconciliation is persisted and P&L-neutral -----------------------------

def test_reconcile_drop_and_resize():
    fills = [
        F("N", "buy", 0.1, 1000, T0, mode="live"),
        F("N", "reconcile", 0.0, 0.0, T0 + 60, "reconcile: wallet 0 vs records 1000", mode="live"),
        F("M", "buy", 0.1, 1000, T0, mode="live"),
        F("M", "reconcile", 0.0, 900, T0 + 60, "reconcile: wallet 900 vs records 1000", mode="live"),
        F("M", "sell", 0.2, 900, T0 + 120, "trailing stop", mode="live"),
    ]
    r = build_pnl(fills, {"N": (5e-4, T0)}, mode="live", now=T0 + 200)
    assert r.open == []  # N no longer shown as an open position
    assert [(c.mint, round(c.onchain_tokens)) for c in r.reconciled] == [("N", 0), ("M", 900)]
    [m] = r.closed
    assert m.realized_sol == pytest.approx(0.2 - 0.09)
    assert m.cost_sol == pytest.approx(0.09)  # cost scaled with the tokens the wallet actually had
    assert r.realized_sol == pytest.approx(0.11)
    text = format_pnl(r, "x.db")
    assert "proceeds unknown" in text and "Unrealized" not in text

    pf = Portfolio(1.0)
    for f in fills:
        pf.apply(Fill(**{k: f[k] for k in Fill.__dataclass_fields__}))
    assert pf.realized_pnl_sol == pytest.approx(0.11) and not pf.holdings


# -- output details ------------------------------------------------------------

def _two_closed():
    return [F("A", "buy", 0.1, 100, T0), F("A", "sell", 0.2, 100, T0 + 60),
            F("B", "buy", 0.1, 100, T0 + 100), F("B", "sell", 0.05, 100, T0 + 160)]


def test_recent_zero_lists_no_trades_and_negative_is_rejected(tmp_path, capsys):
    r = build_pnl(_two_closed(), {}, mode="paper", now=T0 + 999)
    assert "Recent closed trades" not in format_pnl(r, "x.db", recent=0)
    assert format_pnl(r, "x.db", recent=10).count(" exit: ") == 2
    with pytest.raises(SystemExit):
        main(["pnl", "--recent", "-1"])


def test_since_far_past_is_clamped_and_formats():
    assert parse_since("1000000d", T0) == 0.0
    assert parse_since("1969-12-31", T0) == 0.0
    r = build_pnl(_two_closed(), {}, mode="paper", now=T0 + 999, since=0.0)
    assert "since 1970-01-01" in format_pnl(r, "x.db")


def test_live_fees_are_labelled_buy_side_only():
    fills = [F("A", "buy", 0.1, 100, T0, mode="live", fee=0.001), F("A", "sell", 0.2, 100, T0 + 60, mode="live")]
    r = build_pnl(fills, {}, mode="live", now=T0 + 99)
    assert r.fees_sol == pytest.approx(0.001)
    assert "on 1 buys (sell fees come out of sale proceeds" in format_pnl(r, "x.db")
    assert r.to_dict()["fees_note"].startswith("buy-side")


# -- CLI robustness --------------------------------------------------------------

def test_db_folder_and_non_database_exit_cleanly(tmp_path):
    with pytest.raises(SystemExit) as e:
        main(["pnl", "--db", str(tmp_path)])
    assert "is a folder" in str(e.value.code)
    bad = tmp_path / "notes.txt"
    bad.write_text("not a database " * 100)
    with pytest.raises(SystemExit) as e:
        main(["pnl", "--db", str(bad)])
    assert "cannot open" in str(e.value.code)


def test_hints_keep_the_db_flag():
    cfg = Config()
    assert _cmd("pnl", cfg) == "trenchnet pnl"
    cfg.db_path = "my folder/fresh.db"
    assert _cmd("pnl", cfg) == "trenchnet pnl --db 'my folder/fresh.db'"


def test_save_marks_survives_a_locked_database(caplog):
    from trenchnet.brokers.paper import PaperBroker
    from trenchnet.cases import Case
    from trenchnet.engine import Engine
    from trenchnet.judge import HeuristicJudge
    from trenchnet.reporter import Reporter

    cfg = Config()
    store = Store()
    eng = Engine(cfg, store, {}, HeuristicJudge(cfg), Reporter(cfg), PaperBroker(cfg.paper), out=lambda s: None)
    eng.portfolio.apply(Fill("M", "buy", 0.1, 1000, 1e-4, 0, 0, "paper", "x"))
    eng.cases["M"] = Case(mint="M", opened_ts=0)
    eng.cases["M"].update_price(1.1e-4, 5.0)

    def locked(*a, **k):
        raise sqlite3.OperationalError("database is locked")

    store.save_price = locked
    with caplog.at_level(logging.WARNING):
        eng.save_marks()
    assert "could not save last prices" in caplog.text


# -- Ctrl-C handling, including the Windows path ------------------------------------

@pytest.mark.parametrize("windows", [False, True])
async def test_first_ctrl_c_stops_gracefully_second_forces(monkeypatch, windows):
    loop = asyncio.get_running_loop()
    stopped = []
    installed: dict[int, object] = {}
    forced = []
    runner_handler = None

    if windows:
        def no_loop_handlers(*a, **k):
            raise NotImplementedError

        monkeypatch.setattr(loop, "add_signal_handler", no_loop_handlers)
        def runner_handler(signum, frame):
            forced.append(signum)

        def fake_signal(sig, handler):
            old = installed.get(sig, runner_handler)
            installed[sig] = handler
            return old

        monkeypatch.setattr(signal, "signal", fake_signal)

    restore = _install_stop_handlers(loop, lambda: stopped.append(True))
    try:
        if windows:
            handler = installed[signal.SIGINT]
            handler(signal.SIGINT, None)
            await asyncio.sleep(0)
            assert stopped == [True] and forced == []  # the stop is handed to the loop; nothing is cancelled
            handler(signal.SIGINT, None)
            assert forced == [signal.SIGINT]  # second press: asyncio's own handler (force quit)
        else:
            signal.raise_signal(signal.SIGINT)
            await asyncio.sleep(0.05)
            assert stopped == [True]
    finally:
        restore()
    if windows:
        assert installed[signal.SIGINT] is runner_handler  # previous handler put back
