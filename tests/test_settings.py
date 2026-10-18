"""`trenchnet config`: settings changed from the command line, comments kept."""

import pytest

from trenchnet.cli import main
from trenchnet.config import load_config
from trenchnet.settings import SettingError, change, describe, edit_text, resolve, split_assignments

EXAMPLE = """\
# my settings
db_path = "trenchnet.db"

[router]
min_confidence = 0.55

[strategy]
min_tracked_buyers = 3
min_independent_clusters = 2        # the usual group + an outsider
# min_market_cap_sol = 30
dust_sol = 0.005

[live]
rpc_url = "https://rpc.example/#frag"   # not a comment: inside quotes
"""


def test_changes_a_setting_in_place_and_keeps_its_comment(tmp_path):
    path = tmp_path / "trenchnet.toml"
    path.write_text(EXAMPLE)
    out = change(path, ["strategy.min_independent_clusters=1", "strategy.min_tracked_buyers=2"])
    assert out == ["strategy.min_independent_clusters: 2 -> 1 (default: 2)",
                   "strategy.min_tracked_buyers: 3 -> 2 (default: 3)"]
    text = path.read_text()
    assert "min_independent_clusters = 1        # the usual group + an outsider\n" in text
    assert "min_tracked_buyers = 2\n" in text
    assert text.count("\n") == EXAMPLE.count("\n")
    cfg = load_config(path)
    assert (cfg.strategy.min_tracked_buyers, cfg.strategy.min_independent_clusters) == (2, 1)


def test_adds_a_missing_setting_to_its_section_or_a_new_section(tmp_path):
    path = tmp_path / "trenchnet.toml"
    path.write_text(EXAMPLE)
    change(path, ["conviction_entry=true", "paper.starting_balance_sol=10", "min_market_cap_sol=30"])
    text = path.read_text()
    assert "dust_sol = 0.005\nconviction_entry = true\nmin_market_cap_sol = 30\n\n[live]" in text
    assert text.endswith('[live]\nrpc_url = "https://rpc.example/#frag"   # not a comment: inside quotes\n'
                         "\n[paper]\nstarting_balance_sol = 10\n")
    cfg = load_config(path)
    assert cfg.strategy.conviction_entry and cfg.paper.starting_balance_sol == 10
    assert cfg.strategy.min_market_cap_sol == 30


def test_default_removes_the_line(tmp_path):
    path = tmp_path / "trenchnet.toml"
    path.write_text(EXAMPLE)
    assert change(path, ["strategy.min_independent_clusters=1"])
    assert change(path, ["strategy.min_independent_clusters=default"]) == [
        "strategy.min_independent_clusters: 1 -> 2 (the default)"]
    assert "min_independent_clusters" not in path.read_text()
    assert change(path, ["router.min_confidence", "=", "reset"]) == [
        "router.min_confidence: unchanged (the default)"]
    assert "min_confidence" not in path.read_text()


def test_creates_the_file_when_there_is_none(tmp_path):
    path = tmp_path / "trenchnet.toml"
    change(path, ["strategy.min_tracked_buyers=2", "db_path=bot.db"])
    text = path.read_text()
    assert text.startswith("# TRENCHNET settings.")
    cfg = load_config(path)
    assert cfg.strategy.min_tracked_buyers == 2 and cfg.db_path == "bot.db"
    assert text.index("db_path") < text.index("[strategy]")


def test_a_comment_wider_than_the_new_value_still_has_a_gap():
    out = edit_text("[live]\nrpc_url = \"x\" # c\n", [(resolve("live.rpc_url"), "https://a-long-url.example")])
    assert out == '[live]\nrpc_url = "https://a-long-url.example"  # c\n'


@pytest.mark.parametrize("word, problem", [
    ("strategy.min_tracked_buyer=2", "Did you mean strategy.min_tracked_buyers"),
    ("enabled=false", "say which: strategy.enabled or live.enabled"),
    ("strategy.min_tracked_buyers=two", "must be a number"),
    ("strategy.min_tracked_buyers=2.5", "whole number"),
    ("strategy.stop_loss_pct=-0.3", "can't be negative"),
    ("strategy.conviction_entry=yes", "true or false"),
    ("strategy.take_profit=[2.0, 0.5]", "[multiple, fraction] pairs"),
    ("strategy.min_tracked_buyers", "give strategy.min_tracked_buyers a value"),
])
def test_refuses_bad_changes_and_leaves_the_file_alone(tmp_path, word, problem):
    path = tmp_path / "trenchnet.toml"
    path.write_text(EXAMPLE)
    with pytest.raises(SettingError, match=problem.replace("[", r"\[").replace("]", r"\]")):
        change(path, [word])
    assert path.read_text() == EXAMPLE


def test_refuses_to_edit_a_file_that_does_not_load_or_a_value_over_several_lines(tmp_path):
    path = tmp_path / "trenchnet.toml"
    path.write_text("[strategy]\nmin_tracked_buyers = \n")
    with pytest.raises(SettingError, match="doesn't load"):
        change(path, ["strategy.min_tracked_buyers=2"])
    assert describe(path, [])[0].startswith("! ")
    spread = "[strategy]\ntake_profit = [\n  [2.0, 0.5],\n]\n"
    path.write_text(spread)
    with pytest.raises(SettingError, match="nothing was changed"):
        change(path, ["strategy.take_profit=[[3.0, 0.5]]"])
    assert path.read_text() == spread


def test_reads_a_windows_file(tmp_path):
    path = tmp_path / "trenchnet.toml"
    path.write_bytes("﻿[strategy]\r\nmin_tracked_buyers = 3\r\n".encode("utf-8"))
    assert load_config(path).strategy.min_tracked_buyers == 3
    change(path, ["strategy.min_tracked_buyers=2"])
    assert load_config(path).strategy.min_tracked_buyers == 2


def test_spaces_around_the_equals_sign():
    assert split_assignments(["a.b", "=", "1", "c=", "2", "d", "=3", "e"]) == [
        ("a.b", "1"), ("c", "2"), ("d", "3"), ("e", None)]


def test_describe(tmp_path):
    path = tmp_path / "trenchnet.toml"
    assert "every setting is the default" in describe(path, [])[0]
    path.write_text(EXAMPLE.replace("min_tracked_buyers = 3", "min_tracked_buyers = 2"))
    lines = describe(path, [])
    assert any(line.startswith("  strategy.min_tracked_buyers = 2 ") and line.endswith("   (default: 3)")
               for line in lines)
    assert describe(path, ["min_tracked_buyers"]) == [
        "  strategy.min_tracked_buyers = 2   (changed; default: 3)"]
    section = describe(path, ["router"])
    assert section[0] == "[router]" and any(line.startswith("  report_cooldown_s") for line in section)


def test_cli(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    main(["config", "strategy.min_tracked_buyers=2", "strategy.conviction_entry=true"])
    out = capsys.readouterr().out
    assert "strategy.min_tracked_buyers: 3 -> 2" in out and "restart `trenchnet run`" in out
    assert load_config().strategy.conviction_entry is True
    with pytest.raises(SystemExit, match="no setting called"):
        main(["config", "strategy.nope=1"])


def test_other_commands_explain_a_broken_config_and_config_still_works(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "trenchnet.toml").write_text("[strategy]\nmin_tracked_buyer = 2\n")
    with pytest.raises(SystemExit, match="trenchnet.toml: unknown config key: strategy.min_tracked_buyer"):
        main(["roster"])
    main(["config"])
    assert "unknown config key" in capsys.readouterr().out


def test_never_saves_an_edit_that_did_not_take(tmp_path):
    # A "[strategy]" line inside a multi-line string isn't a section: an edit there would load
    # fine but change the string, not the setting.
    path = tmp_path / "trenchnet.toml"
    tricky = '[llm]\nprofile_model = """\n[strategy]\n"""\n'
    path.write_text(tricky)
    with pytest.raises(SettingError, match="couldn't edit strategy.min_tracked_buyers"):
        change(path, ["strategy.min_tracked_buyers=2"])
    assert path.read_text() == tricky
