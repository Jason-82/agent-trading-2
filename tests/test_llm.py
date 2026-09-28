"""Claude-backed stages, exercised against a fake client (no network)."""

from types import SimpleNamespace

import pytest
from anthropic.lib._parse._transform import transform_schema

from trenchnet.cases import Case, TrackedWallet
from trenchnet.config import Config
from trenchnet.judge import Assessment, ClaudeJudge
from trenchnet.llm import LLM
from trenchnet.profiler import Evidence, Profiler, TraderProfile
from trenchnet.reporter import Fact, Reporter, SituationReport
from trenchnet.stats import compute_wallet_stats

from .conftest import mk


class FakeMessages:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    async def parse(self, **kw):
        self.calls.append(kw)
        r = self.responses.pop(0)
        if isinstance(r, Exception):
            raise r
        return r


def fake_llm(*responses):
    msgs = FakeMessages(responses)
    return LLM(client=SimpleNamespace(messages=msgs)), msgs


def ok(parsed):
    return SimpleNamespace(stop_reason="end_turn", stop_details=None, parsed_output=parsed)


REFUSAL = SimpleNamespace(stop_reason="refusal", stop_details=SimpleNamespace(category=None), parsed_output=None)


@pytest.mark.parametrize("model", [TraderProfile, Assessment, SituationReport])
def test_schemas_are_structured_output_compatible(model):
    schema = transform_schema(model.model_json_schema())
    assert schema["type"] == "object"
    assert schema.get("additionalProperties") is False


SNAP = {
    "token": {"tracked_buyers": 2},
    "event": {"side": "buy", "size_vs_typical_entry": 1.0, "is_first_buy_in_token": True},
    "wallet_profile": None, "relationships": {"thin_share": 0}, "last_report": None,
}


async def test_claude_judge_clamps_and_uses_cheap_model():
    cfg = Config()
    a = Assessment(behavior_fit="fits", behavior_fit_confidence=1.4, position_intent="standard",
                   position_intent_confidence=0.7, history="sufficient", attention="first_analysis",
                   attention_confidence=0.9)
    llm, msgs = fake_llm(ok(a))
    got, source = await ClaudeJudge(cfg, llm).assess(SNAP)
    assert source == cfg.llm.judge_model
    assert got.behavior_fit_confidence == 1.0
    assert msgs.calls[0]["model"] == "claude-haiku-4-5"
    assert "output_config" not in msgs.calls[0]  # Haiku 4.5 takes no effort parameter


async def test_claude_judge_falls_back_on_refusal():
    llm, _ = fake_llm(REFUSAL)
    got, source = await ClaudeJudge(Config(), llm).assess(SNAP)
    assert source == "heuristic-fallback"
    assert got.attention == "first_analysis"


async def test_profiler_resolves_trade_refs_and_sends_effort():
    trades = [mk("w", f"m{i}", side, 1.0, 1000, i * 100 + (50 if side == "sell" else 0))
              for i in range(3) for side in ("buy", "sell")]
    prof = TraderProfile(
        style="mixed", summary="s", typical_entry="1 SOL", holding_behavior="h", profit_concentration="p",
        inclusion_evidence=[Evidence(claim="c", trade_refs=["t1", "t99"])], attention_triggers=[],
        missing_history=[], recommend_include=True,
    )
    llm, msgs = fake_llm(ok(prof))
    cfg = Config()
    out, source = await Profiler(cfg, llm).profile(compute_wallet_stats("w", trades), trades)
    assert source == "claude-opus-5-5"
    assert out["inclusion_evidence"][0]["signatures"] == [trades[0].signature]
    assert out["unresolved_refs"] == ["t99"]
    assert msgs.calls[0]["output_config"] == {"effort": "high"}
    assert "t6 |" in msgs.calls[0]["messages"][0]["content"]


async def test_reporter_flags_unsupported_citations():
    cfg = Config()
    t1 = mk("wa", "M", "buy", 1, 1000, 0)
    case = Case(mint="M", opened_ts=0)
    case.add_event(t1)
    tracked = {"wa": TrackedWallet("wa", "A", None, {})}
    rep = SituationReport(
        headline="h", what_changed="w", traders_involved=["A"], relation_to_history="r",
        observed_facts=[Fact(statement="A bought", event_refs=["e1"]), Fact(statement="A added", event_refs=["e7"])],
        interpretations=[], missing_information=[], what_would_change_assessment=[],
    )
    llm, _ = fake_llm(ok(rep))
    out, source, unsupported = await Reporter(cfg, llm).write(case, {"snapshot": 1}, tracked, None)
    assert source == "claude-opus-5-5"
    assert out["observed_facts"][0]["signatures"] == [t1.signature]
    assert unsupported == ["A added"]
