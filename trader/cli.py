"""Headless commands - the same code the GUI uses, runnable from a terminal.

  python -m trader.cli backtest BTC/USDT --tf 1h --bars 1500
  python -m trader.cli paper                # run the paper loop in the foreground
  python -m trader.cli ingest book.pdf      # add a document to the library
  python -m trader.cli learn 3              # extract skills from document id 3 with Claude
  python -m trader.cli skills               # list skills
  python -m trader.cli approve 12 / disable 12
  python -m trader.cli teach "..."          # one teaching message
"""
from __future__ import annotations

import argparse
import json
import sys
import time

from .config import Settings
from .db import Database


def cmd_backtest(a):
    from .market.data import MarketData
    from .backtest.engine import run_backtest
    s = Settings.load()
    if a.tf:
        s.timeframe = a.tf
    md = MarketData(s)
    df = md.candles(a.symbol, s.timeframe, limit=a.bars)
    from .backtest.engine import engine_params
    res = run_backtest(a.symbol, df, s.risk, start_equity=s.risk.capital_limit, allow_short=a.short,
                       **engine_params(s))
    st = res.stats()
    print(json.dumps(st, indent=2))
    if a.trades:
        for t in res.trades:
            print(f"{t.strategy:18} {t.side:5} in={t.entry:.6g} out={t.exit:.6g} pnl={t.pnl:+.4f} R={t.r:+.2f}  {t.reason}")


def cmd_paper(a):
    from .engine import Engine
    s = Settings.load()
    s.mode = "paper"
    db = Database()
    brain = None
    if s.use_llm_for_decisions and s.has_llm():
        from .brain import make_brain
        brain = make_brain(s)
    eng = Engine(s, db, brain=brain, on_event=print)
    eng.start()
    print("paper engine running - Ctrl+C to stop")
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        eng.stop()


def cmd_ingest(a):
    from .knowledge.ingest import ingest_pdf, ingest_url, ingest_text
    db = Database()
    src = a.source
    if src.lower().startswith("http"):
        doc_id, text = ingest_url(db, src)
    elif src.lower().endswith(".pdf"):
        doc_id, text = ingest_pdf(db, src)
    else:
        doc_id, text = ingest_text(db, src, open(src, encoding="utf-8", errors="ignore").read(), kind="text", origin=src)
    print(f"document {doc_id} added: {len(text)} chars")


def cmd_learn(a):
    from .brain import make_brain as Brain
    from .knowledge.skills import add_extracted
    s = Settings.load()
    db = Database()
    doc = db.one("SELECT * FROM knowledge_docs WHERE id=?", (a.doc_id,))
    if not doc:
        sys.exit("no such document")
    text = "\n\n".join(db.doc_chunks(a.doc_id))
    skills = Brain(s).extract_skills(doc["title"], text)
    n = add_extracted(db, skills, source=f"{doc['kind']}:{doc['title']}")
    print(f"{len(skills)} rules found, {n} new skills saved as draft - review and approve them")


def cmd_skills(a):
    from .knowledge.skills import load_seed_skills
    db = Database()
    load_seed_skills(db)
    for r in db.skills():
        print(f"{r['id']:4} {r['status']:9} {r['category']:11} {r['name']}")


def cmd_status(a):
    db = Database()
    st = a.status
    for sid in a.ids:
        db.set_skill_status(sid, st)
    print("ok")


def cmd_teach(a):
    from .brain import make_brain as Brain
    from .knowledge.skills import add_extracted, active_skills
    s = Settings.load()
    db = Database()
    reply, skills = Brain(s).teach_chat([{"role": "user", "content": a.message}], active_skills(db))
    print(reply)
    if skills:
        n = add_extracted(db, skills, source="user", status="approved")
        print(f"{n} skill(s) saved")


def cmd_selftest(a):
    from .diagnostics import run_all
    s = Settings.load(); db = Database()
    rep = run_all(s, db, progress=lambda m: print("…", m), include_ai=not a.no_ai)
    print(rep.summary())


def cmd_docs(a):
    db = Database()
    for d in db.docs():
        print(f"{d['id']:4} {d['kind']:5} {d['chars']:8} {d['title']}")


def main(argv=None):
    p = argparse.ArgumentParser(prog="tgtrader")
    sub = p.add_subparsers(dest="cmd", required=True)
    b = sub.add_parser("backtest"); b.add_argument("symbol"); b.add_argument("--tf"); b.add_argument("--bars", type=int, default=1000)
    b.add_argument("--trades", action="store_true"); b.add_argument("--no-short", dest="short", action="store_false"); b.set_defaults(fn=cmd_backtest)
    sub.add_parser("paper").set_defaults(fn=cmd_paper)
    i = sub.add_parser("ingest"); i.add_argument("source"); i.set_defaults(fn=cmd_ingest)
    l = sub.add_parser("learn"); l.add_argument("doc_id", type=int); l.set_defaults(fn=cmd_learn)
    sub.add_parser("skills").set_defaults(fn=cmd_skills)
    sub.add_parser("docs").set_defaults(fn=cmd_docs)
    st = sub.add_parser("selftest"); st.add_argument("--no-ai", action="store_true"); st.set_defaults(fn=cmd_selftest)
    for st in ("approve", "disable", "draft"):
        c = sub.add_parser(st); c.add_argument("ids", type=int, nargs="+")
        c.set_defaults(fn=cmd_status, status={"approve": "approved", "disable": "disabled", "draft": "draft"}[st])
    t = sub.add_parser("teach"); t.add_argument("message"); t.set_defaults(fn=cmd_teach)
    a = p.parse_args(argv)
    a.fn(a)


if __name__ == "__main__":
    main()
