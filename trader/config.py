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


@dataclass
class RiskSettings:
    # Hard ceiling on the capital the bot may ever put to work (quote currency).
    capital_limit: float = 100.0
    # Fraction of the capital limit risked per trade (0.01 = 1%).
    risk_per_trade: float = 0.01
    # The day is over when this fraction of the capital limit is lost.
    max_daily_loss: float = 0.03
    # Never hold more than this many positions at once.
    max_open_positions: int = 5
    # Stop-loss distance in ATR multiples; take-profit as a multiple of the stop.
    atr_stop_mult: float = 2.0
    reward_risk: float = 2.0
    # Trailing stop kicks in once the trade is this many R in profit (0 = off).
    trail_after_r: float = 1.0
    # Largest single position as a fraction of the capital limit.
    max_position_frac: float = 0.5


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
    align_with_leader: bool = False

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
        raw: dict[str, Any] = json.loads(p.read_text(encoding="utf-8"))
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
        p = self.path()
        p.write_text(json.dumps(self.to_dict(), indent=2, ensure_ascii=False), encoding="utf-8")
        try:
            os.chmod(p, 0o600)
        except OSError:
            pass

    # ---------------------------------------------------------------- helpers
    def has_llm(self) -> bool:
        if self.ai_provider == "openai":
            return bool(self.openai_api_key or os.environ.get("OPENAI_API_KEY"))
        return bool(self.anthropic_api_key or os.environ.get("ANTHROPIC_API_KEY"))

    def has_claude(self) -> bool:
        return bool(self.anthropic_api_key or os.environ.get("ANTHROPIC_API_KEY"))

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
        if self.loop_seconds < 1:
            problems.append("loop_seconds must be at least 1")
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
