"""Backfill against Solana's current RPC behaviour: v1 transactions (Sept 2026),
rate limits, raw base64 transactions, and bad credentials."""

import asyncio
import base64
import json
import time

import httpx
import pytest
from solders.hash import Hash
from solders.instruction import CompiledInstruction
from solders.message import Message, MessageHeader, MessageV1, TransactionConfig
from solders.pubkey import Pubkey
from solders.signature import Signature
from solders.transaction import VersionedTransaction

from trenchnet.cli import main
from trenchnet.models import Side
from trenchnet.solana_rpc import RpcError, RpcHTTPError, SolanaRpc
from trenchnet.sources.rpc_backfill import account_keys, backfill_wallet, fetch_transaction, parse_wallet_tx
from trenchnet.store import Store

V1_ERROR = {"code": -32015, "message": "Transaction version (1) is not supported by the requesting client. "
            "Please try the request again with the following configuration parameter: "
            "\"maxSupportedTransactionVersion\": 1"}


def rpc_with(handler, **kw) -> SolanaRpc:
    kw.setdefault("requests_per_second", 0)
    kw.setdefault("backoff_s", 0.01)
    return SolanaRpc("https://rpc.example/?api-key=SECRET", http=httpx.AsyncClient(transport=httpx.MockTransport(handler)), **kw)


def ok(result):
    return httpx.Response(200, json={"jsonrpc": "2.0", "id": 1, "result": result})


# -- RPC client ------------------------------------------------------------------

async def test_rate_limit_is_retried_and_counted():
    calls = []

    def handler(req):
        calls.append(1)
        if len(calls) < 3:
            return httpx.Response(429, headers={"retry-after": "0"}, text="Too Many Requests")
        return ok({"value": 5_000_000_000})

    rpc = rpc_with(handler)
    assert await rpc.get_balance_sol("W") == 5.0
    assert len(calls) == 3 and rpc.rate_limited == 2


async def test_rate_limit_message_is_clear_and_hides_the_key():
    rpc = rpc_with(lambda req: httpx.Response(429), max_retries=1)
    with pytest.raises(RpcHTTPError) as e:
        await rpc.get_balance_sol("W")
    assert "429" in str(e.value) and "rate limited" in str(e.value)
    assert "SECRET" not in str(e.value) and rpc.host == "rpc.example"


async def test_bad_key_fails_fast():
    calls = []

    def handler(req):
        calls.append(1)
        return httpx.Response(401, text='{"error":"invalid api key"}')

    with pytest.raises(RpcHTTPError) as e:
        await rpc_with(handler).get_balance_sol("W")
    assert e.value.fatal and len(calls) == 1


async def test_requests_are_paced():
    rpc = rpc_with(lambda req: ok({"value": 0}), requests_per_second=20)
    start = time.monotonic()
    await asyncio.gather(*(rpc.get_balance_sol("W") for _ in range(6)))
    assert time.monotonic() - start >= 0.24  # 6 requests at 20/s -> at least 5 gaps of 50ms


async def test_transaction_version_is_negotiated():
    seen = []

    def handler(req):
        opts = json.loads(req.content)["params"][1]
        seen.append(opts["maxSupportedTransactionVersion"])
        if opts["maxSupportedTransactionVersion"] < 1:
            return httpx.Response(200, json={"jsonrpc": "2.0", "id": 1, "error": V1_ERROR})
        return ok({"slot": 1})

    rpc = rpc_with(handler)
    assert rpc.max_tx_version == 1  # v1 asked for up front
    rpc.max_tx_version = 0  # an old client: learns from the error and asks again
    assert await rpc.get_transaction("S") == {"slot": 1}
    assert seen == [0, 1] and rpc.max_tx_version == 1


def test_public_rpc_gets_a_gentler_pace():
    assert SolanaRpc("https://api.mainnet-beta.solana.com").requests_per_second == 3
    assert SolanaRpc("https://mainnet.helius-rpc.com/?api-key=x").requests_per_second == 8


# -- reading transactions of every format --------------------------------------------

W = Pubkey.new_unique()
ATA = Pubkey.new_unique()
PROGRAM = Pubkey.new_unique()
MINT = "Mint1111111111111111111111111111111111pump"


def _b64(msg) -> list:
    tx = VersionedTransaction.populate(msg, [Signature.default()])
    return [base64.b64encode(bytes(tx)).decode(), "base64"]


def _meta(n_keys, sol_pre, sol_post, loaded=None):
    pre, post = [0] * n_keys, [0] * n_keys
    pre[0], post[0] = sol_pre, sol_post
    post[1] = 2_039_280  # new token account's rent
    meta = {"err": None, "fee": 5000, "preBalances": pre, "postBalances": post, "preTokenBalances": [],
            "postTokenBalances": [{"accountIndex": 1, "owner": str(W), "mint": MINT,
                                   "uiTokenAmount": {"uiAmountString": "1000"}}]}
    if loaded is not None:
        meta["loadedAddresses"] = loaded
    return meta


def _buy_tx(transaction, n_keys, loaded=None):
    return {"blockTime": 1_700_000_000, "transaction": transaction,
            "meta": _meta(n_keys, 10**9, 10**9 - 500_000_000 - 5000 - 2_039_280, loaded)}


@pytest.mark.parametrize("version", ["legacy", "v1"])
def test_base64_transactions_are_decoded(version):
    keys = [W, ATA, PROGRAM]
    ix = [CompiledInstruction(program_id_index=2, accounts=bytes([0, 1]), data=bytes([1]))]
    if version == "legacy":
        msg = Message.new_with_compiled_instructions(1, 0, 1, keys, Hash.default(), ix)
    else:
        msg = MessageV1(MessageHeader(1, 0, 1), TransactionConfig(compute_unit_limit=200_000), Hash.default(), keys, ix)
    tx = _buy_tx(_b64(msg), 3)
    assert account_keys(tx) == [str(k) for k in keys]
    [t] = parse_wallet_tx(tx, str(W), "S")
    assert t.side is Side.BUY and t.sol_amount == pytest.approx(0.5) and t.token_amount == 1000


def test_json_encoding_appends_lookup_table_addresses_in_order():
    tx = _buy_tx({"message": {"accountKeys": [str(W), str(ATA)]}}, 4,
                 loaded={"writable": ["LoadedW"], "readonly": ["LoadedR"]})
    assert account_keys(tx) == [str(W), str(ATA), "LoadedW", "LoadedR"]
    parsed = {"message": {"accountKeys": [{"pubkey": str(W)}, {"pubkey": str(ATA)}, {"pubkey": "LoadedW"}]}}
    assert account_keys(_buy_tx(parsed, 3, loaded={"writable": ["LoadedW"], "readonly": []})) == \
        [str(W), str(ATA), "LoadedW"]  # jsonParsed already includes them: not added twice


async def test_large_v1_falls_back_to_base64():
    asked = []

    def handler(req):
        opts = json.loads(req.content)["params"][1]
        asked.append(opts["encoding"])
        if opts["encoding"] == "jsonParsed":
            return httpx.Response(200, json={"jsonrpc": "2.0", "id": 1,
                                             "error": {"code": -32602, "message": "use base64 for this transaction"}})
        return ok({"transaction": ["AAAA", "base64"]})

    assert await fetch_transaction(rpc_with(handler), "S") == {"transaction": ["AAAA", "base64"]}
    assert asked == ["jsonParsed", "base64"]


# -- whole-wallet backfill ----------------------------------------------------------

class FakeRpc:
    def __init__(self, n, flaky=(), fatal=False):
        self.n, self.flaky, self.fatal = n, set(flaky), fatal
        self.calls: dict[str, int] = {}

    async def get_signatures(self, address, limit=1000, before=None):
        return [{"signature": f"s{i}", "err": None} for i in range(self.n)] if before is None else []

    async def get_transaction(self, sig, encoding="jsonParsed"):
        self.calls[sig] = self.calls.get(sig, 0) + 1
        if self.fatal:
            raise RpcHTTPError(401, "HTTP 401 from rpc.example")
        if sig in self.flaky and self.calls[sig] == 1:
            raise RpcHTTPError(429, "HTTP 429 from rpc.example: rate limited")
        return _buy_tx({"message": {"accountKeys": [{"pubkey": str(W)}, {"pubkey": str(ATA)}]}}, 2)


async def test_transient_failures_are_retried_once_at_the_end():
    rpc = FakeRpc(5, flaky={"s1", "s3"})
    progress = []
    res = await backfill_wallet(rpc, str(W), max_signatures=5, retry_pause_s=0,
                                progress=lambda d, t, n: progress.append((d, t)))
    assert len(res.trades) == 5 and res.transactions == 5 and not res.unreadable
    assert rpc.calls["s1"] == 2 and rpc.calls["s0"] == 1
    assert progress[-1] == (5, 5)


async def test_bad_credentials_stop_the_backfill():
    with pytest.raises(RpcHTTPError):
        await backfill_wallet(FakeRpc(5, fatal=True), str(W), max_signatures=5, retry_pause_s=0)


async def test_unreadable_transactions_are_summarised_not_spammed(caplog):
    class Broken(FakeRpc):
        async def get_transaction(self, sig, encoding="jsonParsed"):
            raise RpcError(-32015, "Transaction version (2) is not supported")

    res = await backfill_wallet(Broken(4), str(W), max_signatures=4, retry_pause_s=0)
    assert res.unreadable == {"RPC error -32015": 4}
    assert caplog.text.count("could not be read") == 1


def test_cli_backfill_reports_a_rejected_key(tmp_path, monkeypatch, capsys):
    wallets = tmp_path / "wallets.txt"
    wallets.write_text(f"{W},alpha\n")
    real = httpx.AsyncClient

    def client(*a, **k):
        return real(transport=httpx.MockTransport(lambda req: httpx.Response(401, text="invalid api key")))

    monkeypatch.setattr("trenchnet.solana_rpc.httpx.AsyncClient", client)
    with pytest.raises(SystemExit) as e:
        main(["backfill", str(wallets), "--db", str(tmp_path / "t.db"), "--rpc", "https://rpc.example/?api-key=K"])
    assert "refused" in str(e.value.code) and "source .env" in str(e.value.code)
    assert "Reading history from rpc.example" in capsys.readouterr().out


async def test_unfamiliar_parsed_layout_falls_back_to_raw():
    keys = [W, ATA, PROGRAM]
    ix = [CompiledInstruction(program_id_index=2, accounts=bytes([0, 1]), data=bytes([1]))]
    msg = MessageV1(MessageHeader(1, 0, 1), TransactionConfig(compute_unit_limit=200_000), Hash.default(), keys, ix)

    class OddLayout(FakeRpc):
        async def get_transaction(self, sig, encoding="jsonParsed"):
            if encoding == "jsonParsed":  # a layout the parser doesn't know
                return {"blockTime": 1, "meta": {"err": None}, "transaction": {"messageV2": {}}}
            return _buy_tx(_b64(msg), 3)

    res = await backfill_wallet(OddLayout(2), str(W), max_signatures=2, retry_pause_s=0)
    assert len(res.trades) == 2 and not res.unreadable
