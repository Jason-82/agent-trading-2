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

Setting keys: put them in a file named `.env` in the project folder, one per
line, e.g. `export SOLANA_RPC_URL="https://..."` (the `export` is optional).
Every `trenchnet` command reads it, so there's nothing to load first; git never
uploads it. A key set in the terminal (`export NAME="value"` in Git Bash,
`$env:NAME = "value"` in PowerShell) takes precedence over the file.

**Updating to a newer version:** stop any running `trenchnet run` first
(Ctrl-C), then `git pull` and `pip install -e ".[dev]"`, then start it again.
A newer version may add to `trenchnet.db`, and a bot started before the
update can't write to it afterwards.

The demo builds a roster from 30 days of synthetic history (keeping A–D,
rejecting a scalper, a one-lucky-token wallet and a thin history), then
replays the article's sequence. It skips the token only the recurring trio
buys, enters when outsider D joins, takes profit at 2x, exits on a trailing
stop, and records the "A buys then sells immediately" case without analysis.

## Using it with real wallets

1. **Pick candidates.** Put addresses in `wallets.txt` (`address[,alias]` per
   line; see `examples/wallets.example.txt`). Leaderboards on pump.fun, GMGN or
   Birdeye are reasonable sources; the selection criteria decide who
   actually makes the roster. For traders using the Fomo app, see
   [Fomo traders](#fomo-traders) below.
2. **Backfill history** from any Solana RPC (a paid endpoint such as Helius or
   QuickNode is recommended, it makes one call per transaction):
   ```bash
   export SOLANA_RPC_URL=https://...
   trenchnet backfill wallets.txt --limit 1000
   ```
   It reads about 5 transactions a second (`--rps` changes that), shows one
   progress line per wallet with why any transactions were skipped, and skips any
   line in `wallets.txt` that isn't a wallet address. If it stops early
   (credits used up, provider down) the trades read so far are kept; run the
   same command again later to continue. Running it again re-reads each
   wallet's recent transactions and updates what was saved for them (so a
   newer version of the reader corrects older entries), while older history
   and trades seen live are kept. Add `--new-only` to download only wallets
   you haven't downloaded before.

   Trades paid in USDC (or USDT/USD1) instead of SOL, as on Fomo and on
   USDC-paired pump.fun coins, are counted too: each is valued in SOL at the
   SOL/USD price of its time, taken from Coinbase's or Binance's public price
   history (no key needed; downloaded once and kept in the database).
   `trenchnet inspect <wallet or nickname>` shows, transaction by transaction,
   what the reader made of a wallet's recent activity.
   Or import your own data: `trenchnet import trades.jsonl` (format in
   `sources/replay.py`).
3. **Profile and select:** `trenchnet profile --wallets wallets.txt`, then
   `trenchnet roster --all` to see who was kept and why.
4. **Paper trade live data:** `export PUMPPORTAL_API_KEY=...` then
   `trenchnet run`. With `SOLANA_RPC_URL` set it also watches the wallets
   through your RPC's websocket, which sees trades PumpPortal doesn't report
   under the wallet (aggregator routes, every Fomo trade); `--no-rpc-watch`
   turns that off. Reports: `trenchnet reports --mint <MINT>`.
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

   To stop `trenchnet run`, press Ctrl-C once: it finishes what it is doing
   (an order being confirmed is recorded), prints a summary and exits. Press
   Ctrl-C a second time only if you need to quit immediately.
6. **Backtest** recorded events: `trenchnet replay events.jsonl`. Backtest
   trades are kept apart from paper and live results (each replay replaces
   the previous one); see them with `trenchnet pnl --mode backtest`.

Everything the bot records (trades, roster, reports, its own fills) is in
`trenchnet.db` in the folder you run it from. Every command accepts
`--db FILE` to use a different file.

## Settings

Settings live in `trenchnet.toml` in the folder you run the bot from (all
of them, with their defaults and what they do:
`examples/trenchnet.example.toml`). Change them without opening the file:

```bash
trenchnet config                                   # what you've changed from the defaults
trenchnet config strategy                          # every setting in a section
trenchnet config strategy.min_tracked_buyers=2     # change one (creates the file if needed)
trenchnet config strategy.min_tracked_buyers=default   # back to the default
```

It keeps the file's comments, refuses typos and values of the wrong kind,
and never saves a file that wouldn't load. Restart `trenchnet run` after a
change.

### When the bot buys

- **Confluence** (always on): `min_tracked_buyers` followed wallets (3 by
  default) bought within `entry_window_s` (15 min) and still hold, from at
  least `min_independent_clusters` separate groups (2: wallets that always
  buy together count as one), with the price under `max_run_up` times the
  first followed buy. The position is sold when those wallets have sold
  half their tokens or the first of them leaves, besides the stop-loss,
  take-profit, trailing stop and maximum hold.
- **Conviction buys** (off until `trenchnet config strategy.conviction_entry=true`):
  one followed wallet with a strong record (at least
  `conviction_min_realized_sol`, 20 SOL, realized and a
  `conviction_min_win_rate` of 50%) buys `conviction_size_multiple` (3) times
  its usual entry or more, and still holds. The bot follows within
  `conviction_max_delay_s` (5 min) of that buy, if the price is under
  `max_run_up` times what the wallet paid. Such a position is sold when
  *that* wallet sells half its tokens or no longer holds the token (other
  wallets' trades don't count), besides the price and time exits. `trenchnet run`
  lists which followed wallets qualify when it starts. It copies a single
  wallet, so it is riskier than confluence: the big buy can be bait, or the
  wallet may be about to sell into the followers.

Both share the size and risk limits (`position_size_sol`,
`max_open_positions`, `max_total_exposure_sol`, `daily_loss_limit_sol`).

## Fomo traders

The Fomo app doesn't show its traders' wallet addresses, and its terms forbid
scraping the app or looking users up. Their trades are still ordinary Solana
transactions, though: Fomo pays every user's network fee from one sponsor
wallet and takes its app fee in USDC into one account. `trenchnet fomo`
reads a sample of that account's recent transactions through your own RPC
and lists who traded, by volume:

```bash
trenchnet fomo                                # ~300 RPC calls, about a minute
trenchnet backfill fomo.txt --limit 500       # their history
trenchnet profile --wallets fomo.txt          # keeps only the profitable ones
trenchnet roster --all
```

The wallets go into `fomo.txt` with nicknames like `fomo-9xQe`; running it
again adds new ones. A short sample shows who is active and trades size, not
who trades well; `profile` decides that from each wallet's full history,
with the same criteria as everyone else. Names (which wallet is which Fomo
user) aren't available this way. If you know a particular trader's address,
add it to `fomo.txt` yourself.

Fomo's sponsor pays the network fee, but the trader signs each trade, so
the trades are in the trader's own wallet history like anyone else's. They
are paid in USDC; see step 2 above for how those are counted.

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
  independent clusters by default, and why conviction buys are off unless
  you turn them on.
- **Past profitability is weak evidence** for memecoins: survivorship,
  one-off winners and unrealized gains all flatter a wallet. The selection
  criteria and profiles try to expose this; they cannot remove it.
- **Fomo** has no official public trading API, and automating or scraping
  the app would breach its terms; `trenchnet fomo` only reads public
  blockchain data. The bot follows Fomo traders but its own orders go to
  pump.fun via PumpPortal, priced in SOL. Coins paired with USDC can be
  followed and paper traded; whether PumpPortal can execute a live order on
  a given USDC pair is up to PumpPortal. Price updates for exits come from
  PumpPortal and from the followed wallets' own trades.
- While running, the bot takes each followed wallet's trades in the order
  they reach it, from PumpPortal or your RPC, whichever is first. A trade
  only your RPC sees (a Fomo or aggregator trade) reaches it a second or two
  later than a pump.fun trade PumpPortal reports, so two trades by the same
  wallet that close together can be taken in the wrong order until the next
  backfill, which stores every trade with its on-chain time.
- The Fomo account addresses `trenchnet fomo` uses come from DefiLlama's
  open-source Fomo adapter. If it stops finding trades, Fomo has probably
  changed them: `trenchnet fomo --account <address>`.
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
  prices.py fomo.py                   SOL/USD history, Fomo trader discovery
  brokers/  paper.py pumpportal.py    execution
  sources/  pumpportal_ws.py rpc_ws.py rpc_backfill.py replay.py synthetic.py
  llm.py solana_rpc.py store.py config.py settings.py cli.py
tests/                                offline tests (fake LLM client, local websocket)
```
