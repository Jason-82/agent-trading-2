"""Synthetic data reproducing the article's walkthrough, for demos and tests.

History (30 days): wallets A, B and C repeatedly enter the same tokens within
minutes of each other, A first; each also trades alone. D is a patient
holder with its own, non-overlapping history. E is a pure scalper, F owes
nearly all its profit to one token, G has almost no history — the selection
criteria should keep E, F and G off the roster.

Live sequence on TOKEN: A enters (scaling in), B and C follow, D arrives,
A sells part, D adds while B exits, C exits, D exits. Two control cases: a
DECOY token bought only by the recurring trio, and a LONE token that A buys
and immediately sells.
"""

from __future__ import annotations

import random
from dataclasses import dataclass

from ..models import PriceTick, Side, Trade

B58 = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
T0 = 1_759_000_000.0
MIN = 60.0
HOUR = 3600.0
DAY = 86400.0


@dataclass
class Scenario:
    history: list[Trade]
    live: list[Trade | PriceTick]
    aliases: dict[str, str]  # wallet -> alias
    token: str
    decoy: str
    lone: str
    start_ts: float

    def wallet(self, alias: str) -> str:
        return next(w for w, a in self.aliases.items() if a == alias)


class _Gen:
    def __init__(self, seed: int) -> None:
        self.rng = random.Random(seed)
        self.trades: list[Trade] = []

    def addr(self, prefix: str = "") -> str:
        return prefix + "".join(self.rng.choice(B58) for _ in range(44 - len(prefix)))

    def sig(self) -> str:
        return "".join(self.rng.choice(B58) for _ in range(88))

    def trade(self, wallet: str, mint: str, side: Side, sol: float, tokens: float, ts: float) -> Trade:
        t = Trade(self.sig(), wallet, mint, side, sol, tokens, ts, source="synthetic")
        self.trades.append(t)
        return t

    def round_trip(self, wallet: str, mint: str, t: float, sol: float, hold_s: float, mult: float,
                   n_buys: int = 1, price: float = 3e-8) -> None:
        tokens = 0.0
        for i in range(n_buys):
            p = price * (1 + 0.08 * i)
            part = sol / n_buys
            tokens += part / p
            self.trade(wallet, mint, Side.BUY, part, part / p, t + i * 90)
        self.trade(wallet, mint, Side.SELL, sol * mult, tokens, t + hold_s)


def article_scenario(seed: int = 7) -> Scenario:
    g = _Gen(seed)
    r = g.rng
    names = "ABCDEFG"
    wallets = {a: g.addr() for a in names}
    A, B, C, D, E, F, G = (wallets[a] for a in names)
    start = T0 - 30 * DAY

    # A, B and C arrive together on six tokens, A first, B and C minutes later.
    for k in range(6):
        m = g.addr("pump")
        t = start + k * 4 * DAY + r.uniform(0, 6 * HOUR)
        mult = 0.6 if k == 3 else r.uniform(1.3, 2.5)
        g.round_trip(A, m, t, 0.8, r.uniform(2, 6) * HOUR, mult, n_buys=2)
        g.round_trip(B, m, t + r.uniform(2, 6) * MIN, 0.6, r.uniform(2, 6) * HOUR, mult * 0.95)
        g.round_trip(C, m, t + r.uniform(5, 12) * MIN, 0.5, r.uniform(2, 6) * HOUR, mult * 0.9)

    # Each of the trio also trades alone.
    solo_a = []
    for w, n_buys, size in ((A, 2, 0.8), (B, 1, 0.6), (C, 1, 0.5)):
        for _ in range(5):
            m = g.addr("pump")
            if w == A:
                solo_a.append(m)
            t = start + r.uniform(0, 28 * DAY)
            g.round_trip(w, m, t, size * r.uniform(0.8, 1.2), r.uniform(1, 8) * HOUR,
                         r.uniform(0.5, 2.2) if r.random() < 0.3 else r.uniform(1.2, 2.2), n_buys=n_buys)

    # D: patient holder, never arriving with the trio. One shared token, days apart.
    for k in range(10):
        m = g.addr("pump")
        t = start + k * 2.7 * DAY + r.uniform(0, 5 * HOUR)
        g.round_trip(D, m, t, 1.5 * r.uniform(0.85, 1.15), r.uniform(3, 10) * HOUR,
                     r.uniform(0.7, 1.0) if k in (2, 7) else r.uniform(1.4, 2.8))
    a_first = min(t.ts for t in g.trades if t.wallet == A and t.mint == solo_a[0])
    g.round_trip(D, solo_a[0], a_first + 3 * DAY, 1.4, 4 * HOUR, 1.1)

    # E: scalper. F: one lucky token. G: two trades.
    for _ in range(25):
        g.round_trip(E, g.addr("pump"), start + r.uniform(0, 29 * DAY), 0.3, r.uniform(30, 180), r.uniform(0.95, 1.15))
    for k in range(8):
        g.round_trip(F, g.addr("pump"), start + k * 3 * DAY, 1.0, 2 * HOUR, 12.0 if k == 0 else r.uniform(0.5, 0.9))
    g.round_trip(G, g.addr("pump"), start + 5 * DAY, 0.5, HOUR, 1.4)

    history = sorted(g.trades, key=lambda t: t.ts)
    g.trades = []

    # --- live sequence --------------------------------------------------
    token, decoy, lone = g.addr("pump"), g.addr("pump"), g.addr("pump")
    live: list[Trade | PriceTick] = []
    p0 = 2.8e-8

    def token_price(minute: float) -> float:
        if minute <= 45:
            return p0 * (1 + 2.6 * (minute / 45) ** 1.2)
        return p0 * max(0.3, 3.6 - 2.4 * (minute - 45) / 75)

    def buy(w: str, mint: str, minute: float, sol: float, price_fn) -> float:
        px = price_fn(minute)
        live.append(g.trade(w, mint, Side.BUY, sol, sol / px, T0 + minute * MIN))
        return sol / px

    def sell(w: str, mint: str, minute: float, tokens: float, price_fn) -> None:
        px = price_fn(minute)
        live.append(g.trade(w, mint, Side.SELL, tokens * px, tokens, T0 + minute * MIN))

    for minute in range(0, 121):
        live.append(PriceTick(token, token_price(minute + 0.5), T0 + (minute + 0.5) * MIN,
                              market_cap_sol=token_price(minute + 0.5) * 1e9))

    held: dict[str, float] = {}
    held[A] = buy(A, token, 0, 0.4, token_price)
    held[A] += buy(A, token, 2, 0.4, token_price)
    held[B] = buy(B, token, 4, 0.6, token_price)
    held[C] = buy(C, token, 7, 0.5, token_price)
    held[D] = buy(D, token, 11, 1.5, token_price)
    sell(A, token, 40, held[A] / 2, token_price)
    held[D] += buy(D, token, 55, 1.0, token_price)
    sell(B, token, 55.5, held[B], token_price)
    sell(C, token, 70, held[C], token_price)
    sell(D, token, 95, held[D], token_price)

    # DECOY: only the recurring trio.
    d0 = 150.0

    def decoy_price(minute: float) -> float:
        return 4e-8 * (1 + 0.02 * max(0.0, minute - d0))

    for minute in range(int(d0), int(d0) + 60):
        live.append(PriceTick(decoy, decoy_price(minute + 0.5), T0 + (minute + 0.5) * MIN))
    da = buy(A, decoy, d0, 0.8, decoy_price)
    db = buy(B, decoy, d0 + 3, 0.6, decoy_price)
    dc = buy(C, decoy, d0 + 6, 0.5, decoy_price)
    sell(A, decoy, d0 + 50, da, decoy_price)
    sell(B, decoy, d0 + 52, db, decoy_price)
    sell(C, decoy, d0 + 55, dc, decoy_price)

    # LONE: A buys and immediately sells; the sale is still captured.
    def lone_price(minute: float) -> float:
        return 5e-8 * (1 - 0.1 * (minute - 220))

    la = buy(A, lone, 220, 0.8, lone_price)
    sell(A, lone, 222, la, lone_price)

    live.sort(key=lambda e: e.ts)
    return Scenario(history=history, live=live, aliases={w: a for a, w in wallets.items()},
                    token=token, decoy=decoy, lone=lone, start_ts=T0)
