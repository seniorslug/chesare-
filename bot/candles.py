"""Candle data (GeckoTerminal, free) and the ATH / drawdown / support math.

All levels use candle BODY CLOSES on 1-minute candles, never wicks:
  - ATH = highest 1m close since the pool started
  - drawdown = how far the latest 1m close is below that ATH
  - support holds = no 1m close below the max-drawdown line (e.g. 70%) since the ATH

Pulling every 1m candle for 30 days would take ~45 requests per coin, so we use
hourly candles to find the only hours that could matter, then fetch 1m candles
just for those hours:
  - The 1m-close ATH sits in an hour whose HIGH >= the best hourly close.
  - A 1m close below support can only happen in an hour whose LOW <= support.
This gives the exact 1m-close answer with a handful of requests.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Callable

import requests

log = logging.getLogger(__name__)
BASE = "https://api.geckoterminal.com/api/v2/networks/solana/pools/{pool}/ohlcv/{tf}"

# (ts, open, high, low, close)
Candle = tuple[float, float, float, float, float]


class CandleError(RuntimeError):
    pass


class Gecko:
    """Free public API: about 30 requests/minute, so calls are spaced out."""

    def __init__(self, min_interval: float = 2.2, timeout: int = 20):
        self.s = requests.Session()
        self.s.headers["Accept"] = "application/json"
        self.min_interval = min_interval
        self.timeout = timeout
        self._last = 0.0
        self.calls = 0

    def _get(self, pool: str, tf: str, params: dict) -> list[Candle]:
        for attempt in range(4):
            wait = self._last + self.min_interval - time.time()
            if wait > 0:
                time.sleep(wait)
            self._last = time.time()
            self.calls += 1
            try:
                r = self.s.get(BASE.format(pool=pool, tf=tf), params=params, timeout=self.timeout)
            except requests.RequestException as e:
                log.warning("GeckoTerminal network error: %s", e)
                time.sleep(3 * (attempt + 1))
                continue
            if r.status_code == 429:
                time.sleep(15 * (attempt + 1))
                continue
            if r.status_code >= 400:
                raise CandleError(f"GeckoTerminal {tf} {pool[:6]}…: HTTP {r.status_code}")
            rows = ((r.json().get("data") or {}).get("attributes") or {}).get("ohlcv_list") or []
            out = [(float(x[0]), float(x[1]), float(x[2]), float(x[3]), float(x[4])) for x in rows]
            return sorted(out)
        raise CandleError(f"GeckoTerminal {tf} {pool[:6]}…: gave up after retries")

    def hourly(self, pool: str, before: float | None = None, limit: int = 1000) -> list[Candle]:
        p = {"aggregate": 1, "limit": limit, "currency": "usd", "token": "base"}
        if before:
            p["before_timestamp"] = int(before)
        return self._get(pool, "hour", p)

    def minutes(self, pool: str, end: float, count: int) -> list[Candle]:
        """1m candles with start time in (end - count*60, end]."""
        p = {"aggregate": 1, "limit": max(1, min(int(count), 1000)), "currency": "usd",
             "token": "base", "before_timestamp": int(end) + 1}
        return [c for c in self._get(pool, "minute", p) if c[0] > end - count * 60]


@dataclass
class Zone:
    ok: bool                       # analysis completed
    error: str = ""
    ath_close: float = 0.0
    ath_ts: float = 0.0
    last_close: float = 0.0
    last_ts: float = 0.0
    drawdown_pct: float = 0.0      # latest 1m close vs ATH close
    max_drawdown_pct: float = 0.0  # deepest 1m close since ATH (that we verified)
    support_price: float = 0.0     # ATH close * (1 - max_dd)
    zone_top_price: float = 0.0    # ATH close * (1 - min_dd)
    held_support: bool = False     # no 1m close below support since ATH
    hours_in_zone: float = 0.0     # since price first closed inside the zone
    avg_close: dict = field(default_factory=dict)       # window -> average hourly close
    price_change: dict = field(default_factory=dict)    # window -> % change of close
    minute_calls: int = 0


WINDOW_H = {"1h": 1, "24h": 24, "7d": 168, "30d": 720}
MinuteFetch = Callable[[float, int], list[Candle]]


def _spans(hours: list[float], max_spans: int) -> list[tuple[float, int]]:
    """Group hour-start timestamps into (end_ts, minute_count) requests of <= 1000 minutes."""
    spans: list[list[float]] = []
    for h in sorted(hours):
        if spans and h + 3600 - spans[-1][0] <= 1000 * 60:
            spans[-1][1] = h + 3600
        else:
            spans.append([h, h + 3600])
    return [(end - 60, int((end - start) / 60)) for start, end in spans[:max_spans]]


def analyze(hourly: list[Candle], fetch_minutes: MinuteFetch, now: float,
            min_dd: float, max_dd: float, max_minute_calls: int = 6) -> Zone:
    if len(hourly) < 3:
        return Zone(ok=False, error="not enough candle history")
    calls = 0

    # 1) ATH by 1m close: refine inside hours that could contain it.
    best_hour_close = max(c[4] for c in hourly)
    peak_hours = sorted((c for c in hourly if c[2] >= best_hour_close), key=lambda c: -c[2])
    ath_close, ath_ts = max(((c[4], c[0]) for c in hourly))  # fallback: best hourly close
    for end, count in _spans([c[0] for c in peak_hours[:12]], max_spans=2):
        mins = fetch_minutes(end, count); calls += 1
        for m in mins:
            if m[4] > ath_close:
                ath_close, ath_ts = m[4], m[0]

    support = ath_close * (1 - max_dd / 100)
    zone_top = ath_close * (1 - min_dd / 100)

    # 2) Latest 1m closes (also covers the recent support check).
    recent = fetch_minutes(now, 1000); calls += 1
    if not recent:
        return Zone(ok=False, error="no recent 1m candles", minute_calls=calls)
    last_ts, last_close = recent[-1][0], recent[-1][4]
    recent_start = recent[0][0]

    # 3) Support since ATH: any 1m close below the line? Only hours with a low under it can.
    after = [c for c in hourly if c[0] + 3600 > ath_ts]
    deepest = min([m[4] for m in recent if m[0] >= ath_ts] or [last_close])
    # Older hours whose wick never reached support can't have a 1m close below it.
    risky = [c[0] for c in after if c[3] <= support and c[0] < recent_start]
    spans = _spans(risky, max_spans=10_000)
    budget = max(max_minute_calls - calls, 0)
    for end, count in spans[:budget]:
        mins = fetch_minutes(end, count); calls += 1
        closes = [m[4] for m in mins if m[0] >= ath_ts]
        if closes:
            deepest = min(deepest, min(closes))
    broke = deepest < support
    # If we couldn't check every risky hour, only a confirmed break is a definite answer.
    verified = broke or len(spans) <= budget
    held = verified and not broke

    # 4) Time in zone: first close inside the zone after the ATH.
    entry_ts = None
    for c in after:
        if c[0] >= ath_ts and c[4] <= zone_top:
            entry_ts = c[0] + 3600  # hourly close time (conservative)
            break
    if entry_ts is None:
        for m in recent:
            if m[0] >= ath_ts and m[4] <= zone_top:
                entry_ts = m[0] + 60
                break
    hours_in_zone = max(0.0, (now - entry_ts) / 3600) if entry_ts else 0.0

    # 5) Averages and price change per window (from hourly closes).
    avg, chg = {}, {}
    for w, h in WINDOW_H.items():
        win = [c for c in hourly if c[0] >= now - h * 3600] or hourly[-1:]
        avg[w] = sum(c[4] for c in win) / len(win)
        start_close = win[0][1] if len(win) else last_close
        chg[w] = (last_close / start_close - 1) * 100 if start_close else 0.0

    return Zone(
        ok=verified,
        error="" if verified else "too many support checks to verify",
        ath_close=ath_close, ath_ts=ath_ts, last_close=last_close, last_ts=last_ts,
        drawdown_pct=(1 - last_close / ath_close) * 100,
        max_drawdown_pct=(1 - deepest / ath_close) * 100,
        support_price=support, zone_top_price=zone_top, held_support=held,
        hours_in_zone=hours_in_zone, avg_close=avg, price_change=chg, minute_calls=calls)


def zone_for_pool(gecko: Gecko, pool: str, now: float, min_dd: float, max_dd: float) -> Zone:
    try:
        hourly = gecko.hourly(pool)
        return analyze(hourly, lambda end, n: gecko.minutes(pool, end, n), now, min_dd, max_dd)
    except CandleError as e:
        return Zone(ok=False, error=str(e))


def closes_since(gecko: Gecko, pool: str, since: float, now: float) -> list[Candle]:
    """1m candles that closed after `since` (for the support-break exit)."""
    count = int(min(max((now - since) / 60 + 2, 5), 1000))
    return [c for c in gecko.minutes(pool, now, count) if c[0] + 60 > since and c[0] + 60 <= now]
