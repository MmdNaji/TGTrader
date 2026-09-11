"""What the bot saw when it opened or closed a trade, kept so it can be shown and checked.

The owner's request was "show me the chart analysis behind the trades it makes". A reason
string on its own cannot be checked against anything: "entered on a pullback in an uptrend"
is either true or a story, and by the time anyone looks the chart has moved on. So the bars
are stored with the reasoning, and the view redraws the chart AS IT WAS.

Two rules this module keeps:

- **Only what was actually used.** Every number in here came from the frame the decision was
  taken on, not from a later one. The engine evaluates on the CLOSED bar, so that is the bar
  these values are read from - the same one the strategies saw.
- **The arithmetic, not just the verdict.** "stop 2,372.96" says nothing; "2.0 x ATR(14) of
  42.44 = 84.87 below entry, and the round trip in fees is 4.91, so the target clears it 6.2
  times" is a claim the owner can disagree with. A number nobody can argue with is a number
  nobody is reading.

Rendering is deliberately NOT stored. The payload is facts; the Persian sentences are built
from it at display time, so the wording can improve without rewriting what old trades said.
"""
from __future__ import annotations

from typing import Any

MAX_BARS = 160          # what the analysis chart draws; more is not readable at this size


def bars(df, limit: int = MAX_BARS) -> list[list[float]]:
    """The candles as a compact list: [epoch_seconds, open, high, low, close, volume].

    Rounded to six significant places on purpose - this is stored per trade and a full-precision
    float repeated six times over 160 rows is most of the row for no visible difference on a
    chart 600 pixels wide.
    """
    if df is None or len(df) == 0:
        return []
    tail = df.tail(limit)
    out: list[list[float]] = []
    for ts, row in tail.iterrows():
        try:
            out.append([
                float(ts.timestamp()),
                round(float(row["open"]), 8), round(float(row["high"]), 8),
                round(float(row["low"]), 8), round(float(row["close"]), 8),
                round(float(row.get("volume", 0.0) or 0.0), 4),
            ])
        except Exception:
            continue
    return out


def entry_analysis(*, symbol: str, timeframe: str, side: str, regime: str, snap: dict,
                   signals: list, decision: dict, source: str, price: float,
                   stop: float, target: float, stop_distance: float, qty: float,
                   fee_rate: float, round_trip: float, gross_target: float,
                   reward_risk: float, equity: float, risk_amount: float | None,
                   leader: str | None = None, leader_regime: str | None = None) -> dict[str, Any]:
    """Everything that produced this entry, in the units it was decided in."""
    notional = qty * price
    return {
        "symbol": symbol,
        "timeframe": timeframe,
        "side": side,
        "regime": regime,
        "source": source,                      # rules | llm
        "strategy": decision.get("strategy"),
        "confidence": decision.get("confidence"),
        "reason": decision.get("reason", ""),
        "signals": [
            {"strategy": getattr(s, "strategy", None), "side": getattr(s, "side", None),
             "strength": getattr(s, "strength", None), "reason": getattr(s, "reason", "")}
            for s in (signals or [])
        ],
        "indicators": snap,
        "entry": price,
        "stop": stop,
        "target": target,
        "stop_distance": stop_distance,
        "qty": qty,
        "notional": notional,
        "equity_at_entry": equity,
        "risk_amount": risk_amount,
        "risk_pct_of_equity": (risk_amount / equity) if (risk_amount and equity) else None,
        "reward_risk": reward_risk,
        "fee_rate": fee_rate,
        "round_trip_fee": round_trip,
        "gross_target": gross_target,
        "target_over_fee": (gross_target / round_trip) if round_trip else None,
        "leader": leader,
        "leader_regime": leader_regime,
        "skills_used": decision.get("skills_used") or [],
    }


def exit_analysis(*, symbol: str, timeframe: str, side: str, why: str, entry: float,
                  exit_price: float, stop: float | None, init_stop: float | None,
                  target: float | None, qty: float, pnl: float, r_multiple: float | None,
                  fees: float | None, held_seconds: float | None,
                  regime: str | None = None, snap: dict | None = None) -> dict[str, Any]:
    """Everything about how it ended, including the part the entry got wrong."""
    move = (exit_price - entry) if side == "long" else (entry - exit_price)
    return {
        "symbol": symbol,
        "timeframe": timeframe,
        "side": side,
        "why": why,
        "entry": entry,
        "exit": exit_price,
        "stop": stop,
        "init_stop": init_stop,
        "target": target,
        "qty": qty,
        "move": move,
        "move_pct": (move / entry * 100.0) if entry else None,
        "pnl": pnl,
        "r_multiple": r_multiple,
        "fees": fees,
        "held_seconds": held_seconds,
        "regime_at_exit": regime,
        "indicators_at_exit": snap or {},
    }


# ---------------------------------------------------------------- rendering

FA_REGIME = {"trend_up": "روند صعودی", "trend_down": "روند نزولی", "range": "رِنج",
             "volatile": "پرنوسان", "unknown": "نامشخص"}
FA_WHY = {"stop": "حد ضرر خورد", "target": "به هدف رسید", "manual": "دستی بسته شد",
          "close_all": "بستن همه", "reverse": "سیگنال برعکس", "trail": "حد ضرر دنبال‌کننده",
          "shutdown": "خاموش شدن موتور"}


def _num(v, digits: int = 2) -> str:
    if v is None:
        return "—"
    try:
        return f"{float(v):,.{digits}f}"
    except Exception:
        return str(v)


def _price(v) -> str:
    if v is None:
        return "—"
    v = float(v)
    return f"{v:,.2f}" if abs(v) >= 100 else f"{v:,.6g}"


def explain(payload: dict, kind: str) -> list[tuple[str, str]]:
    """Turn a stored analysis into (heading, text) lines for the panel.

    Built here rather than stored, so improving a sentence improves every past trade's
    reading of itself instead of leaving old rows phrased the old way.
    """
    if kind == "open":
        return _explain_open(payload)
    return _explain_close(payload)


def _explain_open(p: dict) -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    side = "خرید" if p.get("side") == "long" else "فروش"
    regime = FA_REGIME.get(p.get("regime"), p.get("regime") or "—")
    out.append(("چه دید", f"{p.get('symbol')} در تایم‌فریم {p.get('timeframe')} · بازار: {regime}"))

    ind = p.get("indicators") or {}
    bits = []
    if ind.get("rsi14") is not None:
        bits.append(f"RSI(14) = {_num(ind['rsi14'], 1)}")
    if ind.get("adx14") is not None:
        bits.append(f"ADX(14) = {_num(ind['adx14'], 1)}")
    if ind.get("ema20") is not None and ind.get("ema50") is not None:
        above = float(ind["ema20"]) > float(ind["ema50"])
        bits.append(f"EMA20 {'بالای' if above else 'زیر'} EMA50")
    if ind.get("atr14") is not None:
        bits.append(f"ATR(14) = {_price(ind['atr14'])}")
    if ind.get("ret_20") is not None:
        bits.append(f"بازده ۲۰ کندل اخیر {float(ind['ret_20'])*100:+.1f}٪")
    if bits:
        out.append(("اندیکاتورها", " · ".join(bits)))

    sigs = p.get("signals") or []
    if sigs:
        out.append(("سیگنال‌ها", "\n".join(
            f"• {s.get('strategy')} ({s.get('side')}، قدرت {_num(s.get('strength'), 2)}): {s.get('reason')}"
            for s in sigs)))

    src = "هوش مصنوعی" if p.get("source") == "llm" else "قوانین پایه"
    conf = p.get("confidence")
    head = f"تصمیم {side} · منبع: {src}"
    if conf is not None:
        head += f" · اطمینان {float(conf)*100:.0f}٪"
    out.append((head, p.get("reason") or "—"))

    entry, stop, target = p.get("entry"), p.get("stop"), p.get("target")
    sd = p.get("stop_distance") or 0.0
    atr = (p.get("indicators") or {}).get("atr14")
    stop_note = f"فاصله‌ی حد ضرر {_price(sd)}"
    if atr:
        stop_note += f" ({sd / float(atr):.2f} برابر ATR)"
    out.append(("چرا این حد ضرر",
                f"ورود {_price(entry)} · حد ضرر {_price(stop)} · {stop_note}"))

    rr = p.get("reward_risk")
    tof = p.get("target_over_fee")
    line = f"هدف {_price(target)} · نسبت سود به ریسک {_num(rr, 1)}"
    if tof:
        line += (f" · سود ناخالص هدف {_price(p.get('gross_target'))} است و کارمزد رفت‌وبرگشت "
                 f"{_price(p.get('round_trip_fee'))} — یعنی {float(tof):.1f} برابر کارمزد")
    out.append(("چرا این هدف", line))

    risk = p.get("risk_amount")
    rp = p.get("risk_pct_of_equity")
    size = f"حجم {_num(p.get('qty'), 6)} · ارزش {_price(p.get('notional'))} $"
    if risk is not None:
        size += f" · ریسک این معامله {_price(risk)} $"
        if rp:
            size += f" ({float(rp)*100:.2f}٪ سرمایه)"
    out.append(("اندازه‌ی پوزیشن", size))

    if p.get("leader") and p.get("leader_regime"):
        out.append(("بازار کل",
                    f"{p['leader']} در {FA_REGIME.get(p['leader_regime'], p['leader_regime'])} بود"))
    if p.get("skills_used"):
        out.append(("مهارت‌های استفاده‌شده", "، ".join(str(s) for s in p["skills_used"])))
    return out


def _explain_close(p: dict) -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    why = FA_WHY.get(p.get("why"), p.get("why") or "—")
    stop_now, stop_0 = p.get("stop"), p.get("init_stop")
    trailed = (stop_0 is not None and stop_now is not None
               and abs(float(stop_now) - float(stop_0)) > 1e-12)
    if p.get("why") == "stop" and trailed:
        # A trailed stop-out is usually a WINNER being banked, not a loss being taken, and
        # calling both of them "حد ضرر خورد" hides which one just happened.
        why = ("حد ضرر دنبال‌کننده خورد — سود قفل‌شده برداشته شد"
               if (p.get("pnl") or 0) > 0 else "حد ضرر دنبال‌کننده خورد")
    out.append(("چرا بسته شد", why))
    line = f"ورود {_price(p.get('entry'))} → خروج {_price(p.get('exit'))}"
    if p.get("target"):
        line += f" · هدف بود {_price(p.get('target'))}"
    # The stop IN FORCE, not only the one it started with. The first version of this panel
    # printed the initial stop alone, and a short that had trailed from 40.92 down to 38.51
    # then read "stopped out" beside a stop the price never came near - a correct exit made to
    # look impossible. Whenever the two differ, both are shown and the move is named.
    stop_now, stop_0 = p.get("stop"), p.get("init_stop")
    if stop_0 is not None and stop_now is not None and abs(float(stop_now) - float(stop_0)) > 1e-12:
        line += (f" · حد ضرر اولیه {_price(stop_0)} بود و دنبال قیمت تا {_price(stop_now)} "
                 f"جابه‌جا شده بود")
    elif stop_0 is not None:
        line += f" · حد ضرر {_price(stop_0)}"
    out.append(("قیمت‌ها", line))
    mv = p.get("move_pct")
    line = f"حرکت {_price(p.get('move'))}"
    if mv is not None:
        line += f" ({float(mv):+.2f}٪)"
    pnl = p.get("pnl")
    if pnl is not None:
        line += f" · سود/زیان خالص {float(pnl):+,.4f} $"
    if p.get("fees") is not None:
        line += f" (کارمزد {_price(p['fees'])} $)"
    out.append(("نتیجه", line))
    r = p.get("r_multiple")
    if r is not None:
        out.append(("بر حسب R", f"{float(r):+.2f}R — یعنی {abs(float(r)):.2f} برابر همان مبلغی "
                                f"که در این معامله ریسک شده بود"))
    held = p.get("held_seconds")
    if held:
        h = float(held)
        txt = f"{h/86400:.1f} روز" if h >= 86400 else (f"{h/3600:.1f} ساعت" if h >= 3600 else f"{h/60:.0f} دقیقه")
        out.append(("مدت نگه‌داری", txt))
    return out
