from trenchnet.config import GraphConfig
from trenchnet.graph import TradeGraph

from .conftest import mk


def build():
    g = TradeGraph(GraphConfig(co_entry_window_s=1800, min_co_entries=2, min_jaccard=0.15, min_lift=1.5,
                               min_history_tokens=3))
    trades = []
    # A, B, C co-enter 4 tokens (A first); each has solo tokens too.
    for i in range(4):
        t = i * 86400
        trades += [mk("A", f"s{i}", "buy", 1, 1, t), mk("B", f"s{i}", "buy", 1, 1, t + 120),
                   mk("C", f"s{i}", "buy", 1, 1, t + 400)]
    for w in "ABC":
        trades += [mk(w, f"{w}solo{i}", "buy", 1, 1, 500_000 + i) for i in range(3)]
    # D buys one of the shared tokens two days later: shared, but not a co-entry.
    trades += [mk("D", "s0", "buy", 1, 1, 2 * 86400)]
    trades += [mk("D", f"Dsolo{i}", "buy", 1, 1, 600_000 + i) for i in range(5)]
    # Background tokens so the universe is realistic.
    trades += [mk("X", f"bg{i}", "buy", 1, 1, 700_000 + i) for i in range(10)]
    g.add_trades(trades)
    return g


def test_pair_stats_window_and_normalisation():
    g = build()
    ab = g.pair_stats("A", "B")
    assert ab.co_entries == 4 and ab.a_led == 4 and ab.b_led == 0
    assert ab.median_lag_s == 120
    assert ab.jaccard == round(4 / 10, 4)
    assert ab.recurring
    ad = g.pair_stats("A", "D")
    assert ad.shared_tokens == 1 and ad.co_entries == 0  # same token, days apart
    assert not ad.recurring


def test_relationship_summary_groups_and_outsider():
    g = build()
    g.add_trades([mk("A", "NEW", "buy", 1, 1, 10**6), mk("B", "NEW", "buy", 1, 1, 10**6 + 60),
                  mk("C", "NEW", "buy", 1, 1, 10**6 + 200), mk("D", "NEW", "buy", 1, 1, 10**6 + 500)])
    rel = g.relationship_summary("NEW", tracked={"A", "B", "C", "D"})
    assert rel.participants == ["A", "B", "C", "D"]
    assert len(rel.groups) == 1
    assert rel.groups[0].members == ["A", "B", "C"]
    assert rel.groups[0].order_agreement == 1.0
    assert rel.independents == ["D"]
    assert rel.cluster_count == 2
    text = " ".join(rel.lines(lambda w: w))
    assert "A, B and C have recurring shared entries" in text
    assert "D rarely overlaps" in text


def test_current_token_is_excluded_from_history():
    g = TradeGraph(GraphConfig(min_co_entries=1, min_jaccard=0.0, min_lift=0.0, min_history_tokens=0))
    g.add_trades([mk("A", "NEW", "buy", 1, 1, 0), mk("B", "NEW", "buy", 1, 1, 10)])
    rel = g.relationship_summary("NEW")
    assert rel.groups == [] and rel.independents == ["A", "B"]


def test_thin_history_flag_and_untracked_filter():
    g = build()
    g.add_trades([mk("A", "NEW", "buy", 1, 1, 10**6), mk("Z", "NEW", "buy", 1, 1, 10**6 + 5)])
    rel = g.relationship_summary("NEW", tracked={"A", "Z"})
    assert "Z" in rel.thin_history and "A" not in rel.thin_history
    rel2 = g.relationship_summary("NEW", tracked={"A"})
    assert rel2.participants == ["A"]
