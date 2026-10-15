"""Core data types shared across the pipeline."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from enum import Enum
from typing import Any


class Side(str, Enum):
    BUY = "buy"
    SELL = "sell"
    # A transfer with an unknown purpose remains a transfer. It never becomes
    # a sale just because the balance decreased.
    TRANSFER_IN = "transfer_in"
    TRANSFER_OUT = "transfer_out"

    @property
    def is_trade(self) -> bool:
        return self in (Side.BUY, Side.SELL)


@dataclass(frozen=True)
class Trade:
    """One observed wallet action on one token."""

    signature: str
    wallet: str
    mint: str
    side: Side
    sol_amount: float  # SOL spent on a buy / received on a sell; 0 for transfers
    token_amount: float
    ts: float  # unix seconds
    market_cap_sol: float | None = None
    price_sol: float | None = None  # SOL per token; derived from amounts when absent
    source: str = "unknown"
    # Paid for / sold for a stablecoin instead of SOL: which one and how much. sol_amount then
    # holds its value in SOL at the time of the trade.
    quote_mint: str | None = None
    quote_amount: float | None = None

    @property
    def price(self) -> float | None:
        if self.price_sol is not None and self.price_sol > 0:
            return self.price_sol
        if self.side.is_trade and self.token_amount > 0 and self.sol_amount > 0:
            return self.sol_amount / self.token_amount
        return None

    @property
    def key(self) -> tuple[str, str, str, str]:
        return (self.signature, self.wallet, self.mint, self.side.value)

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["side"] = self.side.value
        return d

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Trade":
        return cls(
            signature=str(d["signature"]),
            wallet=str(d["wallet"]),
            mint=str(d["mint"]),
            side=Side(d["side"]),
            sol_amount=float(d.get("sol_amount") or 0.0),
            token_amount=float(d.get("token_amount") or 0.0),
            ts=float(d["ts"]),
            market_cap_sol=_opt_float(d.get("market_cap_sol")),
            price_sol=_opt_float(d.get("price_sol")),
            source=str(d.get("source") or "unknown"),
            quote_mint=d.get("quote_mint") or None,
            quote_amount=_opt_float(d.get("quote_amount")),
        )


@dataclass(frozen=True)
class PriceTick:
    """A price observation for a token (from any wallet's trade)."""

    mint: str
    price_sol: float
    ts: float
    market_cap_sol: float | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "PriceTick":
        return cls(
            mint=str(d["mint"]),
            price_sol=float(d["price_sol"]),
            ts=float(d["ts"]),
            market_cap_sol=_opt_float(d.get("market_cap_sol")),
        )


def _opt_float(v: Any) -> float | None:
    return None if v is None else float(v)


def short(addr: str, n: int = 4) -> str:
    """Shorten a base58 address for display."""
    return addr if len(addr) <= 2 * n + 1 else f"{addr[:n]}…{addr[-n:]}"
