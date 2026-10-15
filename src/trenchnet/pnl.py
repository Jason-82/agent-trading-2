"""Profit and loss from the bot's own stored fills.

Everything is in SOL, so a positive number means the bot did better than
simply holding the SOL it traded with. Realized figures come straight from
the fills; open positions are marked to the last price the bot saw, which can
be stale, so unrealized figures are estimates.
"""

from __future__ import annotations

import re
import statistics
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone

from .brokers.base import Fill
from .models import short
from .portfolio import Portfolio, utc_day

_FILL_FIELDS = tuple(Fill.__dataclass_fields__)
_DURATION = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*(m|min|h|d|w)\s*$", re.IGNORECASE)
_UNIT_S = {"m": 60, "min": 60, "h": 3600, "d": 86400, "w": 7 * 86400}


def parse_since(text: str, now: float) -> float:
    """'90m', '24h', '7d', '2w' (relative to now) or a UTC date 'YYYY-MM-DD'."""
    m = _DURATION.match(text)
    if m:
        return now - float(m.group(1)) * _UNIT_S[m.group(2).lower()]
    try:
        return datetime.strptime(text.strip(), "%Y-%m-%d").replace(tzinfo=timezone.utc).timestamp()
    except ValueError:
        raise ValueError(f"can't read --since {text!r}: use e.g. 24h, 7d, 2w or 2026-10-01") from None


@dataclass
class ClosedTrade:
    mint: str
    entry_ts: float
    exit_ts: float
    cost_sol: float  # everything spent buying this position
    proceeds_sol: float  # everything received selling it
    realized_sol: float
    exit_reason: str

    @property
    def return_pct(self) -> float | None:
        return 100 * self.realized_sol / self.cost_sol if self.cost_sol > 0 else None

    @property
    def hold_s(self) -> float:
        return self.exit_ts - self.entry_ts


@dataclass
class OpenPosition:
    mint: str
    tokens: float
    cost_sol: float
    entry_ts: float
    realized_so_far_sol: float  # from partial sells (take-profits)
    mark_price: float | None
    mark_ts: float | None

    @property
    def value_sol(self) -> float | None:
        return self.tokens * self.mark_price if self.mark_price is not None else None

    @property
    def unrealized_sol(self) -> float | None:
        v = self.value_sol
        return v - self.cost_sol if v is not None else None


@dataclass
class PnlReport:
    mode: str
    since: float | None
    now: float
    realized_sol: float = 0.0
    fees_sol: float = 0.0
    n_fills: int = 0
    n_buys: int = 0
    deployed_sol: float = 0.0  # SOL spent on buys in the window
    closed: list[ClosedTrade] = field(default_factory=list)  # exited in the window, oldest first
    by_day: list[tuple[str, float, int]] = field(default_factory=list)  # (UTC day, realized, sells)
    open: list[OpenPosition] = field(default_factory=list)  # as of now, regardless of window

    @property
    def wins(self) -> list[ClosedTrade]:
        return [c for c in self.closed if c.realized_sol > 0]

    @property
    def losses(self) -> list[ClosedTrade]:
        return [c for c in self.closed if c.realized_sol < 0]

    @property
    def unrealized_sol(self) -> float | None:
        marked = [p.unrealized_sol for p in self.open if p.unrealized_sol is not None]
        return sum(marked) if marked else None

    def to_dict(self) -> dict:
        d = asdict(self)
        d["closed"] = [asdict(c) | {"return_pct": c.return_pct, "hold_s": c.hold_s} for c in self.closed]
        d["open"] = [asdict(p) | {"value_sol": p.value_sol, "unrealized_sol": p.unrealized_sol} for p in self.open]
        d["by_day"] = [{"day": day, "realized_sol": pnl, "sells": n} for day, pnl, n in self.by_day]
        d["summary"] = {
            "closed_trades": len(self.closed), "wins": len(self.wins), "losses": len(self.losses),
            "win_rate": len(self.wins) / len(self.closed) if self.closed else None,
            "unrealized_sol": self.unrealized_sol,
        }
        return d


def build_pnl(fills: list[dict], marks: dict[str, tuple[float, float]], *, mode: str, now: float,
              since: float | None = None) -> PnlReport:
    """Replay every stored fill for `mode` (cost basis needs the full history),
    then report what happened inside the window [since, now]."""
    rep = PnlReport(mode=mode, since=since, now=now)
    pf = Portfolio(0.0)
    spent: dict[str, float] = {}
    received: dict[str, float] = {}
    days: dict[str, list] = {}
    in_window = (lambda ts: True) if since is None else (lambda ts: ts >= since)  # noqa: E731

    for row in sorted((r for r in fills if r["mode"] == mode), key=lambda r: (r["ts"], r.get("id", 0))):
        f = Fill(**{k: row[k] for k in _FILL_FIELDS})
        if f.side == "buy":
            spent[f.mint] = spent.get(f.mint, 0.0) + f.sol_amount
        elif f.mint in pf.holdings:
            received[f.mint] = received.get(f.mint, 0.0) + f.sol_amount
        n_closed = len(pf.closed)
        pnl = pf.apply(f)
        if in_window(f.ts):
            rep.n_fills += 1
            rep.fees_sol += f.fee_sol
            if f.side == "buy":
                rep.n_buys += 1
                rep.deployed_sol += f.sol_amount
            else:
                rep.realized_sol += pnl
                day = days.setdefault(utc_day(f.ts), [0.0, 0])
                day[0] += pnl
                day[1] += 1
        if len(pf.closed) > n_closed:  # this fill closed the position
            c = pf.closed[-1]
            trade = ClosedTrade(mint=c["mint"], entry_ts=c["entry_ts"], exit_ts=c["exit_ts"],
                                cost_sol=spent.pop(f.mint, 0.0), proceeds_sol=received.pop(f.mint, 0.0),
                                realized_sol=c["realized_sol"], exit_reason=c["exit_reason"])
            if in_window(trade.exit_ts):
                rep.closed.append(trade)

    rep.by_day = sorted((d, v[0], v[1]) for d, v in days.items())
    for h in sorted(pf.holdings.values(), key=lambda h: h.entry_ts):
        price, ts = marks.get(h.mint, (None, None))
        rep.open.append(OpenPosition(mint=h.mint, tokens=h.tokens, cost_sol=h.cost_sol, entry_ts=h.entry_ts,
                                     realized_so_far_sol=h.realized_sol, mark_price=price, mark_ts=ts))
    return rep


def _when(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d %H:%M")


def _dur(s: float) -> str:
    if s >= 86400:
        return f"{s / 86400:.1f}d"
    if s >= 3600:
        return f"{s / 3600:.1f}h"
    return f"{s / 60:.0f}m"


def _sol(x: float | None) -> str:
    return "-" if x is None else f"{x:+.4f}"


def format_pnl(r: PnlReport, db_path: str, recent: int = 10) -> str:
    window = "all time" if r.since is None else f"since {_when(r.since)} UTC"
    out = [f"=== {r.mode.upper()} trading P&L - {window} - {db_path} ==="]
    if not r.n_fills and not r.open:
        out.append("  No fills in this period.")
        return "\n".join(out)

    wins, losses = r.wins, r.losses
    rows: list[tuple[str, str]] = [
        ("Realized P&L", f"{_sol(r.realized_sol)} SOL   (positive = better than just holding SOL)"),
    ]
    if r.closed:
        rows.append(("Closed trades", f"{len(r.closed)}   won {len(wins)}, lost {len(losses)}, "
                                      f"win rate {len(wins) / len(r.closed):.0%}"))
        rows.append(("Avg win / avg loss", f"{_sol(statistics.mean(c.realized_sol for c in wins) if wins else None)}"
                                           f" / {_sol(statistics.mean(c.realized_sol for c in losses) if losses else None)} SOL"))
        if len(r.closed) > 1:
            best = max(r.closed, key=lambda c: c.realized_sol)
            worst = min(r.closed, key=lambda c: c.realized_sol)
            rows.append(("Best / worst", f"{_sol(best.realized_sol)} ({short(best.mint)}) / "
                                         f"{_sol(worst.realized_sol)} ({short(worst.mint)})"))
        gross_loss = -sum(c.realized_sol for c in losses)
        if gross_loss > 0:
            rows.append(("Profit factor", f"{sum(c.realized_sol for c in wins) / gross_loss:.2f}"
                                          "   (gross wins / gross losses; above 1 is profitable)"))
        cost = sum(c.cost_sol for c in r.closed)
        if cost > 0:
            rows.append(("Return on closed", f"{100 * sum(c.realized_sol for c in r.closed) / cost:+.1f}% "
                                             f"of the {cost:.4f} SOL put into them"))
        rows.append(("Median hold", _dur(statistics.median(c.hold_s for c in r.closed))))
    else:
        rows.append(("Closed trades", "0"))
    rows.append(("Buys", f"{r.n_buys}, {r.deployed_sol:.4f} SOL spent"))
    rows.append(("Fees paid", f"{r.fees_sol:.4f} SOL over {r.n_fills} fills"))
    rows.append(("Open positions", str(len(r.open))))
    if r.open:
        unmarked = sum(1 for p in r.open if p.mark_price is None)
        note = f"   ({unmarked} without a price, not included)" if unmarked else ""
        rows.append(("Unrealized (est.)", f"{_sol(r.unrealized_sol)} SOL at last seen prices{note}"))
    width = max(len(k) for k, _ in rows)
    out += [f"  {k:<{width}}  {v}" for k, v in rows]

    if r.by_day:
        out.append("")
        out.append("  By day (UTC)   realized SOL   sells")
        for day, pnl, n in r.by_day:
            out.append(f"  {day}     {pnl:+11.4f}   {n:5d}")

    if r.open:
        out.append("")
        out.append("  Open positions")
        for p in r.open:
            mark = (f"worth ~{p.value_sol:.4f} SOL ({_sol(p.unrealized_sol)}), price from "
                    f"{_dur(r.now - p.mark_ts)} ago" if p.mark_price is not None and p.mark_ts is not None
                    else "no price seen yet")
            out.append(f"  {short(p.mint)}  since {_when(p.entry_ts)}  cost {p.cost_sol:.4f} SOL  {mark}")

    if r.closed:
        out.append("")
        shown = r.closed[-recent:][::-1]
        more = len(r.closed) - len(shown)
        out.append(f"  Recent closed trades (newest first{f', {more} older not shown' if more else ''})")
        for c in shown:
            pct = f" ({c.return_pct:+.1f}%)" if c.return_pct is not None else ""
            out.append(f"  {short(c.mint)}  {_when(c.entry_ts)} -> {_when(c.exit_ts)}  {_dur(c.hold_s):>5}  "
                       f"{_sol(c.realized_sol)} SOL{pct}  exit: {c.exit_reason}")
    return "\n".join(out)
