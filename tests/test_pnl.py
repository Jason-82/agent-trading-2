import json

import pytest

from trenchnet.brokers.base import Fill
from trenchnet.cli import main
from trenchnet.pnl import build_pnl, format_pnl, parse_since
from trenchnet.store import Store

DAY = 86400.0
T0 = 1_760_000_000.0  # 2025-10-09 08:53:20 UTC


def F(mint, side, sol, tokens, ts, reason="r", mode="paper", fee=0.0):
    return Fill(mint, side, sol, tokens, sol / tokens, fee, ts, mode, reason).to_dict()


FILLS = [
    # A: take-profit half at 2x, rest at 1.5x -> +0.075 on 0.1
    F("A", "buy", 0.1, 1000, T0, fee=0.001),
    F("A", "sell", 0.1, 500, T0 + 600, "take-profit 1: 2.0x entry", fee=0.001),
    F("A", "sell", 0.075, 500, T0 + 1200, "trailing stop", fee=0.001),
    # B: stop-loss next day -> -0.04 on 0.1
    F("B", "buy", 0.1, 1000, T0 + DAY),
    F("B", "sell", 0.06, 1000, T0 + DAY + 300, "stop-loss"),
    # C: still open
    F("C", "buy", 0.2, 2000, T0 + 2 * DAY),
    # a live fill must not leak into the paper report
    F("L", "buy", 0.05, 100, T0, mode="live"),
]


def test_realized_closed_trades_and_days():
    r = build_pnl(FILLS, {"C": (2e-4, T0 + 2 * DAY + 60)}, mode="paper", now=T0 + 3 * DAY)
    assert r.realized_sol == pytest.approx(0.075 - 0.04)
    assert [c.mint for c in r.closed] == ["A", "B"]
    a, b = r.closed
    assert (a.cost_sol, a.proceeds_sol) == (pytest.approx(0.1), pytest.approx(0.175))
    assert a.return_pct == pytest.approx(75.0) and a.exit_reason == "trailing stop"
    assert b.realized_sol == pytest.approx(-0.04)
    assert [(d, round(p, 4), n) for d, p, n in r.by_day] == [("2025-10-09", 0.075, 2), ("2025-10-10", -0.04, 1)]
    assert r.n_buys == 3 and r.deployed_sol == pytest.approx(0.4)
    assert r.fees_sol == pytest.approx(0.003)
    [c] = r.open
    assert c.mint == "C" and c.unrealized_sol == pytest.approx(2000 * 2e-4 - 0.2)
    assert len(r.wins) == 1 and len(r.losses) == 1


def test_window_keeps_full_cost_basis():
    # Window starts after A's take-profit: only A's final sell and B count.
    r = build_pnl(FILLS, {}, mode="paper", now=T0 + 3 * DAY, since=T0 + 900)
    assert r.realized_sol == pytest.approx(0.025 - 0.04)
    assert [c.mint for c in r.closed] == ["A", "B"]
    assert r.closed[0].realized_sol == pytest.approx(0.075)  # whole position, incl. earlier take-profit
    assert r.n_buys == 2


def test_open_position_without_price_is_reported_not_guessed():
    r = build_pnl(FILLS, {}, mode="paper", now=T0 + 3 * DAY)
    assert r.open[0].mark_price is None and r.unrealized_sol is None
    assert "no price seen yet" in format_pnl(r, "x.db")


def test_live_mode_is_separate():
    r = build_pnl(FILLS, {}, mode="live", now=T0)
    assert r.n_buys == 1 and [p.mint for p in r.open] == ["L"] and r.realized_sol == 0


def test_parse_since():
    now = T0
    assert parse_since("24h", now) == now - DAY
    assert parse_since("7d", now) == now - 7 * DAY
    assert parse_since("90m", now) == now - 5400
    assert parse_since("2w", now) == now - 14 * DAY
    assert parse_since("2025-10-09", now) == 1759968000.0
    with pytest.raises(ValueError):
        parse_since("yesterday", now)


def test_last_price_prefers_newest_source():
    from trenchnet.models import Side, Trade

    s = Store()
    s.add_trade(Trade("sig", "w", "M", Side.BUY, 1.0, 1000, 100.0))
    assert s.last_price("M") == (pytest.approx(1e-3), 100.0)
    s.save_price("M", 5e-3, 200.0)
    assert s.last_price("M") == (5e-3, 200.0)
    s.save_price("M", 9e-3, 150.0)  # older than what we have: ignored
    assert s.last_price("M") == (5e-3, 200.0)
    # A tracked trade after the last saved mark (e.g. the bot was killed between ticks) wins.
    s.add_trade(Trade("sig2", "w2", "M", Side.SELL, 2.0, 100, 300.0))
    assert s.last_price("M") == (pytest.approx(2e-2), 300.0)
    assert s.last_price("unknown") is None


def _db_with_fills(path):
    s = Store(path)
    for f in FILLS:
        s.add_fill(**f)
    s.save_price("C", 2e-4, T0 + 2 * DAY)
    s.close()


def test_cli_pnl_db_flag_before_or_after_command(tmp_path, capsys):
    db = str(tmp_path / "t.db")
    _db_with_fills(db)
    main(["pnl", "--db", db])
    out = capsys.readouterr().out
    assert "PAPER trading P&L" in out and "LIVE trading P&L" in out
    assert "+0.0350 SOL" in out
    main(["--db", db, "pnl", "--mode", "paper", "--json"])
    [rep] = json.loads(capsys.readouterr().out)
    assert rep["summary"]["closed_trades"] == 2 and rep["open"][0]["mark_price"] == 2e-4


def test_cli_pnl_missing_db_is_not_created(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    with pytest.raises(SystemExit) as e:
        main(["pnl"])
    assert "no database" in str(e.value.code)
    assert not (tmp_path / "trenchnet.db").exists()


def test_cli_pnl_empty_db(tmp_path):
    db = str(tmp_path / "e.db")
    Store(db).close()
    with pytest.raises(SystemExit) as e:
        main(["pnl", "--db", db])
    assert "no paper or live trades recorded" in str(e.value.code)


async def test_engine_saves_marks_for_held_tokens():
    from trenchnet.cases import Case
    from trenchnet.config import Config
    from trenchnet.engine import Engine
    from trenchnet.judge import HeuristicJudge
    from trenchnet.brokers.paper import PaperBroker
    from trenchnet.reporter import Reporter

    cfg = Config()
    store = Store()
    eng = Engine(cfg, store, {}, HeuristicJudge(cfg), Reporter(cfg), PaperBroker(cfg.paper), out=lambda s: None)
    eng.portfolio.apply(Fill("M", "buy", 0.1, 1000, 1e-4, 0, 0, "paper", "x"))
    case = eng.cases["M"] = Case(mint="M", opened_ts=0)
    case.update_price(3e-4, 50.0)
    await eng.tick(60.0)
    assert store.last_price("M") == (3e-4, 50.0)
