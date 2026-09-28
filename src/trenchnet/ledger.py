"""Per-wallet, per-token position accounting.

Average-cost accounting with two honesty rules from the article:

* Sales of tokens we never saw bought (history starts mid-position) do not
  produce realized PnL. Their proceeds are kept separately as
  ``unattributed_proceeds_sol`` so missing history stays visible.
* Transfers move tokens (and their cost basis) but never realize PnL and are
  never counted as sales.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field

from .models import Side, Trade

DUST_FRACTION = 1e-3


@dataclass
class Position:
    wallet: str
    mint: str
    tokens: float = 0.0  # observed balance (known + unknown basis)
    basis_tokens: float = 0.0  # portion of balance with an observed cost basis
    cost_sol: float = 0.0  # cost basis of basis_tokens
    bought_sol: float = 0.0
    bought_tokens: float = 0.0
    sold_sol: float = 0.0
    sold_tokens: float = 0.0
    transferred_in: float = 0.0
    transferred_out: float = 0.0
    realized_pnl_sol: float = 0.0
    unattributed_proceeds_sol: float = 0.0
    n_buys: int = 0
    n_sells: int = 0
    first_ts: float | None = None
    first_buy_ts: float | None = None
    first_sell_ts: float | None = None
    last_ts: float | None = None
    closed_ts: float | None = None
    closed_by_transfer: bool = False
    peak_tokens: float = 0.0
    rounds: int = 1
    signatures: list[str] = field(default_factory=list)

    # -- mutation -----------------------------------------------------------

    def _new_round(self) -> None:
        """Re-entry after the position was closed: per-round figures restart
        (so an old, larger peak can't make the new position look like dust);
        realized PnL and unattributed proceeds stay cumulative."""
        self.bought_sol = self.bought_tokens = self.sold_sol = self.sold_tokens = 0.0
        self.transferred_in = self.transferred_out = 0.0
        self.n_buys = self.n_sells = 0
        self.first_buy_ts = self.first_sell_ts = self.closed_ts = None
        self.closed_by_transfer = False
        self.peak_tokens = 0.0
        self.rounds += 1

    def apply(self, t: Trade) -> None:
        if t.side in (Side.BUY, Side.TRANSFER_IN) and self.closed_ts is not None:
            self._new_round()
        if self.first_ts is None:
            self.first_ts = t.ts
        self.last_ts = t.ts
        self.signatures.append(t.signature)

        if t.side is Side.BUY:
            self.tokens += t.token_amount
            self.basis_tokens += t.token_amount
            self.cost_sol += t.sol_amount
            self.bought_sol += t.sol_amount
            self.bought_tokens += t.token_amount
            self.n_buys += 1
            if self.first_buy_ts is None:
                self.first_buy_ts = t.ts
        elif t.side is Side.SELL:
            q = t.token_amount
            if q <= 0:
                return
            covered = min(q, self.basis_tokens)
            if covered > 0:
                basis_part = self.cost_sol * covered / self.basis_tokens
                proceeds_part = t.sol_amount * covered / q
                self.realized_pnl_sol += proceeds_part - basis_part
                self.cost_sol -= basis_part
                self.basis_tokens -= covered
            uncovered = q - covered
            if uncovered > 0:
                self.unattributed_proceeds_sol += t.sol_amount * uncovered / q
            self.tokens = max(0.0, self.tokens - q)
            self.sold_sol += t.sol_amount
            self.sold_tokens += q
            self.n_sells += 1
            if self.first_sell_ts is None:
                self.first_sell_ts = t.ts
            if self._is_dust():
                self._close(t.ts, by_transfer=False)
        elif t.side is Side.TRANSFER_OUT:
            q = min(t.token_amount, self.tokens)
            if self.tokens > 0 and q > 0:
                # Cost basis leaves with the tokens, proportionally.
                frac = q / self.tokens
                self.cost_sol -= self.cost_sol * frac
                self.basis_tokens -= self.basis_tokens * frac
                self.tokens -= q
            self.transferred_out += t.token_amount
            if self._is_dust() and self.closed_ts is None:
                self._close(t.ts, by_transfer=True)
        elif t.side is Side.TRANSFER_IN:
            self.tokens += t.token_amount
            self.transferred_in += t.token_amount

        self.peak_tokens = max(self.peak_tokens, self.tokens)

    def _dust(self) -> float:
        return max(1e-9, self.peak_tokens * DUST_FRACTION)

    def _is_dust(self) -> bool:
        return self.tokens <= self._dust()

    def _close(self, ts: float, by_transfer: bool) -> None:
        # Write off the (negligible) cost of dust left behind by a sale.
        if not by_transfer and self.basis_tokens > 0:
            self.realized_pnl_sol -= self.cost_sol
        self.tokens = 0.0
        self.basis_tokens = 0.0
        self.cost_sol = 0.0
        self.closed_ts = ts
        self.closed_by_transfer = by_transfer

    # -- views --------------------------------------------------------------

    @property
    def is_open(self) -> bool:
        return self.tokens > self._dust()

    @property
    def missing_basis(self) -> bool:
        """True when the wallet sold tokens we never saw it acquire."""
        return self.unattributed_proceeds_sol > 0

    @property
    def acquired_tokens(self) -> float:
        return self.bought_tokens + self.transferred_in

    @property
    def sold_fraction(self) -> float:
        """Share of acquired tokens that were *sold* (transfers excluded)."""
        acq = self.acquired_tokens
        return min(1.0, self.sold_tokens / acq) if acq > 0 else 0.0

    @property
    def status(self) -> str:
        if self.closed_ts is not None:
            return "transferred_out" if self.closed_by_transfer else "exited"
        if not self.is_open:
            return "flat"
        if self.n_sells > 0:
            return "reduced"
        if self.transferred_out > 0:
            return "partially_transferred"
        return "holding"

    @property
    def avg_entry_price(self) -> float | None:
        if self.basis_tokens > 0 and self.cost_sol > 0:
            return self.cost_sol / self.basis_tokens
        if self.bought_tokens > 0:
            return self.bought_sol / self.bought_tokens
        return None

    def hold_seconds(self, now: float) -> float | None:
        if self.first_buy_ts is None:
            return None
        end = self.closed_ts if self.closed_ts is not None else now
        return max(0.0, end - self.first_buy_ts)

    def unrealized_pnl_sol(self, price: float | None) -> float | None:
        if price is None or self.basis_tokens <= 0:
            return None
        return self.basis_tokens * price - self.cost_sol


class Ledger:
    """All positions, indexed by wallet and by mint."""

    def __init__(self) -> None:
        self._positions: dict[tuple[str, str], Position] = {}
        self._by_wallet: dict[str, dict[str, Position]] = defaultdict(dict)
        self._by_mint: dict[str, dict[str, Position]] = defaultdict(dict)

    def apply(self, t: Trade) -> Position:
        key = (t.wallet, t.mint)
        pos = self._positions.get(key)
        if pos is None:
            pos = Position(wallet=t.wallet, mint=t.mint)
            self._positions[key] = pos
            self._by_wallet[t.wallet][t.mint] = pos
            self._by_mint[t.mint][t.wallet] = pos
        pos.apply(t)
        return pos

    def get(self, wallet: str, mint: str) -> Position | None:
        return self._positions.get((wallet, mint))

    def for_wallet(self, wallet: str) -> dict[str, Position]:
        return self._by_wallet.get(wallet, {})

    def for_mint(self, mint: str) -> dict[str, Position]:
        return self._by_mint.get(mint, {})

    def __len__(self) -> int:
        return len(self._positions)
