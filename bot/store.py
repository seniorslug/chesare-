"""Positions, trade history and cooldowns, kept in a small SQLite file."""
from __future__ import annotations

import json
import sqlite3
import time
from dataclasses import asdict, dataclass
from pathlib import Path


@dataclass
class Position:
    token: str
    symbol: str
    entry_price: float
    size_usd: float
    qty: float
    opened_at: float
    peak_price: float
    entry_reason: str = ""
    gmgn_stop_id: str = ""
    last_sm_check: float = 0.0
    last_price: float = 0.0
    pool: str = ""
    support_price: float = 0.0
    ath_price: float = 0.0
    last_candle_ts: float = 0.0


SCHEMA = """
CREATE TABLE IF NOT EXISTS positions (token TEXT PRIMARY KEY, data TEXT);
CREATE TABLE IF NOT EXISTS trades (
  id INTEGER PRIMARY KEY, token TEXT, symbol TEXT, side TEXT, price REAL,
  usd REAL, pnl_usd REAL, reason TEXT, ts REAL, mode TEXT, detail TEXT);
CREATE TABLE IF NOT EXISTS kv (k TEXT PRIMARY KEY, v TEXT);
CREATE TABLE IF NOT EXISTS scans (
  ts REAL, token TEXT, nf1h REAL, nf24h REAL, nf7d REAL, traders INTEGER);
CREATE INDEX IF NOT EXISTS scans_token ON scans(token, ts);
CREATE TABLE IF NOT EXISTS lab (
  id INTEGER PRIMARY KEY, ts REAL, token TEXT, symbol TEXT, score REAL,
  qualified INTEGER, bought INTEGER, price0 REAL, features TEXT,
  r4h REAL, r24h REAL, r72h REAL, max72 REAL, min72 REAL, done INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS signals (
  id INTEGER PRIMARY KEY, ts REAL, token TEXT, symbol TEXT, taken INTEGER, detail TEXT);
"""


class Store:
    def __init__(self, path: Path):
        self.db = sqlite3.connect(path)
        self.db.executescript(SCHEMA)
        self._migrate()

    def _migrate(self) -> None:
        """Bring databases saved by older versions up to date (CREATE IF NOT EXISTS won't add columns)."""
        cols = {r[1] for r in self.db.execute("PRAGMA table_info(trades)")}
        if "detail" not in cols:
            self.db.execute("ALTER TABLE trades ADD COLUMN detail TEXT")
            self.db.commit()

    # positions
    def positions(self) -> list[Position]:
        return [Position(**json.loads(r[0])) for r in self.db.execute("SELECT data FROM positions")]

    def save_position(self, p: Position) -> None:
        self.db.execute("REPLACE INTO positions VALUES (?, ?)", (p.token, json.dumps(asdict(p))))
        self.db.commit()

    def remove_position(self, token: str) -> None:
        self.db.execute("DELETE FROM positions WHERE token = ?", (token,))
        self.db.commit()

    # trades
    def log_trade(self, token, symbol, side, price, usd, pnl, reason, mode, detail: dict | None = None) -> None:
        """`detail` (buys only): the entry checklist as the bot saw it, so the dashboard can explain the buy."""
        self.db.execute(
            "INSERT INTO trades (token, symbol, side, price, usd, pnl_usd, reason, ts, mode, detail) "
            "VALUES (?,?,?,?,?,?,?,?,?,?)",
            (token, symbol, side, price, usd, pnl, reason, time.time(), mode,
             json.dumps(detail) if detail else None))
        self.db.commit()

    def trades(self) -> list[sqlite3.Row]:
        self.db.row_factory = sqlite3.Row
        rows = list(self.db.execute("SELECT * FROM trades ORDER BY ts"))
        self.db.row_factory = None
        return rows

    def realized_pnl_since(self, ts: float) -> float:
        r = self.db.execute("SELECT COALESCE(SUM(pnl_usd),0) FROM trades WHERE side='sell' AND ts>=?", (ts,))
        return r.fetchone()[0]

    def last_exit(self, token: str) -> float:
        r = self.db.execute("SELECT MAX(ts) FROM trades WHERE token=? AND side='sell'", (token,))
        return r.fetchone()[0] or 0.0

    def log_signal(self, token, symbol, taken, detail: dict) -> None:
        self.db.execute("INSERT INTO signals (ts, token, symbol, taken, detail) VALUES (?,?,?,?,?)",
                        (time.time(), token, symbol, int(taken), json.dumps(detail)))
        self.db.commit()

    def signals(self, limit: int = 50) -> list[dict]:
        rows = self.db.execute(
            "SELECT ts, token, symbol, taken, detail FROM signals ORDER BY ts DESC LIMIT ?", (limit,))
        return [{"ts": r[0], "token": r[1], "symbol": r[2], "taken": bool(r[3]),
                 "detail": json.loads(r[4] or "{}")} for r in rows]

    def taken_signals(self) -> list[dict]:
        """Every signal the bot acted on (oldest first). Used to explain buys made before trades stored their own detail."""
        rows = self.db.execute("SELECT ts, token, detail FROM signals WHERE taken = 1 ORDER BY ts")
        return [{"ts": r[0], "token": r[1], "detail": json.loads(r[2] or "{}")} for r in rows]

    def add_equity_point(self, equity: float, keep: int = 3000) -> None:
        hist = self.get("equity_history", [])
        hist.append([time.time(), round(equity, 2)])
        self.set("equity_history", hist[-keep:])

    # scan memory (for persistence across scans)
    def record_scan(self, rows: list[dict], ts: float) -> None:
        self.db.executemany("INSERT INTO scans VALUES (?,?,?,?,?,?)", [
            (ts, r.get("token_address"), r.get("net_flow_1h_usd"), r.get("net_flow_24h_usd"),
             r.get("net_flow_7d_usd"), r.get("trader_count")) for r in rows])
        self.db.execute("DELETE FROM scans WHERE ts < ?", (ts - 7 * 86400,))
        self.db.commit()

    def streaks(self, before: float, window_h: float = 30) -> dict[str, int]:
        """How many earlier scans (in the window) saw each token with net smart money buying."""
        rows = self.db.execute(
            "SELECT token, COUNT(DISTINCT ts) FROM scans WHERE ts < ? AND ts >= ? AND nf24h > 0 GROUP BY token",
            (before, before - window_h * 3600))
        return {t: n for t, n in rows}

    # signal lab
    def lab_add(self, token, symbol, score, qualified: bool, bought: bool, price0, features: dict) -> None:
        self.db.execute(
            "INSERT INTO lab (ts, token, symbol, score, qualified, bought, price0, features, max72, min72) "
            "VALUES (?,?,?,?,?,?,?,?,?,?)",
            (time.time(), token, symbol, score, int(qualified), int(bought), price0,
             json.dumps(features), 0.0, 0.0))
        self.db.commit()

    def lab_open(self) -> list[dict]:
        cols = ["id", "ts", "token", "price0", "r4h", "r24h", "r72h", "max72", "min72"]
        rows = self.db.execute(f"SELECT {','.join(cols)} FROM lab WHERE done = 0")
        return [dict(zip(cols, r)) for r in rows]

    def lab_update(self, row: dict) -> None:
        self.db.execute("UPDATE lab SET r4h=?, r24h=?, r72h=?, max72=?, min72=?, done=? WHERE id=?",
                        (row["r4h"], row["r24h"], row["r72h"], row["max72"], row["min72"],
                         int(row.get("done", 0)), row["id"]))
        self.db.commit()

    def lab_all(self, limit: int = 2000) -> list[dict]:
        cols = ["ts", "token", "symbol", "score", "qualified", "bought", "price0",
                "features", "r4h", "r24h", "r72h", "max72", "min72", "done"]
        rows = self.db.execute(f"SELECT {','.join(cols)} FROM lab ORDER BY ts DESC LIMIT ?", (limit,))
        out = []
        for r in rows:
            d = dict(zip(cols, r)); d["features"] = json.loads(d["features"] or "{}"); out.append(d)
        return out

    # key/value (paper cash balance, last scan time)
    def get(self, k: str, default=None):
        r = self.db.execute("SELECT v FROM kv WHERE k=?", (k,)).fetchone()
        return json.loads(r[0]) if r else default

    def set(self, k: str, v) -> None:
        self.db.execute("REPLACE INTO kv VALUES (?, ?)", (k, json.dumps(v)))
        self.db.commit()
