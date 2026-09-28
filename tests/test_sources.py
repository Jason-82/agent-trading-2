import pytest

from trenchnet.config import load_config
from trenchnet.models import PriceTick, Side, Trade
from trenchnet.sources.pumpportal_ws import parse_message
from trenchnet.sources.replay import read_events, write_events
from trenchnet.sources.rpc_backfill import TOKEN_ACCOUNT_RENT_SOL, WSOL, parse_wallet_tx

W = "Wallet111111111111111111111111111111111111"
MINT = "Mint1111111111111111111111111111111111pump"


def test_pumpportal_parse_tracked_and_untracked():
    msg = {"signature": "S", "mint": MINT, "traderPublicKey": W, "txType": "buy", "solAmount": 0.5,
           "tokenAmount": 1_000_000, "vSolInBondingCurve": 40.0, "vTokensInBondingCurve": 8e8,
           "marketCapSol": 50.0, "pool": "pump"}
    tick, trade = parse_message(msg, {W}, now=123.0)
    assert isinstance(tick, PriceTick) and tick.price_sol == pytest.approx(5e-8)
    assert isinstance(trade, Trade) and trade.side is Side.BUY and trade.ts == 123.0
    assert trade.source == "pumpportal:pump"
    assert [type(e) for e in parse_message(msg, set(), now=1.0)] == [PriceTick]
    assert parse_message({"message": "Successfully subscribed"}, {W}, 1.0) == []
    assert parse_message({"txType": "create", "mint": MINT}, {W}, 1.0) == []


def _tx(sol_pre, sol_post, pre_tok, post_tok, fee=5000, payer=True):
    keys = [W, "Other"] if payer else ["Other", W]
    i = keys.index(W)
    pre_bal, post_bal = [0, 0], [0, 0]
    pre_bal[i], post_bal[i] = sol_pre, sol_post

    def bal(amounts):
        return [{"owner": W, "mint": m, "uiTokenAmount": {"uiAmountString": str(a)}} for m, a in amounts.items()]

    return {
        "blockTime": 1_700_000_000,
        "meta": {"err": None, "fee": fee, "preBalances": pre_bal, "postBalances": post_bal,
                 "preTokenBalances": bal(pre_tok), "postTokenBalances": bal(post_tok)},
        "transaction": {"message": {"accountKeys": [{"pubkey": k} for k in keys]}},
    }


def test_backfill_buy_excludes_fee_and_rent():
    rent = int(TOKEN_ACCOUNT_RENT_SOL * 1e9)
    tx = _tx(10_000_000_000, 10_000_000_000 - 500_000_000 - 5000 - rent, {}, {MINT: 1000})
    [t] = parse_wallet_tx(tx, W, "S1")
    assert t.side is Side.BUY and t.sol_amount == pytest.approx(0.5) and t.token_amount == 1000


def test_backfill_sell_via_wsol_and_transfer():
    tx = _tx(1_000_000_000, 1_000_000_000 - 5000, {MINT: 1000, WSOL: 0}, {MINT: 0, WSOL: 0.8})
    [t] = parse_wallet_tx(tx, W, "S2")
    assert t.side is Side.SELL and t.sol_amount == pytest.approx(0.8)
    tx = _tx(1_000_000_000, 1_000_000_000 - 5000, {MINT: 1000}, {MINT: 400})
    [t] = parse_wallet_tx(tx, W, "S3")
    assert t.side is Side.TRANSFER_OUT and t.sol_amount == 0 and t.token_amount == 600


def test_backfill_skips_multi_token_and_failed():
    tx = _tx(1_000_000_000, 900_000_000, {}, {MINT: 10, "Other": 5})
    assert parse_wallet_tx(tx, W, "S4") == []
    tx["meta"]["err"] = {"InstructionError": [0, "x"]}
    assert parse_wallet_tx(tx, W, "S5") == []


def test_replay_roundtrip(tmp_path):
    evs = [Trade("s", W, MINT, Side.SELL, 1.0, 10, 5.0), PriceTick(MINT, 1e-7, 1.0)]
    p = tmp_path / "e.jsonl"
    write_events(p, evs)
    back = read_events(p)
    assert back == sorted(evs, key=lambda e: e.ts)


def test_config_rejects_unknown_keys(tmp_path):
    p = tmp_path / "c.toml"
    p.write_text("[strategy]\nposition_size_sol = 0.2\n")
    assert load_config(p).strategy.position_size_sol == 0.2
    p.write_text("[strategy]\nposition_size = 0.2\n")
    with pytest.raises(ValueError, match="strategy.position_size"):
        load_config(p)
