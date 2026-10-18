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
    ("strategy.take_profit=[2.0, 0.5]", "[multiple of the entry price, fraction to sell] pairs"),
    ("strategy.take_profit=[[2.0, 50]]", "fractions from 0 to 1"),
    ("strategy.stop_loss_pct=35", "35 would mean 3500%; for 35% use 0.35"),
    ("exit_on_cohort_sold_fraction=50", "fraction from 0 to 1"),
    ("conviction_min_win_rate=1.5", "fraction from 0 to 1"),
    ("strategy.min_tracked_buyers", "give strategy.min_tracked_buyers a value"),
])
def test_refuses_bad_changes_and_leaves_the_file_alone(tmp_path, word, problem):
    path = tmp_path / "trenchnet.toml"
    path.write_text(EXAMPLE)
    with pytest.raises(SettingError, match=problem.replace("[", r"\[").replace("]", r"\]")):
        change(path, [word])
    assert path.read_text() == EXAMPLE


def test_refuses_to_edit_a_file_that_does_not_load(tmp_path):
    path = tmp_path / "trenchnet.toml"
    path.write_text("[strategy]\nmin_tracked_buyers = \n")
    with pytest.raises(SettingError, match="can't be read: it isn't valid TOML"):
        change(path, ["strategy.min_tracked_buyers=2"])
    assert describe(path, [])[0].startswith("! ")


def test_a_value_over_several_lines(tmp_path):
    path = tmp_path / "trenchnet.toml"
    path.write_text("[strategy]\ntake_profit = [\n  [2.0, 0.5],\n  [4.0, 0.25],\n]\n\n[live]\nenabled = false\n")
    change(path, ["strategy.conviction_entry=true"])  # goes after the whole list, not inside it
    assert load_config(path).strategy.conviction_entry is True
    assert load_config(path).strategy.take_profit == [[2.0, 0.5], [4.0, 0.25]]
    change(path, ["strategy.take_profit=[[3.0, 0.5]]"])
    assert path.read_text() == ("[strategy]\ntake_profit = [[3.0, 0.5]]\nconviction_entry = true\n\n"
                                "[live]\nenabled = false\n")
    path.write_text("[strategy]\ntake_profit = [\n  [2.0, 0.5],\n]\nenabled = true\n")
    change(path, ["strategy.take_profit=default"])
    assert path.read_text() == "[strategy]\nenabled = true\n"


def test_reads_a_windows_file(tmp_path):
    path = tmp_path / "trenchnet.toml"
    path.write_bytes("﻿[strategy]\r\nmin_tracked_buyers = 3\r\n".encode("utf-8"))
    assert load_config(path).strategy.min_tracked_buyers == 3
    change(path, ["strategy.min_tracked_buyers=2"])
    assert load_config(path).strategy.min_tracked_buyers == 2


def test_spaces_around_the_equals_sign():
    assert split_assignments(["a.b", "=", "1", "c=", "2", "d", "=3", "e"]) == [
        ("a.b", "1"), ("c", "2"), ("d", "3"), ("e", None)]
    # A value that ends in "=" (an API key in a URL) doesn't swallow the next setting.
    assert split_assignments(["live.rpc_url=https://rpc.example/?api-key=abc=", "strategy.enabled=false"]) == [
        ("live.rpc_url", "https://rpc.example/?api-key=abc="), ("strategy.enabled", "false")]
    assert split_assignments(["live.rpc_url=", "https://rpc.example/?api-key=abc"]) == [
        ("live.rpc_url", "https://rpc.example/?api-key=abc")]
    assert split_assignments(["live.pool=", "strategy.enabled=false"]) == [
        ("live.pool", ""), ("strategy.enabled", "false")]


def test_an_empty_value_is_refused(tmp_path):
    path = tmp_path / "trenchnet.toml"
    with pytest.raises(SettingError, match="give live.pool a value"):
        change(path, ["live.pool=", "strategy.enabled=false"])
    assert not path.exists()


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
    with pytest.raises(SettingError, match="couldn.t edit .* automatically"):
        change(path, ["strategy.min_tracked_buyers=2"])
    assert path.read_text() == tricky


def test_hand_typed_values_of_the_wrong_kind_stop_the_bot_with_a_plain_message(tmp_path, monkeypatch, capsys):
    path = tmp_path / "trenchnet.toml"
    path.write_text('[strategy]\nconviction_entry = "false"   # keep it off\nmin_tracked_buyers = 2\n'
                    'stop_loss_pct = 35\n')
    with pytest.raises(ValueError, match=r'conviction_entry must be true or false, not "false" \(remove the quotes\)'):
        load_config(path)
    lines = describe(path, [])
    assert lines[0] == f'! {path}: strategy.conviction_entry must be true or false, not "false" (remove the quotes)'
    assert lines[1] == f"! {path}: strategy.stop_loss_pct is a fraction from 0 to 1, so 35 would mean 3500%; for 35% use 0.35"
    assert any(line.startswith("  strategy.min_tracked_buyers = 2 ") for line in lines)  # what does load is shown
    assert "every setting is the default" not in lines
    with pytest.raises(SettingError, match="stop_loss_pct is a fraction"):  # the other bad line is still there
        change(path, ["strategy.conviction_entry=false"])
    change(path, ["strategy.conviction_entry=false", "strategy.stop_loss_pct=0.35"])  # fixes both
    assert load_config(path).strategy.conviction_entry is False
    monkeypatch.chdir(tmp_path)
    path.write_text('[strategy]\nmin_tracked_buyers = "3"\n')
    with pytest.raises(SystemExit, match=r'min_tracked_buyers must be a number, not "3" \(remove the quotes\)'):
        main(["roster"])


def test_a_failed_save_leaves_the_old_file(tmp_path, monkeypatch):
    path = tmp_path / "trenchnet.toml"
    path.write_text(EXAMPLE)

    def full_disk(src, dst):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr("trenchnet.settings.os.replace", full_disk)
    with pytest.raises(OSError):
        change(path, ["strategy.min_tracked_buyers=2"])
    assert path.read_text() == EXAMPLE and [p.name for p in tmp_path.iterdir()] == ["trenchnet.toml"]


@pytest.mark.parametrize("words, problem", [
    (["strategy.take_profit=[[2.0,", "0.5],", "[4.0,", "0.25]]"],
     "'0.5],' looks like part of the value of strategy.take_profit: put quotes around a value with spaces, "
     "e.g. strategy.take_profit='[[2.0, 0.5], [4.0, 0.25]]'"),
    (["llm.judge_model=claude", "haiku", "strategy.enabled=true"], "e.g. llm.judge_model='claude haiku'"),
    (["strategy", "min_tracked_buyers=2"], "write the section and the setting together: strategy.min_tracked_buyers=2"),
    (["min_tracked_buyers=2", "strategy.min_tracked_buyers=4"],
     "strategy.min_tracked_buyers is given twice (2 and 4); give it once"),
])
def test_explains_commands_typed_slightly_wrong(tmp_path, words, problem):
    path = tmp_path / "trenchnet.toml"
    with pytest.raises(SettingError) as e:
        change(path, words)
    assert problem in str(e.value) and not path.exists()


def test_a_value_without_equals_sign_when_showing(tmp_path):
    with pytest.raises(SettingError, match="to change strategy.min_tracked_buyers, write strategy.min_tracked_buyers=2"):
        describe(tmp_path / "trenchnet.toml", ["strategy.min_tracked_buyers", "2"])


def test_config_option_after_the_command_name(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    main(["config", "--config", "other.toml", "strategy.min_tracked_buyers=2"])
    assert load_config(tmp_path / "other.toml").strategy.min_tracked_buyers == 2
    assert not (tmp_path / "trenchnet.toml").exists()
    capsys.readouterr()
    main(["--config", "other.toml", "config", "strategy.min_tracked_buyers"])
    assert capsys.readouterr().out == "  strategy.min_tracked_buyers = 2   (changed; default: 3)\n"


def test_a_file_in_this_pcs_ansi_code_page_keeps_its_comments(tmp_path, monkeypatch):
    monkeypatch.setattr("trenchnet.textfile.locale.getpreferredencoding", lambda do_setlocale=True: "cp1251")
    path = tmp_path / "trenchnet.toml"
    path.write_bytes("# мои настройки\n[strategy]\nmin_tracked_buyers = 3\n".encode("cp1251"))
    change(path, ["strategy.min_tracked_buyers=2"])
    assert path.read_text(encoding="utf-8").startswith("# мои настройки\n")


def test_refuses_to_rewrite_a_file_with_unreadable_characters(tmp_path, monkeypatch):
    monkeypatch.setattr("trenchnet.textfile.locale.getpreferredencoding", lambda do_setlocale=True: "UTF-8")
    path = tmp_path / "trenchnet.toml"
    original = b"# \x81\x8d odd bytes\n[strategy]\nmin_tracked_buyers = 3\n"
    path.write_bytes(original)
    with pytest.raises(SettingError, match="save it as UTF-8"):
        change(path, ["strategy.min_tracked_buyers=2"])
    assert path.read_bytes() == original
