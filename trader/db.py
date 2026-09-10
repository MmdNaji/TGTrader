"""SQLite storage: trades, decisions, skills, knowledge (with FTS5 search)."""
from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Iterable

from .config import data_dir

SCHEMA = """
CREATE TABLE IF NOT EXISTS trades (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    mode TEXT NOT NULL,                -- paper | live | backtest
    symbol TEXT NOT NULL,
    side TEXT NOT NULL,                -- long | short
    qty REAL NOT NULL,
    entry_price REAL NOT NULL,
    stop_price REAL,
    init_stop REAL,                    -- the stop the trade OPENED with; R is measured from this
    entry_fee REAL,                    -- fee paid on entry, subtracted when the trade closes
    take_profit REAL,
    exit_price REAL,
    pnl REAL,
    r_multiple REAL,
    strategy TEXT,
    reason TEXT,
    opened_at REAL NOT NULL,
    closed_at REAL,
    status TEXT NOT NULL DEFAULT 'open'  -- open | closed
);
CREATE INDEX IF NOT EXISTS idx_trades_status ON trades(status);

CREATE TABLE IF NOT EXISTS decisions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    symbol TEXT NOT NULL,
    action TEXT NOT NULL,              -- buy | sell | hold | close
    confidence REAL,
    source TEXT,                       -- rules | llm | risk
    reason TEXT,
    payload TEXT                       -- JSON snapshot used for the decision
);

CREATE TABLE IF NOT EXISTS equity (
    ts REAL NOT NULL,
    mode TEXT NOT NULL,
    equity REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS seed_removed (
    name TEXT PRIMARY KEY,             -- lower-cased name of a shipped skill the owner deleted
    removed_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS skills (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    category TEXT NOT NULL,            -- risk | entry | exit | regime | psychology | market
    rule TEXT NOT NULL,                -- the skill itself, in plain language
    source TEXT,                       -- seed | book:<title> | url:<...> | user
    status TEXT NOT NULL DEFAULT 'draft',  -- draft | approved | disabled
    weight REAL NOT NULL DEFAULT 1.0,
    backtest TEXT,                     -- JSON result of the last backtest, if any
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS knowledge_docs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    title TEXT NOT NULL,
    kind TEXT NOT NULL,                -- pdf | url | text
    origin TEXT,
    chars INTEGER NOT NULL,
    added_at REAL NOT NULL
);

CREATE VIRTUAL TABLE IF NOT EXISTS knowledge_chunks USING fts5(
    doc_id UNINDEXED, seq UNINDEXED, content, tokenize='unicode61'
);

CREATE TABLE IF NOT EXISTS journal (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    level TEXT NOT NULL,
    message TEXT NOT NULL
);
"""


class Database:
    def __init__(self, path: Path | None = None):
        self.path = path or (data_dir() / "trader.db")
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(str(self.path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.executescript(SCHEMA)
            # migrate databases created before these columns existed
            for col in ("init_stop", "entry_fee"):
                try:
                    self._conn.execute(f"ALTER TABLE trades ADD COLUMN {col} REAL")
                except sqlite3.OperationalError:
                    pass          # already there
            self._conn.commit()

    # ------------------------------------------------------------ low level
    def execute(self, sql: str, params: Iterable[Any] = ()) -> sqlite3.Cursor:
        with self._lock:
            cur = self._conn.execute(sql, tuple(params))
            self._conn.commit()
            return cur

    def query(self, sql: str, params: Iterable[Any] = ()) -> list[sqlite3.Row]:
        with self._lock:
            return self._conn.execute(sql, tuple(params)).fetchall()

    def one(self, sql: str, params: Iterable[Any] = ()) -> sqlite3.Row | None:
        rows = self.query(sql, params)
        return rows[0] if rows else None

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # ------------------------------------------------------------ journal
    def log(self, message: str, level: str = "info") -> None:
        self.execute("INSERT INTO journal(ts, level, message) VALUES (?,?,?)", (time.time(), level, message))

    def recent_journal(self, limit: int = 200) -> list[sqlite3.Row]:
        return self.query("SELECT * FROM journal ORDER BY id DESC LIMIT ?", (limit,))

    # ------------------------------------------------------------ decisions
    def add_decision(self, symbol: str, action: str, confidence: float | None, source: str,
                     reason: str, payload: dict[str, Any] | None = None) -> int:
        cur = self.execute(
            "INSERT INTO decisions(ts, symbol, action, confidence, source, reason, payload) VALUES (?,?,?,?,?,?,?)",
            (time.time(), symbol, action, confidence, source, reason, json.dumps(payload or {}, ensure_ascii=False)),
        )
        return int(cur.lastrowid)

    def recent_decisions(self, limit: int = 50) -> list[sqlite3.Row]:
        return self.query("SELECT * FROM decisions ORDER BY id DESC LIMIT ?", (limit,))

    # ------------------------------------------------------------ trades
    def open_trade(self, mode: str, symbol: str, side: str, qty: float, entry: float,
                   stop: float | None, tp: float | None, strategy: str, reason: str,
                   entry_fee: float = 0.0) -> int:
        cur = self.execute(
            "INSERT INTO trades(mode, symbol, side, qty, entry_price, stop_price, init_stop, entry_fee,"
            " take_profit, strategy, reason, opened_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (mode, symbol, side, qty, entry, stop, stop, entry_fee, tp, strategy, reason, time.time()),
        )
        return int(cur.lastrowid)

    def close_trade(self, trade_id: int, exit_price: float, pnl: float, r_multiple: float | None) -> bool:
        """Close an OPEN trade. Returns False if it was already closed, so a double close
        (GUI and engine racing on the same row) cannot rewrite the P&L twice."""
        cur = self.execute(
            "UPDATE trades SET exit_price=?, pnl=?, r_multiple=?, closed_at=?, status='closed'"
            " WHERE id=? AND status='open'",
            (exit_price, pnl, r_multiple, time.time(), trade_id),
        )
        return cur.rowcount > 0

    def update_stop(self, trade_id: int, stop: float) -> None:
        self.execute("UPDATE trades SET stop_price=? WHERE id=?", (stop, trade_id))

    def open_trades(self, mode: str) -> list[sqlite3.Row]:
        return self.query("SELECT * FROM trades WHERE status='open' AND mode=? ORDER BY id", (mode,))

    def closed_trades(self, mode: str, limit: int = 200) -> list[sqlite3.Row]:
        return self.query("SELECT * FROM trades WHERE status='closed' AND mode=? ORDER BY closed_at DESC LIMIT ?", (mode, limit))

    def pnl_since(self, mode: str, since_ts: float) -> float:
        row = self.one("SELECT COALESCE(SUM(pnl),0) AS p FROM trades WHERE mode=? AND status='closed' AND closed_at>=?", (mode, since_ts))
        return float(row["p"]) if row else 0.0

    def trade_stats(self, mode: str) -> dict[str, Any]:
        rows = self.closed_trades(mode, limit=100000)
        n = len(rows)
        if n == 0:
            return {"trades": 0, "win_rate": 0.0, "pnl": 0.0, "profit_factor": 0.0, "avg_r": 0.0}
        wins = [r["pnl"] for r in rows if (r["pnl"] or 0) > 0]
        losses = [-r["pnl"] for r in rows if (r["pnl"] or 0) < 0]
        rs = [r["r_multiple"] for r in rows if r["r_multiple"] is not None]
        return {
            "trades": n,
            "win_rate": len(wins) / n,
            "pnl": sum(r["pnl"] or 0 for r in rows),
            "profit_factor": (sum(wins) / sum(losses)) if losses else float("inf") if wins else 0.0,
            "avg_r": (sum(rs) / len(rs)) if rs else 0.0,
        }

    # ------------------------------------------------------------ equity
    def reset_mode(self, mode: str) -> None:
        """Wipe all trades and the equity history for a mode, and clear the decision log
        (decisions are not tagged by mode - they are a shared display log)."""
        self.execute("DELETE FROM trades WHERE mode=?", (mode,))
        self.execute("DELETE FROM equity WHERE mode=?", (mode,))
        self.execute("DELETE FROM decisions")

    def record_equity(self, mode: str, equity: float) -> None:
        self.execute("INSERT INTO equity(ts, mode, equity) VALUES (?,?,?)", (time.time(), mode, equity))

    def equity_curve(self, mode: str, limit: int = 2000) -> list[tuple[float, float]]:
        rows = self.query("SELECT ts, equity FROM equity WHERE mode=? ORDER BY ts DESC LIMIT ?", (mode, limit))
        return [(r["ts"], r["equity"]) for r in reversed(rows)]

    # ------------------------------------------------------------ skills
    def add_skill(self, name: str, category: str, rule: str, source: str, status: str = "draft",
                  weight: float = 1.0) -> int:
        now = time.time()
        cur = self.execute(
            "INSERT INTO skills(name, category, rule, source, status, weight, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?)",
            (name.strip(), category, rule.strip(), source, status, weight, now, now),
        )
        return int(cur.lastrowid)

    def skill_exists(self, name: str) -> bool:
        return self.one("SELECT 1 FROM skills WHERE lower(name)=lower(?)", (name.strip(),)) is not None

    def set_skill_status(self, skill_id: int, status: str) -> None:
        self.execute("UPDATE skills SET status=?, updated_at=? WHERE id=?", (status, time.time(), skill_id))

    def update_skill(self, skill_id: int, **fields: Any) -> None:
        allowed = {"name", "category", "rule", "weight", "backtest", "status"}
        sets = {k: v for k, v in fields.items() if k in allowed}
        if not sets:
            return
        sets["updated_at"] = time.time()
        cols = ", ".join(f"{k}=?" for k in sets)
        self.execute(f"UPDATE skills SET {cols} WHERE id=?", (*sets.values(), skill_id))

    def delete_skill(self, skill_id: int) -> None:
        """Delete a skill, and remember it if it came from the shipped seed files.

        Without the tombstone, load_seed_skills() puts every seed rule back on the next start,
        so deleting a rule the owner disagrees with lasted until the app was reopened."""
        row = self.one("SELECT name, source FROM skills WHERE id=?", (skill_id,))
        if row and str(row["source"] or "").startswith("seed:"):
            self.execute("INSERT OR IGNORE INTO seed_removed(name, removed_at) VALUES (?,?)",
                         (row["name"].strip().lower(), time.time()))
        self.execute("DELETE FROM skills WHERE id=?", (skill_id,))

    def removed_seed_names(self) -> set[str]:
        return {r["name"] for r in self.query("SELECT name FROM seed_removed")}

    def restore_seed_skill(self, name: str) -> None:
        """Undo a deletion, so a seed rule can come back on the next start."""
        self.execute("DELETE FROM seed_removed WHERE name=?", (name.strip().lower(),))

    def skills(self, status: str | None = None) -> list[sqlite3.Row]:
        if status:
            return self.query("SELECT * FROM skills WHERE status=? ORDER BY category, id", (status,))
        return self.query("SELECT * FROM skills ORDER BY category, id")

    # ------------------------------------------------------------ knowledge
    def add_doc(self, title: str, kind: str, origin: str, chunks: list[str]) -> int:
        with self._lock:
            cur = self._conn.execute(
                "INSERT INTO knowledge_docs(title, kind, origin, chars, added_at) VALUES (?,?,?,?,?)",
                (title, kind, origin, sum(len(c) for c in chunks), time.time()),
            )
            doc_id = int(cur.lastrowid)
            self._conn.executemany(
                "INSERT INTO knowledge_chunks(doc_id, seq, content) VALUES (?,?,?)",
                [(doc_id, i, c) for i, c in enumerate(chunks)],
            )
            self._conn.commit()
        return doc_id

    def docs(self) -> list[sqlite3.Row]:
        return self.query("SELECT * FROM knowledge_docs ORDER BY id DESC")

    def delete_doc(self, doc_id: int) -> None:
        with self._lock:
            self._conn.execute("DELETE FROM knowledge_chunks WHERE doc_id=?", (doc_id,))
            self._conn.execute("DELETE FROM knowledge_docs WHERE id=?", (doc_id,))
            self._conn.commit()

    def doc_chunks(self, doc_id: int) -> list[str]:
        return [r["content"] for r in self.query(
            "SELECT content FROM knowledge_chunks WHERE doc_id=? ORDER BY seq", (doc_id,))]

    def search_knowledge(self, text: str, limit: int = 8) -> list[sqlite3.Row]:
        """Full-text search. Each word becomes an OR term so a sentence still matches."""
        words = [w for w in "".join(ch if ch.isalnum() or ch.isspace() else " " for ch in text).split() if len(w) > 2]
        if not words:
            return []
        q = " OR ".join(f'"{w}"' for w in words[:20])
        return self.query(
            "SELECT k.doc_id, k.seq, k.content, d.title FROM knowledge_chunks k JOIN knowledge_docs d ON d.id=k.doc_id"
            " WHERE knowledge_chunks MATCH ? ORDER BY bm25(knowledge_chunks) LIMIT ?",
            (q, limit),
        )
