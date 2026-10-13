"""Price, liquidity and pair age from DexScreener's free public API.

Used for the "price is dropping" side of the signal, the age double-check,
and for tracking open positions (trailing stop). No key needed.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass

import requests

log = logging.getLogger(__name__)
URL = "https://api.dexscreener.com/tokens/v1/solana/{}"


@dataclass
class Quote:
    token: str
    symbol: str
    price_usd: float
    change_1h: float
    change_6h: float
    change_24h: float
    liquidity_usd: float
    market_cap: float
    pair_age_days: float
    pair_url: str
    buys_1h: int = 0
    sells_1h: int = 0
    buys_24h: int = 0
    sells_24h: int = 0
    volume_24h: float = 0.0


def _f(x, default=0.0) -> float:
    try:
        return float(x)
    except (TypeError, ValueError):
        return default


def parse_pairs(token: str, pairs: list[dict]) -> Quote | None:
    """Pick the deepest pool where this token is the base token."""
    mine = [p for p in pairs if p.get("baseToken", {}).get("address") == token]
    if not mine:
        return None
    best = max(mine, key=lambda p: _f(p.get("liquidity", {}).get("usd")))
    oldest_ms = min((p.get("pairCreatedAt") or time.time() * 1000) for p in mine)
    pc = best.get("priceChange") or {}
    tx = best.get("txns") or {}
    h1, h24 = tx.get("h1") or {}, tx.get("h24") or {}
    return Quote(
        token=token,
        symbol=best.get("baseToken", {}).get("symbol", "?"),
        price_usd=_f(best.get("priceUsd")),
        change_1h=_f(pc.get("h1")),
        change_6h=_f(pc.get("h6")),
        change_24h=_f(pc.get("h24")),
        liquidity_usd=_f(best.get("liquidity", {}).get("usd")),
        market_cap=_f(best.get("marketCap") or best.get("fdv")),
        pair_age_days=(time.time() * 1000 - oldest_ms) / 86_400_000,
        pair_url=best.get("url", ""),
        buys_1h=int(_f(h1.get("buys"))), sells_1h=int(_f(h1.get("sells"))),
        buys_24h=int(_f(h24.get("buys"))), sells_24h=int(_f(h24.get("sells"))),
        volume_24h=_f((best.get("volume") or {}).get("h24")),
    )


def get_quotes(tokens: list[str]) -> dict[str, Quote]:
    out: dict[str, Quote] = {}
    for i in range(0, len(tokens), 30):  # API takes up to 30 addresses
        chunk = tokens[i:i + 30]
        try:
            r = requests.get(URL.format(",".join(chunk)), timeout=20)
            r.raise_for_status()
            pairs = r.json() or []
        except Exception as e:  # network hiccup: skip this round
            log.warning("DexScreener error: %s", e)
            continue
        for t in chunk:
            q = parse_pairs(t, pairs)
            if q and q.price_usd > 0:
                out[t] = q
    return out
