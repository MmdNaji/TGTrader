"""How good does this setup look, on a 0..1 scale, from things the chart can actually say.

WHY THIS IS SEPARATE AND SMALL. The owner asked for the bot to put more on some coins than
others rather than splitting the capital evenly. The size of a trade is the fastest way to lose
an account, so the number that drives it has to be something that can be argued with and
measured - not a feeling, and not the model's own enthusiasm dressed up as arithmetic.

Every input here is a fact about the chart at entry and every one of them is a number the
strategies already compute:

    trend strength      ADX(14). A breakout in a 35-ADX trend is not the same trade as the
                        identical breakout in a 12-ADX chop, and the difference is measured.
    room over the fee   how many round trips the target clears. A setup whose reward is six
                        times the cost of trading it survives being wrong more often than one
                        at three times.
    agreement           does the regime point the same way as the signal. A long in trend_up is
                        the setup the rule was written for; a long in trend_down is the rule
                        firing anyway.
    not over-extended   RSI far into the extreme on the side you are entering is late, and late
                        entries are where the stop sits closest to the noise.

They are averaged, NOT multiplied, so no single input can veto a trade - that is the job of the
entry filter, and a sizing score that can reach zero is a second entry filter nobody asked for.

WHETHER THIS EARNS ITS PLACE IS A MEASUREMENT, NOT AN ARGUMENT. The default is off. See
`scripts/conviction_exp.py` for the walk-forward that decides it.
"""
from __future__ import annotations

from typing import Any


def score(snap: dict[str, Any], side: str, regime: str,
          gross_target: float, round_trip: float) -> float:
    """0..1, where 0.5 means "an ordinary setup" and the size is unchanged."""
    parts: list[float] = []

    adx = snap.get("adx14")
    if adx is not None:
        # 15 is the usual floor for "there is a trend at all", 40 is a strong one.
        parts.append(_band(float(adx), 15.0, 40.0))

    if round_trip > 0:
        # the entry filter already demands 3x; 3 -> 0, 8 -> 1
        parts.append(_band(gross_target / round_trip, 3.0, 8.0))

    if regime in ("trend_up", "trend_down"):
        agrees = (side == "long" and regime == "trend_up") or \
                 (side == "short" and regime == "trend_down")
        parts.append(1.0 if agrees else 0.0)
    elif regime == "range":
        # a mean-reversion setup in a range is doing what it was written for; neither bonus nor
        # penalty, because this score is about conviction and not about which rule fired
        parts.append(0.5)

    rsi = snap.get("rsi14")
    if rsi is not None:
        r = float(rsi)
        # entering long at 80 or short at 20 is late; 50 is neutral
        late = (r - 50.0) / 30.0 if side == "long" else (50.0 - r) / 30.0
        parts.append(_band(1.0 - max(0.0, late), 0.0, 1.0))

    if not parts:
        return 0.5
    return max(0.0, min(1.0, sum(parts) / len(parts)))


def _band(v: float, lo: float, hi: float) -> float:
    if hi <= lo:
        return 0.5
    return max(0.0, min(1.0, (v - lo) / (hi - lo)))
