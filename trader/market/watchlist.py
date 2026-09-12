"""Watch the whole market and keep the engine pointed at where the setups actually are.

WHAT THIS IS FOR, measured before it was built. The bot used to trade a list someone typed
in. On a real bybit day, 34 pairs clear the liquidity floor and the rules see a setup on
**two of them**. A hand-typed list of eight is therefore usually looking at eight coins that
have nothing to offer, while somewhere in the market two do.

WHAT IT IS NOT. It is not a way of knowing which coin will go up. Measured on 21 liquid pairs
and ~1,000 daily bars, taking every trade the rules found across the WHOLE universe against
200 randomly drawn 8-coin lists, capped at 4 open either way and scored in R so sizing cannot
flatter it:

    first half    whole market +19.3R   ·  random lists: median +9.7R, worst -5.9, best +27.9
                  beat 96% of them
    second half   whole market  +5.4R   ·  random lists: median +8.8R, worst -11.3, best +22.8
                  beat 38% of them

So it is better than most lists in one half and slightly worse than a coin flip in the other.
What it does reliably is stay out of the bottom: it was positive in both halves and nowhere
near the worst list either time, and it removes a decision nobody can make in advance. A fixed
list can do better - and can do -11.3R - and there is no way to tell which one you have picked.

That is the whole claim. Anything stronger than it would be a rule fitted to a date, which is
how every coin-ranking idea in this project has died: momentum looked worth +3.7% at one split
and averaged -0.10% across four.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Callable

from ..config import Settings
from . import scanner


@dataclass
class Watch:
    """The result of one sweep: what to trade, and everything that was looked at."""
    symbols: list[str] = field(default_factory=list)
    rows: list[dict[str, Any]] = field(default_factory=list)
    at: float = 0.0
    note: str = ""

    @property
    def with_signal(self) -> list[dict[str, Any]]:
        return [r for r in self.rows if (r.get("signal_strength") or 0) > 0]


def choose(settings: Settings, market_data, want: int = 4, pool: int = 40,
           timeframe: str | None = None, keep: list[str] | tuple[str, ...] = (),
           min_strength: float = 0.0, allow_short: bool = True,
           min_volume: float | None = None,
           on_progress: Callable[[str], None] | None = None,
           abort: Callable[[], bool] | None = None) -> Watch:
    """One sweep of the market: rank by liquidity, look at the charts, pick where a setup is.

    ``keep`` is never dropped - those are the open positions, and a symbol leaving the
    watchlist must not take a live trade's management with it. The engine adds open positions
    to its own pass as well; this is the belt to that braces.

    ``allow_short`` comes from the broker. On the first live sweep the top pick was a SHORT
    setup on a spot account, which cannot be taken at all - so half the watchlist was symbols
    the engine would look at every minute and never trade. A watchlist of things you cannot
    act on is worse than a shorter one.
    """
    say = on_progress or (lambda m: None)
    stop = abort or (lambda: False)
    tf = timeframe or settings.timeframe

    # `min_volume` comes from the ACCOUNT, not from a constant - see scanner.volume_floor. The
    # flat $3M floor meant 39 of the 390 active pairs were ever looked at, which is why the
    # owner's reading of it was "you only added the famous coins". It was right.
    rows = scanner.scan(settings, market_data, limit=pool,
                        min_volume=(scanner.MIN_QUOTE_VOLUME if min_volume is None
                                    else float(min_volume)),
                        on_progress=say, abort=stop)
    if stop():
        return Watch(symbols=list(keep), at=time.time(), note="متوقف شد")
    deep = scanner.deepen(settings, market_data, rows, timeframe=tf, on_progress=say, abort=stop)

    # Strongest live signal first. Liquidity breaks a tie, because between two identical setups
    # the one you can actually get in and out of is the better trade.
    def tradeable(r: dict) -> bool:
        if (r.get("signal_strength") or 0) <= min_strength:
            return False
        return allow_short or not str(r.get("signal", "")).startswith("short")

    ranked = sorted(
        (r for r in deep if tradeable(r)),
        key=lambda r: (-(r.get("signal_strength") or 0.0), -(r.get("volume_usd") or 0.0)),
    )
    picked = list(dict.fromkeys(list(keep) + [r["symbol"] for r in ranked]))[:max(want, len(keep))]

    if not ranked:
        shorts = sum(1 for r in deep if str(r.get("signal", "")).startswith("short"))
        extra = (f" ({shorts} تا ستاپ فروش داشتند که روی حساب نقدی قابل اجرا نیست)"
                 if shorts and not allow_short else "")
        note = (f"از {len(deep)} ارز بررسی‌شده، هیچ‌کدام همین حالا ستاپ قابل‌معامله ندارند{extra} — "
                f"ربات منتظر می‌ماند و این درست‌ترین کاری است که می‌تواند بکند.")
    else:
        top = "، ".join(f"{r['symbol']} ({r['signal']})" for r in ranked[:3])
        note = f"از {len(deep)} ارز بررسی‌شده، {len(ranked)} تا ستاپ دارند: {top}"
    return Watch(symbols=picked, rows=deep, at=time.time(), note=note)
