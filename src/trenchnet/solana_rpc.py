"""Minimal async Solana JSON-RPC client (standard methods only).

Requests are paced to stay under the provider's rate limit (Helius' free
plan allows 10 requests/second), and 429 / 5xx answers are retried with
backoff, honouring Retry-After when the provider sends it.
"""

from __future__ import annotations

import asyncio
import itertools
import re
from typing import Any
from urllib.parse import urlparse

import httpx

LAMPORTS_PER_SOL = 1_000_000_000
PUBLIC_RPC_HOSTS = ("api.mainnet-beta.solana.com",)
MAX_BACKOFF_S = 30.0

# "...try the request again with the following configuration parameter: "maxSupportedTransactionVersion": 1"
_VERSION_HINT = re.compile(r'"maxSupportedTransactionVersion"\s*:\s*(\d+)')
VERSION_UNSUPPORTED = -32015
# Helius: 429 "max usage reached" once the plan's monthly credits are spent; Alchemy: "Monthly capacity limit exceeded".
_QUOTA = re.compile(r"max usage|credit|quota|capacity limit|monthly limit", re.IGNORECASE)


def clean_rpc_url(raw: str) -> str:
    """Tidy an RPC URL copied into .env (stray quotes, Windows line endings)
    and check it is a full http(s) URL. Errors never echo the URL, which
    usually contains the API key."""
    url = raw.strip().strip("\"'").strip()
    p = urlparse(url)
    if p.scheme not in ("http", "https") or not p.hostname:
        raise ValueError("the RPC URL must be a full address starting with https://, "
                         "e.g. https://mainnet.helius-rpc.com/?api-key=...")
    return url


def _excerpt(text: str, n: int = 120) -> str:
    return " ".join(text.split())[:n]


class RpcError(RuntimeError):
    """The RPC answered, but with a JSON-RPC error."""

    def __init__(self, code: int | None, message: str) -> None:
        super().__init__(f"RPC error {code}: {message}")
        self.code = code
        self.message = message


class RpcHTTPError(RuntimeError):
    """The RPC could not be reached or refused the request (after retries)."""

    def __init__(self, status: int | None, message: str, *, kind: str = "transient") -> None:
        super().__init__(message)
        self.status = status
        # transient: retry later | credentials: URL/key rejected | quota: plan's credits used up | url: not a usable RPC URL
        self.kind = "credentials" if kind == "transient" and status in (401, 403, 404) else kind

    @property
    def fatal(self) -> bool:
        """Retrying other requests won't help until the user changes something."""
        return self.kind != "transient"


class SolanaRpc:
    def __init__(self, url: str, http: httpx.AsyncClient | None = None, max_retries: int = 5,
                 requests_per_second: float | None = None, backoff_s: float = 1.0) -> None:
        self.url = clean_rpc_url(url)
        self.http = http or httpx.AsyncClient(timeout=30.0)
        self.max_retries = max_retries
        self.backoff_s = backoff_s
        if requests_per_second is None:
            requests_per_second = 3.0 if self.is_public else 8.0
        self.requests_per_second = requests_per_second
        self._interval = 1.0 / requests_per_second if requests_per_second > 0 else 0.0
        self._next_slot = 0.0
        self._pace = asyncio.Lock()
        self._ids = itertools.count(1)
        # Solana added transaction format v1 in Sept 2026; raised automatically if a newer one appears.
        self.max_tx_version = 1
        self.rate_limited = 0  # 429 answers seen, for reporting

    @property
    def host(self) -> str:
        """Host only: safe to print (the URL's query string usually holds the API key)."""
        return urlparse(self.url).hostname or "<invalid RPC URL>"

    @property
    def is_public(self) -> bool:
        return self.host in PUBLIC_RPC_HOSTS

    async def _throttle(self) -> None:
        if not self._interval:
            return
        async with self._pace:
            loop = asyncio.get_running_loop()
            now = loop.time()
            if self._next_slot > now:
                await asyncio.sleep(self._next_slot - now)
                now = loop.time()
            self._next_slot = max(now, self._next_slot) + self._interval

    async def call(self, method: str, params: list[Any] | None = None) -> Any:
        body = {"jsonrpc": "2.0", "id": next(self._ids), "method": method, "params": params or []}
        return await self.post_raw(body)

    async def post_raw(self, body: dict | str) -> Any:
        delay = self.backoff_s
        for attempt in range(self.max_retries + 1):
            await self._throttle()
            wait = delay
            r = None
            try:
                if isinstance(body, str):
                    r = await self.http.post(self.url, content=body, headers={"Content-Type": "application/json"})
                else:
                    r = await self.http.post(self.url, json=body)
            except (httpx.InvalidURL, httpx.UnsupportedProtocol) as e:
                raise RpcHTTPError(None, f"the RPC URL for {self.host} is not usable ({type(e).__name__})",
                                   kind="url") from None
            except httpx.TransportError as e:
                problem = RpcHTTPError(None, f"could not reach the RPC at {self.host}: {type(e).__name__}")
            else:
                if r.status_code == 429:
                    if _QUOTA.search(r.text):
                        raise RpcHTTPError(429, f"HTTP 429 from {self.host}: the RPC plan's credits are used up "
                                                f"({_excerpt(r.text)})", kind="quota")
                    self.rate_limited += 1
                    problem = RpcHTTPError(429, f"HTTP 429 from {self.host}: rate limited by the RPC provider")
                elif r.status_code >= 500:
                    problem = RpcHTTPError(r.status_code, f"HTTP {r.status_code} from {self.host} (server error)"
                                                          f"{': ' + _excerpt(r.text) if r.text.strip() else ''}")
                elif 300 <= r.status_code < 400:
                    loc = urlparse(r.headers.get("location", ""))
                    where = f"{loc.scheme}://{loc.hostname}" if loc.hostname else "another address"
                    raise RpcHTTPError(r.status_code, f"HTTP {r.status_code} from {self.host}: the RPC redirects to "
                                                      f"{where}; put that https:// URL in SOLANA_RPC_URL", kind="url")
                elif r.status_code >= 400:
                    raise RpcHTTPError(r.status_code, f"HTTP {r.status_code} from {self.host}: {_excerpt(r.text, 200)}")
                else:
                    try:
                        data = r.json()
                    except ValueError:
                        data = None
                    if not isinstance(data, dict):
                        raise RpcHTTPError(r.status_code, f"{self.host} did not answer like a Solana RPC "
                                                          "(is SOLANA_RPC_URL the RPC URL, not a web page?)", kind="url")
                    if "error" in data:
                        err = data["error"] or {}
                        raise RpcError(err.get("code"), str(err.get("message", err)))
                    return data.get("result")
            if r is not None and r.status_code in (429, 503):
                retry_after = r.headers.get("retry-after", "")
                if retry_after.replace(".", "", 1).isdigit():
                    wait = max(wait, float(retry_after))
            if attempt == self.max_retries:
                raise problem
            await asyncio.sleep(min(wait, MAX_BACKOFF_S))
            delay = min(delay * 2, MAX_BACKOFF_S)
        raise AssertionError("unreachable")

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
        if st.get("confirmationStatus") in ("confirmed", "finalized"):
            return True
        return None  # "processed": not final yet

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

    async def get_transaction(self, signature: str, encoding: str = "jsonParsed") -> dict | None:
        """Fetch a transaction of any version. If the node says a newer
        transaction version exists, opt in to it and ask again."""
        while True:
            try:
                return await self.call("getTransaction", [signature, {
                    "encoding": encoding, "maxSupportedTransactionVersion": self.max_tx_version,
                    "commitment": "confirmed",
                }])
            except RpcError as e:
                hint = _VERSION_HINT.search(e.message) if e.code == VERSION_UNSUPPORTED else None
                if hint and int(hint.group(1)) > self.max_tx_version:
                    self.max_tx_version = int(hint.group(1))
                    continue
                raise

    async def aclose(self) -> None:
        await self.http.aclose()
