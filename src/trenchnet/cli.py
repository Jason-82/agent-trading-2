"""Command-line interface.

    trenchnet demo                     # offline walkthrough of the article's scenario
    trenchnet import trades.jsonl      # load historical trades
    trenchnet backfill wallets.txt     # pull wallet history from a Solana RPC
    trenchnet inspect <wallet>         # how a wallet's recent transactions are read
    trenchnet fomo                     # find wallets trading through the Fomo app
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
import math
import os
import re
import shlex
import signal
import sqlite3
import sys
import time
from collections import Counter
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


def _read_text_any(path: str) -> str:
    """Text saved by Notepad, Excel or PowerShell: UTF-8 (with or without a
    BOM), UTF-16, or the Windows ANSI code page."""
    try:
        raw = Path(path).read_bytes()
    except OSError as e:
        sys.exit(f"can't read {path}: {e.strerror}")
    if raw.startswith((b"\xff\xfe", b"\xfe\xff")):
        return raw.decode("utf-16")
    try:
        return raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        return raw.decode("cp1252", errors="replace")


def _read_wallets(path: str) -> tuple[list[str], dict[str, str]]:
    """`address[,alias]` per line (a space or tab also separates the alias).
    Lines that aren't Solana addresses are reported with their line number
    and skipped, so a typo can't derail a long backfill."""
    from solders.pubkey import Pubkey

    wallets, aliases, bad = [], {}, []
    for n, line in enumerate(_read_text_any(path).splitlines(), 1):
        line = line.split("#", 1)[0].strip().lstrip("\ufeff").strip()
        if not line:
            continue
        parts = re.split(r"[,\s]+", line, maxsplit=1)
        addr = parts[0]
        try:
            Pubkey.from_string(addr)
        except ValueError:
            bad.append(f"  line {n}: {addr!r}")
            continue
        if addr not in aliases and addr not in wallets:
            wallets.append(addr)
        if len(parts) > 1 and parts[1].strip(" ,"):
            aliases[addr] = parts[1].strip(" ,")
    if bad:
        print(f"Skipping {len(bad)} line(s) in {path} that aren't Solana wallet addresses:\n" + "\n".join(bad),
              file=sys.stderr)
    if not wallets:
        sys.exit(f"{path} has no valid wallet addresses in it")
    return wallets, aliases


class _Progress:
    """A single updating status line (\\r). Ended with a newline before any
    log message or other output so the two never run together."""

    def __init__(self) -> None:
        self.open = False

    def show(self, text: str) -> None:
        sys.stdout.write("\r" + text + " " * 8)
        sys.stdout.flush()
        self.open = True

    def end(self) -> None:
        if self.open:
            sys.stdout.write("\n")
            sys.stdout.flush()
            self.open = False


PROGRESS = _Progress()


class _EndProgressLine(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        PROGRESS.end()
        return True


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


def _wallet_summary(res) -> str:
    from .models import Side
    from .sources.rpc_backfill import SKIP_REASONS

    sides = Counter(t.side for t in res.trades)
    parts = [f"{sides[Side.BUY]} buys", f"{sides[Side.SELL]} sells"]
    transfers = sides[Side.TRANSFER_IN] + sides[Side.TRANSFER_OUT]
    if transfers:
        parts.append(f"{transfers} transfers")
    in_stable = sum(1 for t in res.trades if t.quote_mint)
    text = f"{', '.join(parts)} from {res.transactions} transactions"
    if in_stable:
        text += f" ({in_stable} of the trades paid in USDC/stablecoins)"
    if res.ignored and (not res.trades or sum(res.ignored.values()) > len(res.trades)):
        why = "; ".join(f"{c} {SKIP_REASONS.get(r, r).split(' (')[0]}" for r, c in res.ignored.most_common(2))
        text += f" [skipped: {why}]"
    return text


_STOP_ADVICE = {
    "credentials": "The RPC rejected the URL or API key. Check SOLANA_RPC_URL (the full Helius URL including "
                   "?api-key=...) and run `source .env`.",
    "url": "Check SOLANA_RPC_URL: it should be the full https:// RPC URL from your Helius dashboard.",
    "quota": "Your RPC plan's credits are used up for now. Trades read so far are saved. Wait for the plan to reset "
             "(Helius: monthly), add credits, or use another RPC URL, then run the same command again.",
    "transient": "The RPC kept failing (rate limits, server or network problems). Trades read so far are saved; "
                 "wait a while and run the same command again (already-saved trades are skipped).",
}


def _rpc_from(url: str, rps: float | None = None):
    from .solana_rpc import SolanaRpc

    try:
        return SolanaRpc(url, requests_per_second=rps)
    except ValueError as e:
        sys.exit(f"SOLANA_RPC_URL problem: {e}. If you made .env in Notepad, re-save it and run `source .env`.")


async def cmd_backfill(args, cfg: Config) -> None:
    from .prices import SolUsdPrices
    from .solana_rpc import RpcError, RpcHTTPError
    from .sources.rpc_backfill import BackfillResult, backfill_wallet

    wallets, _ = _read_wallets(args.wallets)
    rpc = _rpc_from(args.rpc or os.environ.get("SOLANA_RPC_URL") or cfg.live.rpc_url, args.rps)
    print(f"Reading history from {rpc.host}, up to {rpc.requests_per_second:g} requests/second.")
    if rpc.is_public:
        print("  This is Solana's free public RPC: slow and heavily rate limited. To use your Helius key,\n"
              "  run `source .env` first (or set SOLANA_RPC_URL).", file=sys.stderr)
    store = Store(cfg.db_path)
    if args.new_only:
        have = store.wallet_trade_counts()
        before = len(wallets)
        wallets = [w for w in wallets if not have.get(w)]
        print(f"--new-only: {before - len(wallets)} wallet(s) already downloaded, {len(wallets)} to go.")
        if not wallets:
            await rpc.aclose()
            print(f"Nothing to do. Next: {_cmd('profile --wallets ' + shlex.quote(args.wallets), cfg)}")
            return
    prices = SolUsdPrices(store)
    total_new = total_fixed = unpriced = 0
    done_wallets = 0
    skipped: list[str] = []
    failures_in_a_row = 0
    stop: RpcHTTPError | None = None

    def save(w: str, res: BackfillResult) -> tuple[int, int]:
        # The transactions read this time replace what was saved for them before (an older
        # reader may have read them differently); older history and live trades are kept.
        nonlocal total_new, total_fixed, unpriced
        new, fixed = store.replace_trades(w, res.trades, res.read)
        total_new += new
        total_fixed += fixed
        unpriced += res.unpriced
        return new, fixed

    try:
        for i, w in enumerate(wallets, 1):
            label = f"[{i}/{len(wallets)}] {short(w)}"
            res = BackfillResult()
            try:
                await backfill_wallet(
                    rpc, w, max_signatures=args.limit, result=res, prices=prices,
                    known_traded=store.traded_mints(w),
                    progress=lambda d, t, n, label=label: PROGRESS.show(
                        f"{label}: {d}/{t} transactions read, {n} trades found"))
            except (RpcHTTPError, RpcError) as e:
                save(w, res)
                if isinstance(e, RpcHTTPError) and e.fatal:
                    stop = e
                    break
                PROGRESS.show(f"{label}: skipped, could not list its transactions ({e})")
                PROGRESS.end()
                skipped.append(w)
                failures_in_a_row += 1
                if failures_in_a_row >= 3 or done_wallets == 0 and failures_in_a_row == len(wallets):
                    stop = e if isinstance(e, RpcHTTPError) else RpcHTTPError(None, str(e))
                    break
                continue
            except asyncio.CancelledError:  # Ctrl-C: keep what this wallet has so far, then stop
                n, _ = save(w, res)
                PROGRESS.end()
                print(f"Stopped. Saved {total_new} new trades, including {n} from the wallet in progress. "
                      f"Run the same command again to continue.", file=sys.stderr)
                raise
            failures_in_a_row = 0
            done_wallets += 1
            n, fixed = save(w, res)
            line = f"{label}: {_wallet_summary(res)} ({n} new" + (f", {fixed} earlier entries corrected" if fixed else "") + ")"
            if res.unreadable:
                why = ", ".join(f"{c} {r}" for r, c in res.unreadable.most_common())
                line += f"; {sum(res.unreadable.values())} unreadable ({why})"
            PROGRESS.show(line)
            PROGRESS.end()
            if res.stopped is not None:
                stop = res.stopped
                break
    finally:
        PROGRESS.end()
        await rpc.aclose()
        await prices.aclose()
    print(f"{total_new} new trades saved to {cfg.db_path}."
          + (f" {total_fixed} earlier entries were read differently this time and were corrected." if total_fixed else ""))
    if unpriced:
        print(f"{unpriced} trades paid in USDC/stablecoins were left out: "
              f"{prices.problem or 'no SOL/USD price for their time'}. Run the same command again later to add them.")
    if skipped:
        print(f"{len(skipped)} wallet(s) were skipped because their transactions couldn't be listed; "
              "run the same command again later to retry them.")
    if stop is not None:
        sys.exit(f"Stopped early: {stop}\n{_STOP_ADVICE.get(stop.kind, _STOP_ADVICE['transient'])}")
    if rpc.rate_limited:
        print(f"(the RPC asked us to slow down {rpc.rate_limited} times; if that number is large, "
              f"try --rps {rpc.requests_per_second / 2:g})")
    print(f"Next: {_cmd('profile --wallets ' + shlex.quote(args.wallets), cfg)}")


def _resolve_wallet(text: str, cfg: Config) -> str:
    """A full address, a nickname (from the roster or wallets.txt), or the
    short form printed by other commands, e.g. 4vw5…9Ud9."""
    from solders.pubkey import Pubkey

    text = text.strip()
    try:
        Pubkey.from_string(text)
        return text
    except ValueError:
        pass
    known: dict[str, str] = {}  # wallet -> nickname
    if Path("wallets.txt").exists():
        try:
            ws, al = _read_wallets("wallets.txt")
            known.update({w: al.get(w, "") for w in ws})
        except SystemExit:
            pass
    if cfg.db_path == ":memory:" or Path(cfg.db_path).exists():
        for r in Store(cfg.db_path).roster(included_only=False):
            known[r["wallet"]] = r["alias"] or known.get(r["wallet"], "")
    by_alias = [w for w, a in known.items() if a and a.lower() == text.lower()]
    m = re.fullmatch(r"(\w+)(?:…|\.\.\.)(\w+)", text)
    by_short = [w for w in known if m and w.startswith(m.group(1)) and w.endswith(m.group(2))]
    found = by_alias or by_short
    if len(found) == 1:
        return found[0]
    if len(found) > 1:
        sys.exit(f"{text!r} matches several wallets; use the full address")
    sys.exit(f"{text!r} is not a wallet address, and no wallet in wallets.txt or the roster has that nickname")


_SHORT_SKIP = {
    "failed": "failed on-chain",
    "not_in_tx": "wallet not in tx",
    "no_token_change": "no token change",
    "several_tokens": "2+ tokens changed",
    "no_time": "no block time",
    "unreadable": "could not read",
    "quote_conversion": "only SOL/stablecoins",
    "other_quote": "USDC trade, no SOL price",
}


async def cmd_inspect(args, cfg: Config) -> None:
    from .prices import SolUsdPrices
    from .solana_rpc import RpcError, RpcHTTPError
    from .sources.rpc_backfill import QUOTE_MINTS, analyze_wallet_tx, fetch_transaction, list_wallet_signatures, \
        value_in_sol

    wallet = _resolve_wallet(args.wallet, cfg)
    rpc = _rpc_from(args.rpc or os.environ.get("SOLANA_RPC_URL") or cfg.live.rpc_url)
    store = Store(cfg.db_path) if cfg.db_path == ":memory:" or Path(cfg.db_path).exists() else None
    prices = SolUsdPrices(store)
    counts: Counter = Counter()
    examples: dict[str, list[str]] = {}
    try:
        try:
            sigs = await list_wallet_signatures(rpc, wallet, args.limit, retry_pause_s=2.0, successful_only=False)
        except (RpcHTTPError, RpcError) as e:
            sys.exit(f"Could not list the wallet's transactions: {e}")
        print(f"Wallet {wallet}: its last {len(sigs)} transactions (including its USDC account's), newest first "
              f"(read from {rpc.host})\n")
        print(f"{'time (UTC)':<17}  {'read as':<27} {'SOL change':>11}  token changes (wallet-owned)")
        for s in sigs:
            sig = s["signature"]
            when = iso(s.get("blockTime")) or "?"
            if s.get("err") is not None:
                kind, sol, tokens = "failed", "", ""
            else:
                try:
                    view = analyze_wallet_tx(await fetch_transaction(rpc, sig), wallet, sig)
                except (RpcHTTPError, RpcError) as e:
                    kind, sol, tokens = "unreadable", "", str(e)[:60]
                else:
                    kind = view.trade.side.value.upper().replace("_", " ") if view.trade is not None else view.status
                    sol = f"{view.sol_delta:+.4f}"
                    names = {m: QUOTE_MINTS.get(m) or short(m) for m in view.token_deltas}
                    tokens = ", ".join(f"{names[m]} {d:+,.6g}" for m, d in list(view.token_deltas.items())[:3])
                    if len(view.token_deltas) > 3:
                        tokens += f", +{len(view.token_deltas) - 3} more"
                    if view.pending is not None:
                        t = view.pending
                        price = await prices.price_at(t.ts)
                        kind = f"{t.side.value.upper()} in {QUOTE_MINTS[t.quote_mint or '']}"
                        if price:
                            kind += f" ={value_in_sol(view, price).sol_amount:.3f} SOL"
                        else:
                            kind = "other_quote"
            key = kind.split(" in ")[0]  # "BUY in USDC =0.712 SOL" counts as a BUY
            counts[key] += 1
            examples.setdefault(key, [])
            if len(examples[key]) < 2:
                examples[key].append(sig)
            label = kind if kind not in _SHORT_SKIP else "skipped: " + _SHORT_SKIP[kind]
            print(f"{when[:16]:<17}  {label:<27} {sol:>11}  {tokens}")
    finally:
        await rpc.aclose()
        await prices.aclose()

    print("\nSummary: " + ", ".join(f"{c} {_SHORT_SKIP.get(k, k.lower())}" for k, c in counts.most_common()))
    traded = sum(c for k, c in counts.items() if k in ("BUY", "SELL"))
    if counts and not traded:
        print("No buys or sells found. If this wallet does trade, open an example below on Solscan "
              "and compare it with what the reader saw.")
    elif counts.get("BUY") and not counts.get("SELL"):
        print("Buys but no sells in this window: the wallet may sell from a different wallet (look for "
              "TRANSFER OUT), or simply hold.")
    for kind, sigs_ in examples.items():
        if kind in ("BUY", "SELL"):
            continue
        for sig in sigs_:
            print(f"  {_SHORT_SKIP.get(kind, kind.lower())}: https://solscan.io/tx/{sig}")
    if store is not None:
        stored = Counter(t.side.value for t in store.trades(wallets=[wallet]))
        if stored:
            print("Saved in the database for this wallet: " + ", ".join(f"{c} {k}" for k, c in stored.most_common()))


async def cmd_fomo(args, cfg: Config) -> None:
    from .fomo import FOMO_FEE_ACCOUNT, discover, merge_wallet_file
    from .prices import SolUsdPrices
    from .solana_rpc import RpcError, RpcHTTPError

    accounts = args.account or [FOMO_FEE_ACCOUNT]
    rpc = _rpc_from(args.rpc or os.environ.get("SOLANA_RPC_URL") or cfg.live.rpc_url, args.rps)
    store = Store(cfg.db_path)
    prices = SolUsdPrices(store)
    print(f"Listing the last {args.pages * 1000:,} transactions of Fomo's fee account "
          f"{', '.join(short(a) for a in accounts)} (from {rpc.host}), then reading up to {args.sample} of them "
          "spread over that period.")
    try:
        found = await discover(rpc, accounts, pages=args.pages, sample=args.sample, prices=prices,
                               progress=lambda d, t, n: PROGRESS.show(f"{d}/{t} transactions read, {n} traders found"))
    except (RpcHTTPError, RpcError) as e:
        PROGRESS.end()
        advice = _STOP_ADVICE.get(e.kind, "") if isinstance(e, RpcHTTPError) else ""
        sys.exit(f"Could not list Fomo's transactions: {e}\n{advice}".rstrip())
    finally:
        PROGRESS.end()
        await rpc.aclose()
        await prices.aclose()

    span = ""
    if found.span:
        span = f", from {iso(found.span[0])[:16]} to {iso(found.span[1])[:16]} UTC"
    print(f"Read {found.read} of {found.listed:,} recent transactions{span}: {found.fomo_trades} trades "
          f"by {len(found.traders)} wallets ({found.no_trade} other transactions: deposits, withdrawals, ...).")
    if found.unreadable:
        print("  unreadable: " + ", ".join(f"{c} {r}" for r, c in found.unreadable.most_common()))
    if found.stopped is not None:
        print(f"Stopped early: {found.stopped}", file=sys.stderr)
    if not found.traders:
        sys.exit("No Fomo trades were recognised. Fomo may have changed its fee account: open a recent Fomo trade on "
                 "Solscan, find the USDC account the app fee went to, and pass it with --account ADDRESS.")
    if found.found_by.get("signer"):
        print(f"  ({found.found_by['signer']} traders were recognised as the transaction's other signer, "
              "not by the fee payment)")
    trades = sum(t.trades for t in found.traders.values())
    own = sum(t.own_address_in_tx for t in found.traders.values())
    if own < trades:
        print(f"  In {trades - own} of {trades} trades the trader's own address wasn't in the transaction (only their "
              "token accounts were); backfill reads those through the wallet's USDC account.")

    ranked = [t for t in found.ranked() if t.trades >= args.min_trades]
    print(f"\n{'#':>3}  {'wallet':<44}  {'trades':>6} {'buys':>5} {'sells':>5}  {'volume':>10}  tokens")
    for i, t in enumerate(ranked[:args.show], 1):
        print(f"{i:>3}  {t.wallet:<44}  {t.trades:>6} {t.buys:>5} {t.sells:>5}  {'$' + format(t.usd, ',.0f'):>10}  "
              f"{len(t.tokens)}")
    if len(ranked) > args.show:
        print(f"  ... and {len(ranked) - args.show} more")
    top = ranked[:args.top]
    if not top:
        sys.exit(f"No wallet made at least {args.min_trades} trades in the sample; try --min-trades 1 or a bigger --sample.")
    when = iso(found.span[1])[:10] if found.span else "today"
    added, already = merge_wallet_file(
        args.out, top, lambda t: f"Fomo sample {when}: {t.trades} trades, ${t.usd:,.0f}")
    print(f"\nAdded {added} wallets to {args.out}" + (f" ({already} were already there)" if already else "")
          + ". Volume in a short sample says who is active and trades size, not who trades well:")
    print(f"  {_cmd('backfill ' + shlex.quote(args.out) + ' --limit 500', cfg)}     # their history (~1 min per wallet)")
    print(f"  {_cmd('profile --wallets ' + shlex.quote(args.out), cfg)}        # keeps only the profitable ones")


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

        rpc = _rpc_from(os.environ.get("SOLANA_RPC_URL") or cfg.live.rpc_url)
        kp = Keypair.from_base58_string(key.strip())
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


def _positive_rate(text: str) -> float:
    try:
        v = float(text)
    except ValueError:
        raise argparse.ArgumentTypeError("must be a number, e.g. 5 or 0.5") from None
    if not math.isfinite(v) or v <= 0:
        raise argparse.ArgumentTypeError("must be a number greater than 0, e.g. 5 or 0.5")
    return v


def _positive_int(text: str) -> int:
    try:
        n = int(text)
    except ValueError:
        raise argparse.ArgumentTypeError(f"{text!r} is not a whole number") from None
    if n <= 0:
        raise argparse.ArgumentTypeError("must be at least 1")
    return n


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
    s.add_argument("--limit", type=_positive_int, default=500,
                   help="how many recent transactions to read per wallet (default 500)")
    s.add_argument("--rpc", help="RPC URL (default: $SOLANA_RPC_URL or [live].rpc_url)")
    s.add_argument("--rps", type=_positive_rate, default=None,
                   help="max requests per second (default 5, or 3 on Solana's public RPC)")
    group = s.add_mutually_exclusive_group()
    group.add_argument("--new-only", action="store_true", help="skip wallets that already have trades saved")
    # Every run now replaces what it re-reads; kept so older instructions still work.
    group.add_argument("--refresh", action="store_true", help=argparse.SUPPRESS)
    s.set_defaults(fn=cmd_backfill)

    s = command("inspect", help="show how a wallet's recent transactions are read (buy/sell/skipped and why)")
    s.add_argument("wallet", help="full address, nickname, or the short form like 4vw5…9Ud9")
    s.add_argument("--limit", type=int, default=30, help="how many recent transactions (default 30)")
    s.add_argument("--rpc", help="RPC URL (default: $SOLANA_RPC_URL)")
    s.set_defaults(fn=cmd_inspect)

    s = command("fomo", help="find wallets that trade through the Fomo app (from public blockchain data)")
    s.add_argument("--sample", type=_positive_int, default=300,
                   help="how many recent Fomo transactions to read (default 300; one RPC call each)")
    s.add_argument("--pages", type=_positive_int, default=3,
                   help="spread the sample over the last N x 1,000 Fomo transactions (default 3)")
    s.add_argument("--top", type=_positive_int, default=30, help="how many wallets to add to the file (default 30)")
    s.add_argument("--min-trades", type=_positive_int, default=2,
                   help="only wallets with at least this many trades in the sample (default 2)")
    s.add_argument("--show", type=_nonneg_int, default=20, help="how many to list on screen (default 20)")
    s.add_argument("--out", default="fomo.txt", help="wallets file to add them to (default fomo.txt)")
    s.add_argument("--account", action="append", metavar="ADDRESS",
                   help="Fomo account to sample instead of its USDC fee account (repeatable)")
    s.add_argument("--rpc", help="RPC URL (default: $SOLANA_RPC_URL)")
    s.add_argument("--rps", type=_positive_rate, default=None, help="max requests per second (default 5)")
    s.set_defaults(fn=cmd_fomo)

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
    for handler in logging.getLogger().handlers:
        handler.addFilter(_EndProgressLine())
    for noisy in ("httpx", "httpcore"):  # their INFO lines include request URLs, i.e. the RPC API key
        logging.getLogger(noisy).setLevel(logging.WARNING)
    cfg = load_config(args.config)
    if getattr(args, "db_path", None):
        cfg.db_path = args.db_path
    try:
        asyncio.run(args.fn(args, cfg))
    except KeyboardInterrupt:
        # A forced quit (second Ctrl-C), or Ctrl-C outside `run`'s main loop.
        PROGRESS.end()
        print("stopped", file=sys.stderr)
        sys.exit(130)


if __name__ == "__main__":
    main()
