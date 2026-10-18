"""Keys in .env are picked up without `source .env`."""

import asyncio
import json
import logging

import pytest
import websockets

from trenchnet.config import PumpPortalConfig
from trenchnet.envfile import load_env_file
from trenchnet.sources.pumpportal_ws import PumpPortalSource

NAMES = ("SOLANA_RPC_URL", "PUMPPORTAL_API_KEY", "ANTHROPIC_API_KEY", "TN_A", "TN_B", "TN_C")


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    for n in NAMES:
        monkeypatch.delenv(n, raising=False)


def test_reads_the_export_lines_written_for_git_bash(tmp_path):
    import os

    env = tmp_path / ".env"
    env.write_bytes(("﻿# my keys\r\n"
                     'export SOLANA_RPC_URL="https://mainnet.helius-rpc.com/?api-key=abc-123"\r\n'
                     "export PUMPPORTAL_API_KEY='pp_key'\r\n"
                     "\r\n"
                     "ANTHROPIC_API_KEY=sk-ant-xyz  # plain form, with a comment\r\n").encode("utf-8"))
    assert load_env_file(env) == ["SOLANA_RPC_URL", "PUMPPORTAL_API_KEY", "ANTHROPIC_API_KEY"]
    assert os.environ["SOLANA_RPC_URL"] == "https://mainnet.helius-rpc.com/?api-key=abc-123"
    assert os.environ["PUMPPORTAL_API_KEY"] == "pp_key"
    assert os.environ["ANTHROPIC_API_KEY"] == "sk-ant-xyz"


def test_one_line_form_and_spaces_around_the_equals_sign(tmp_path):
    import os

    env = tmp_path / ".env"
    env.write_text('export TN_A="one" TN_B="two"\nTN_C = "three"\n')
    load_env_file(env)
    assert (os.environ["TN_A"], os.environ["TN_B"], os.environ["TN_C"]) == ("one", "two", "three")


def test_a_key_set_in_the_terminal_wins(tmp_path, monkeypatch):
    import os

    monkeypatch.setenv("TN_A", "from terminal")
    (tmp_path / ".env").write_text('export TN_A="from file"\n')
    assert load_env_file(tmp_path / ".env") == []
    assert os.environ["TN_A"] == "from terminal"


def test_a_broken_line_is_skipped_not_fatal(tmp_path, caplog):
    import os

    (tmp_path / ".env").write_text('export TN_A="unclosed\nexport TN_B="fine"\nnonsense here\n')
    assert load_env_file(tmp_path / ".env") == ["TN_B"]
    assert os.environ["TN_B"] == "fine" and "line 1" in caplog.text and "line 3" in caplog.text


def test_missing_file_is_fine(tmp_path):
    assert load_env_file(tmp_path / ".env") == []


def test_every_command_loads_env_from_the_current_folder(tmp_path, monkeypatch):
    import os

    import trenchnet.cli as cli

    monkeypatch.setattr(cli, "load_env_file", load_env_file)  # the real one (tests normally stub it)
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".env").write_text('export TN_A="loaded"\n')
    with pytest.raises(SystemExit):  # no database here: the command stops, but .env was read first
        cli.main(["roster"])
    assert os.environ["TN_A"] == "loaded"


async def test_pumpportal_balance_error_is_explained_once_without_the_key(caplog):
    async def handler(ws):
        await ws.recv()
        for _ in range(3):
            await ws.send(json.dumps({"errors": "Minimum balance not met for PumpSwap websocket data."}))
        await ws.send(json.dumps({"signature": "S1", "mint": "M1", "traderPublicKey": "W", "txType": "buy",
                                  "solAmount": 1.0, "tokenAmount": 2e7}))
        await ws.wait_closed()

    caplog.set_level(logging.WARNING)
    async with websockets.serve(handler, "127.0.0.1", 0) as server:
        port = server.sockets[0].getsockname()[1]
        src = PumpPortalSource(PumpPortalConfig(ws_url=f"ws://127.0.0.1:{port}"), ["W"], api_key="SECRETKEY")
        async for _ in src.events():
            src.stop()
    assert caplog.text.count("Minimum balance not met") == 1
    assert "Send about 0.05 SOL" in caplog.text and "SECRETKEY" not in caplog.text
