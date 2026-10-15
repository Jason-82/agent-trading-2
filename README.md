# TRENCHNET

A pump.fun trading bot built on the "TRENCHNET" approach: follow a selected
group of traders, connect their trades in a wallet–token graph, make narrow
model judgments about each new event, write readable situation reports, and
(on top of the article's design) turn confluence into paper or live trades
with hard risk limits.

```
candidate wallets ──► stats (code) ──► profiles (Opus) ──► roster (explicit criteria)
                                                              │
PumpPortal stream ──► trade by a roster wallet                ▼
                        ├─► ledger + wallet–token graph      (code)
                        ├─► snapshot: positions, profiles, relationships (code)
                        ├─► narrow judgments                 (Haiku, or any Judge)
                        ├─► route: observe / analyze / insufficient evidence (rules)
                        ├─► situation report                 (Opus, cites events)
                        └─► entry / exit rules ──► paper or live broker (deterministic)
```

## How the article maps to the code

| Article stage | Module | Notes |
|---|---|---|
| 1. Opus builds trader profiles | `stats.py`, `profiler.py` | Code computes realized PnL, holding periods, entry sizes, profit concentration, missing history. The model interprets and must cite trades. Roster selection uses explicit, inspectable criteria. |
| 2. The graph connects their trades | `graph.py` | Co-entries inside a time window, normalised by each wallet's activity (Jaccard + lift), lead–lag order, recurring groups vs. outsiders, thin-history flags. The current token is excluded from its own history. |
| 3. Jev evaluates the changes | `judge.py` | Four narrow questions (behavior fit, position intent, history sufficiency, attention) answered with a constrained schema. Counting stays in code. Routing uses explicit rules; low confidence stays uncertain. |
| 4. Opus writes the situation report | `reporter.py` | What changed, who, relation to history, what would change the reading. Every fact cites events; unresolvable citations are flagged. Reports are append-only and chained. |
| 5. Following one token | `cases.py`, `engine.py` | One case per token, full event sequence, transfers never counted as sales. |
| *(not in the article)* trading | `strategy.py`, `portfolio.py`, `brokers/` | The article is explicit that its routes are **not** trading orders. This layer is the added step, and it is deterministic: models explain, rules trade. |

Model choices: the article's "Opus 5.5" is used for profiles and reports
(`claude-opus-5-5`). "Jev" (TypeSafe AI's decision model) could not be
verified, so stage 3 uses `claude-haiku-4-5` with structured outputs behind a
`Judge` protocol — plug in Jev or anything else that returns an `Assessment`.
Every model stage has an offline fallback (heuristic profiles and judge,
template reports), so the pipeline runs without an API key.

## Quick start

Needs Python 3.11 or newer.

```bash
git clone https://github.com/Jason-82/agent-trading-2.git
cd agent-trading-2

# Create and activate a private Python environment for the project:
python3 -m venv .venv && source .venv/bin/activate        # macOS / Linux
python -m venv .venv && source .venv/Scripts/activate     # Windows, Git Bash
# python -m venv .venv; .venv\Scripts\Activate.ps1        # Windows, PowerShell

pip install -e ".[dev]"          # includes the live-trading extras and test tools
pytest -q                        # offline test suite
trenchnet demo                   # the article's A/B/C/D walkthrough, offline
trenchnet demo --llm             # same, with Claude (needs ANTHROPIC_API_KEY)
```

Activate the environment again in every new terminal (the `source ...activate`
line) before running `trenchnet`. On Windows the command is `python`, not
`python3`; if PowerShell refuses to run `Activate.ps1`, run
`Set-ExecutionPolicy -Scope CurrentUser RemoteSigned` once.

Setting keys: `export NAME="value"` in Git Bash, macOS or Linux;
`$env:NAME = "value"` in PowerShell. They last until you close the terminal.

The demo builds a roster from 30 days of synthetic history (keeping A–D,
rejecting a scalper, a one-lucky-token wallet and a thin history), then
replays the article's sequence. It skips the token only the recurring trio
buys, enters when outsider D joins, takes profit at 2x, exits on a trailing
stop, and records the "A buys then sells immediately" case without analysis.

## Using it with real wallets

1. **Pick candidates.** Put addresses in `wallets.txt` (`address[,alias]` per
   line; see `examples/wallets.example.txt`). Leaderboards on pump.fun, Fomo,
   GMGN or Birdeye are reasonable sources; the selection criteria decide who
   actually makes the roster.
2. **Backfill history** from any Solana RPC (a paid endpoint such as Helius or
   QuickNode is recommended, it makes one call per transaction):
   ```bash
   export SOLANA_RPC_URL=https://...
   trenchnet backfill wallets.txt --limit 1000
   ```
   Or import your own data: `trenchnet import trades.jsonl` (format in
   `sources/replay.py`).
3. **Profile and select:** `trenchnet profile --wallets wallets.txt`, then
   `trenchnet roster --all` to see who was kept and why.
4. **Paper trade live data:** `export PUMPPORTAL_API_KEY=...` then
   `trenchnet run`. Reports: `trenchnet reports --mint <MINT>`.
5. **Check results** at any time, even while the bot is stopped:
   ```bash
   trenchnet pnl                 # all time
   trenchnet pnl --since 7d      # last week (also 24h, 2w, or a date like 2026-10-01)
   trenchnet pnl --json          # for spreadsheets / scripts
   ```
   It shows realized P&L, win rate, best/worst trades, fees, P&L by day and
   open positions valued at the last price the bot saw. Everything is in SOL,
   so a positive number means the bot beat simply holding SOL. Paper and live
   trades are reported separately.
6. **Backtest** recorded events: `trenchnet replay events.jsonl`.

Everything the bot records (trades, roster, reports, its own fills) is in
`trenchnet.db` in the folder you run it from. Every command accepts
`--db FILE` to use a different file.

Configuration lives in `trenchnet.toml` (copy `examples/trenchnet.example.toml`).

## Live trading

Live orders go through PumpPortal's Local Transaction API: the transaction is
built by PumpPortal, **signed locally** (your key never leaves the process) and
sent through your RPC; fills are reconciled from on-chain balances. It needs
all three of:

```bash
# trenchnet.toml
[live]
enabled = true
max_position_sol = 0.05     # hard cap per buy

export SOLANA_PRIVATE_KEY=<base58 key of a dedicated hot wallet>
export TRENCHNET_LIVE_ACK="I understand this trades real funds"
trenchnet run --live
```

Use a fresh wallet holding only what you are prepared to lose.

## Things to know before trusting it

- **You are always late.** You see a buy after it lands; the price has already
  moved, and fast wallets may finish before you enter. The paper broker adds a
  latency penalty for this, but real slippage on thin bonding curves can be worse.
- **Followed wallets can be adversarial.** Wallets that are known to be copied
  are sometimes used to bait followers into buying before they sell. The
  graph's "recurring group" signal describes co-trading, not ownership — a
  cluster could be one operator. That is why entries require at least two
  independent clusters by default.
- **Past profitability is weak evidence** for memecoins: survivorship,
  one-off winners and unrealized gains all flatter a wallet. The selection
  criteria and profiles try to expose this; they cannot remove it.
- **Fomo** has no official public trading API. An independent data API
  (fomoapi.io) reportedly exposes Fomo trader PnL and feeds, which could feed
  step 1, but I could not verify its schema. Automating the Fomo app itself
  would likely breach its terms. Execution here is pump.fun via PumpPortal.
- **PumpPortal** meters account/token-trade subscriptions (API key + funded
  PumpPortal wallet) and charges a fee per trade; check their current pricing.
- Paper-trade for a meaningful period and compare against simply holding
  SOL before enabling `--live`. Nothing here is financial advice.

## Layout

```
src/trenchnet/
  models.py ledger.py stats.py        trades, position accounting, wallet stats
  profiler.py graph.py                stage 1 and stage 2
  cases.py judge.py reporter.py       snapshots, stage 3, stage 4
  strategy.py portfolio.py engine.py  trading rules, bot holdings, event loop
  pnl.py                              profit and loss report from stored fills
  brokers/  paper.py pumpportal.py    execution
  sources/  pumpportal_ws.py rpc_backfill.py replay.py synthetic.py
  llm.py solana_rpc.py store.py config.py cli.py
tests/                                offline tests (fake LLM client, local websocket)
```
