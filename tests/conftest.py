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
