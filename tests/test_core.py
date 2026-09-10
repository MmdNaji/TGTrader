"""Offline tests: indicators, regime, strategies, risk sizing, paper broker, backtest, knowledge, skills, engine loop."""
from __future__ import annotations

import os
import tempfile
import time
from pathlib import Path

import pytest
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
    reasons = {t.reason.rsplit("-> ", 1)[-1] for t in res.trades}
    for t in res.trades:
        assert t.exit > 0
        assert t.reason.rsplit("-> ", 1)[-1] in ("stop", "target", "regime flipped"), t.reason
    # the engine closes a trend trade when the regime turns against it; the backtest models a
    # different system if it does not, so prove that exit really fires here
    assert "regime flipped" in reasons


def test_the_backtest_sits_out_after_a_stop_like_the_engine():
    """The engine refuses a symbol for COOLDOWN_BARS after a stop-out. Without the same rule
    the backtest re-enters the losing idea on the next bar and reports a trade count the engine
    will never produce."""
    df = synth(1200, seed=11)
    risk = RiskSettings(capital_limit=1000)
    none = run_backtest("X/Y", df, risk, start_equity=1000, cooldown_bars=0)
    two = run_backtest("X/Y", df, risk, start_equity=1000, cooldown_bars=2)
    assert len(two.trades) < len(none.trades), "the cooldown must actually remove entries"
    # and after every stop, the next entry is at least two bars later
    stops = [t for t in two.trades if t.reason.endswith("stop")]
    assert stops, "the sample should contain at least one stop-out"
    for st in stops:
        after = [t for t in two.trades if t.entry_i > st.exit_i]
        if after:
            assert min(t.entry_i for t in after) >= st.exit_i + 2


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
    assert updater.configured() and updater.UPDATE_URL.startswith("http")
    updater.mark_attempt("9.9.9"); assert updater.attempted_recently("9.9.9") and not updater.attempted_recently("1.0.0")


def test_kcex_symbol_and_intervals():
    from trader.market.kcex import kcex_symbol, INTERVALS
    assert kcex_symbol("BTC/USDT") == "BTC_USDT" and kcex_symbol("eth/usdt:usdt") == "ETH_USDT"
    assert INTERVALS["1d"][0] == "Day1" and INTERVALS["1h"][1] == 3600
    s = Settings(); s.mode = "live"; s.exchange.exchange_id = "kcex"; s.computer.enabled = False; s.anthropic_api_key = "k"
    assert any("has no trading API" in p for p in s.validate())
    s.computer.enabled = True
    assert not any("has no trading API" in p for p in s.validate())


def test_provider_selection_and_chart_widget():
    from trader.brain import make_brain
    s = Settings(); s.ai_provider = "openai"; s.openai_api_key = "x"
    assert make_brain(s).name == "openai" and s.has_llm() and not s.has_claude()
    s.mode = "live"; s.computer.enabled = True
    assert any("Claude" in p for p in s.validate())
    import os as _os
    _os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication
    from PySide6.QtGui import QImage
    from trader.gui.chart import CandleChart
    app = QApplication.instance() or QApplication([])
    ch = CandleChart(); ch.resize(900, 500)
    ch.set_data(enrich(synth(300)), "X/Y", "1h", {"side": "long", "entry_price": 100.0, "stop_price": 95.0, "take_profit": 110.0},
                [{"side": "long", "opened_at": 0, "entry_price": 1, "closed_at": 0, "exit_price": 1}])
    img = QImage(900, 500, QImage.Format_ARGB32); ch.render(img)
    assert img.pixelColor(450, 200).isValid()   # rendered without raising


def test_proxy_policy():
    from trader import net
    s = Settings(); s.proxy_mode = "none"; s.exchange.proxy = "http://127.0.0.1:1"
    assert net.resolve_proxy(s) is None and net.ccxt_proxy_params(s) == {}
    s.proxy_mode = "manual"
    assert net.resolve_proxy(s) == "http://127.0.0.1:1" and net.ccxt_proxy_params(s)["httpsProxy"] == "http://127.0.0.1:1"
    s.exchange.proxy = "socks5://127.0.0.1:2"
    assert net.ccxt_proxy_params(s) == {"socksProxy": "socks5://127.0.0.1:2"}
    s.proxy_mode = "system"; s.exchange.proxy = ""
    assert net.resolve_proxy(s) in (None,) or isinstance(net.resolve_proxy(s), str)


def test_engine_one_decision_per_bar():
    s = Settings(); s.mode = "paper"; s.symbols = ["X/Y"]; s.use_llm_for_decisions = False
    s.risk.capital_limit = 1000
    db = Database(Path(os.environ["TGTRADER_HOME"]) / "t4.db")
    pb = PaperBroker(1000); pb.reset(1000)
    eng = Engine(s, db, broker=pb)
    df = enrich(synth(400, seed=7))
    # feed the SAME frame (same last bar) twice; with no signals the second pass must be skipped
    from trader.strategy.builtin import evaluate_all as _ev
    from trader.strategy.regime import detect_regime as _reg
    quiet = None
    for i in range(200, len(df)):
        w = df.iloc[: i + 1]
        if not _ev("X/Y", w, _reg(w)):
            quiet = w; break
    assert quiet is not None
    eng.last_prices["X/Y"] = float(quiet["close"].iloc[-1])
    before = len(db.recent_decisions(999))
    eng._consider_entry("X/Y", quiet, float(quiet["close"].iloc[-1]), [])
    eng._consider_entry("X/Y", quiet, float(quiet["close"].iloc[-1]), [])  # same bar again -> skipped
    after = len(db.recent_decisions(999))
    assert after - before <= 1


# --------------------------------------------------------------------------- money-path fixes
def _engine(name: str, symbols=("X/Y",), balance=1000.0):
    s = Settings(); s.mode = "paper"; s.symbols = list(symbols); s.use_llm_for_decisions = False
    s.risk.capital_limit = balance; s.paper_start_balance = balance
    db = Database(Path(os.environ["TGTRADER_HOME"]) / name)
    pb = PaperBroker(balance); pb.reset(balance)
    eng = Engine(s, db, broker=pb)
    eng.market = FakeMarket(synth(900, seed=5))
    return s, db, pb, eng


def test_paper_broker_never_opens_a_position_on_a_close():
    pb = PaperBroker(1000); pb.reset(1000)
    try:
        pb.market_order("X/Y", "sell", 1.0, 100.0, close=True)
        assert False, "closing a position the broker does not hold must raise"
    except RuntimeError as exc:
        assert "no open" in str(exc)
    assert pb.positions() == {} and pb.cash() == 1000     # and it must not have moved any money


def test_close_charges_both_fees_and_measures_r_from_the_original_stop():
    s, db, pb, eng = _engine("t_fees.db")
    fill = pb.market_order("X/Y", "buy", 1.0, 100.0)
    tid = db.open_trade("paper", "X/Y", "long", fill.qty, fill.price, 95.0, 110.0, "t", "r",
                        entry_fee=fill.fee)
    # the stop has since been trailed up to break-even; R must still be measured from 95
    db.update_stop(tid, 100.0)
    pos = dict(db.open_trades("paper")[0])
    assert eng.close_position(pos, 110.0, "target")
    row = dict(db.closed_trades("paper")[0])
    exit_fill_price = 110.0 * (1 - pb.slippage)
    gross = (exit_fill_price - fill.price) * fill.qty
    exit_fee = fill.qty * exit_fill_price * pb.fee_rate
    assert abs(row["pnl"] - (gross - exit_fee - fill.fee)) < 1e-9
    assert row["pnl"] < gross                                   # fees really were charged
    # R from the ORIGINAL stop (~5 wide), not from the trailed one (0 wide -> infinite R)
    assert 1.0 < row["r_multiple"] < 3.0


def test_a_second_close_cannot_rewrite_the_pnl():
    s, db, pb, eng = _engine("t_double.db")
    fill = pb.market_order("X/Y", "buy", 1.0, 100.0)
    db.open_trade("paper", "X/Y", "long", fill.qty, fill.price, 95.0, 110.0, "t", "r", entry_fee=fill.fee)
    pos = dict(db.open_trades("paper")[0])
    assert eng.close_position(pos, 110.0, "target")
    first = dict(db.closed_trades("paper")[0])["pnl"]
    assert eng.close_position(pos, 50.0, "target") is False    # the row is no longer open
    assert dict(db.closed_trades("paper")[0])["pnl"] == first
    assert len(db.closed_trades("paper")) == 1


def test_stop_is_tested_against_the_live_price_not_the_whole_bar():
    s, db, pb, eng = _engine("t_bar.db")
    fill = pb.market_order("X/Y", "buy", 1.0, 100.0)
    db.open_trade("paper", "X/Y", "long", fill.qty, fill.price, 90.0, 130.0, "t", "r", entry_fee=fill.fee)
    pos = dict(db.open_trades("paper")[0])
    df = enrich(synth(400, seed=11))
    # a bar whose LOW is far below the stop, while the live price is comfortably above it
    df = df.copy()
    df.iloc[-1, df.columns.get_loc("low")] = 50.0
    df.iloc[-1, df.columns.get_loc("high")] = 200.0
    closed = eng._manage(pos, df, 100.0)
    assert closed is False and db.open_trades("paper"), "the in-progress bar must not trigger the stop"
    # the live price crossing it does close the trade
    assert eng._manage(dict(db.open_trades("paper")[0]), df, 89.0) is True


def test_a_position_stays_managed_after_its_symbol_leaves_the_settings():
    s, db, pb, eng = _engine("t_orphan.db", symbols=("A/B",))
    fill = pb.market_order("X/Y", "buy", 1.0, 100.0)
    db.open_trade("paper", "X/Y", "long", fill.qty, fill.price, 99.9, 100.1, "t", "r", entry_fee=fill.fee)
    assert "X/Y" not in s.symbols
    eng.loop_once()
    # the X/Y trade must have been managed and closed; A/B is free to open one of its own
    assert not [r for r in db.open_trades("paper") if r["symbol"] == "X/Y"], \
        "an open position must be managed even off the symbol list"
    assert dict(db.closed_trades("paper")[0])["symbol"] == "X/Y"


def test_one_broken_symbol_does_not_abandon_the_others():
    s, db, pb, eng = _engine("t_broken.db", symbols=("BAD/X", "X/Y"))
    good = FakeMarket(synth(900, seed=5))

    class Flaky:
        is_kcex = False
        def candles(self, symbol, timeframe=None, limit=400, max_age=20.0):
            if symbol == "BAD/X":
                raise RuntimeError("exchange unreachable")
            return good.candles(symbol, timeframe, limit, max_age)
        def price(self, symbol):
            return good.price(symbol)
    eng.market = Flaky()
    fill = pb.market_order("X/Y", "buy", 1.0, 100.0)
    db.open_trade("paper", "X/Y", "long", fill.qty, fill.price, 99.9, 100.1, "t", "r", entry_fee=fill.fee)
    eng.loop_once()                       # BAD/X raises first; X/Y must still be managed
    assert not [r for r in db.open_trades("paper") if r["symbol"] == "X/Y"]
    assert eng.status.get("error") == ""  # and the pass itself did not fail


def test_no_re_entry_on_the_same_bar_and_a_cooldown_after_a_stop():
    s, db, pb, eng = _engine("t_cool.db")
    fill = pb.market_order("X/Y", "buy", 1.0, 100.0)
    db.open_trade("paper", "X/Y", "long", fill.qty, fill.price, 99.0, 200.0, "t", "r", entry_fee=fill.fee)
    pos = dict(db.open_trades("paper")[0])
    assert eng.close_position(pos, 98.0, "stop")
    assert eng._cooldown["X/Y"] is not None, "a stop-out must start a cooldown"
    df = enrich(synth(400, seed=13))
    before = len(db.recent_decisions(9999))
    eng._consider_entry("X/Y", df, float(df["close"].iloc[-1]), [])
    assert len(db.recent_decisions(9999)) == before, "no evaluation at all while cooling down"


def test_a_model_failure_holds_instead_of_falling_back_to_the_raw_rules():
    s, db, pb, eng = _engine("t_brain.db")
    s.use_llm_for_decisions = True

    class DeadBrain:
        def decide(self, *a, **k):
            raise RuntimeError("api unreachable")
    eng.brain = DeadBrain()
    df = enrich(synth(900, seed=5))
    for i in range(300, len(df)):
        eng._consider_entry("X/Y", df.iloc[: i + 1], float(df["close"].iloc[i]), [])
    assert not db.open_trades("paper"), "a dead model must not silently trade a different system"
    assert any("model unavailable" in (d["reason"] or "") for d in db.recent_decisions(9999))


def test_total_open_risk_is_capped_by_its_own_setting():
    """max_open_risk is about simultaneous exposure; max_daily_loss is about losses already
    realised today. Conflating them (the first version of this did) costs about 9% of return,
    because a 3% cap turns every trade after the third into a token position."""
    rm = RiskManager(RiskSettings(capital_limit=1000, risk_per_trade=0.01, max_daily_loss=0.03,
                                  max_open_risk=0.06), None, "paper")
    budget = 0.06 * 1000
    opens = []
    for _ in range(20):
        sz = rm.size("long", 100.0, 2.0, 1000.0, open_positions=opens)
        if sz is None:
            break
        opens.append({"entry_price": 100.0, "init_stop": 98.0, "qty": sz.qty})
        assert rm.open_risk(opens) <= budget + 1e-9
    assert len(opens) >= 5, "a 6% cap at 1% risk per trade must allow around six positions"
    assert rm.size("long", 100.0, 2.0, 1000.0, open_positions=opens) is None
    # it refuses rather than handing back a token position that still pays a full round trip
    nearly_full = [{"entry_price": 100.0, "init_stop": 98.0, "qty": 29.0}]   # 58 of 60 used
    assert rm.size("long", 100.0, 2.0, 1000.0, open_positions=nearly_full) is None
    # and with the cap off it never interferes
    rm.risk.max_open_risk = 0.0
    assert rm.size("long", 100.0, 2.0, 1000.0, open_positions=opens) is not None


def test_the_day_boundary_is_exactly_utc_midnight():
    rm = RiskManager(RiskSettings(), None, "paper")
    d = rm.day_start()
    assert time.gmtime(d)[3:6] == (0, 0, 0)
    assert 0 <= time.time() - d < 86400


def test_a_target_that_does_not_clear_the_fees_is_refused():
    """The real reason a fast preset bleeds: it is right about direction and still loses,
    because the move it is aiming at is smaller than the fees on the way in and out."""
    s, db, pb, eng = _engine("t_feefilter.db")
    s.risk.reward_risk = 1.0                     # aiming at 1R, which fees eat at this stop width
    s.use_llm_for_decisions = True

    class TinyStopBrain:
        def decide(self, *a, **k):
            return {"action": "buy", "confidence": 0.99, "reason": "scalp it",
                    "stop_distance_atr": 0.001, "skills_used": []}
    eng.brain = TinyStopBrain()
    full = enrich(synth(900, seed=5))
    # the model is only consulted when there is something to look at, so use a trending window
    df = next(w for w in (full.iloc[: i + 1] for i in range(300, len(full)))
              if detect_regime(w) in ("trend_up", "trend_down"))
    price = float(df["close"].iloc[-1])
    eng._consider_entry("X/Y", df, price, [])
    assert not db.open_trades("paper")
    assert any("round-trip fee" in (d["reason"] or "") for d in db.recent_decisions(9999))
    # the same setup with a target that does clear the fees is allowed through
    s.risk.reward_risk = 3.0
    eng._last_bar.clear(); eng._entered_bar.clear(); eng._llm_bar.clear()
    eng._consider_entry("X/Y", df, price, [])
    assert db.open_trades("paper"), "a target that clears the fees must not be blocked"


def test_settings_round_trip_keeps_the_new_fields():
    s = Settings()
    assert s.align_with_leader is False, \
        "measured: the leader filter cost ~1.2% of return on 1d and changed nothing on 4h"
    s.align_with_leader = True; s.position_pct = 37.5
    s2 = Settings.from_dict(s.to_dict())
    assert s2.align_with_leader is True and s2.position_pct == 37.5
    # a settings file written by an older build has neither field and must still load
    raw = s.to_dict(); raw.pop("align_with_leader"); raw.pop("position_pct")
    s3 = Settings.from_dict(raw)
    assert s3.align_with_leader is False and s3.position_pct == 0.0
    bad = Settings(); bad.position_pct = 400
    assert any("position_pct" in p for p in bad.validate())


def test_the_leader_filter_refuses_a_long_while_bitcoin_is_breaking_down():
    s, db, pb, eng = _engine("t_lead.db", symbols=("ETH/USDT",))
    s.market = "crypto"; s.align_with_leader = True; s.use_llm_for_decisions = True

    class BuyBrain:
        def decide(self, *a, **k):
            return {"action": "buy", "confidence": 0.99, "reason": "up", "stop_distance_atr": 2.0,
                    "skills_used": []}
    eng.brain = BuyBrain()
    full = enrich(synth(900, seed=5))
    df = next(w for w in (full.iloc[: i + 1] for i in range(300, len(full)))
              if detect_regime(w) in ("trend_up", "trend_down"))
    price = float(df["close"].iloc[-1])
    eng._leader_regime = "trend_down"
    eng._consider_entry("ETH/USDT", df, price, [])
    assert not db.open_trades("paper")
    assert any("against the market leader" in (d["reason"] or "") for d in db.recent_decisions(9999))
    # unknown leader must mean "do not filter", never "refuse everything"
    eng._leader_regime = None
    eng._last_bar.clear(); eng._entered_bar.clear(); eng._llm_bar.clear()
    eng._consider_entry("ETH/USDT", df, price, [])
    assert db.open_trades("paper"), "a data hiccup on BTC must not stop every other symbol trading"


class FakeMt5:
    """Enough of the MetaTrader5 module to drive Mt5Broker without Windows."""
    ORDER_TYPE_BUY, ORDER_TYPE_SELL = 0, 1
    TRADE_ACTION_DEAL, ORDER_TIME_GTC, ORDER_FILLING_IOC = 1, 0, 1
    TRADE_RETCODE_DONE = 10009

    class _Obj:
        def __init__(self, **kw): self.__dict__.update(kw)

    def __init__(self, positions=()):
        self.sent = []
        self._positions = list(positions)

    def initialize(self, **kw): return True
    def last_error(self): return (0, "")
    def symbol_select(self, sym, on): return True
    def symbol_info(self, sym):
        return self._Obj(trade_contract_size=100000.0, volume_min=0.01, volume_step=0.01, volume_max=50.0)
    def symbol_info_tick(self, sym): return self._Obj(ask=1.1001, bid=1.0999)
    def account_info(self): return self._Obj(equity=10000.0, margin_free=9000.0)
    def positions_get(self, symbol=None): return tuple(self._positions)
    def history_deals_get(self, ticket=None): return (self._Obj(commission=-0.7, swap=0.0),)
    def order_send(self, req):
        self.sent.append(req)
        return self._Obj(retcode=self.TRADE_RETCODE_DONE, volume=req["volume"],
                         price=req["price"], order=1, deal=2)


@pytest.fixture
def mt5_module():
    """Put the fake in sys.modules and take it out again.

    requirements.txt installs the REAL MetaTrader5 on Windows, so leaving an instance of a fake
    behind under that name shadows a genuine package for every test that runs afterwards - and
    it is an instance, not a module, so the shadowing is not even type-correct."""
    import sys as _sys
    saved = _sys.modules.get("MetaTrader5", None)
    had = "MetaTrader5" in _sys.modules
    yield lambda fake: _sys.modules.__setitem__("MetaTrader5", fake)
    if had:
        _sys.modules["MetaTrader5"] = saved
    else:
        _sys.modules.pop("MetaTrader5", None)


def _mt5_broker(fake, install):
    from trader.execution.mt5_broker import Mt5Broker
    install(fake)
    return Mt5Broker(Settings())


def test_mt5_sizes_in_lots_while_the_engine_sizes_in_units(mt5_module):
    fake = FakeMt5()
    b = _mt5_broker(fake, mt5_module)
    # limits must come back in UNITS, or the risk manager compares 0.01 against 20000
    assert b.limits("EUR/USD") == (0.01 * 100000, 0.01 * 100000)
    fill = b.market_order("EUR/USD", "buy", 20000.0, 1.10)     # 20,000 units = 0.2 lots
    assert fake.sent[-1]["volume"] == pytest.approx(0.2), fake.sent[-1]["volume"]
    assert fill.qty == pytest.approx(20000.0), "the fill must be reported back in units"
    # rounding is always DOWN, so the step can never make an order bigger than intended
    b.market_order("EUR/USD", "buy", 25900.0, 1.10)
    assert fake.sent[-1]["volume"] == pytest.approx(0.25)
    # below the broker's minimum is refused, not silently rounded up
    with pytest.raises(RuntimeError, match="minimum"):
        b.market_order("EUR/USD", "buy", 500.0, 1.10)


def test_mt5_closes_by_ticket_not_by_an_opposite_deal(mt5_module):
    pos = FakeMt5._Obj(magic=777001, ticket=555, volume=0.2)
    fake = FakeMt5(positions=[pos])
    b = _mt5_broker(fake, mt5_module)
    b.market_order("EUR/USD", "sell", 20000.0, 1.10, close=True)
    req = fake.sent[-1]
    assert req.get("position") == 555, "a hedging account needs the ticket, or this opens a short"
    assert req["volume"] == pytest.approx(0.2)
    # nothing open -> refuse, never open the other side
    empty = FakeMt5()
    b2 = _mt5_broker(empty, mt5_module)
    with pytest.raises(RuntimeError, match="no open"):
        b2.market_order("EUR/USD", "sell", 20000.0, 1.10, close=True)
    assert not empty.sent


def test_the_screen_broker_survives_a_restart():
    from trader.execution.computer import ComputerBroker
    s = Settings(); s.risk.capital_limit = 500.0
    b = ComputerBroker(s, client=None, confirm=lambda _: True)
    b.reset(500.0)
    b._cash -= 100.0
    b._positions["X/Y"] = {"qty": 2.0, "price": 50.0}
    b._save()
    again = ComputerBroker(s, client=None, confirm=lambda _: True)
    assert again.cash() == 400.0 and again.positions() == {"X/Y": {"qty": 2.0, "price": 50.0}}, \
        "a restart used to forget the spend and then credit money that was never debited"
    again.reset(500.0)


def test_an_empty_candle_response_is_a_failure_not_a_source():
    """An exchange answering 200 with an empty list used to count as a working source: the empty
    frame was cached, the fallback chain never ran, and every caller died on .iloc[-1]."""
    import pandas as pd
    from trader.market.data import MarketData
    s = Settings(); s.proxy_mode = "none"; s.market = "crypto"
    md = MarketData.__new__(MarketData)
    md.settings = s; md._cache = {}; md.active_source = None; md.notice = ""
    md.on_notice = lambda m: None
    calls = []

    def try_sources(what, fn):
        # walk two sources the way the real chain does: the first is empty, the second works
        for src in ("emptyone", "goodone"):
            try:
                calls.append(src)
                return fn(src)
            except Exception:
                continue
        raise RuntimeError("no source")
    md._try_sources = try_sources

    good = pd.DataFrame({"open": [1.0, 2.0], "high": [1.0, 2.0], "low": [1.0, 2.0],
                         "close": [1.0, 2.0], "volume": [1.0, 1.0]},
                        index=pd.date_range("2025-01-01", periods=2, freq="h", tz="UTC"))

    class FakeEx:
        def __init__(self, rows): self.rows = rows
        def fetch_ohlcv(self, symbol, tf, limit=400): return self.rows
    md._ex = lambda src: FakeEx([] if src == "emptyone" else
                                [[1735689600000, 1, 1, 1, 1, 1], [1735693200000, 2, 2, 2, 2, 1]])
    out = md.candles("A/B", "1h", limit=10)
    assert calls == ["emptyone", "goodone"], "the empty source must not end the chain"
    assert len(out) == 2


def test_rsi_is_nan_while_it_is_warming_up():
    import numpy as np
    from trader.market.indicators import rsi
    out = rsi(pd.Series(np.random.default_rng(3).normal(100, 1, 60)))
    assert out.iloc[:13].isna().all(), "the warm-up used to report 100 - maximum overbought"
    assert out.iloc[20:].notna().all()
    # the genuine all-up and all-down cases still report the extremes rather than NaN
    assert rsi(pd.Series(np.arange(1, 60, dtype=float))).iloc[-1] > 99
    assert rsi(pd.Series(np.arange(60, 1, -1, dtype=float))).iloc[-1] < 1


def test_settings_survive_a_truncated_file():
    import json as _json
    from trader.config import Settings as S
    p = S.path()
    original = p.read_text(encoding="utf-8") if p.exists() else None
    try:
        s = S(); s.risk.capital_limit = 4321.0; s.save()
        assert _json.loads(p.read_text(encoding="utf-8"))["risk"]["capital_limit"] == 4321.0
        assert not p.with_suffix(".json.tmp").exists(), "the temp file must be renamed away"
        p.write_text('{"mode": "pap', encoding="utf-8")     # a crash mid-write
        back = S.load()                                      # must not raise
        assert back.mode == "paper" and p.with_suffix(".json.broken").exists()
    finally:
        p.with_suffix(".json.broken").unlink(missing_ok=True)
        if original is not None:
            p.write_text(original, encoding="utf-8")


def test_a_deleted_seed_skill_stays_deleted():
    db = Database(Path(os.environ["TGTRADER_HOME"]) / "t_seed.db")
    assert load_seed_skills(db) > 100
    row = next(r for r in db.skills() if str(r["source"]).startswith("seed:"))
    db.delete_skill(row["id"])
    assert not db.skill_exists(row["name"])
    assert load_seed_skills(db) == 0, "restarting used to put every deleted rule straight back"
    assert not db.skill_exists(row["name"])
    db.restore_seed_skill(row["name"])
    assert load_seed_skills(db) == 1 and db.skill_exists(row["name"])


class FakeCcxt:
    """Enough of a ccxt exchange to drive CcxtBroker without a network or an account."""

    def __init__(self, order):
        self.order = order
        self.sent = []

    def load_markets(self): return {}
    def amount_to_precision(self, symbol, qty): return f"{float(qty):.8f}"
    def create_order(self, symbol, type_, side, amount):
        self.sent.append((symbol, type_, side, amount))
        return dict(self.order, id="1")
    def fetch_order(self, oid, symbol): return self.order


def _ccxt_broker(order):
    from trader.execution.ccxt_broker import CcxtBroker
    b = CcxtBroker.__new__(CcxtBroker)
    b.settings = Settings()
    b.ex = FakeCcxt(order)
    return b


def test_ccxt_never_reports_a_fill_the_exchange_did_not_make():
    """Falling back to the requested amount opened a journal position that did not exist, and
    the next close then tried to sell coins that were never bought."""
    b = _ccxt_broker({"filled": 0, "status": "canceled", "average": None, "price": None})
    with pytest.raises(RuntimeError, match="no fill"):
        b.market_order("BTC/USDT", "buy", 0.5, 30000.0)


def test_ccxt_converts_a_base_currency_fee_into_money():
    """On a spot buy most exchanges charge the fee in the BASE asset, so fee.cost is a quantity
    of coins. Everything above this layer subtracts Fill.fee from a quote-currency P&L, so
    taking that number as dollars understated the cost by roughly the price."""
    quote_fee = _ccxt_broker({"filled": 0.5, "average": 30000.0,
                              "fee": {"cost": 15.0, "currency": "USDT"}})
    assert quote_fee.market_order("BTC/USDT", "buy", 0.5, 30000.0).fee == pytest.approx(15.0)

    base_fee = _ccxt_broker({"filled": 0.5, "average": 30000.0,
                             "fee": {"cost": 0.0005, "currency": "BTC"}})
    assert base_fee.market_order("BTC/USDT", "buy", 0.5, 30000.0).fee == pytest.approx(15.0)

    # a discount token is not a cost against this trade's quote balance, and is not guessed at
    bnb_fee = _ccxt_broker({"filled": 0.5, "average": 30000.0,
                            "fee": {"cost": 0.02, "currency": "BNB"}})
    assert bnb_fee.market_order("BTC/USDT", "buy", 0.5, 30000.0).fee == 0.0

    # no currency reported at all: assume the quote, which is what ccxt's unified format means
    bare = _ccxt_broker({"filled": 0.5, "average": 30000.0, "fee": {"cost": 15.0}})
    assert bare.market_order("BTC/USDT", "buy", 0.5, 30000.0).fee == pytest.approx(15.0)


def test_every_backtest_in_the_app_runs_the_same_system():
    """The Backtest page, the self-test page and the CLI each built their own argument list, so
    one app reported three different backtests for one set of settings - and the scalp preset
    was measured against a strategy list with no Scalp strategy in it."""
    import inspect
    from trader.backtest.engine import engine_params
    from trader.strategy.builtin import Scalp

    s = Settings(); s.aggressiveness = "scalp"; s.position_pct = 12.0
    p = engine_params(s)
    assert p["min_confidence"] == 0.0 and p["position_pct"] == 12.0
    assert any(isinstance(x, Scalp) for x in p["strategies"]), \
        "the scalp preset must be measured WITH the scalp strategy"

    s.aggressiveness = "normal"
    p = engine_params(s)
    assert p["min_confidence"] == 0.55
    assert not any(isinstance(x, Scalp) for x in p["strategies"])

    # every caller goes through the helper rather than spelling the arguments out again
    from trader.gui import app as gui
    from trader import diagnostics, cli
    for mod in (gui, diagnostics, cli):
        src = inspect.getsource(mod)
        if "run_backtest(" in src:
            assert "engine_params(" in src, f"{mod.__name__} builds its own backtest arguments"


def test_profit_factor_is_not_shown_as_infinity_on_a_tiny_sample():
    """The dashboard printed "∞" next to a 100% win rate after one trade. That reads as a
    flawless system; it means there is nothing to divide by yet."""
    import os as _os
    _os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from trader.gui.app import profit_factor
    assert profit_factor({"trades": 0, "profit_factor": 0.0}) == "—"
    assert profit_factor({"trades": 1, "profit_factor": float("inf")}) == "—"
    assert profit_factor({"trades": 40, "profit_factor": 1.83}) == "1.83"
    assert profit_factor({"trades": 40, "profit_factor": 0.6}) == "0.60"


def test_the_data_layer_gives_up_when_the_caller_is_shutting_down():
    """Six fallback sources at a 20-second timeout is two minutes of work nobody wants any
    more - and it is the reason a price feed could not be joined when the window closed."""
    from trader.market.data import MarketData
    s = Settings(); s.proxy_mode = "none"
    md = MarketData.__new__(MarketData)
    md.settings = s; md.active_source = None; md.notice = ""; md.on_notice = lambda m: None
    md.abort = lambda: False
    tried = []

    def fn(src):
        tried.append(src)
        raise RuntimeError("blocked")

    try:
        md._try_sources("X/Y", fn)
    except RuntimeError:
        pass
    walked = len(tried)
    assert walked >= 2, "the chain should normally try more than one source"

    tried.clear()
    md.abort = lambda: True
    try:
        md._try_sources("X/Y", fn)
        assert False, "an aborted lookup must raise, not return nothing"
    except RuntimeError as exc:
        assert "shutting down" in str(exc)
    assert tried == [], "it must not even try the first source once told to stop"


def test_engine_stop_can_wait_for_its_own_thread():
    """The loop thread is a daemon: a caller that stops the engine and lets the process go can
    cut it between placing a real exchange order and writing it to the journal."""
    import threading
    s, db, pb, eng = _engine("t_stop.db")
    eng.market = FakeMarket(synth(900, seed=5))
    eng.start()
    assert eng.running()
    assert eng.stop(wait=10.0) is True, "stop(wait) must return True once the thread has ended"
    assert not eng.running()
    assert not any(t.name == "engine" and t.is_alive() for t in threading.enumerate())


def test_the_open_risk_cap_judges_the_position_that_will_actually_be_traded():
    """It used to run before the notional and cash caps, so it compared against the raw
    risk_per_trade size. With a capital limit that caps the notional far below that, the number
    it judged was fiction: measured on the owner's own settings the real second position risked
    $28 of a $60 budget with $31 free, and was refused because the check thought it wanted $100.
    One position, for hours, and nothing said why."""
    r = RiskSettings(capital_limit=1000, risk_per_trade=0.10, max_open_risk=0.06,
                     max_daily_loss=0.045, max_position_frac=0.5, max_open_positions=20)
    rm = RiskManager(r, None, "paper")
    opens, cash = [], 1000.0
    for _ in range(6):
        sz = rm.size("short", 0.0838, 0.004776, 1000.0, cash=cash, open_positions=opens)
        if sz is None:
            break
        opens.append({"entry_price": 0.0838, "init_stop": 0.0886, "qty": sz.qty})
        cash -= sz.notional
    assert len(opens) >= 2, "a second position must fit inside the open-risk budget"
    assert rm.open_risk(opens) <= 0.06 * 1000 + 1e-9, "and the cap must still hold"
    # with settings that fit each other it fills up to the cap instead of stopping at one
    r2 = RiskSettings(capital_limit=1000, risk_per_trade=0.01, max_open_risk=0.06,
                      max_position_frac=0.2)
    rm2 = RiskManager(r2, None, "paper")
    opens2, cash2 = [], 1000.0
    for _ in range(10):
        sz = rm2.size("short", 0.0838, 0.004776, 1000.0, cash=cash2, open_positions=opens2)
        if sz is None:
            break
        opens2.append({"entry_price": 0.0838, "init_stop": 0.0886, "qty": sz.qty})
        cash2 -= sz.notional
    assert len(opens2) >= 5 and rm2.open_risk(opens2) <= 0.06 * 1000 + 1e-9


def test_no_dust_positions():
    """Once the cash is nearly spent the caps happily produce a $15 or a $0.40 position: a full
    round trip and two spreads to put a rounding error to work."""
    r = RiskSettings(capital_limit=1000, risk_per_trade=0.01, max_open_risk=0.5,
                     max_position_frac=0.5)
    rm = RiskManager(r, None, "paper")
    assert rm.size("long", 100.0, 2.0, 1000.0, cash=1000.0) is not None
    assert rm.size("long", 100.0, 2.0, 1000.0, cash=5.0) is None, "a $5 position is not a trade"


def test_ccxt_is_given_exactly_one_proxy_key():
    """ccxt counts how many of httpProxy/httpsProxy/socksProxy are set and REFUSES the request
    when more than one is. Setting both broke every ccxt exchange for anyone using a proxy, and
    left KCEX carrying every symbol alone until it started answering "Too Many Requests"."""
    from trader import net
    s = Settings(); s.proxy_mode = "manual"
    s.exchange.proxy = "http://127.0.0.1:10809"
    p = net.ccxt_proxy_params(s)
    assert len(p) == 1 and "httpsProxy" in p, p
    s.exchange.proxy = "socks5://127.0.0.1:10808"
    p = net.ccxt_proxy_params(s)
    assert len(p) == 1 and "socksProxy" in p, p
    s.proxy_mode = "none"
    assert net.ccxt_proxy_params(s) == {}


def test_every_fallback_source_exists_in_this_ccxt():
    """"gateio" was in the list and ccxt renamed it to "gate", so that entry was a guaranteed
    AttributeError in the middle of every fallback walk."""
    import ccxt
    from trader.market.data import FALLBACK_SOURCES, available_sources
    missing = [s for s in FALLBACK_SOURCES if s != "kcex" and not hasattr(ccxt, s)]
    assert not missing, f"these fallback sources do not exist in ccxt: {missing}"
    assert "kcex" in available_sources()


def test_settings_warn_when_the_risk_knobs_contradict_each_other():
    s = Settings()
    s.risk.capital_limit = 1000
    s.risk.risk_per_trade = 0.10        # $100 a trade
    s.risk.max_open_risk = 0.06         # but only $60 may be at risk at once
    s.risk.max_open_positions = 20
    s.risk.max_position_frac = 0.5      # and each position takes half the account
    advice = s.advisories()
    assert any("سقف ریسک همزمان" in p for p in advice), advice
    assert any("نقدینگی" in p for p in advice), advice
    # ...but an advisory must NEVER stop the engine starting - validate() is the blocker list
    assert not [p for p in s.validate() if "ریسک همزمان" in p or "نقدینگی" in p], \
        "a contradictory-but-legal setting is a warning, not a refusal to run"

    ok = Settings()
    ok.risk.capital_limit = 1000; ok.risk.risk_per_trade = 0.01
    ok.risk.max_open_risk = 0.06; ok.risk.max_open_positions = 5
    ok.risk.max_position_frac = 0.2
    ok.symbols = ["BTC/USDT"]
    assert ok.advisories() == [], ok.advisories()

    # and the SHIPPED defaults must not contradict themselves
    d = Settings(); d.symbols = d.symbols[: d.risk.max_open_positions]
    assert d.advisories() == [], f"the default settings fight each other: {d.advisories()}"


def test_a_position_is_still_managed_when_candles_are_unavailable():
    """Candles come from the kline endpoint and the price from the ticker endpoint; they fail
    independently, and a rate limit usually hits the heavier one first. Skipping the whole
    symbol meant a live position sat for hours with nobody watching its stop."""
    s, db, pb, eng = _engine("t_nocandles.db", symbols=("X/Y",))
    fill = pb.market_order("X/Y", "buy", 1.0, 100.0)
    db.open_trade("paper", "X/Y", "long", fill.qty, fill.price, 95.0, 110.0, "t", "r",
                  entry_fee=fill.fee)

    class KlinesDown:
        """The exact shape of the failure in the owner's log: klines rate-limited, ticker fine."""
        is_kcex = False
        price_now = 120.0
        def candles(self, symbol, timeframe=None, limit=400, max_age=20.0):
            raise RuntimeError("KCEX kline: Too Many Requests")
        def price(self, symbol):
            return self.price_now

    eng.market = KlinesDown()
    eng.loop_once()
    assert not db.open_trades("paper"), "the target was passed and nobody closed it"
    row = dict(db.closed_trades("paper")[0])
    assert row["exit_price"] > 100.0

    # and when even the price is gone, it says so instead of failing silently
    s2, db2, pb2, eng2 = _engine("t_noprice.db", symbols=("X/Y",))
    f2 = pb2.market_order("X/Y", "buy", 1.0, 100.0)
    db2.open_trade("paper", "X/Y", "long", f2.qty, f2.price, 95.0, 110.0, "t", "r", entry_fee=f2.fee)

    class AllDown(KlinesDown):
        def price(self, symbol):
            raise RuntimeError("no market-data source reachable")

    eng2.market = AllDown()
    eng2._unmanaged["X/Y"] = time.time() - 600      # ten minutes with no price
    eng2.loop_once()
    assert db2.open_trades("paper"), "it cannot close a position it has no price for"
    assert any("UNMANAGED" in (l["message"] or "") for l in db2.recent_journal(50)), \
        "a position nobody can watch must be reported, not skipped in silence"
