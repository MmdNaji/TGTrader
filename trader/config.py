"""Application settings.

Everything the user can change lives in one JSON file under the per-user data
directory (``%APPDATA%\\TGTrader`` on Windows, ``~/.tgtrader`` elsewhere).
Secrets (API keys) are stored in that file too - it is the user's own machine
and the file is created with owner-only permissions where the OS supports it.
"""
from __future__ import annotations

import json
import os
import sys
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any


def data_dir() -> Path:
    override = os.environ.get("TGTRADER_HOME")
    if override:
        p = Path(override)
    elif sys.platform.startswith("win"):
        p = Path(os.environ.get("APPDATA", Path.home())) / "TGTrader"
    else:
        p = Path.home() / ".tgtrader"
    p.mkdir(parents=True, exist_ok=True)
    return p


# Exchanges we can read prices from but that publish no trading API: orders go through screen control.
NO_API_EXCHANGES = ("kcex",)


# The label each setting carries in the window. An advisory that names a field has to use the
# name the user can actually see: the first version of those messages said «حداکثر پوزیشن
# همزمان» when the field is called «حداکثر پوزیشن باز», and pointed at a percentage that had no
# field at all. Both are read from here now, and a test checks the window uses the same strings.
LABELS = {
    "capital_limit": "سقف سرمایه‌ی ربات",
    "risk_per_trade": "ریسک هر معامله",
    "position_pct": "درصد سرمایه در هر معامله",
    "max_open_risk": "سقف ریسک همزمان همه‌ی پوزیشن‌ها",
    "max_daily_loss": "حداکثر زیان روزانه",
    "max_open_positions": "حداکثر پوزیشن باز",
    "max_position_frac": "بزرگ‌ترین پوزیشن (٪ سرمایه)",
    "symbols": "نمادها",
}


@dataclass
class RiskSettings:
    # Hard ceiling on the capital the bot may ever put to work (quote currency).
    capital_limit: float = 100.0
    # Fraction of the capital limit risked per trade (0.01 = 1%).
    risk_per_trade: float = 0.01
    # The day is over when this fraction of the capital limit is lost.
    max_daily_loss: float = 0.03
    # Never hold more than this many positions at once. Kept consistent with
    # max_position_frac below: at 25% of the capital limit each, four is what the cash reaches.
    max_open_positions: int = 4
    # Stop-loss distance in ATR multiples - but ONLY where nothing else set one. Every built-in
    # strategy computes its own stop from its own setup, so this number decides nothing for
    # them; it is the fallback for the model path and for any rule that leaves it blank. It was
    # swept at 1.5 / 2.0 / 2.5 / 3.0 over 21 liquid pairs and 1,000 daily bars and every result
    # was IDENTICAL to the last digit, which is how this was found.
    atr_stop_mult: float = 2.0
    # Take-profit as a multiple of the stop. 2.5, not 2.0, and this is the one setting that was
    # moved on evidence rather than taste. Measured on real bybit daily bars, 21 liquid pairs,
    # ~1,000 bars each, long-only, with the live confidence gate and real fees and slippage:
    #
    #            worst half   worst third   even coins   odd coins   win rate
    #   rr 2.0      +99.6         -3.2         +177.4      +339.7     41-48%
    #   rr 2.5     +189.7        +12.8         +335.0      +351.6     45-51%
    #   rr 3.0     +218.2        +34.0         +272.4      +375.8     34-51%
    #
    # 2.0 is the only one that LOSES money on a third of the history. 2.5 and 3.0 both survive
    # every split; 2.5 is taken because it wins on more of them and its win rate is higher in
    # every single split, and the owner's stated goal is more winning trades.
    #
    # Widening the STOP is not the same lever and does not work: scaling each strategy's own
    # stop x3 lifted the first half's win rate to 50% and took the second half to +20 from
    # +218. That is a rule fitted to a date, and it is the shape to watch for here.
    reward_risk: float = 2.5
    # Sell part of a position once it is this many R in profit and move the stop to break-even,
    # letting the rest run. 0 = off, and off is the default because it is a TRADE-OFF, not an
    # improvement. Measured on 21 liquid pairs and ~1,000 daily bars, four independent splits,
    # portfolio of at most 4 open, scored in R:
    #
    #                  worst win%   worst net   worst drawdown   longest wait
    #   off               39.6%       +3.4R         18.1R           352 days
    #   half at 1.0R      46.2%       +3.0R         14.0R           369 days
    #   half at 1.5R      41.5%       +6.9R         16.9R           369 days
    #
    # So 1.0R buys a better win rate in every split and a 23% shallower drawdown, and pays
    # about 12% of the return for it. 1.5R doubles the worst split's return and leaves the win
    # rate alone. Neither is free and neither is wrong - which one is right depends on what the
    # owner is actually trying to avoid, so the app states the numbers and does not choose.
    partial_take_r: float = 0.0
    partial_take_frac: float = 0.5     # how much of the position to sell at that point

    # Trailing stop kicks in once the trade is this many R in profit (0 = off).
    trail_after_r: float = 1.0
    # Largest single position as a fraction of the capital limit. 25%, not 50%: at a half the
    # account per trade only two positions fit however many the other settings allow, and the
    # mismatch is invisible until the bot has one trade open and refuses every other.
    max_position_frac: float = 0.25
    # Ceiling on the TOTAL money at risk across every open position at once (entry to original
    # stop), as a fraction of the capital limit. This is a different thing from max_daily_loss:
    # that one is about losses already taken today, this one is about how much can be lost at
    # the same moment if a correlated market takes every stop out together. 0 = no cap.
    max_open_risk: float = 0.06

    # Let the size of a trade depend on how good the setup looks, inside a band. The owner asked
    # for exactly this: "don't just divide the ceiling up - work out from your own analysis how
    # much to put on each coin, sometimes more, sometimes less."
    #
    # IT IS OFF, AND THAT IS A MEASUREMENT RATHER THAN CAUTION. Built, wired through the
    # backtest, and walked forward over 32 coins and 1,000 daily bars in MONEY (R cannot see
    # this - it divides P&L by the risk the trade was opened with, so it normalises position
    # size out by construction):
    #
    #                   flat      x2 band    x3 band
    #   whole          +561.8     +565.0     +503.1
    #   half 1         +362.2     +375.1     +319.9
    #   half 2         +421.0     +395.0     +349.6
    #   third 1        +173.2     +119.1      +18.3
    #   third 2        -266.0     -303.0     -347.0
    #   third 3        +128.4     +102.8      +68.9
    #
    # It beat flat sizing in two windows of six and lost in four, and the wider band was worse
    # in EVERY window - the harm grows with how hard you lean on it, which is what a wrong axis
    # looks like rather than a mistuned one. The worst of it is `third 2`: in the one stretch
    # that lost money it made the loss BIGGER (-266 -> -303 -> -347), because the setups this
    # score likes are the ones that hurt most in a bad market. Sizing up into the bad period is
    # the single worst property a sizing rule can have.
    #
    # The machinery stays because the MODEL's own confidence is a different signal from this
    # four-input chart score, and that one cannot be backtested here. Turning this on means
    # accepting the table above.
    conviction_sizing: bool = False
    conviction_band: float = 2.0


@dataclass
class ExchangeSettings:
    # ccxt exchange id: bybit, binance, kucoin, okx, mexc ... (crypto)
    exchange_id: str = "bybit"
    api_key: str = ""
    secret: str = ""
    password: str = ""  # some exchanges (okx, kucoin) need a passphrase
    sandbox: bool = False
    # Proxy for exchanges that block the user's region, e.g. socks5://127.0.0.1:1080
    proxy: str = ""


@dataclass
class ComputerSettings:
    """Driving an exchange that has no API through its own website/app."""
    enabled: bool = False
    # Free-form description the model reads before it touches the screen:
    # which window/site is the exchange, where the order form is, etc.
    exchange_notes: str = ""
    # Ask the human before the final click that submits an order.
    confirm_before_submit: bool = True
    max_steps: int = 25
    # Screenshots wider than this are downscaled before they are sent.
    max_screenshot_width: int = 1280


@dataclass
class Settings:
    # --- one switch instead of twenty ---
    # The owner's words, after a day of being handed settings to decide: "I said from the start
    # it should be AI-based - put in one option where it chooses everything itself, watches all
    # the coins, opens trades, with whatever capital it wants and whatever stop it wants."
    #
    # That is a fair complaint about this app. Every number below has a measured best value
    # recorded next to it, and the app was still asking the owner to type them in - and then
    # putting a banner across the dashboard when the answer disagreed with the measurement.
    # A setting whose right value is known is not a setting, it is homework.
    #
    # With this on, the bot sets everything it has evidence about (see `effective()`), watches
    # the whole market instead of a typed list, and decides each trade with the model. What it
    # does NOT take is the two things that are not measurements: HOW MUCH MONEY it may use
    # (`risk.capital_limit`) and whether it is paper or live (`mode`). Those are the owner's,
    # and no amount of backtesting makes them the app's to choose.
    #
    # Nothing here is written over the saved settings. `effective()` returns a COPY, so turning
    # this off puts the owner's own numbers straight back.
    autopilot: bool = False

    # --- AI ---
    ai_provider: str = "claude"   # claude | openai
    anthropic_api_key: str = ""
    model: str = "claude-opus-5"
    openai_api_key: str = ""
    openai_model: str = "gpt-5"
    effort: str = "high"          # low | medium | high | xhigh | max
    use_llm_for_decisions: bool = True
    auto_update: bool = True
    # How eager to open trades: normal (patient, waits for a real setup),
    # high (lower bar), scalp (rules-only, trades on almost any momentum signal,
    # meant for watching activity on a short timeframe - not a profit setting).
    aggressiveness: str = "normal"   # normal | high | scalp
    # Percent of capital to put into each trade as notional. 0 = automatic
    # risk-based sizing (from risk_per_trade and the stop distance).
    position_pct: float = 0.0
    # Refuse an altcoin trade that fights the market leader (BTC).
    # DEFAULT OFF, and that is a measured decision, not an oversight. Backtested over 8 alts x
    # 1000 bars: on 1d it cost about 1.2% of return (mean +6.72% -> +5.57%) and on 4h it changed
    # nothing, because the strategies' own regime filter already correlates with Bitcoin's. It
    # stays available because it is a real correlation control in a crash, but it is not free.
    #
    # RE-MEASURED on DRAWDOWN rather than return, because that was the open question - the
    # owner's real complaint is long red stretches, and a filter can be worth paying return for
    # if it shortens them. It does not earn its place there either. Portfolio of 4, 21 pairs,
    # four splits, in R:
    #
    #                          worst win%   worst net   worst drawdown   longest wait
    #   plain                     39.6%       +3.4R         18.1R           352 days
    #   leader filter             36.0%       +2.7R         16.7R           333 days
    #   leader + half at 1R       44.7%       +0.1R         13.0R           340 days
    #
    # It buys 1.4R of drawdown and 19 days for a worse win rate and a worse worst-case return,
    # and stacked with the scale-out it takes the worst split to break-even. The scale-out ALONE
    # does better on every count that matters (44.6% / +3.0R / 14.0R).
    #
    # And the number none of them move: the longest wait for a new high is 333-369 days in every
    # configuration tried. That is a property of this strategy on this market, not of a setting,
    # and nothing measured here shortens it.
    align_with_leader: bool = False

    # Let the PAPER account short on crypto. Off, because the live crypto broker here is a spot
    # account and cannot: a paper run that shorts is reporting trades going live would refuse.
    # Turn it on only to see what a margin account would have done - and then do not read the
    # result as what this account will do. Measured worth of shorts on 21 pairs, ~1,000 daily
    # bars: win rate 45.7% -> 46.6% on the first half and 42.7% -> 48.1% on the second, and the
    # second half's return roughly doubled. Real, and not available on spot.
    paper_allow_short: bool = False

    # How long a trade's analysis keeps its CANDLES. The words - what it saw, why it entered,
    # the arithmetic - are kept for ever and are a few hundred bytes; the bars are 98% of the
    # size and only matter while a trade is recent enough to argue about. Measured: 25.8 KB per
    # trade, 1.3 MB a month at 50 trades, ~15 MB a year, and the whole-market watch only
    # raises the trade count. 0 = keep everything.
    analysis_keep_days: int = 180

    # --- watch the whole market ---
    # OFF by default, and the numbers are the reason rather than caution. Measured on 21 liquid
    # pairs and ~1,000 daily bars, against 200 randomly drawn 8-coin lists, capped at 4 open
    # either way and scored in R: watching everything beat 96% of the random lists on the first
    # half of the history and 38% of them on the second. It was positive on both halves and
    # nowhere near the worst list either time.
    #
    # So it is worth having and it is not free money. What it reliably buys is that nobody has
    # to guess which coins to type in - a fixed list can do better than this and can do -11.3R,
    # and there is no way to know in advance which one you picked. See market/watchlist.py.
    # The server sweeps the whole market every ten minutes and publishes the result; the app
    # reads it instead of making 300 requests of its own. See market/feed.py. Falls back to
    # sweeping locally the moment anything is wrong with it, so this is a shortcut and never a
    # dependency - and it never carries an instruction, only prices, indicators and headlines.
    use_market_feed: bool = True
    market_feed_url: str = "http://91.107.163.109:40002/market.json"

    auto_symbols: bool = False
    auto_symbols_count: int = 4        # how many to hand the engine at a time
    # How many of the pairs that clear the liquidity floor get their charts read each sweep.
    # This is the SECOND cage and it used to undo the first: the floor decides which coins are
    # tradeable at all, and then this took only the most liquid 40 of them - so raising the
    # floor alone changed nothing and the sweep still saw the same famous names. Measured on the
    # live bybit spot market: 390 active USDT pairs, 260 clear the floor a $1,000 account gets,
    # and 40 was looking at the top sixth of those.
    #
    # The cost is one candle request per symbol, so this is a real trade-off on a home
    # connection - about a third of a second each, ~40s at 120. The server-side sweep is what
    # removes the trade-off rather than splitting the difference.
    auto_symbols_pool: int = 40
    auto_symbols_every_min: int = 60   # a sweep costs ~13s of requests; hourly on a daily chart
                                       # is already far more often than a daily bar changes

    # --- trading ---
    mode: str = "paper"           # paper | live
    market: str = "crypto"        # crypto | forex
    symbols: list[str] = field(default_factory=lambda: ["BTC/USDT", "ETH/USDT", "SOL/USDT", "BNB/USDT", "XRP/USDT"])
    # Where candles/prices come from: "auto" = the trading exchange, then public fallbacks
    # (mexc, kcex, gateio, htx, bitget) when it is blocked from the user's country.
    data_source: str = "auto"
    # none | system (OS proxy set by the VPN app) | manual (exchange.proxy URL)
    proxy_mode: str = "system"
    timeframe: str = "1d"
    loop_seconds: int = 60
    paper_start_balance: float = 100.0
    # forex via MetaTrader 5 (Windows only)
    mt5_login: int = 0
    mt5_password: str = ""
    mt5_server: str = ""

    risk: RiskSettings = field(default_factory=RiskSettings)
    exchange: ExchangeSettings = field(default_factory=ExchangeSettings)
    computer: ComputerSettings = field(default_factory=ComputerSettings)

    # --- misc ---
    language: str = "fa"
    telegram_bot_token: str = ""
    telegram_chat_id: str = ""

    # ---------------------------------------------------------------- autopilot
    # What autopilot decides, and why each one. Every value here is the one this project
    # MEASURED, with the evidence recorded beside the field above - so this table adds no new
    # opinion, it just stops asking the owner to retype conclusions the app already reached.
    AUTO_TOP = {
        "use_llm_for_decisions": True,   # the whole point: the model decides, not a fixed rule
        "aggressiveness": "normal",      # "high" and "scalp" both measured worse
        "timeframe": "1d",               # daily won every sweep this project has run
        "position_pct": 0.0,             # size from risk and the stop, not a flat percentage
        "align_with_leader": False,      # measured: costs return AND drawdown, earns neither
        "auto_symbols": True,            # watch everything rather than a list someone typed
        "use_market_feed": True,         # and let the server do the 300 requests
        "auto_symbols_count": 4,
        # Three times what the manual default is. Autopilot is the mode that was asked to
        # "search the whole market", and the whole market is 390 pairs of which ~260 are
        # reachable for a small account - 120 is as far as a home connection goes in a sweep
        # without the requests becoming the thing that breaks.
        "auto_symbols_pool": 120,
        "auto_symbols_every_min": 60,
    }
    AUTO_RISK = {
        "risk_per_trade": 0.01,
        "max_daily_loss": 0.03,
        "max_open_positions": 4,
        "max_position_frac": 0.25,       # 4 x 25% - the two numbers have to agree or the bot
        "max_open_risk": 0.06,           #   opens one trade and refuses every other
        "atr_stop_mult": 2.0,
        "reward_risk": 2.5,              # 2.0 is the only value that LOSES on a third of history
        "trail_after_r": 1.0,
        # Take half off at 1R. The default is OFF because, stated as a choice, it is a genuine
        # trade-off and the app refuses to make the owner's mind up for them. Autopilot's whole
        # job is to make it up: measured over four splits it raised the win rate in EVERY one
        # (39.6% -> 46.2% at worst) and cut the worst drawdown from 18.1R to 14.0R, for about
        # 12% of the return. The owner's stated goal is more winning trades than losing ones in
        # a month, so that is the side of the trade-off autopilot buys.
        "partial_take_r": 1.0,
        "partial_take_frac": 0.5,
    }
    # Managed settings that the window has no field for at all, so there is nothing to grey
    # out. Listed explicitly rather than left as a silent gap: the window's test compares
    # AUTO_TOP + AUTO_RISK against the fields it locks, and without this a real omission and a
    # setting that was never on screen look identical to it.
    AUTO_NO_FIELD = ("partial_take_frac", "auto_symbols_pool", "use_market_feed")

    # The two things autopilot must never take. Not because they are hard - because they are
    # not measurements. No backtest can say how much of someone's money they are willing to
    # lose, or whether today is the day to point this at a real exchange.
    AUTO_NEVER = ("capital_limit", "mode")

    def effective(self) -> "Settings":
        """The settings actually in force. With autopilot off this is self.

        A COPY, deliberately. The engine runs on what this returns and the settings page keeps
        showing and saving what the owner typed, so turning autopilot off restores their own
        numbers with nothing to undo.
        """
        if not self.autopilot:
            return self
        import copy
        s = copy.deepcopy(self)
        for k, v in self.AUTO_TOP.items():
            setattr(s, k, v)
        for k, v in self.AUTO_RISK.items():
            setattr(s.risk, k, v)
        return s

    def autopilot_managed(self, field: str) -> bool:
        """Is this field the bot's to set right now? Drives the greying-out in the window."""
        return bool(self.autopilot) and (field in self.AUTO_TOP or field in self.AUTO_RISK)

    # ---------------------------------------------------------------- io
    @classmethod
    def path(cls) -> Path:
        return data_dir() / "settings.json"

    @classmethod
    def load(cls) -> "Settings":
        p = cls.path()
        if not p.exists():
            s = cls()
            s.save()
            return s
        try:
            raw: dict[str, Any] = json.loads(p.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError, OSError):
            # Damaged beyond reading: keep it for inspection and start from the defaults rather
            # than refusing to open at all. Losing the settings is bad; losing the app is worse.
            try:
                p.replace(p.with_suffix(".json.broken"))
            except OSError:
                pass
            s = cls()
            s.save()
            return s
        return cls.from_dict(raw)

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "Settings":
        s = cls()
        for k, v in raw.items():
            if k == "risk" and isinstance(v, dict):
                s.risk = RiskSettings(**{a: b for a, b in v.items() if a in RiskSettings.__dataclass_fields__})
            elif k == "exchange" and isinstance(v, dict):
                s.exchange = ExchangeSettings(**{a: b for a, b in v.items() if a in ExchangeSettings.__dataclass_fields__})
            elif k == "computer" and isinstance(v, dict):
                s.computer = ComputerSettings(**{a: b for a, b in v.items() if a in ComputerSettings.__dataclass_fields__})
            elif k in cls.__dataclass_fields__:
                setattr(s, k, v)
        return s

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def save(self) -> None:
        """Write through a temporary file and rename over the original.

        Writing in place means a crash or a full disk mid-write leaves a truncated file, and
        load() then raises JSONDecodeError on every start - the app simply never opens again
        and the only fix is deleting a file the user does not know about. A rename is atomic."""
        p = self.path()
        tmp = p.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(self.to_dict(), indent=2, ensure_ascii=False), encoding="utf-8")
        try:
            os.chmod(tmp, 0o600)
        except OSError:
            pass
        os.replace(tmp, p)

    # ---------------------------------------------------------------- helpers
    def has_llm(self) -> bool:
        if self.ai_provider == "openai":
            return bool(self.openai_api_key or os.environ.get("OPENAI_API_KEY"))
        return bool(self.anthropic_api_key or os.environ.get("ANTHROPIC_API_KEY"))

    def has_claude(self) -> bool:
        return bool(self.anthropic_api_key or os.environ.get("ANTHROPIC_API_KEY"))

    def advisories(self) -> list[str]:
        """Settings that are legal but work against each other, in plain language.

        Deliberately NOT part of validate(): validate() decides whether the engine may start at
        all, and none of these are reasons to refuse. They are reasons the bot will quietly do
        less than the settings appear to promise - which is exactly what happened here: one
        position open for hours while every other entry was refused with a truncated message
        nobody could read.
        """
        out: list[str] = []
        if self.autopilot:
            # Every advisory below is "this number you chose disagrees with what we measured".
            # Under autopilot the bot chose the measured number itself, so there is nothing to
            # tell anyone - and a banner nagging about a field the owner no longer controls is
            # the exact complaint that produced autopilot in the first place.
            return out
        r = self.risk
        L = LABELS
        # Kept SHORT on purpose. These are shown in a banner across the top of the dashboard,
        # and the first version of the risk one ran to 330 characters - a paragraph in a strip
        # that is one or two lines tall. A warning that does not fit is a warning nobody reads.
        # Every one of them names the field and says what to set; the arithmetic lives in the
        # field's own help text on the settings page, where there is room for it.
        # A default that moves on evidence reaches NEW installs only. Settings.load() lays the
        # saved file over the defaults, so anyone who has ever opened the settings page keeps
        # the old number - and the most important trading change of the day never arrives at the
        # person it was measured for. Their settings are theirs and nothing here rewrites them;
        # saying nothing is not the alternative when the number is on hand.
        if r.reward_risk < 2.4:
            out.append(
                f"«نسبت سود به ضرر» شما {r.reward_risk:.1f} است. روی ۲۱ جفت و ~۱۰۰۰ کندل روزانه، "
                f"۲.۰ تنها مقداری بود که در یک‌سوم تاریخ ضرر داد؛ ۲.۵ در همان بازه سود داد. "
                f"(تنظیمات ← ریسک)")
        # The same shape, the other way round: a setting someone has deliberately turned up,
        # which does nothing at all for the built-in strategies.
        if abs(r.atr_stop_mult - 2.0) > 0.01:
            out.append(
                f"«حد ضرر (ATR ×)» روی {r.atr_stop_mult:.1f} است ولی برای استراتژی‌های داخلی "
                f"بی‌اثر است — هرکدام حد ضرر خودشان را می‌سازند. فقط مسیر هوش مصنوعی از آن "
                f"استفاده می‌کند.")
        if r.max_open_risk > 0 and r.risk_per_trade > r.max_open_risk:
            out.append(
                f"«{L['risk_per_trade']}» {r.risk_per_trade*100:.1f}٪ از «{L['max_open_risk']}» "
                f"{r.max_open_risk*100:.1f}٪ بیشتر است — بعد از اولین معامله بودجه تمام می‌شود. "
                f"روی {r.max_open_risk*100/3:.0f}٪ یا کمتر بگذار.")
        if r.max_position_frac > 0 and r.max_open_positions > 1:
            fits = int(1 / r.max_position_frac)
            if fits < r.max_open_positions:
                out.append(
                    f"«{L['max_open_positions']}» {r.max_open_positions} است ولی نقدینگی با "
                    f"«{L['max_position_frac']}» {r.max_position_frac*100:.0f}٪ فقط به {fits} "
                    f"پوزیشن می‌رسد. {100//max(r.max_open_positions,1)}٪ بگذار.")
        # A setting that looks like it controls risk and does not. Position size is the SMALLER
        # of "risk this fraction of capital" and "never exceed this fraction of capital as
        # notional". The second is a fraction of PRICE and the first a fraction of the STOP
        # DISTANCE, so the risk target can only ever bind if
        #     stop distance / price  >=  risk_per_trade / max_position_frac
        # Found on the owner's own machine: risk_per_trade 5% with max_position_frac 5% needs a
        # stop 100% of price away, so his "5% risk" was really 0.54% and nothing said so.
        if r.max_position_frac > 0 and r.risk_per_trade > 0:
            need = r.risk_per_trade / r.max_position_frac
            if need > 0.25:
                out.append(
                    f"«{L['risk_per_trade']}» {r.risk_per_trade*100:.1f}٪ اثری ندارد — "
                    f"«{L['max_position_frac']}» {r.max_position_frac*100:.1f}٪ زودتر می‌بندد. "
                    f"ریسک واقعی ≈ {r.max_position_frac*10:.2f}٪.")
        if len(self.symbols) > r.max_open_positions:
            out.append(f"{len(self.symbols)} نماد داری ولی «{L['max_open_positions']}» "
                       f"{r.max_open_positions} است — بقیه فقط بررسی می‌شوند.")
        return out

    def validate(self) -> list[str]:
        problems: list[str] = []
        if self.mode not in ("paper", "live"):
            problems.append("mode must be paper or live")
        if self.risk.capital_limit <= 0:
            problems.append("capital_limit must be positive")
        if not (0 < self.risk.risk_per_trade <= 0.1):
            problems.append("risk_per_trade must be between 0 and 0.1 (10%)")
        if not (0 < self.risk.max_daily_loss <= 0.5):
            problems.append("max_daily_loss must be between 0 and 0.5")
        if not (0 <= self.position_pct <= 100):
            problems.append("position_pct must be between 0 and 100")
        if self.risk.reward_risk <= 0:
            problems.append("reward_risk must be positive")
        if self.risk.partial_take_r < 0:
            problems.append("partial_take_r cannot be negative")
        if not (0.05 <= self.risk.partial_take_frac <= 0.95):
            problems.append("partial_take_frac must be between 0.05 and 0.95")
        if self.loop_seconds < 1:
            problems.append("loop_seconds must be at least 1")
        if self.auto_symbols:
            if not (1 <= self.auto_symbols_count <= 12):
                problems.append("auto_symbols_count must be between 1 and 12")
            if not (5 <= self.auto_symbols_pool <= 400):
                problems.append("auto_symbols_pool must be between 5 and 400")
            if self.auto_symbols_every_min < 5:
                problems.append("auto_symbols_every_min must be at least 5")
        if self.mode == "live" and self.market == "crypto" and not self.computer.enabled:
            if self.exchange.exchange_id.lower() in NO_API_EXCHANGES:
                problems.append(f"{self.exchange.exchange_id} has no trading API - enable screen control (Settings -> Screen control) for live orders")
            elif not (self.exchange.api_key and self.exchange.secret):
                problems.append("live crypto trading needs the exchange API key and secret")
        if not self.symbols:
            problems.append("at least one symbol is required")
        if self.mode == "live" and self.computer.enabled and not self.has_claude():
            problems.append("screen control needs the Claude (Anthropic) API key, whichever AI makes the decisions")
        return problems
