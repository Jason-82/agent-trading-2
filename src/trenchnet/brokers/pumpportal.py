"""Live execution through PumpPortal's Local Transaction API.

PumpPortal builds an unsigned transaction; it is signed locally with your
keypair (the private key never leaves this process) and sent through your own
RPC endpoint. Fills are reconciled from on-chain balances after confirmation.

This path moves real money. It is only constructed when live trading is
explicitly enabled (see cli.py), and every buy is capped by
[live].max_position_sol regardless of strategy sizing.
"""

from __future__ import annotations

import logging

import httpx

from ..config import Config
from ..solana_rpc import SolanaRpc
from .base import Fill

log = logging.getLogger(__name__)


class PumpPortalBroker:
    mode = "live"

    def __init__(self, cfg: Config, keypair, rpc: SolanaRpc, http: httpx.AsyncClient | None = None) -> None:
        from solders.keypair import Keypair  # optional dependency: pip install trenchnet[live]

        if not isinstance(keypair, Keypair):
            raise TypeError("keypair must be a solders Keypair")
        self.cfg = cfg
        self.kp = keypair
        self.owner = str(keypair.pubkey())
        self.rpc = rpc
        self.http = http or httpx.AsyncClient(timeout=20.0)

    async def _submit(self, action: str, mint: str, amount: float | str, in_sol: bool) -> str:
        from solders.commitment_config import CommitmentLevel
        from solders.rpc.config import RpcSendTransactionConfig
        from solders.rpc.requests import SendVersionedTransaction
        from solders.transaction import VersionedTransaction

        lc = self.cfg.live
        r = await self.http.post(self.cfg.pumpportal.trade_url, data={
            "publicKey": self.owner,
            "action": action,
            "mint": mint,
            "amount": amount,
            "denominatedInSol": "true" if in_sol else "false",
            "slippage": lc.slippage_pct,
            "priorityFee": lc.priority_fee_sol,
            "pool": lc.pool,
        })
        if r.status_code != 200:
            raise RuntimeError(f"PumpPortal {action} failed: HTTP {r.status_code} {r.text[:200]}")
        unsigned = VersionedTransaction.from_bytes(r.content)
        signed = VersionedTransaction(unsigned.message, [self.kp])
        cfg = RpcSendTransactionConfig(preflight_commitment=CommitmentLevel.Confirmed)
        sig = await self.rpc.post_raw(SendVersionedTransaction(signed, cfg).to_json())
        if not sig:
            raise RuntimeError("RPC returned no signature")
        return str(sig)

    async def buy(self, mint: str, sol_amount: float, ref_price: float, ts: float, reason: str) -> Fill | None:
        sol_amount = min(sol_amount, self.cfg.live.max_position_sol)
        try:
            pre_tokens = await self.rpc.get_token_balance(self.owner, mint)
            pre_sol = await self.rpc.get_balance_sol(self.owner)
            sig = await self._submit("buy", mint, sol_amount, in_sol=True)
            if not await self.rpc.wait_confirmed(sig, self.cfg.live.confirm_timeout_s):
                log.error("buy %s not confirmed (sig %s)", mint, sig)
                return None
            got = await self.rpc.get_token_balance(self.owner, mint) - pre_tokens
            spent = pre_sol - await self.rpc.get_balance_sol(self.owner)
        except Exception as e:  # noqa: BLE001 - a failed live order must never crash the engine
            log.error("live buy %s failed: %s", mint, e)
            return None
        if got <= 0:
            log.error("buy %s confirmed but no tokens received (sig %s)", mint, sig)
            return None
        spent = spent if spent > 0 else sol_amount
        return Fill(mint, "buy", spent, got, spent / got, max(0.0, spent - sol_amount), ts, self.mode, reason, sig)

    async def sell(self, mint: str, token_amount: float, ref_price: float, ts: float, reason: str) -> Fill | None:
        try:
            balance = await self.rpc.get_token_balance(self.owner, mint)
            qty = min(token_amount, balance)
            if qty <= 0:
                log.error("sell %s: no token balance", mint)
                return None
            amount: float | str = "100%" if qty >= balance * 0.999 else qty
            pre_sol = await self.rpc.get_balance_sol(self.owner)
            sig = await self._submit("sell", mint, amount, in_sol=False)
            if not await self.rpc.wait_confirmed(sig, self.cfg.live.confirm_timeout_s):
                log.error("sell %s not confirmed (sig %s)", mint, sig)
                return None
            sold = balance - await self.rpc.get_token_balance(self.owner, mint)
            received = await self.rpc.get_balance_sol(self.owner) - pre_sol
        except Exception as e:  # noqa: BLE001
            log.error("live sell %s failed: %s", mint, e)
            return None
        if sold <= 0:
            return None
        return Fill(mint, "sell", max(received, 0.0), sold, max(received, 0.0) / sold, 0.0, ts, self.mode, reason, sig)
