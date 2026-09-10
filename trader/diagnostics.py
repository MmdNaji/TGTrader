"""Self-test: runs every subsystem on the user's machine and builds a readable report.
Secrets never enter the report - only whether a key is present.
"""
from __future__ import annotations

import platform
import sys
import time
import traceback
from dataclasses import dataclass, field, asdict
from typing import Any, Callable

from . import __version__, updater
from .config import Settings
from .db import Database


@dataclass
class Check:
    name: str
    ok: bool
    detail: str = ""
    ms: int = 0
    data: dict[str, Any] = field(default_factory=dict)


@dataclass
class Report:
    version: str
    started_at: float
    platform: str
    frozen: bool
    settings: dict[str, Any]
    checks: list[Check] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def summary(self) -> str:
        ok = sum(1 for c in self.checks if c.ok)
        lines = [f"TGTrader {self.version} self-test: {ok}/{len(self.checks)} passed  ({self.platform}{', exe' if self.frozen else ', source'})"]
        for c in self.checks:
            lines.append(f"{'✓' if c.ok else '✗'} {c.name} [{c.ms} ms] {c.detail}")
        return "\n".join(lines)


def _safe_settings(s: Settings) -> dict[str, Any]:
    """Configuration facts without any secret values."""
    return {
        "ai_provider": s.ai_provider, "model": s.model, "openai_model": s.openai_model, "effort": s.effort,
        "has_claude_key": bool(s.anthropic_api_key), "has_openai_key": bool(s.openai_api_key),
        "use_llm": s.use_llm_for_decisions, "mode": s.mode, "market": s.market,
        "exchange": s.exchange.exchange_id, "has_exchange_key": bool(s.exchange.api_key), "proxy_mode": getattr(s, "proxy_mode", "system"), "proxy_set": bool(s.exchange.proxy), "data_source": getattr(s, "data_source", "auto"),
        "symbols": s.symbols, "timeframe": s.timeframe, "loop_seconds": s.loop_seconds,
        "risk": asdict(s.risk), "computer_enabled": s.computer.enabled, "computer_confirm": s.computer.confirm_before_submit,
        "computer_notes_len": len(s.computer.exchange_notes or ""), "auto_update": s.auto_update,
    }


def run_all(settings: Settings, db: Database, progress: Callable[[str], None] | None = None,
            include_ai: bool = True) -> Report:
    say = progress or (lambda m: None)
    rep = Report(version=__version__, started_at=time.time(),
                 platform=f"{platform.system()} {platform.release()} / Python {sys.version.split()[0]}",
                 frozen=updater.is_frozen(), settings=_safe_settings(settings))
    ctx: dict[str, Any] = {}

    def run(name: str, fn: Callable[[], str | tuple[str, dict[str, Any]]]):
        say(name)
        t0 = time.time()
        try:
            out = fn()
            detail, data = (out if isinstance(out, tuple) else (out, {}))
            rep.checks.append(Check(name, True, detail, int((time.time() - t0) * 1000), data))
        except Exception as exc:
            tb = traceback.format_exc(limit=2).strip().splitlines()[-1]
            rep.checks.append(Check(name, False, f"{exc} | {tb}"[:600], int((time.time() - t0) * 1000)))

    # ------------------------------------------------------------ network
    def c_net():
        from .net import probe
        info = probe(settings)
        return (f"via {info['proxy']}: ip {info['ip']} {info['country']} {info.get('city','')} ({info.get('org','')})", info)
    run("اتصال اینترنت / پروکسی", c_net)

    # ------------------------------------------------------------ basics
    def c_settings():
        problems = settings.validate()
        if problems:
            raise RuntimeError("; ".join(problems))
        return "settings valid"
    run("تنظیمات معتبر", c_settings)

    def c_db():
        n = len(db.skills()); db.log("selftest", "info")
        return f"{n} skills, journal write ok"
    run("پایگاه داده", c_db)

    # ------------------------------------------------------------ market data
    def c_market():
        from .market.data import MarketData
        from .market.indicators import enrich, snapshot
        from .strategy.regime import detect_regime
        md = MarketData(settings); sym = settings.symbols[0]
        df = enrich(md.candles(sym, settings.timeframe, limit=400))
        px = md.price(sym); ctx["df"], ctx["sym"], ctx["md"] = df, sym, md
        snap = snapshot(df); ctx["snap"] = snap; ctx["regime"] = detect_regime(df)
        src = md.active_source or settings.exchange.exchange_id
        return (f"source {src}: {sym} {settings.timeframe}: {len(df)} bars, price {px:g}, regime {ctx['regime']}, RSI {snap.get('rsi14')}"
                + (f" | {md.notice}" if md.notice else ""), {"bars": len(df), "price": px, "regime": ctx["regime"], "source": src})
    run(f"داده‌ی بازار ({settings.exchange.exchange_id})", c_market)

    def c_kcex():
        from .market.kcex import KcexData
        from .net import resolve_proxy
        k = KcexData(proxy=resolve_proxy(settings) or "")
        px = k.price("BTC/USDT"); df = k.candles("BTC/USDT", "1d", 50)
        return f"KCEX BTC {px:g}, {len(df)} daily bars, {len(k.symbols())} symbols"
    run("داده‌ی KCEX", c_kcex)

    # ------------------------------------------------------------ strategies + risk + backtest
    def c_rules():
        from .strategy.builtin import evaluate_all
        sigs = evaluate_all(ctx["sym"], ctx["df"], ctx["regime"]); ctx["sigs"] = sigs
        return f"{len(sigs)} signal(s) now: " + (", ".join(f"{s.strategy}:{s.side}" for s in sigs) or "none (normal)")
    run("قوانین پایه", c_rules)

    def c_risk():
        from .risk.manager import RiskManager
        rm = RiskManager(settings.risk, db, settings.mode)
        atr = ctx["snap"].get("atr14") or 1.0; px = float(ctx["df"]["close"].iloc[-1])
        sz = rm.size("long", px, settings.risk.atr_stop_mult * atr, settings.risk.capital_limit)
        if sz is None:
            raise RuntimeError(f"capital limit {settings.risk.capital_limit} too small for {ctx['sym']} at {px:g}")
        return f"qty {sz.qty:.6g}, risk {sz.risk_amount:.4f}, stop {sz.stop_price:.6g}, tp {sz.take_profit:.6g}, kill={rm.kill_switch_on()}"
    run("مدیریت ریسک", c_risk)

    def c_backtest():
        from .backtest.engine import run_backtest
        st = run_backtest(ctx["sym"], ctx["df"], settings.risk, start_equity=settings.risk.capital_limit).stats()
        return (f"{st['trades']} trades, return {st['return_pct']}%, PF {st['profit_factor']}, avgR {st['avg_r']}", st)
    run("بک‌تست", c_backtest)

    def c_paper():
        from .execution.paper import PaperBroker
        pb = PaperBroker(100.0)
        return f"paper cash {pb.cash():.4f}, open paper positions {len(pb.positions())}"
    run("حساب کاغذی", c_paper)

    # ------------------------------------------------------------ AI
    if include_ai:
        def c_claude():
            if not settings.anthropic_api_key:
                raise RuntimeError("no Claude key")
            from .brain.claude import Brain
            return "Claude replied: " + Brain(settings).ping()
        run("اتصال Claude", c_claude)

        def c_openai():
            if not settings.openai_api_key:
                raise RuntimeError("no OpenAI key")
            from .brain.openai_brain import OpenAIBrain
            return f"OpenAI ({settings.openai_model}) replied: " + OpenAIBrain(settings).ping()
        run("اتصال OpenAI", c_openai)

        def c_decide():
            from .brain import make_brain
            from .knowledge.skills import skills_prompt_block
            brain = make_brain(settings)
            sigs = [{"strategy": s.strategy, "side": s.side, "strength": s.strength, "reason": s.reason} for s in ctx.get("sigs", [])]
            d = brain.decide(ctx["sym"], ctx["snap"], ctx["regime"], sigs, None, skills_prompt_block(db), [])
            return (f"{settings.ai_provider}: {d['action']} conf {d['confidence']:.2f}, stop {d['stop_distance_atr']} ATR, "
                    f"skills {len(d.get('skills_used', []))} - {d['reason'][:160]}", d)
        run(f"تصمیم واقعی هوش مصنوعی ({settings.ai_provider})", c_decide)

        def c_learn():
            from .brain import make_brain
            text = ("Rule one: never risk more than two percent of equity on a single trade. "
                    "Rule two: in an uptrend, buy pullbacks to the 20-day moving average when RSI is below 55. "
                    "Rule three: if a breakout closes back inside the range within two bars, exit immediately.")
            sk = make_brain(settings).extract_skills("selftest text", text)
            return f"{len(sk)} rules extracted: " + "; ".join(s["name"] for s in sk)[:200]
        run("یادگیری از متن", c_learn)

        def c_teach():
            from .brain import make_brain
            from .knowledge.skills import active_skills
            reply, sk = make_brain(settings).teach_chat(
                [{"role": "user", "content": "سلام. فقط تأیید کن که این پیام را دریافت کردی و هیچ مهارتی نساز."}], active_skills(db))
            return f"reply: {reply[:120]} | skills proposed: {len(sk)}"
        run("چت آموزشی", c_teach)

    # ------------------------------------------------------------ screen control prerequisites (no clicks)
    def c_screen():
        import pyautogui  # type: ignore
        from .execution.computer import Screen
        sc = Screen(settings.computer.max_screenshot_width)
        png = sc.screenshot_png()
        w, h = pyautogui.size()
        return f"screen {w}x{h}, screenshot {len(png)//1024} KB (scale {sc.scale:.2f}), failsafe on"
    run("پیش‌نیاز کنترل صفحه", c_screen)

    # ------------------------------------------------------------ update
    def c_update():
        rel = updater.check()
        return f"repo {updater.UPDATE_REPO}: " + (f"newer {rel.version} available" if rel else "up to date")
    run("بررسی به‌روزرسانی", c_update)

    return rep
