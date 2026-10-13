"""The trading rules. Pure functions so they can be tested without any API.

Entry = smart money accumulating a young coin while its price is falling:
  1. Nansen netflow: token <= max age, smart money net buying over 24h, enough distinct wallets
  2. DexScreener: price down hard over 24h and not already bouncing over 6h,
     enough liquidity, pool age also <= max age (double-check)
  3. Nansen flow intelligence on the token confirms smart traders are net buyers

Exit (first one that triggers):
  - hard stop loss from entry
  - trailing stop from the peak since entry
  - smart money turns net seller (follow them out)
  - max hold time
"""
from __future__ import annotations

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
    score: float
    detail: dict = field(default_factory=dict)


def screen(netflows: list[dict], quotes: dict[str, Quote], cfg) -> tuple[list[Candidate], list[dict]]:
    """Return (passing candidates ranked best first, rejects with reasons)."""
    u, e = cfg["universe"], cfg["entry"]
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
            if q.pair_age_days > u["max_token_age_days"] + 1:
                why.append(f"pool age {q.pair_age_days:.0f}d")
            if q.liquidity_usd < u["min_liquidity_usd"]:
                why.append(f"liquidity ${q.liquidity_usd:,.0f}")
            if q.change_24h > e["max_price_change_24h_pct"]:
                why.append(f"price 24h {q.change_24h:+.1f}% (not dropping enough)")
            if q.change_6h > e["max_price_change_6h_pct"]:
                why.append(f"price 6h {q.change_6h:+.1f}% (already bouncing)")
        if why:
            rejected.append({"token": token, "symbol": sym, "why": why})
            continue
        mcap = q.market_cap or float(row.get("market_cap_usd") or 0) or 1.0
        # Divergence score: how big the buying is relative to the coin's size,
        # times how hard price has fallen. Bigger = stronger disagreement.
        score = (nf / mcap * 100) * abs(q.change_24h)
        passed.append(Candidate(token, sym, nf, traders, float(age), q, score, {
            "sm_netflow_24h": nf, "sm_traders": traders, "age_days": age,
            "price_24h": q.change_24h, "price_6h": q.change_6h,
            "liquidity": q.liquidity_usd, "mcap": mcap, "score": round(score, 3)}))
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


def exit_reason(pos: Position, price: float, now: float, cfg, sm_flow: dict | None = None) -> str | None:
    """Return why to sell, or None to keep holding. Call update_peak first."""
    x = cfg["exit"]
    change = (price / pos.entry_price - 1) * 100
    if change <= -x["hard_stop_loss_pct"]:
        return f"hard stop {change:.1f}%"
    peak_gain = (pos.peak_price / pos.entry_price - 1) * 100
    drawdown = (1 - price / pos.peak_price) * 100
    if peak_gain >= x["trailing_activation_pct"] and drawdown >= x["trailing_stop_pct"]:
        return f"trailing stop: {drawdown:.1f}% off peak (peak {peak_gain:+.1f}%, now {change:+.1f}%)"
    if x["exit_on_sm_outflow"] and sm_flow:
        nf = float(sm_flow.get("smart_trader_net_flow_usd") or 0)
        if nf <= x["sm_exit_netflow_usd"]:
            return f"smart money exiting: net ${nf:,.0f} over {x['sm_exit_timeframe']} (now {change:+.1f}%)"
    if (now - pos.opened_at) / 86400 >= x["max_hold_days"]:
        return f"max hold reached ({change:+.1f}%)"
    return None


def paper_fill(price: float, side: str, cfg) -> float:
    """Simulated fill price after slippage and fees."""
    r = cfg["risk"]
    cost = (r["assumed_slippage_pct"] + r["assumed_fee_pct"]) / 100
    return price * (1 + cost) if side == "buy" else price * (1 - cost)
