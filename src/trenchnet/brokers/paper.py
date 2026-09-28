"""Paper broker: fills at the last observed price, made worse by slippage,
fees, and a latency penalty (a follower always arrives after the wallets it
follows). Deliberately pessimistic; tune the bps in [paper] to your venue."""

from __future__ import annotations

from ..config import PaperConfig
from .base import Fill


class PaperBroker:
    mode = "paper"

    def __init__(self, cfg: PaperConfig) -> None:
        self.cfg = cfg

    def _impact(self) -> float:
        return (self.cfg.slippage_bps + self.cfg.latency_penalty_bps) / 1e4

    async def buy(self, mint: str, sol_amount: float, ref_price: float, ts: float, reason: str) -> Fill | None:
        if sol_amount <= 0 or ref_price <= 0:
            return None
        fee = sol_amount * self.cfg.fee_bps / 1e4
        px = ref_price * (1 + self._impact())
        tokens = (sol_amount - fee) / px
        return Fill(mint, "buy", sol_amount, tokens, px, fee, ts, self.mode, reason)

    async def sell(self, mint: str, token_amount: float, ref_price: float, ts: float, reason: str) -> Fill | None:
        if token_amount <= 0 or ref_price <= 0:
            return None
        px = ref_price * max(0.0, 1 - self._impact())
        gross = token_amount * px
        fee = gross * self.cfg.fee_bps / 1e4
        return Fill(mint, "sell", gross - fee, token_amount, px, fee, ts, self.mode, reason)
