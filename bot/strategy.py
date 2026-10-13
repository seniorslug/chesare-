"""The trading rules. Pure functions so they can be tested without any API.

Entry = smart money accumulating a young coin while its price is falling:
  1. Hard filters: token <= max age, smart money net buying over 24h, enough distinct
     wallets, price down hard over 24h (but not a rug-like collapse), not already
     bouncing over 6h, enough liquidity relative to market cap
  2. Entry score 0-100 (see entry_score): breadth, intensity, persistence across
     scans, still buying now, divergence depth, stabilizing, liquidity. Buy only
     at or above min_entry_score.
  3. (optional, off by default) Nansen flow intelligence confirms smart traders are net buyers

Exit (first one that triggers):
  - hard stop loss from entry
  - trailing stop from the peak since entry
  - smart money turns net seller (follow them out)
  - max hold time
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field

from .prices import Quote
from .store import Position


@dataclass
class Candidate:
    token: str
    symbol: str
    sm_netflow_24h: float
    sm_traders: int
    age_days: float
    quote: Quote
    score: float                       # 0-100 entry quality
    qualified: bool = False            # score >= min_entry_score
    detail: dict = field(default_factory=dict)


def _clamp(x: float) -> float:
    return max(0.0, min(1.0, x))


def entry_score(row: dict, q: Quote, streak: int) -> tuple[float, dict]:
    """0-100 score for how strong the smart money divergence looks.

    Weights are a starting point; the signal lab measures which parts
    actually predict returns so they can be re-weighted with evidence.
    """
    nf24 = float(row.get("net_flow_24h_usd") or 0)
    nf1h = row.get("net_flow_1h_usd")
    nf7d = float(row.get("net_flow_7d_usd") or 0)
    traders = int(row.get("trader_count") or 0)
    mcap = q.market_cap or float(row.get("market_cap_usd") or 0) or 1.0
    intensity = nf24 / mcap * 100                  # % of market cap bought net in 24h
    drop = -q.change_24h
    liq_ratio = q.liquidity_usd / mcap

    parts = {
        # Many distinct smart wallets beat one whale.
        "breadth": 20 * _clamp((math.log(max(traders, 1)) - math.log(3)) / (math.log(25) - math.log(3))),
        # Buying that is large relative to the coin's size.
        "intensity": 20 * _clamp((intensity - 0.3) / (3.0 - 0.3)),
        # Seen accumulating in earlier scans, and over the week, not just today.
        "persistence": 14 * _clamp(streak / 2) + (6 if nf7d > nf24 > 0 else 0),
        # Still buying in the last hour (not already rotating out).
        "buying_now": 10 * (1.0 if nf1h is not None and float(nf1h) > 0 else
                            0.5 if nf1h is None or float(nf1h) == 0 else 0.0),
        # A real discount, but not a collapse that looks like a rug.
        "divergence": 15 * (_clamp((drop - 10) / 30) if drop <= 55 else _clamp((80 - drop) / 25)),
        # Selling pressure is easing: last hour flat-ish, not still dumping.
        "stabilizing": 10 * (1.0 if -3 <= q.change_1h <= 10 else
                             _clamp((q.change_1h + 10) / 7) if q.change_1h < -3 else 0.5),
        # Enough pool depth for the market cap to exit cleanly.
        "liquidity": 5 * _clamp((liq_ratio - 0.03) / (0.10 - 0.03)),
    }
    total = round(sum(parts.values()), 1)
    feats = {
        "sm_netflow_24h": nf24, "sm_netflow_1h": None if nf1h is None else float(nf1h),
        "sm_netflow_7d": nf7d, "sm_traders": traders, "intensity_pct": round(intensity, 2),
        "streak": streak, "price_1h": q.change_1h, "price_6h": q.change_6h, "price_24h": q.change_24h,
        "liquidity": q.liquidity_usd, "mcap": mcap, "liq_ratio": round(liq_ratio, 3),
        "buys_1h": q.buys_1h, "sells_1h": q.sells_1h, "volume_24h": q.volume_24h,
        "parts": {k: round(v, 1) for k, v in parts.items()}, "score": total,
    }
    return total, feats


def screen(netflows: list[dict], quotes: dict[str, Quote], cfg,
           streaks: dict[str, int] | None = None) -> tuple[list[Candidate], list[dict]]:
    """Hard filters, then score. Returns (all coins passing hard filters, best score first;
    rejects with reasons). Only candidates with .qualified are bought."""
    u, e = cfg["universe"], cfg["entry"]
    streaks = streaks or {}
    min_score = e.get("min_entry_score", 0)
    passed, rejected = [], []
    for row in netflows:
        token = row.get("token_address", "")
        sym = row.get("token_symbol", "?")
        nf = float(row.get("net_flow_24h_usd") or 0)
        traders = int(row.get("trader_count") or 0)
        age = row.get("token_age_days")
        q = quotes.get(token)
        why = []
        if age is None or age > u["max_token_age_days"]:
            why.append(f"age {age}d")
        if nf < e["min_sm_netflow_24h_usd"]:
            why.append(f"SM netflow ${nf:,.0f}")
        if traders < e["min_sm_trader_count"]:
            why.append(f"{traders} SM wallets")
        if q is None:
            why.append("no price data")
        else:
            mcap = q.market_cap or float(row.get("market_cap_usd") or 0) or 1.0
            if q.pair_age_days > u["max_token_age_days"] + 1:
                why.append(f"pool age {q.pair_age_days:.0f}d")
            if q.liquidity_usd < u["min_liquidity_usd"]:
                why.append(f"liquidity ${q.liquidity_usd:,.0f}")
            if q.liquidity_usd / mcap < e.get("min_liquidity_to_mcap", 0):
                why.append(f"thin pool ({q.liquidity_usd / mcap:.1%} of mcap)")
            if q.change_24h > e["max_price_change_24h_pct"]:
                why.append(f"price 24h {q.change_24h:+.1f}% (not dropping enough)")
            if q.change_24h < e.get("min_price_change_24h_pct", -100):
                why.append(f"price 24h {q.change_24h:+.1f}% (collapse, likely rug)")
            if q.change_6h > e["max_price_change_6h_pct"]:
                why.append(f"price 6h {q.change_6h:+.1f}% (already bouncing)")
        if why:
            rejected.append({"token": token, "symbol": sym, "why": why})
            continue
        score, feats = entry_score(row, q, streaks.get(token, 0))
        feats["age_days"] = age
        passed.append(Candidate(token, sym, nf, traders, float(age), q, score, score >= min_score, feats))
    passed.sort(key=lambda c: c.score, reverse=True)
    return passed, rejected


def confirmed(flow: dict, cfg) -> tuple[bool, str]:
    e = cfg["entry"]
    nf = float(flow.get("smart_trader_net_flow_usd") or 0)
    wallets = int(flow.get("smart_trader_wallet_count") or 0)
    ok = nf >= e["min_confirm_netflow_usd"] and wallets >= e["min_confirm_wallets"]
    return ok, f"flow-intel {e['confirm_timeframe']}: SM net ${nf:,.0f} from {wallets} wallets"


def update_peak(pos: Position, price: float) -> None:
    if price > pos.peak_price:
        pos.peak_price = price


def exit_reason(pos: Position, price: float, now: float, cfg, sm_netflow: float | None = None) -> str | None:
    """Return why to sell, or None to keep holding. Call update_peak first."""
    x = cfg["exit"]
    change = (price / pos.entry_price - 1) * 100
    if change <= -x["hard_stop_loss_pct"]:
        return f"hard stop {change:.1f}%"
    peak_gain = (pos.peak_price / pos.entry_price - 1) * 100
    drawdown = (1 - price / pos.peak_price) * 100
    if peak_gain >= x["trailing_activation_pct"] and drawdown >= x["trailing_stop_pct"]:
        return f"trailing stop: {drawdown:.1f}% off peak (peak {peak_gain:+.1f}%, now {change:+.1f}%)"
    if x["exit_on_sm_outflow"] and sm_netflow is not None and sm_netflow <= x["sm_exit_netflow_usd"]:
        return f"smart money exiting: net ${sm_netflow:,.0f} over {x['sm_exit_window']} (now {change:+.1f}%)"
    if (now - pos.opened_at) / 86400 >= x["max_hold_days"]:
        return f"max hold reached ({change:+.1f}%)"
    return None


def paper_fill(price: float, side: str, cfg) -> float:
    """Simulated fill price after slippage and fees."""
    r = cfg["risk"]
    cost = (r["assumed_slippage_pct"] + r["assumed_fee_pct"]) / 100
    return price * (1 + cost) if side == "buy" else price * (1 - cost)
