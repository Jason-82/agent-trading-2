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
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import signal
import sys
import time
from pathlib import Path

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
    store = Store(args.db or ":memory:")
    store.add_trades(sc.history)
    llm = _llm(cfg, args.llm)
    print(f"Loaded {len(sc.history)} historical trades for {len(sc.aliases)} candidate wallets.\n")
    print("=== stage 1: roster ===")
    rows = await build_roster(store, cfg, llm, aliases=sc.aliases, now=sc.start_ts)
    _print_roster(rows)
    tracked = tracked_from_roster(store.roster())
    print(f"\nTOKEN={short(sc.token)}  DECOY={short(sc.decoy)}  LONE={short(sc.lone)}\n")
    print("=== stages 2-4 + strategy: live replay ===")
    engine = Engine(cfg, store, tracked, _judge(cfg, llm), Reporter(cfg, llm), PaperBroker(cfg.paper),
                    history=sc.history)
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
    rows = Store(cfg.db_path).roster(included_only=not args.all)
    if args.json:
        print(json.dumps(rows, indent=2))
    else:
        _print_roster(rows)


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
    engine = Engine(cfg, store, tracked, _judge(cfg, llm), Reporter(cfg, llm), PaperBroker(cfg.paper),
                    history=history)
    for ev in events:
        await engine.handle(ev)
    _print_summary(engine)


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
            if onchain <= 0:
                print(f"warning: stored holding {short(mint)} has no on-chain balance; dropping it")
                del engine.portfolio.holdings[mint]
            elif abs(onchain - h.tokens) > 1e-6 * max(onchain, h.tokens):
                print(f"note: {short(mint)} on-chain balance {onchain:.0f} differs from records {h.tokens:.0f}; "
                      "using on-chain")
                h.cost_sol *= onchain / h.tokens
                h.tokens = onchain
    for mint in engine.portfolio.holdings:
        await source.watch_tokens([mint])
    if engine.portfolio.holdings:
        print(f"resumed {len(engine.portfolio.holdings)} open position(s)")

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, source.stop)
        except NotImplementedError:
            pass

    async def ticker() -> None:
        while True:
            await asyncio.sleep(30)
            await engine.tick(time.time())

    tick_task = asyncio.create_task(ticker())
    try:
        async for ev in source.events():
            await engine.handle(ev)
    finally:
        tick_task.cancel()
        await engine.drain()
        if rpc is not None:
            await rpc.aclose()
        _print_summary(engine)


def _age(seconds: float) -> str:
    for unit, n in (("d", 86400), ("h", 3600), ("min", 60)):
        if seconds >= n:
            return f"{seconds / n:.0f}{unit}"
    return f"{seconds:.0f}s"


async def cmd_reports(args, cfg: Config) -> None:
    store = Store(cfg.db_path)
    now = time.time()
    for r in reversed(store.reports(args.mint, limit=args.limit)):
        # A report keeps its own timestamp; an old reading is never shown as a fresh development.
        print(f"#{r['id']} {short(r['mint'])} (as of {iso(r['ts'])}, {_age(now - r['ts'])} ago)")
        print(format_report(r["report"], r["ts"], r["source"]))
        print()


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(prog="trenchnet", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--config", help="path to trenchnet.toml")
    p.add_argument("-v", "--verbose", action="store_true")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("demo", help="offline walkthrough of the article's scenario")
    s.add_argument("--llm", action="store_true", help="use Claude for profiles, judge and reports")
    s.add_argument("--seed", type=int, default=7)
    s.add_argument("--db", help="write demo state to this SQLite file instead of memory")
    s.set_defaults(fn=cmd_demo)

    s = sub.add_parser("import", help="import trades from JSONL")
    s.add_argument("file")
    s.set_defaults(fn=cmd_import)

    s = sub.add_parser("backfill", help="backfill wallet history from a Solana RPC")
    s.add_argument("wallets", help="file with one address[,alias] per line")
    s.add_argument("--limit", type=int, default=500, help="max signatures per wallet")
    s.add_argument("--rpc", help="RPC URL (default: $SOLANA_RPC_URL or [live].rpc_url)")
    s.set_defaults(fn=cmd_backfill)

    s = sub.add_parser("profile", help="compute stats, profile wallets and select the roster")
    s.add_argument("--wallets", help="limit to wallets in this file (address[,alias] per line)")
    s.add_argument("--all", action="store_true", help="also profile wallets that fail selection")
    s.add_argument("--no-llm", action="store_true")
    s.set_defaults(fn=cmd_profile)

    s = sub.add_parser("roster", help="show the roster")
    s.add_argument("--all", action="store_true", help="include excluded candidates")
    s.add_argument("--json", action="store_true")
    s.set_defaults(fn=cmd_roster)

    s = sub.add_parser("replay", help="backtest on a JSONL event file")
    s.add_argument("file")
    s.add_argument("--no-llm", action="store_true")
    s.set_defaults(fn=cmd_replay)

    s = sub.add_parser("run", help="follow the roster live via PumpPortal")
    s.add_argument("--live", action="store_true", help="place real orders (requires config + env opt-in)")
    s.add_argument("--no-llm", action="store_true")
    s.set_defaults(fn=cmd_run)

    s = sub.add_parser("reports", help="show situation reports")
    s.add_argument("--mint")
    s.add_argument("--limit", type=int, default=20)
    s.set_defaults(fn=cmd_reports)

    args = p.parse_args(argv)
    logging.basicConfig(level=logging.INFO if args.verbose else logging.WARNING,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    cfg = load_config(args.config)
    asyncio.run(args.fn(args, cfg))


if __name__ == "__main__":
    main()
