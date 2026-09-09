"""Strategy interface. A strategy looks at enriched candles and emits a Signal or None."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import pandas as pd


@dataclass
class Signal:
    symbol: str
    side: str                 # long | short
    strength: float           # 0..1 - how convinced the rule is
    strategy: str
    reason: str
    # Suggested stop distance in price units (the risk manager may widen it).
    stop_distance: float | None = None
    meta: dict[str, Any] = field(default_factory=dict)


class Strategy:
    name = "base"
    # Which market regimes this strategy is designed for. Empty = any.
    regimes: tuple[str, ...] = ()

    def evaluate(self, symbol: str, df: pd.DataFrame, regime: str) -> Signal | None:  # pragma: no cover - interface
        raise NotImplementedError

    def wants_regime(self, regime: str) -> bool:
        return not self.regimes or regime in self.regimes
