from __future__ import annotations

import itertools

import pytest

from trenchnet.config import Config
from trenchnet.models import Side, Trade

_sig = itertools.count(1)


def mk(wallet: str, mint: str, side: str, sol: float, tokens: float, ts: float) -> Trade:
    return Trade(f"sig{next(_sig)}", wallet, mint, Side(side), sol, tokens, ts)


@pytest.fixture
def cfg() -> Config:
    return Config()


@pytest.fixture(autouse=True)
def _no_real_env_file(monkeypatch):
    """Tests drive the CLI from the project folder; never pick up the user's real .env keys."""
    import trenchnet.cli

    monkeypatch.setattr(trenchnet.cli, "load_env_file", lambda path=".env": [])
