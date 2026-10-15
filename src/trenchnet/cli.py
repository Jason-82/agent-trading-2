"""Command-line interface.

    trenchnet demo                     # offline walkthrough of the article's scenario
    trenchnet import trades.jsonl      # load historical trades
    trenchnet backfill wallets.txt     # pull wallet history from a Solana RPC
    trenchnet profile                  # stats + profiles + roster selection
    trenchnet roster                   # show the roster
    trenchnet replay events.jsonl      # backtest the pipeline on recorded events
    trenchnet run                      # live data, paper trading
    trenchnet run --live               # live data, real orders (see README first)
    trenchnet reports [--mint MINT]    # situation reports
    trenchnet pnl [--since 7d]         # profit and loss from the bot's trades

Every command takes --db FILE to use a database other than trenchnet.db.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import shlex
import signal
import sqlite3
import sys
import time
from pathlib import Path
from typing import Callable

from .brokers.base import Fill
from .brokers.paper import PaperBroker
from .cases import iso
from .config import Config, load_config
from .engine import Engine, tracked_from_roster
from .judge import ClaudeJudge, HeuristicJudge
from .llm import LLM
from .models import short
from .profiler import build_roster
from .reporter import Reporter, format_report
from .store import Store

LIVE_ACK_ENV = "TRENCHNET_LIVE_ACK"
LIVE_ACK_VALUE = "I understand this trades real funds"

log = logging.getLogger("trenchnet")


def _cmd(sub: str, cfg: Config) -> str:
    """A command line the user can copy, keeping a non-default --db."""
    extra = "" if cfg.db_path == Config().db_path else f" --db {shlex.quote(cfg.db_path)}"
    return f"trenchnet {sub}{extra}"


def _llm(cfg: Config, enabled: bool) -> LLM | None:
    if not enabled:
        cfg.llm.use_llm_profiles = cfg.llm.use_llm_judge = cfg.llm.use_llm_reports = False
        return None
    if not (os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN")
            or os.environ.get("ANTHROPIC_PROFILE") or Path.home().joinpath(".config/anthropic").exists()):
        print("note: no Anthropic credentials found; using heuristic profiles, judge and template reports.\n"
              "      Set ANTHROPIC_API_KEY (or run `ant auth login`) to enable Claude.", file=sys.stderr)
        return _llm(cfg, False)
    return LLM()


def _judge(cfg: Config, llm: LLM | None):
    return ClaudeJudge(cfg, llm) if (llm is not None and cfg.llm.use_llm_judge) else HeuristicJudge(cfg)


def _read_wallets(path: str) -> tuple[list[str], dict[str, str]]:
    wallets, aliases = [], {}
    for line in Path(path).read_text().splitlines():
        line = line.split("#", 1)[0].strip()
        if not line:
            continue
        parts = [p.strip() for p in line.split(",")]
        wallets.append(parts[0])
        if len(parts) > 1 and parts[1]:
            aliases[parts[0]] = parts[1]
    return wallets, aliases


def _print_roster(rows: list[dict]) -> None:
    for r in rows:
        mark = "✓" if r["included"] else "·"
        prof = r.get("profile") or {}
        print(f"{mark} {r['alias'] or '':>2} {short(r['wallet'])}  score={r['score']:<8} "
              f"style={prof.get('style', '-'):<20} {'; '.join(r['reasons'])}")
        if r["included"] and prof:
            print(f"      {prof.get('summary', '')}")
            for trig in prof.get("attention_triggers", [])[:3]:
                print(f"      watch: {trig}")


def _print_summary(engine: Engine) -> None:
    s = engine.summary()
    print("\n=== portfolio ===")
    for k, v in s.items():
        print(f"  {k}: {v}")
    for c in engine.portfolio.closed:
        print(f"  closed {short(c['mint'])}: {c['realized_sol']:+.4f} SOL  "
              f"({iso(c['entry_ts'])} → {iso(c['exit_ts'])}; exit: {c['exit_reason']})")
    for h in engine.portfolio.holdings.values():
        print(f"  open {short(h.mint)}: {h.tokens:.0f} tokens, cost {h.cost_sol:.4f} SOL")


# -- commands ---------------------------------------------------------------

async def cmd_demo(args, cfg: Config) -> None:
    from .sources.synthetic import article_scenario

    sc = article_scenario(seed=args.seed)
    store = Store(getattr(args, "db_path", None) or ":memory:")
    store.clear_fills("backtest")
    store.add_trades(sc.history)
    llm = _llm(cfg, args.llm)
    print(f"Loaded {len(sc.history)} historical trades for {len(sc.aliases)} candidate wallets.\n")
    print("=== stage 1: roster ===")
    rows = await build_roster(store, cfg, llm, aliases=sc.aliases, now=sc.start_ts)
    _print_roster(rows)
    tracked = tracked_from_roster(store.roster())
    print(f"\nTOKEN={short(sc.token)}  DECOY={short(sc.decoy)}  LONE={short(sc.lone)}\n")
    print("=== stages 2-4 + strategy: live replay ===")
    engine = Engine(cfg, store, tracked, _judge(cfg, llm), Reporter(cfg, llm),
                    PaperBroker(cfg.paper, mode="backtest"), history=sc.history)
    for ev in sc.live:
        await engine.handle(ev)
    await engine.tick(engine.now + 7 * 3600)
    _print_summary(engine)


async def cmd_import(args, cfg: Config) -> None:
    from .sources.replay import read_events, trades_only

    store = Store(cfg.db_path)
    n = store.add_trades(trades_only(read_events(args.file)))
    print(f"imported {n} new trades into {cfg.db_path}")


async def cmd_backfill(args, cfg: Config) -> None:
    from .solana_rpc import SolanaRpc
    from .sources.rpc_backfill import backfill_wallet

    wallets, _ = _read_wallets(args.wallets)
    rpc = SolanaRpc(args.rpc or os.environ.get("SOLANA_RPC_URL") or cfg.live.rpc_url)
    store = Store(cfg.db_path)
    try:
        for w in wallets:
            trades = await backfill_wallet(rpc, w, max_signatures=args.limit)
            n = store.add_trades(trades)
            print(f"{short(w)}: {len(trades)} trades parsed, {n} new")
    finally:
        await rpc.aclose()


async def cmd_profile(args, cfg: Config) -> None:
    store = Store(cfg.db_path)
    wallets, aliases = _read_wallets(args.wallets) if args.wallets else (None, {})
    rows = await build_roster(store, cfg, _llm(cfg, not args.no_llm), wallets=wallets, aliases=aliases,
                              profile_all=args.all)
    _print_roster(rows)


async def cmd_roster(args, cfg: Config) -> None:
    rows = _existing_store(cfg).roster(included_only=not args.all)
    if args.json:
        print(json.dumps(rows, indent=2))
    else:
        _print_roster(rows)


def _existing_store(cfg: Config) -> Store:
    """Open the database for a read-only command without creating an empty one
    in the wrong folder."""
    path = Path(cfg.db_path)
    if cfg.db_path != ":memory:":
        if not path.exists():
            sys.exit(f"no database at {path.resolve()}\n"
                     "Run this from the folder where you ran `trenchnet run` / `trenchnet profile`, "
                     "or point at the file with --db PATH.")
        if path.is_dir():
            sys.exit(f"{path.resolve()} is a folder; pass the database file inside it, "
                     f"e.g. --db {shlex.quote(str(path / 'trenchnet.db'))}")
    try:
        return Store(cfg.db_path)
    except sqlite3.DatabaseError as e:
        sys.exit(f"cannot open {path.resolve()} as a trenchnet database: {e}")


def _load_tracked(store: Store) -> dict:
    tracked = tracked_from_roster(store.roster())
    if not tracked:
        sys.exit("roster is empty: import or backfill trades, then run `trenchnet profile`")
    return tracked


async def cmd_replay(args, cfg: Config) -> None:
    from .sources.replay import read_events

    store = Store(cfg.db_path)
    tracked = _load_tracked(store)
    events = read_events(args.file)
    start = events[0].ts if events else time.time()
    history = store.trades(wallets=list(tracked), until=start - 1e-6)
    llm = _llm(cfg, not args.no_llm)
    # Backtest fills are labelled separately and replaced on each replay, so they
    # never mix with paper/live results or pile up across re-runs.
    cleared = store.clear_fills("backtest")
    if cleared:
        print(f"(replacing the previous backtest's {cleared} fills)")
    engine = Engine(cfg, store, tracked, _judge(cfg, llm), Reporter(cfg, llm),
                    PaperBroker(cfg.paper, mode="backtest"), history=history)
    for ev in events:
        await engine.handle(ev)
    _print_summary(engine)
    print(f"Backtest results again later: {_cmd('pnl --mode backtest', cfg)}")


async def cmd_run(args, cfg: Config) -> None:
    from .sources.pumpportal_ws import PumpPortalSource

    store = Store(cfg.db_path)
    tracked = _load_tracked(store)
    llm = _llm(cfg, not args.no_llm)
    api_key = os.environ.get(cfg.pumpportal.api_key_env)
    if not api_key:
        print(f"warning: {cfg.pumpportal.api_key_env} not set; PumpPortal requires an API key for "
              "account and token trade subscriptions", file=sys.stderr)
    source = PumpPortalSource(cfg.pumpportal, tracked.keys(), api_key)

    rpc = None
    owner = None
    if args.live:
        if not cfg.live.enabled:
            sys.exit("live trading is disabled: set [live] enabled = true in trenchnet.toml")
        if os.environ.get(LIVE_ACK_ENV) != LIVE_ACK_VALUE:
            sys.exit(f'live trading requires {LIVE_ACK_ENV}="{LIVE_ACK_VALUE}"')
        key = os.environ.get(cfg.live.private_key_env)
        if not key:
            sys.exit(f"live trading requires the base58 private key in {cfg.live.private_key_env}")
        from solders.keypair import Keypair

        from .brokers.pumpportal import PumpPortalBroker
        from .solana_rpc import SolanaRpc

        rpc = SolanaRpc(os.environ.get("SOLANA_RPC_URL") or cfg.live.rpc_url)
        kp = Keypair.from_base58_string(key)
        owner = str(kp.pubkey())
        broker = PumpPortalBroker(cfg, kp, rpc)
        balance = await rpc.get_balance_sol(owner)
        cfg.paper.starting_balance_sol = balance
        print(f"LIVE trading from {short(str(kp.pubkey()))} (balance {balance:.4f} SOL, "
              f"max {cfg.live.max_position_sol} SOL per buy)")
    else:
        broker = PaperBroker(cfg.paper)
        print("paper trading (no real orders)")

    history = store.trades(wallets=list(tracked))
    engine = Engine(cfg, store, tracked, _judge(cfg, llm), Reporter(cfg, llm), broker, history=history,
                    background_reports=cfg.llm.background_reports,
                    on_hold=lambda m: source.watch_tokens([m]), on_release=lambda m: source.unwatch_tokens([m]))
    print(f"following {len(tracked)} wallets: {', '.join(t.alias for t in tracked.values())}")

    # Pick up where the last run left off: holdings, traded tokens, today's PnL.
    engine.restore(store.fills(mode=broker.mode))
    if rpc is not None and owner is not None:
        engine.portfolio.cash_sol = cfg.paper.starting_balance_sol  # on-chain balance, not replayed fills
        for mint, h in list(engine.portfolio.holdings.items()):
            onchain = await rpc.get_token_balance(owner, mint)
            if abs(onchain - h.tokens) <= 1e-6 * max(onchain, h.tokens):
                continue
            if onchain <= 0:
                print(f"warning: stored holding {short(mint)} has no on-chain balance; closing it in the records")
            else:
                print(f"note: {short(mint)} on-chain balance {onchain:.0f} differs from records {h.tokens:.0f}; "
                      "using on-chain")
            # Saved as a fill so restarts and `trenchnet pnl` see the same holdings as this run.
            adj = Fill(mint, "reconcile", 0.0, max(onchain, 0.0), 0.0, 0.0, time.time(), broker.mode,
                       f"reconcile: wallet {onchain:.0f} vs records {h.tokens:.0f}")
            engine.portfolio.apply(adj)
            store.add_fill(**adj.to_dict())
    for mint in engine.portfolio.holdings:
        await source.watch_tokens([mint])
    if engine.portfolio.holdings:
        print(f"resumed {len(engine.portfolio.holdings)} open position(s)")

    restore_signals = _install_stop_handlers(asyncio.get_running_loop(), source.stop)

    async def ticker() -> None:
        while True:
            await asyncio.sleep(30)
            try:
                await engine.tick(time.time())
            except Exception:  # noqa: BLE001 - one bad tick must not end time-based exits for the session
                log.exception("tick failed; retrying in 30s")

    tick_task = asyncio.create_task(ticker())
    try:
        async for ev in source.events():
            await engine.handle(ev)
    finally:
        tick_task.cancel()
        await engine.drain()
        engine.save_marks()
        if rpc is not None:
            await rpc.aclose()
        _print_summary(engine)
        print(f"Full history: {_cmd('pnl', cfg)}")
        restore_signals()


def _install_stop_handlers(loop: asyncio.AbstractEventLoop, stop: Callable[[], None]) -> Callable[[], None]:
    """First Ctrl-C: stop after the current event (an order being confirmed
    finishes and is recorded). Second Ctrl-C: quit immediately.

    POSIX uses loop.add_signal_handler. Windows' event loop doesn't support
    it, so a plain signal handler hands the stop to the loop instead of
    letting the default handler cancel whatever is running, which could be a
    live order awaiting confirmation. Returns a function that restores the
    previous handlers."""
    presses = 0
    previous: dict[int, object] = {}
    posix: list[int] = []

    def on_signal(signum: int | None = None, frame: object = None) -> None:
        nonlocal presses
        presses += 1
        if presses == 1:
            print("stopping after the current event... (press Ctrl-C again to quit immediately)",
                  file=sys.stderr, flush=True)
            loop.call_soon_threadsafe(stop)
            return
        prev = previous.get(signum) if signum is not None else None
        if callable(prev):
            prev(signum, frame)  # asyncio's own handler: cancels the run, ending in KeyboardInterrupt
        else:
            raise KeyboardInterrupt

    sigs = [signal.SIGINT, signal.SIGTERM] + ([signal.SIGBREAK] if hasattr(signal, "SIGBREAK") else [])
    for sig in sigs:
        try:
            loop.add_signal_handler(sig, on_signal)
            posix.append(sig)
        except (NotImplementedError, RuntimeError):
            try:
                previous[sig] = signal.signal(sig, on_signal)
            except (OSError, ValueError):
                pass

    def restore() -> None:
        for sig in posix:
            loop.remove_signal_handler(sig)
        for sig, old in previous.items():
            signal.signal(sig, old)

    return restore


def _age(seconds: float) -> str:
    for unit, n in (("d", 86400), ("h", 3600), ("min", 60)):
        if seconds >= n:
            return f"{seconds / n:.0f}{unit}"
    return f"{seconds:.0f}s"


async def cmd_reports(args, cfg: Config) -> None:
    store = _existing_store(cfg)
    now = time.time()
    for r in reversed(store.reports(args.mint, limit=args.limit)):
        # A report keeps its own timestamp; an old reading is never shown as a fresh development.
        print(f"#{r['id']} {short(r['mint'])} (as of {iso(r['ts'])}, {_age(now - r['ts'])} ago)")
        print(format_report(r["report"], r["ts"], r["source"]))
        print()


async def cmd_pnl(args, cfg: Config) -> None:
    from .pnl import build_pnl, format_pnl, parse_since

    store = _existing_store(cfg)
    now = time.time()
    try:
        since = parse_since(args.since, now) if args.since else None
    except ValueError as e:
        sys.exit(str(e))
    fills = store.fills()
    modes = [args.mode] if args.mode else [m for m in ("paper", "live") if any(f["mode"] == m for f in fills)]
    if not modes:
        hint = (f"\n(There are backtest results: {_cmd('pnl --mode backtest', cfg)})"
                if any(f["mode"] == "backtest" for f in fills) else "")
        sys.exit(f"no paper or live trades recorded in {cfg.db_path} yet. "
                 f"Paper trade first: {_cmd('run', cfg)}{hint}")
    reports = []
    for mode in modes:
        held = {f["mint"] for f in fills if f["mode"] == mode}
        marks = {m: p for m in held if (p := store.last_price(m)) is not None}
        reports.append(build_pnl(fills, marks, mode=mode, now=now, since=since))
    if args.json:
        print(json.dumps([r.to_dict() for r in reports], indent=2))
        return
    print("\n\n".join(format_pnl(r, cfg.db_path, recent=args.recent) for r in reports))


def _nonneg_int(text: str) -> int:
    v = int(text)
    if v < 0:
        raise argparse.ArgumentTypeError("must be 0 or more")
    return v


def _utf8_output() -> None:
    """Windows writes redirected output as cp1252, which can't encode the
    symbols in reports; use UTF-8 and never crash on an odd character.
    Line buffering keeps `trenchnet run` output live when piped (tee, older
    Git Bash windows)."""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)
        except (AttributeError, ValueError, OSError):
            pass


def main(argv: list[str] | None = None) -> None:
    _utf8_output()
    # --db works before or after the command name (`trenchnet --db x.db pnl` or `trenchnet pnl --db x.db`).
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--db", dest="db_path", default=argparse.SUPPRESS, metavar="FILE",
                        help="SQLite database file (default: db_path in trenchnet.toml, i.e. trenchnet.db; "
                             "the demo uses memory unless this is given)")
    p = argparse.ArgumentParser(prog="trenchnet", description=__doc__, parents=[common],
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--config", help="path to trenchnet.toml")
    p.add_argument("-v", "--verbose", action="store_true")
    sub = p.add_subparsers(dest="cmd", required=True)

    def command(name: str, help: str) -> argparse.ArgumentParser:
        return sub.add_parser(name, help=help, parents=[common])

    s = command("demo", help="offline walkthrough of the article's scenario")
    s.add_argument("--llm", action="store_true", help="use Claude for profiles, judge and reports")
    s.add_argument("--seed", type=int, default=7)
    s.set_defaults(fn=cmd_demo)

    s = command("import", help="import trades from JSONL")
    s.add_argument("file")
    s.set_defaults(fn=cmd_import)

    s = command("backfill", help="backfill wallet history from a Solana RPC")
    s.add_argument("wallets", help="file with one address[,alias] per line")
    s.add_argument("--limit", type=int, default=500, help="max signatures per wallet")
    s.add_argument("--rpc", help="RPC URL (default: $SOLANA_RPC_URL or [live].rpc_url)")
    s.set_defaults(fn=cmd_backfill)

    s = command("profile", help="compute stats, profile wallets and select the roster")
    s.add_argument("--wallets", help="limit to wallets in this file (address[,alias] per line)")
    s.add_argument("--all", action="store_true", help="also profile wallets that fail selection")
    s.add_argument("--no-llm", action="store_true")
    s.set_defaults(fn=cmd_profile)

    s = command("roster", help="show the roster")
    s.add_argument("--all", action="store_true", help="include excluded candidates")
    s.add_argument("--json", action="store_true")
    s.set_defaults(fn=cmd_roster)

    s = command("replay", help="backtest on a JSONL event file")
    s.add_argument("file")
    s.add_argument("--no-llm", action="store_true")
    s.set_defaults(fn=cmd_replay)

    s = command("run", help="follow the roster live via PumpPortal")
    s.add_argument("--live", action="store_true", help="place real orders (requires config + env opt-in)")
    s.add_argument("--no-llm", action="store_true")
    s.set_defaults(fn=cmd_run)

    s = command("reports", help="show situation reports")
    s.add_argument("--mint")
    s.add_argument("--limit", type=int, default=20)
    s.set_defaults(fn=cmd_reports)

    s = command("pnl", help="profit and loss from the bot's paper/live trades")
    s.add_argument("--since", help="only this period: 24h, 7d, 2w, or a UTC date like 2026-10-01")
    s.add_argument("--mode", choices=("paper", "live", "backtest"),
                   help="default: paper and live (whichever have trades); backtest = the last replay/demo")
    s.add_argument("--recent", type=_nonneg_int, default=10, help="how many closed trades to list (0 = none)")
    s.add_argument("--json", action="store_true")
    s.set_defaults(fn=cmd_pnl)

    args = p.parse_args(argv)
    logging.basicConfig(level=logging.INFO if args.verbose else logging.WARNING,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    cfg = load_config(args.config)
    if getattr(args, "db_path", None):
        cfg.db_path = args.db_path
    try:
        asyncio.run(args.fn(args, cfg))
    except KeyboardInterrupt:
        # A forced quit (second Ctrl-C), or Ctrl-C outside `run`'s main loop.
        print("stopped", file=sys.stderr)
        sys.exit(130)


if __name__ == "__main__":
    main()
