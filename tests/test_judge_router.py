import copy

import pytest

from trenchnet.judge import Assessment, HeuristicJudge, route

BASE = {
    "token": {"tracked_buyers": 3, "tracked_holders": 3},
    "event": {"ref": "e3", "wallet": "C", "side": "buy", "sol": 0.5, "is_first_buy_in_token": True,
              "size_vs_typical_entry": 1.0, "position_status_after": "holding",
              "minutes_since_wallet_first_buy": 0, "others_reduced_or_exited": []},
    "wallet_profile": {"style": "patient_holder",
                       "history": {"closed_positions": 10, "n_tokens": 10, "p25_hold_s": 7200, "p75_hold_s": 18000}},
    "relationships": {"thin_share": 0.0},
    "last_report": None,
}


def snap(**event):
    s = copy.deepcopy(BASE)
    s["event"].update(event)
    return s


@pytest.fixture
def judge(cfg):
    return HeuristicJudge(cfg)


def test_buy_size_drives_fit_and_intent(judge):
    assert judge.assess_sync(snap(size_vs_typical_entry=1.0)).position_intent == "standard"
    a = judge.assess_sync(snap(size_vs_typical_entry=0.2))
    assert a.position_intent == "exploratory" and a.behavior_fit == "partial"
    a = judge.assess_sync(snap(size_vs_typical_entry=4.0))
    assert a.position_intent == "conviction" and a.behavior_fit == "partial"
    assert judge.assess_sync(snap(size_vs_typical_entry=10)).behavior_fit == "does_not_fit"


def test_small_first_buy_by_accumulator_is_standard(judge):
    s = snap(size_vs_typical_entry=0.4)
    s["wallet_profile"]["style"] = "accumulator"
    assert judge.assess_sync(s).position_intent == "standard"


def test_sell_hold_time_vs_profile(judge):
    a = judge.assess_sync(snap(side="sell", position_status_after="exited", minutes_since_wallet_first_buy=180))
    assert a.position_intent == "full_exit" and a.behavior_fit == "fits"
    a = judge.assess_sync(snap(side="sell", position_status_after="exited", minutes_since_wallet_first_buy=2))
    assert a.behavior_fit == "does_not_fit"


def test_transfer_is_transfer(judge):
    assert judge.assess_sync(snap(side="transfer_out")).position_intent == "transfer"


def test_history_sufficiency(judge):
    s = snap()
    s["wallet_profile"]["history"]["closed_positions"] = 1
    assert judge.assess_sync(s).history == "insufficient"


def test_attention(judge):
    assert judge.assess_sync(snap()).attention == "first_analysis"
    s = snap(is_first_buy_in_token=False)
    s["last_report"] = {"headline": "x"}
    assert judge.assess_sync(s).attention == "no_change"
    s["event"]["others_reduced_or_exited"] = ["B"]
    assert judge.assess_sync(s).attention == "update_report"


def A(**kw):
    base = dict(behavior_fit="fits", behavior_fit_confidence=0.8, position_intent="standard",
                position_intent_confidence=0.8, history="sufficient", attention="first_analysis",
                attention_confidence=0.8)
    base.update(kw)
    return Assessment(**base)


def test_route_rules(cfg):
    rc = cfg.router
    assert route(snap(), A(), rc).route == "analyze"
    assert route(snap(), A(attention="no_change"), rc).route == "observe"
    d = route(snap(), A(attention_confidence=0.3), rc)
    assert d.route == "observe" and d.uncertain
    s = snap()
    s["token"]["tracked_buyers"] = 1
    assert route(s, A(), rc).route == "observe"
    s = snap()
    s["relationships"]["thin_share"] = 0.75
    assert route(s, A(history="insufficient"), rc).route == "insufficient_evidence"


def test_assessment_clamps():
    a = A(attention_confidence=1.7, behavior_fit_confidence=-0.2).clamped()
    assert a.attention_confidence == 1.0 and a.behavior_fit_confidence == 0.0
