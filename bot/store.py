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


SCHEMA = """
CREATE TABLE IF NOT EXISTS positions (token TEXT PRIMARY KEY, data TEXT);
CREATE TABLE IF NOT EXISTS trades (
  id INTEGER PRIMARY KEY, token TEXT, symbol TEXT, side TEXT, price REAL,
  usd REAL, pnl_usd REAL, reason TEXT, ts REAL, mode TEXT);
CREATE TABLE IF NOT EXISTS kv (k TEXT PRIMARY KEY, v TEXT);
CREATE TABLE IF NOT EXISTS signals (
  id INTEGER PRIMARY KEY, ts REAL, token TEXT, symbol TEXT, taken INTEGER, detail TEXT);
"""


class Store:
    def __init__(self, path: Path):
        self.db = sqlite3.connect(path)
        self.db.executescript(SCHEMA)

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
    def log_trade(self, token, symbol, side, price, usd, pnl, reason, mode) -> None:
        self.db.execute(
            "INSERT INTO trades (token, symbol, side, price, usd, pnl_usd, reason, ts, mode) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            (token, symbol, side, price, usd, pnl, reason, time.time(), mode))
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

    def add_equity_point(self, equity: float, keep: int = 3000) -> None:
        hist = self.get("equity_history", [])
        hist.append([time.time(), round(equity, 2)])
        self.set("equity_history", hist[-keep:])

    # key/value (paper cash balance, last scan time)
    def get(self, k: str, default=None):
        r = self.db.execute("SELECT v FROM kv WHERE k=?", (k,)).fetchone()
        return json.loads(r[0]) if r else default

    def set(self, k: str, v) -> None:
        self.db.execute("REPLACE INTO kv VALUES (?, ?)", (k, json.dumps(v)))
        self.db.commit()
