"""Minimal async Solana JSON-RPC client (standard methods only)."""

from __future__ import annotations

import asyncio
import itertools
from typing import Any

import httpx

LAMPORTS_PER_SOL = 1_000_000_000


class RpcError(RuntimeError):
    pass


class SolanaRpc:
    def __init__(self, url: str, http: httpx.AsyncClient | None = None, max_retries: int = 3) -> None:
        self.url = url
        self.http = http or httpx.AsyncClient(timeout=30.0)
        self.max_retries = max_retries
        self._ids = itertools.count(1)

    async def call(self, method: str, params: list[Any] | None = None) -> Any:
        body = {"jsonrpc": "2.0", "id": next(self._ids), "method": method, "params": params or []}
        return await self.post_raw(body)

    async def post_raw(self, body: dict | str) -> Any:
        delay = 0.5
        for attempt in range(self.max_retries + 1):
            try:
                if isinstance(body, str):
                    r = await self.http.post(self.url, content=body, headers={"Content-Type": "application/json"})
                else:
                    r = await self.http.post(self.url, json=body)
                if r.status_code == 429 or r.status_code >= 500:
                    raise httpx.HTTPStatusError("retryable", request=r.request, response=r)
                r.raise_for_status()
                data = r.json()
                if "error" in data:
                    raise RpcError(str(data["error"]))
                return data.get("result")
            except (httpx.TransportError, httpx.HTTPStatusError):
                if attempt == self.max_retries:
                    raise
                await asyncio.sleep(delay)
                delay *= 2

    async def get_balance_sol(self, owner: str) -> float:
        res = await self.call("getBalance", [owner, {"commitment": "confirmed"}])
        return res["value"] / LAMPORTS_PER_SOL

    async def get_token_balance(self, owner: str, mint: str) -> float:
        res = await self.call("getTokenAccountsByOwner", [
            owner, {"mint": mint}, {"encoding": "jsonParsed", "commitment": "confirmed"},
        ])
        total = 0.0
        for acc in res.get("value", []):
            amt = acc["account"]["data"]["parsed"]["info"]["tokenAmount"]
            total += float(amt.get("uiAmountString") or amt.get("uiAmount") or 0)
        return total

    async def signature_confirmed(self, signature: str) -> bool | None:
        """True if confirmed without error, False if failed, None if unknown yet."""
        res = await self.call("getSignatureStatuses", [[signature], {"searchTransactionHistory": False}])
        st = (res or {}).get("value", [None])[0]
        if st is None:
            return None
        if st.get("err") is not None:
            return False
        return st.get("confirmationStatus") in ("confirmed", "finalized")

    async def wait_confirmed(self, signature: str, timeout_s: float) -> bool:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout_s
        while loop.time() < deadline:
            ok = await self.signature_confirmed(signature)
            if ok is not None:
                return ok
            await asyncio.sleep(1.5)
        return False

    async def get_signatures(self, address: str, limit: int = 1000, before: str | None = None) -> list[dict]:
        opts: dict[str, Any] = {"limit": min(limit, 1000)}
        if before:
            opts["before"] = before
        return await self.call("getSignaturesForAddress", [address, opts]) or []

    async def get_transaction(self, signature: str) -> dict | None:
        return await self.call("getTransaction", [
            signature, {"encoding": "jsonParsed", "maxSupportedTransactionVersion": 0, "commitment": "confirmed"},
        ])

    async def aclose(self) -> None:
        await self.http.aclose()
