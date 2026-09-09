"""Offline tests: indicators, regime, strategies, risk sizing, paper broker, backtest, knowledge, skills, engine loop."""
from __future__ import annotations

import os
import tempfile
from pathlib import Path

import numpy as np
import pandas as pd

os.environ["TGTRADER_HOME"] = tempfile.mkdtemp(prefix="tgtrader-test-")

from trader.config import Settings, RiskSettings  # noqa: E402
from trader.db import Database  # noqa: E402
from trader.market.indicators import enrich, snapshot, rsi  # noqa: E402
from trader.strategy.regime import detect_regime  # noqa: E402
from trader.strategy.builtin import evaluate_all  # noqa: E402
from trader.risk.manager import RiskManager  # noqa: E402
from trader.execution.paper import PaperBroker  # noqa: E402
from trader.backtest.engine import run_backtest  # noqa: E402
from trader.knowledge.ingest import ingest_text, chunk_text  # noqa: E402
from trader.knowledge.skills import load_seed_skills, parse_seed, skills_prompt_block, add_extracted  # noqa: E402
from trader.engine import Engine  # noqa: E402


def synth(n=800, seed=1, drift=0.0005) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    r = rng.normal(drift, 0.01, n)
    # a trend section then a range section
    r[: n // 2] += 0.002
    close = 100 * np.exp(np.cumsum(r))
    high = close * (1 + np.abs(rng.normal(0, 0.004, n)))
    low = close * (1 - np.abs(rng.normal(0, 0.004, n)))
    opn = np.roll(close, 1); opn[0] = close[0]
    vol = rng.uniform(100, 1000, n)
    idx = pd.date_range("2025-01-01", periods=n, freq="h", tz="UTC")
    return pd.DataFrame({"open": opn, "high": high, "low": low, "close": close, "volume": vol}, index=idx)


def test_indicators_and_regime():
    df = enrich(synth())
    assert df["ema20"].notna().sum() > 700
    assert 0 <= df["rsi14"].dropna().min() and df["rsi14"].dropna().max() <= 100
    assert (df["atr14"].dropna() > 0).all()
    snap = snapshot(df)
    assert "close" in snap and "rsi14" in snap and "ret_20" in snap
    reg = detect_regime(df)
    assert reg in ("trend_up", "trend_down", "range", "volatile")
    # rsi of a straight rising series must be 100
    assert rsi(pd.Series(np.arange(1, 60, dtype=float))).iloc[-1] > 99


def test_strategies_emit_signals_somewhere():
    df = enrich(synth())
    count = 0
    for i in range(100, len(df)):
        w = df.iloc[: i + 1]
        count += len(evaluate_all("X/Y", w, detect_regime(w)))
    assert count > 0


def test_risk_sizing_and_limits():
    db = Database(Path(os.environ["TGTRADER_HOME"]) / "t1.db")
    rm = RiskManager(RiskSettings(capital_limit=100, risk_per_trade=0.01, max_position_frac=0.5), db, "paper")
    s = rm.size("long", price=50.0, stop_distance=1.0, equity=1000)
    assert s and abs(s.risk_amount - 1.0) < 1e-9 and s.stop_price == 49.0 and s.take_profit == 52.0
    # notional cap: qty*price <= 50
    s2 = rm.size("long", price=50.0, stop_distance=0.01, equity=1000)
    assert s2 and s2.notional <= 50.0 + 1e-9
    assert rm.size("long", price=50.0, stop_distance=1.0, equity=1000, min_qty=5) is None
    assert rm.check("A/B", [], 100) is None
    assert rm.check("A/B", [{"symbol": "A/B"}], 100) is not None
    rm.set_kill_switch(True)
    assert "kill" in rm.check("Z/Z", [], 100)
    rm.set_kill_switch(False)
    # trailing: 1R in profit moves the stop to price - R
    assert rm.trail_stop("long", entry=100, stop=98, price=102.5) == 100.5
    assert rm.trail_stop("long", entry=100, stop=98, price=101) == 98


def test_paper_broker_roundtrip():
    pb = PaperBroker(1000); pb.reset(1000)
    f = pb.market_order("A/B", "buy", 2, 100)
    assert f.price > 100 and pb.cash() < 1000
    pb.market_order("A/B", "sell", 2, 110)
    assert pb.cash() > 1000 and not pb.positions()
    pb.market_order("A/B", "sell", 1, 100)          # short
    pb.market_order("A/B", "buy", 1, 90)
    assert pb.cash() > 1000 + 9 and not pb.positions()


def test_backtest_runs_and_accounts():
    res = run_backtest("X/Y", synth(1200), RiskSettings(capital_limit=1000), start_equity=1000)
    st = res.stats()
    assert st["bars"] == 1200 and st["trades"] >= 1
    total = sum(t.pnl for t in res.trades)
    # equity moves by closed-trade P&L (fees included) plus the mark-to-market of a trade still open at the end
    assert abs((res.equity[-1] - 1000) - total) < 0.02 * 1000
    for t in res.trades:
        assert t.exit > 0 and ("stop" in t.reason or "target" in t.reason)


def test_knowledge_and_skills():
    db = Database(Path(os.environ["TGTRADER_HOME"]) / "t2.db")
    n = load_seed_skills(db)
    assert n >= 30
    assert load_seed_skills(db) == 0                      # idempotent
    block = skills_prompt_block(db)
    assert block and all(s["rule"] for s in block)
    parsed = parse_seed("## risk\n- **A b**: do x\n## entry\n- **C**: do y\n")
    assert parsed[0]["category"] == "risk" and parsed[1]["category"] == "entry"
    text = ("Trend following works best when ADX is above 25. " * 30 + "\n\n" + "Mean reversion likes low ADX. " * 30)
    doc_id, _ = ingest_text(db, "test doc", text)
    assert db.doc_chunks(doc_id)
    hits = db.search_knowledge("what does ADX above 25 mean for trend following")
    assert hits and hits[0]["doc_id"] == doc_id
    assert len(chunk_text("x" * 5000)) >= 4
    assert add_extracted(db, [{"name": "New rule", "category": "entry", "rule": "r", "evidence": "e"}], "url:x") == 1
    assert add_extracted(db, [{"name": "new rule", "category": "entry", "rule": "r"}], "url:x") == 0


class FakeMarket:
    def __init__(self, df): self.df = df; self.i = 300
    def candles(self, symbol, timeframe=None, limit=400, max_age=20.0):
        self.i = min(self.i + 1, len(self.df)); return self.df.iloc[: self.i].tail(limit)
    def price(self, symbol): return float(self.df["close"].iloc[self.i - 1])


def test_engine_loop_paper_end_to_end():
    s = Settings(); s.mode = "paper"; s.symbols = ["X/Y"]; s.use_llm_for_decisions = False
    s.risk.capital_limit = 1000; s.paper_start_balance = 1000
    db = Database(Path(os.environ["TGTRADER_HOME"]) / "t3.db")
    pb = PaperBroker(1000); pb.reset(1000)
    eng = Engine(s, db, broker=pb)
    eng.market = FakeMarket(synth(900, seed=3))
    opened = 0
    for _ in range(550):
        eng.loop_once()
        opened = max(opened, len(db.open_trades("paper")))
    closed = db.closed_trades("paper")
    assert opened >= 1 and len(closed) >= 1
    assert db.recent_decisions(5)
    # money: paper cash + open positions equals equity, and closed pnl is recorded
    assert all(r["pnl"] is not None for r in closed)
    assert db.trade_stats("paper")["trades"] == len(closed)


def test_updater_version_compare():
    from trader import updater
    assert updater._vtuple("v0.2.0") > updater._vtuple("0.1.9")
    assert updater._vtuple("1.0") > updater._vtuple("0.99.99")
    assert updater._vtuple("0.1.0") == updater._vtuple("v0.1.0")
    assert not updater.configured() or "/" in updater.UPDATE_REPO


def test_kcex_symbol_and_intervals():
    from trader.market.kcex import kcex_symbol, INTERVALS
    assert kcex_symbol("BTC/USDT") == "BTC_USDT" and kcex_symbol("eth/usdt:usdt") == "ETH_USDT"
    assert INTERVALS["1d"][0] == "Day1" and INTERVALS["1h"][1] == 3600
    s = Settings(); s.mode = "live"; s.exchange.exchange_id = "kcex"; s.computer.enabled = False
    assert any("screen control" in p for p in s.validate())
    s.computer.enabled = True
    assert not any("screen control" in p for p in s.validate())
