"""End-to-end run of the article's scenario, fully offline."""

import pytest

from trenchnet.brokers.paper import PaperBroker
from trenchnet.config import Config
from trenchnet.engine import Engine, tracked_from_roster
from trenchnet.judge import HeuristicJudge
from trenchnet.profiler import build_roster
from trenchnet.reporter import Reporter
from trenchnet.sources.synthetic import article_scenario
from trenchnet.store import Store


@pytest.fixture
async def run():
    cfg = Config()
    cfg.llm.use_llm_profiles = cfg.llm.use_llm_judge = cfg.llm.use_llm_reports = False
    sc = article_scenario()
    store = Store()
    store.add_trades(sc.history)
    await build_roster(store, cfg, None, aliases=sc.aliases, now=sc.start_ts)
    tracked = tracked_from_roster(store.roster())
    lines: list[str] = []
    engine = Engine(cfg, store, tracked, HeuristicJudge(cfg), Reporter(cfg), PaperBroker(cfg.paper),
                    history=sc.history, out=lines.append)
    for ev in sc.live:
        await engine.handle(ev)
    await engine.tick(engine.now + 7 * 3600)
    return sc, store, engine, lines


async def test_roster_selection(run):
    sc, store, _, _ = run
    included = {r["alias"] for r in store.roster()}
    assert included == {"A", "B", "C", "D"}
    everyone = {r["alias"]: r for r in store.roster(included_only=False)}
    assert "within 5 min" in " ".join(everyone["E"]["reasons"])
    assert "from one token" in " ".join(everyone["F"]["reasons"])
    assert everyone["A"]["profile"]["style"] == "accumulator"
    assert everyone["D"]["profile"]["style"] == "patient_holder"


async def test_trades_only_the_broadened_token(run):
    sc, store, engine, lines = run
    buys = [f for f in store.fills() if f["side"] == "buy"]
    assert [f["mint"] for f in buys] == [sc.token]
    assert not engine.portfolio.holdings
    assert engine.portfolio.realized_pnl_sol > 0
    assert any("buyers are one recurring group" in line for line in lines)  # the DECOY skip


async def test_reports_chain_and_cite_events(run):
    sc, store, _, _ = run
    reports = list(reversed(store.reports(sc.token, limit=100)))
    assert len(reports) >= 4
    assert reports[0]["previous_id"] is None
    for prev, cur in zip(reports, reports[1:]):
        assert cur["previous_id"] == prev["id"]
    for r in reports:
        assert r["unsupported_citations"] == []
        for f in r["report"]["observed_facts"]:
            assert set(f["signatures"]) <= set(r["evidence_signatures"])
    d_report = next(r for r in reports if "D bought" in r["report"]["headline"])
    assert "broadened" in " ".join(d_report["report"]["interpretations"])


async def test_quiet_case_is_still_captured(run):
    sc, store, engine, _ = run
    lone = store.trades(mint=sc.lone)
    assert [t.side.value for t in lone] == ["buy", "sell"]
    assert store.reports(sc.lone) == []
    assert engine.cases[sc.lone].assessments[-1]["route"] == "observe"
