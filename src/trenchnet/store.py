"""SQLite persistence. Reports and assessments are append-only: a new report
never rewrites an earlier one, so you can follow how a reading evolved."""

from __future__ import annotations

import json
import logging
import sqlite3
from pathlib import Path
from typing import Any, Iterable

from .models import Side, Trade

log = logging.getLogger(__name__)

SCHEMA = """
CREATE TABLE IF NOT EXISTS trades (
    signature TEXT NOT NULL,
    wallet TEXT NOT NULL,
    mint TEXT NOT NULL,
    side TEXT NOT NULL,
    sol_amount REAL NOT NULL,
    token_amount REAL NOT NULL,
    ts REAL NOT NULL,
    market_cap_sol REAL,
    price_sol REAL,
    source TEXT,
    quote_mint TEXT,
    quote_amount REAL,
    PRIMARY KEY (signature, wallet, mint, side)
);
CREATE INDEX IF NOT EXISTS trades_wallet_ts ON trades(wallet, ts);
CREATE INDEX IF NOT EXISTS trades_mint_ts ON trades(mint, ts);

CREATE TABLE IF NOT EXISTS roster (
    wallet TEXT PRIMARY KEY,
    alias TEXT,
    included INTEGER NOT NULL,
    score REAL NOT NULL,
    reasons TEXT NOT NULL,
    stats TEXT NOT NULL,
    profile TEXT,
    profile_source TEXT,
    updated_ts REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS assessments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    mint TEXT NOT NULL,
    signature TEXT NOT NULL,
    wallet TEXT NOT NULL,
    ts REAL NOT NULL,
    route TEXT NOT NULL,
    reasons TEXT NOT NULL,
    assessment TEXT NOT NULL,
    source TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS assessments_mint ON assessments(mint, ts);

CREATE TABLE IF NOT EXISTS reports (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    mint TEXT NOT NULL,
    ts REAL NOT NULL,
    trigger_signature TEXT NOT NULL,
    previous_id INTEGER,
    source TEXT NOT NULL,
    report TEXT NOT NULL,
    evidence_signatures TEXT NOT NULL,
    unsupported_citations TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS reports_mint ON reports(mint, ts);

CREATE TABLE IF NOT EXISTS prices (
    mint TEXT PRIMARY KEY,
    price_sol REAL NOT NULL,
    ts REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS sol_usd (
    bucket INTEGER PRIMARY KEY,  -- start of a 5-minute candle, unix seconds
    price REAL NOT NULL,
    source TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS fills (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    mint TEXT NOT NULL,
    side TEXT NOT NULL,
    sol_amount REAL NOT NULL,
    token_amount REAL NOT NULL,
    price_sol REAL NOT NULL,
    fee_sol REAL NOT NULL,
    ts REAL NOT NULL,
    mode TEXT NOT NULL,
    reason TEXT NOT NULL,
    tx_signature TEXT
);
"""


class Store:
    def __init__(self, path: str | Path = ":memory:") -> None:
        self.path = str(path)
        self.db = sqlite3.connect(self.path)
        self.db.row_factory = sqlite3.Row
        self.db.executescript(SCHEMA)
        self._migrate()

    def _migrate(self) -> None:
        """Bring a database made by an older version up to date."""
        cols = {r["name"] for r in self.db.execute("PRAGMA table_info(trades)")}
        added = []
        for name, kind in (("quote_mint", "TEXT"), ("quote_amount", "REAL")):
            if name not in cols:
                self.db.execute(f"ALTER TABLE trades ADD COLUMN {name} {kind}")
                added.append(name)
        self.db.commit()
        if added and self.path != ":memory:" and self.db.execute("SELECT COUNT(*) FROM trades").fetchone()[0]:
            # An older version still running on this file can't write trades any more.
            log.warning("updated %s for this version of trenchnet; if a `trenchnet run` started before the "
                        "update is still running, stop it and start it again", self.path)

    def close(self) -> None:
        self.db.close()

    # -- trades -------------------------------------------------------------

    _INSERT = ("INSERT OR IGNORE INTO trades (signature, wallet, mint, side, sol_amount, token_amount, ts,"
               " market_cap_sol, price_sol, source, quote_mint, quote_amount) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)")

    @staticmethod
    def _row(t: Trade) -> tuple:
        return (t.signature, t.wallet, t.mint, t.side.value, t.sol_amount, t.token_amount, t.ts,
                t.market_cap_sol, t.price_sol, t.source, t.quote_mint, t.quote_amount)

    def add_trades(self, trades: Iterable[Trade]) -> int:
        cur = self.db.executemany(self._INSERT, [self._row(t) for t in trades])
        self.db.commit()
        return cur.rowcount

    def replace_trades(self, wallet: str, trades: Iterable[Trade], signatures: Iterable[str],
                       source: str = "rpc") -> tuple[int, int]:
        """Save a fresh reading of some of a wallet's transactions. Stored rows
        from `source` for those signatures are replaced by `trades`, all in one
        database transaction; rows for other signatures (older history, trades
        seen live) are untouched.

        Trades in the same second replay in the order they were saved, so a
        second is only rewritten in chain order when this reading covers every
        row stored for it. Where it doesn't (a neighbour couldn't be read this
        time, or was seen live), rows that read the same are updated where they
        are instead of being moved behind their neighbours.

        Returns (rows that weren't stored before, old rows that are gone because
        the transaction now reads differently)."""
        trades = list(trades)
        sigs = sorted(set(signatures) | {t.signature for t in trades})
        sig_set = set(sigs)
        new_keys = {t.key for t in trades}
        with self.db:
            rows: list[sqlite3.Row] = []
            for i in range(0, len(sigs), 500):  # stay under SQLite's limit on query parameters
                chunk = sigs[i:i + 500]
                rows += self.db.execute(
                    f"SELECT signature, wallet, mint, side, ts FROM trades WHERE wallet = ? AND source = ?"
                    f" AND signature IN ({','.join('?' * len(chunk))})", [wallet, source, *chunk]).fetchall()
            old = {tuple(r)[:4] for r in rows}
            partial: set[float] = set()
            for ts in {r["ts"] for r in rows} | {t.ts for t in trades}:
                for r in self.db.execute("SELECT signature, source FROM trades WHERE wallet = ? AND ts = ?",
                                         (wallet, ts)):
                    if r["source"] != source or r["signature"] not in sig_set:
                        partial.add(ts)
                        break
            keep = {tuple(r)[:4] for r in rows if r["ts"] in partial} & new_keys
            self.db.executemany(
                "DELETE FROM trades WHERE signature = ? AND wallet = ? AND mint = ? AND side = ? AND source = ?",
                [(*k, source) for k in old - keep])
            self.db.executemany(
                "UPDATE trades SET sol_amount = ?, token_amount = ?, ts = ?, market_cap_sol = ?, price_sol = ?,"
                " quote_mint = ?, quote_amount = ? WHERE signature = ? AND wallet = ? AND mint = ? AND side = ?"
                " AND source = ?",
                [(t.sol_amount, t.token_amount, t.ts, t.market_cap_sol, t.price_sol, t.quote_mint, t.quote_amount,
                  *t.key, source) for t in trades if t.key in keep])
            inserted = self.db.executemany(self._INSERT, [self._row(t) for t in trades if t.key not in keep]).rowcount
        return inserted - len((old - keep) & new_keys), len(old - new_keys)

    def traded_mints(self, wallet: str) -> set[str]:
        """Tokens the wallet bought or sold in the stored history."""
        return {r[0] for r in self.db.execute(
            "SELECT DISTINCT mint FROM trades WHERE wallet = ? AND side IN ('buy', 'sell')", (wallet,))}

    def add_trade(self, t: Trade) -> bool:
        return self.add_trades([t]) > 0

    def trades(self, wallets: Iterable[str] | None = None, mint: str | None = None,
               until: float | None = None) -> list[Trade]:
        q, args = "SELECT * FROM trades WHERE 1=1", []
        if wallets is not None:
            ws = list(wallets)
            if not ws:
                return []
            q += f" AND wallet IN ({','.join('?' * len(ws))})"
            args += ws
        if mint is not None:
            q += " AND mint = ?"
            args.append(mint)
        if until is not None:
            q += " AND ts <= ?"
            args.append(until)
        q += " ORDER BY ts, rowid"
        return [self._row_to_trade(r) for r in self.db.execute(q, args)]

    def delete_trades(self, wallet: str, source: str) -> int:
        cur = self.db.execute("DELETE FROM trades WHERE wallet = ? AND source = ?", (wallet, source))
        self.db.commit()
        return cur.rowcount

    def wallet_trade_counts(self) -> dict[str, int]:
        return {r[0]: r[1] for r in self.db.execute("SELECT wallet, COUNT(*) FROM trades GROUP BY wallet")}

    def wallets(self) -> list[str]:
        return [r[0] for r in self.db.execute("SELECT DISTINCT wallet FROM trades ORDER BY wallet")]

    @staticmethod
    def _row_to_trade(r: sqlite3.Row) -> Trade:
        return Trade(
            signature=r["signature"], wallet=r["wallet"], mint=r["mint"], side=Side(r["side"]),
            sol_amount=r["sol_amount"], token_amount=r["token_amount"], ts=r["ts"],
            market_cap_sol=r["market_cap_sol"], price_sol=r["price_sol"], source=r["source"] or "unknown",
            quote_mint=r["quote_mint"], quote_amount=r["quote_amount"],
        )

    # -- roster -------------------------------------------------------------

    def upsert_roster(self, *, wallet: str, alias: str | None, included: bool, score: float,
                      reasons: list[str], stats: dict, profile: dict | None,
                      profile_source: str | None, ts: float) -> None:
        self.db.execute(
            "INSERT OR REPLACE INTO roster VALUES (?,?,?,?,?,?,?,?,?)",
            (wallet, alias, int(included), score, json.dumps(reasons), json.dumps(stats),
             json.dumps(profile) if profile is not None else None, profile_source, ts),
        )
        self.db.commit()

    def roster(self, included_only: bool = True) -> list[dict[str, Any]]:
        q = "SELECT * FROM roster" + (" WHERE included = 1" if included_only else "") + " ORDER BY score DESC"
        out = []
        for r in self.db.execute(q):
            out.append({
                "wallet": r["wallet"], "alias": r["alias"], "included": bool(r["included"]),
                "score": r["score"], "reasons": json.loads(r["reasons"]), "stats": json.loads(r["stats"]),
                "profile": json.loads(r["profile"]) if r["profile"] else None,
                "profile_source": r["profile_source"], "updated_ts": r["updated_ts"],
            })
        return out

    # -- assessments & reports -----------------------------------------------

    def add_assessment(self, *, mint: str, signature: str, wallet: str, ts: float, route: str,
                       reasons: list[str], assessment: dict, source: str) -> None:
        self.db.execute(
            "INSERT INTO assessments (mint, signature, wallet, ts, route, reasons, assessment, source)"
            " VALUES (?,?,?,?,?,?,?,?)",
            (mint, signature, wallet, ts, route, json.dumps(reasons), json.dumps(assessment), source),
        )
        self.db.commit()

    def add_report(self, *, mint: str, ts: float, trigger_signature: str, previous_id: int | None,
                   source: str, report: dict, evidence_signatures: list[str],
                   unsupported_citations: list[str]) -> int:
        cur = self.db.execute(
            "INSERT INTO reports (mint, ts, trigger_signature, previous_id, source, report,"
            " evidence_signatures, unsupported_citations) VALUES (?,?,?,?,?,?,?,?)",
            (mint, ts, trigger_signature, previous_id, source, json.dumps(report),
             json.dumps(evidence_signatures), json.dumps(unsupported_citations)),
        )
        self.db.commit()
        return int(cur.lastrowid)

    def reports(self, mint: str | None = None, limit: int = 50) -> list[dict[str, Any]]:
        q, args = "SELECT * FROM reports", []
        if mint:
            q += " WHERE mint = ?"
            args.append(mint)
        q += " ORDER BY ts DESC, id DESC LIMIT ?"
        args.append(limit)
        return [
            {
                "id": r["id"], "mint": r["mint"], "ts": r["ts"], "trigger_signature": r["trigger_signature"],
                "previous_id": r["previous_id"], "source": r["source"], "report": json.loads(r["report"]),
                "evidence_signatures": json.loads(r["evidence_signatures"]),
                "unsupported_citations": json.loads(r["unsupported_citations"]),
            }
            for r in self.db.execute(q, args)
        ]

    # -- prices -------------------------------------------------------------

    def save_price(self, mint: str, price_sol: float, ts: float) -> None:
        """Remember the latest price seen for a token (used to value open positions)."""
        self.db.execute(
            "INSERT INTO prices VALUES (?,?,?) ON CONFLICT(mint) DO UPDATE SET"
            " price_sol = excluded.price_sol, ts = excluded.ts WHERE excluded.ts >= prices.ts",
            (mint, price_sol, ts),
        )
        self.db.commit()

    def last_price(self, mint: str) -> tuple[float, float] | None:
        """(price, ts) of the newest price we have: a saved price or a stored trade."""
        best: tuple[float, float] | None = None
        r = self.db.execute("SELECT price_sol, ts FROM prices WHERE mint = ?", (mint,)).fetchone()
        if r is not None:
            best = (r["price_sol"], r["ts"])
        r = self.db.execute(
            "SELECT * FROM trades WHERE mint = ? AND side IN ('buy', 'sell') ORDER BY ts DESC, rowid DESC LIMIT 1",
            (mint,),
        ).fetchone()
        if r is not None:
            t = self._row_to_trade(r)
            if t.price is not None and (best is None or t.ts > best[1]):
                best = (t.price, t.ts)
        return best

    # -- SOL/USD candles (for trades priced in stablecoins) -----------------

    def save_sol_usd(self, rows: Iterable[tuple[int, float, str]]) -> None:
        self.db.executemany("INSERT OR REPLACE INTO sol_usd VALUES (?,?,?)", list(rows))
        self.db.commit()

    def sol_usd(self, lo: int, hi: int) -> dict[int, float]:
        return {r[0]: r[1] for r in self.db.execute(
            "SELECT bucket, price FROM sol_usd WHERE bucket BETWEEN ? AND ?", (lo, hi))}

    # -- fills --------------------------------------------------------------

    def add_fill(self, *, mint: str, side: str, sol_amount: float, token_amount: float, price_sol: float,
                 fee_sol: float, ts: float, mode: str, reason: str, tx_signature: str | None) -> None:
        self.db.execute(
            "INSERT INTO fills (mint, side, sol_amount, token_amount, price_sol, fee_sol, ts, mode, reason,"
            " tx_signature) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (mint, side, sol_amount, token_amount, price_sol, fee_sol, ts, mode, reason, tx_signature),
        )
        self.db.commit()

    def clear_fills(self, mode: str) -> int:
        cur = self.db.execute("DELETE FROM fills WHERE mode = ?", (mode,))
        self.db.commit()
        return cur.rowcount

    def fills(self, mode: str | None = None) -> list[dict[str, Any]]:
        q, args = "SELECT * FROM fills", []
        if mode:
            q += " WHERE mode = ?"
            args.append(mode)
        q += " ORDER BY ts, id"
        return [dict(r) for r in self.db.execute(q, args)]
