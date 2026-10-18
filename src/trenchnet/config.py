"""Configuration: dataclass defaults, optionally overridden by a TOML file."""

from __future__ import annotations

import tomllib
from dataclasses import dataclass, field, fields, is_dataclass
from pathlib import Path
from typing import Any

from .stats import SelectionCriteria
from .textfile import read_text_any


@dataclass
class LLMConfig:
    # Stage 1 (profiles) and stage 4 (situation reports): the expensive model.
    profile_model: str = "claude-opus-5-5"
    report_model: str = "claude-opus-5-5"
    profile_effort: str = "high"
    report_effort: str = "medium"
    # Stage 3 (narrow judgments): a cheap, fast model standing in for Jev.
    judge_model: str = "claude-haiku-4-5"
    use_llm_profiles: bool = True
    use_llm_judge: bool = True
    use_llm_reports: bool = True
    max_trades_in_prompt: int = 400
    # Live mode: write reports in the background so trading never waits on them.
    background_reports: bool = True


@dataclass
class GraphConfig:
    co_entry_window_s: float = 1800.0  # "arriving together" vs. days apart
    min_co_entries: int = 2
    min_jaccard: float = 0.15
    min_lift: float = 1.5
    min_history_tokens: int = 5  # below this a wallet's overlap numbers are thin


@dataclass
class RouterConfig:
    min_buyers_for_analysis: int = 2
    min_confidence: float = 0.55
    # A token gets a new report when another followed wallet joins it or its last holder leaves;
    # otherwise at most one per this many seconds (a wallet flipping in and out isn't news each time).
    report_cooldown_s: float = 900.0


@dataclass
class StrategyConfig:
    enabled: bool = True
    # Entry: confluence of tracked buyers.
    min_tracked_buyers: int = 3
    entry_window_s: float = 900.0
    min_independent_clusters: int = 2  # e.g. the usual trio + an outsider = 2
    min_supportive_buys: int = 1  # buys judged "fits" with standard/conviction intent
    max_insufficient_history_share: float = 0.5
    min_active_holder_fraction: float = 0.5  # buyer still holds >= this share of what it bought
    max_entry_delay_s: float = 1800.0
    max_run_up: float = 3.0  # skip if price > N x the first tracked entry price
    min_market_cap_sol: float | None = None
    max_market_cap_sol: float | None = None
    # Entry: a conviction buy, i.e. one followed wallet with a strong record buying far more than
    # it usually does. Off by default: it copies a single wallet, without the confluence checks above.
    # Such a position is sold when that wallet sells (exit_on_cohort_sold_fraction of its tokens) or
    # no longer holds the token, besides the price and time exits.
    conviction_entry: bool = False
    conviction_size_multiple: float = 3.0  # the buy is at least this many times its median entry
    conviction_min_realized_sol: float = 20.0  # the wallet's realized profit in its history
    conviction_min_win_rate: float = 0.5
    conviction_max_delay_s: float = 300.0  # follow within this long of its buy, or not at all
    # Sizing and risk.
    position_size_sol: float = 0.1
    max_open_positions: int = 5
    max_total_exposure_sol: float = 0.5
    daily_loss_limit_sol: float = 0.3
    # Exits.
    stop_loss_pct: float = 0.35
    take_profit: list[list[float]] = field(default_factory=lambda: [[2.0, 0.5]])  # [multiple, fraction]
    trailing_stop_pct: float = 0.30  # armed after the first take-profit
    max_hold_s: float = 6 * 3600.0
    exit_on_cohort_sold_fraction: float = 0.5
    exit_on_leader_exit: bool = True
    reenter_same_mint: bool = False
    # A "buy" for less than this is tokens pushed into the wallet by someone else (spam that shows
    # up as a buy on trackers, often aimed at well-known wallets), not a trade. Sales for less than
    # this of tokens the bot isn't following are clean-ups of old leftovers and are not shown.
    dust_sol: float = 0.005


@dataclass
class PaperConfig:
    starting_balance_sol: float = 5.0
    fee_bps: float = 150.0  # bonding-curve fee + PumpPortal fee; adjust to your venue
    slippage_bps: float = 300.0
    latency_penalty_bps: float = 200.0  # you are always later than the wallets you follow


@dataclass
class LiveConfig:
    enabled: bool = False
    rpc_url: str = "https://api.mainnet-beta.solana.com"
    private_key_env: str = "SOLANA_PRIVATE_KEY"
    slippage_pct: float = 15.0
    priority_fee_sol: float = 0.0005
    pool: str = "auto"
    max_position_sol: float = 0.05  # hard cap, applied on top of strategy sizing
    confirm_timeout_s: float = 45.0


@dataclass
class PumpPortalConfig:
    ws_url: str = "wss://pumpportal.fun/api/data"
    trade_url: str = "https://pumpportal.fun/api/trade-local"
    api_key_env: str = "PUMPPORTAL_API_KEY"


@dataclass
class Config:
    db_path: str = "trenchnet.db"
    llm: LLMConfig = field(default_factory=LLMConfig)
    selection: SelectionCriteria = field(default_factory=SelectionCriteria)
    graph: GraphConfig = field(default_factory=GraphConfig)
    router: RouterConfig = field(default_factory=RouterConfig)
    strategy: StrategyConfig = field(default_factory=StrategyConfig)
    paper: PaperConfig = field(default_factory=PaperConfig)
    live: LiveConfig = field(default_factory=LiveConfig)
    pumpportal: PumpPortalConfig = field(default_factory=PumpPortalConfig)


def _apply(obj: Any, data: dict[str, Any], path: str) -> None:
    names = {f.name: f for f in fields(obj)}
    for key, value in data.items():
        if key not in names:
            raise ValueError(f"unknown config key: {path}{key}")
        current = getattr(obj, key)
        if is_dataclass(current):
            if not isinstance(value, dict):
                raise ValueError(f"config section {path}{key} must be a table")
            _apply(current, value, f"{path}{key}.")
        else:
            setattr(obj, key, value)


def load_config(path: str | Path | None = None) -> Config:
    cfg = Config()
    if path is None:
        default = Path("trenchnet.toml")
        if not default.exists():
            return cfg
        path = default
    _apply(cfg, tomllib.loads(read_text_any(path)), "")  # also a file Notepad saved with a BOM or as UTF-16
    return cfg
