"""`trenchnet inspect` and the per-wallet skip reasons."""

import json

import httpx
import pytest
from solders.pubkey import Pubkey

from trenchnet.cli import _resolve_wallet, main
from trenchnet.config import Config
from trenchnet.sources.rpc_backfill import analyze_wallet_tx

W = str(Pubkey.new_unique())
ATA = str(Pubkey.new_unique())
OTHER = str(Pubkey.new_unique())


def tx(sol_pre, sol_post, pre_tok, post_tok, keys=None):
    keys = keys or [W, ATA, OTHER]
    idx = {"MintA": 1, "MintB": 2, "USD1": 2}

    def bal(amounts):
        return [{"accountIndex": idx[m], "owner": W, "mint": m, "uiTokenAmount": {"uiAmountString": str(a)}}
                for m, a in amounts.items()]

    n = len(keys)
    return {"blockTime": 1_760_000_000, "transaction": {"message": {"accountKeys": [{"pubkey": k} for k in keys]}},
            "meta": {"err": None, "fee": 5000, "preBalances": [sol_pre] + [0] * (n - 1),
                     "postBalances": [sol_post] + [0] * (n - 1), "preTokenBalances": bal(pre_tok),
                     "postTokenBalances": bal(post_tok)}}


TXS = {
    "buy": tx(10**9, 10**9 - 300_005_000, {"MintA": 0}, {"MintA": 500}),
    "sell": tx(10**9, 10**9 + 450_000_000 - 5000, {"MintA": 500}, {"MintA": 0}),
    "solonly": tx(10**9, 10**9 - 1_000_005_000, {}, {}),
    "usd1swap": tx(10**9, 10**9 - 5000, {"USD1": 100, "MintA": 0}, {"USD1": 0, "MintA": 900}),
    "elsewhere": tx(10**9, 10**9, {}, {}, keys=[OTHER, ATA]),
}


def test_analyze_reports_why():
    assert analyze_wallet_tx(TXS["buy"], W, "s").trade.side.value == "buy"
    assert analyze_wallet_tx(TXS["sell"], W, "s").trade.side.value == "sell"
    v = analyze_wallet_tx(TXS["solonly"], W, "s")
    assert v.status == "no_token_change" and v.sol_delta == pytest.approx(-1.0)
    v = analyze_wallet_tx(TXS["usd1swap"], W, "s")
    assert v.status == "several_tokens" and v.token_deltas == {"USD1": -100.0, "MintA": 900.0}
    assert analyze_wallet_tx(TXS["elsewhere"], W, "s").status == "not_in_tx"


def _serve(monkeypatch):
    names = list(TXS) + ["failed"]

    def handler(req):
        b = json.loads(req.content)
        if b["method"] == "getSignaturesForAddress":
            return httpx.Response(200, json={"result": [
                {"signature": f"{n}Sig", "blockTime": 1_760_000_000, "err": {"x": 1} if n == "failed" else None}
                for n in names]})
        return httpx.Response(200, json={"result": TXS[b["params"][0][:-3]]})

    real = httpx.AsyncClient
    monkeypatch.setattr("trenchnet.solana_rpc.httpx.AsyncClient",
                        lambda *a, **k: real(transport=httpx.MockTransport(handler)))


def test_inspect_prints_each_transaction_and_examples(tmp_path, monkeypatch, capsys):
    _serve(monkeypatch)
    monkeypatch.chdir(tmp_path)
    (tmp_path / "wallets.txt").write_text(f"{W},decu\n")
    main(["inspect", "decu", "--rpc", "https://rpc.example/?api-key=K"])
    out = capsys.readouterr().out
    assert "BUY" in out and "SELL" in out
    assert "skipped: no token change" in out
    assert "skipped: 2+ tokens changed" in out and "USD1 -100" in out
    assert "skipped: wallet not in tx" in out and "skipped: failed on-chain" in out
    assert "2+ tokens changed: https://solscan.io/tx/usd1swapSig" in out
    assert "api-key" not in out


def test_resolve_wallet_by_nickname_or_short_form(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "wallets.txt").write_text(f"{W},decu\n")
    cfg = Config()
    cfg.db_path = str(tmp_path / "none.db")
    assert _resolve_wallet(W, cfg) == W
    assert _resolve_wallet("DECU", cfg) == W
    assert _resolve_wallet(f"{W[:4]}…{W[-4:]}", cfg) == W
    assert _resolve_wallet(f"{W[:4]}...{W[-4:]}", cfg) == W
    with pytest.raises(SystemExit):
        _resolve_wallet("nobody", cfg)
