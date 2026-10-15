from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Protocol


@dataclass(frozen=True)
class Fill:
    mint: str
    side: str  # "buy" | "sell" | "reconcile" (token_amount = on-chain balance found; no SOL moved)
    sol_amount: float  # SOL spent incl. fees (buy) / received net of fees (sell)
    token_amount: float
    price_sol: float  # effective execution price
    fee_sol: float
    ts: float
    mode: str  # "paper" (trenchnet run) | "live" | "backtest" (replay, demo)
    reason: str
    tx_signature: str | None = None

    def to_dict(self) -> dict:
        return asdict(self)


class Broker(Protocol):
    mode: str

    async def buy(self, mint: str, sol_amount: float, ref_price: float, ts: float, reason: str) -> Fill | None: ...

    async def sell(self, mint: str, token_amount: float, ref_price: float, ts: float, reason: str) -> Fill | None: ...
