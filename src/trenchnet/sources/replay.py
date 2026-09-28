"""JSONL event files for import and backtesting.

Each line is either a trade::

    {"type": "trade", "signature": "...", "wallet": "...", "mint": "...", "side": "buy",
     "sol_amount": 0.5, "token_amount": 1.2e7, "ts": 1727500000, "market_cap_sol": 42.0}

or a price tick::

    {"type": "price", "mint": "...", "price_sol": 3.1e-8, "ts": 1727500060}

Lines without "type" are treated as trades.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Iterable, Iterator

from ..models import PriceTick, Trade


def read_events(path: str | Path) -> list[Trade | PriceTick]:
    out: list[Trade | PriceTick] = []
    with open(path) as f:
        for n, line in enumerate(f, 1):
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            try:
                d = json.loads(line)
                out.append(PriceTick.from_dict(d) if d.get("type") == "price" else Trade.from_dict(d))
            except (KeyError, ValueError) as e:
                raise ValueError(f"{path}:{n}: {e}") from e
    out.sort(key=lambda e: e.ts)
    return out


def write_events(path: str | Path, events: Iterable[Trade | PriceTick]) -> None:
    with open(path, "w") as f:
        for e in events:
            d = e.to_dict()
            d["type"] = "price" if isinstance(e, PriceTick) else "trade"
            f.write(json.dumps(d) + "\n")


def trades_only(events: Iterable[Trade | PriceTick]) -> Iterator[Trade]:
    return (e for e in events if isinstance(e, Trade))
