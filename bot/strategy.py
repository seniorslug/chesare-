"""The trading rules. Pure functions so they can be tested without any API.

Entry: smart money silently accumulating a coin that has pulled back into the
61.8-76% zone (config) below its all-time high and is holding there.
  1. Prefilter (free): age, smart wallets, still buying in 24h, liquidity, and an
     upper bound on % of supply bought, so only real contenders get candle checks.
  2. Candles (1m body closes, see candles.py): ATH market cap >= $10M, latest close
     61.8-76% below ATH, no close below the 76% line since the ATH, held in the zone
     for a minimum time, price down over the accumulation window.
  3. Smart money net bought >= 0.5% of supply over the window (priced at the
     window's average price, so a dip doesn't inflate it).
  4. Score 0-100 ranks qualifying coins (and feeds the signal lab).

Exit (first one that triggers):
  - support broken: a 1m close below the max-drawdown line (76% from ATH) (checked in runner)
  - hard stop loss from entry (backstop)
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


def supply_pct(net_flow_usd: float, q: Quote, avg_price: float | None) -> float:
    """Smart money net buying as % of total supply.
    tokens bought ~= USD / average price over the window; supply = mcap / price."""
    if not q.price_usd or not q.market_cap:
        return 0.0
    supply = q.market_cap / q.price_usd
    px = avg_price or q.price_usd
    return net_flow_usd / px / supply * 100


def prefilter(netflows: list[dict], quotes: dict[str, Quote], cfg) -> tuple[list[dict], list[dict]]:
    """Cheap checks before pulling candles. Returns (rows worth a candle check, rejects)."""
    u, e = cfg["universe"], cfg["entry"]
    win = e["accumulation_window"]
    keep, rejected = [], []
    for row in netflows:
        token, sym = row.get("token_address", ""), row.get("token_symbol", "?")
        q = quotes.get(token)
        age = row.get("token_age_days")
        nf_win = float(row.get(f"net_flow_{win}_usd") or 0)
        nf24 = float(row.get("net_flow_24h_usd") or 0)
        traders = int(row.get("trader_count") or 0)
        why = []
        if age is None or age > u["max_token_age_days"]:
            why.append(f"age {age}d")
        if traders < e["min_sm_trader_count"]:
            why.append(f"{traders} SM wallets")
        if nf24 <= 0 and e["require_sm_buying_24h"]:
            why.append("smart money not buying in last 24h")
        if q is None:
            why.append("no price data")
        else:
            mcap = q.market_cap or 1.0
            if q.liquidity_usd < u["min_liquidity_usd"]:
                why.append(f"liquidity ${q.liquidity_usd:,.0f}")
            if q.liquidity_usd / mcap < e["min_liquidity_to_mcap"]:
                why.append(f"thin pool ({q.liquidity_usd / mcap:.1%} of mcap)")
            if not q.pair_address:
                why.append("no pool address")
            # Upper bound on supply % (current price <= average price during a dip),
            # so anything below the minimum here can't pass the exact check later.
            rough = supply_pct(nf_win, q, None)
            if rough < e["min_sm_supply_pct"]:
                why.append(f"SM bought ~{rough:.2f}% of supply ({win})")
        if why:
            rejected.append({"token": token, "symbol": sym, "why": why})
        else:
            row = dict(row); row["_rough_supply"] = supply_pct(nf_win, q, None)
            keep.append(row)
    keep.sort(key=lambda r: r["_rough_supply"], reverse=True)
    return keep, rejected


def evaluate(row: dict, q: Quote, z, streak: int, cfg) -> tuple[Candidate | None, list[str], dict]:
    """Apply the ATH-drawdown + smart money accumulation rules to one coin.
    Returns (candidate or None, failed rules, features)."""
    e = cfg["entry"]
    win = e["accumulation_window"]
    nf_win = float(row.get(f"net_flow_{win}_usd") or 0)
    nf24 = float(row.get("net_flow_24h_usd") or 0)
    nf1h = row.get("net_flow_1h_usd")
    nf7d = float(row.get("net_flow_7d_usd") or 0)
    traders = int(row.get("trader_count") or 0)
    supply = (q.market_cap / q.price_usd) if q.price_usd else 0
    ath_mcap = z.ath_close * supply
    sup_pct = supply_pct(nf_win, q, z.avg_close.get(win))
    sup24 = supply_pct(nf24, q, z.avg_close.get("24h"))
    price_win = z.price_change.get(win, 0.0)

    fails = []
    if not z.ok:
        fails.append(f"candles: {z.error}")
    else:
        if ath_mcap < e["min_ath_mcap_usd"]:
            fails.append(f"ATH mcap ${ath_mcap / 1e6:.1f}M < ${e['min_ath_mcap_usd'] / 1e6:.0f}M")
        if not (e["min_drawdown_pct"] <= z.drawdown_pct <= e["max_drawdown_pct"]):
            fails.append(f"{z.drawdown_pct:.1f}% below ATH (needs {e['min_drawdown_pct']}–{e['max_drawdown_pct']}%)")
        if not z.held_support:
            fails.append(f"closed {z.max_drawdown_pct:.1f}% below ATH (support at {e['max_drawdown_pct']}% broken)")
        if z.hours_in_zone < e["min_hours_in_zone"]:
            fails.append(f"only {z.hours_in_zone:.1f}h in zone (needs {e['min_hours_in_zone']}h holding)")
        if price_win >= 0 and e["require_price_dropping"]:
            fails.append(f"price {price_win:+.1f}% over {win} (not dropping)")
    if sup_pct < e["min_sm_supply_pct"]:
        fails.append(f"SM bought {sup_pct:.2f}% of supply over {win} (needs {e['min_sm_supply_pct']}%)")

    mcap = q.market_cap or 1.0
    liq_ratio = q.liquidity_usd / mcap
    parts = {
        # How much of the supply smart money took during the dip (0.5% -> 0, 2%+ -> full).
        "supply": 25 * _clamp((sup_pct - 0.5) / 1.5),
        # Many distinct smart wallets beat one whale.
        "breadth": 20 * _clamp((math.log(max(traders, 1)) - math.log(3)) / (math.log(25) - math.log(3))),
        # Seen accumulating in earlier scans, and over the week.
        "persistence": 14 * _clamp(streak / 2) + (6 if nf7d > nf24 > 0 else 0),
        # Still buying in the last hour.
        "buying_now": 10 * (1.0 if nf1h is not None and float(nf1h) > 0 else
                            0.5 if nf1h is None or float(nf1h) == 0 else 0.0),
        # How long the zone has held (1h -> 0, 24h+ -> full).
        "holding": 15 * _clamp((z.hours_in_zone - 1) / 23) if z.ok else 0.0,
        # Not still dumping in the last hour.
        "stabilizing": 5 * (1.0 if q.change_1h >= -3 else _clamp((q.change_1h + 10) / 7)),
        # Pool depth relative to market cap.
        "liquidity": 5 * _clamp((liq_ratio - 0.03) / (0.10 - 0.03)),
    }
    score = round(sum(parts.values()), 1)
    feats = {
        "ath_close": z.ath_close, "ath_ts": z.ath_ts, "ath_mcap": ath_mcap,
        "drawdown_pct": round(z.drawdown_pct, 2), "max_drawdown_pct": round(z.max_drawdown_pct, 2),
        "support_price": z.support_price, "held_support": z.held_support,
        "hours_in_zone": round(z.hours_in_zone, 1), "price_window_pct": round(price_win, 1),
        "sm_window": win, "sm_netflow_window": nf_win, "sm_supply_pct": round(sup_pct, 3),
        "sm_supply_pct_24h": round(sup24, 3), "sm_netflow_24h": nf24,
        "sm_netflow_1h": None if nf1h is None else float(nf1h), "sm_netflow_7d": nf7d,
        "sm_traders": traders, "streak": streak, "price_1h": q.change_1h, "price_24h": q.change_24h,
        "liquidity": q.liquidity_usd, "mcap": q.market_cap, "liq_ratio": round(liq_ratio, 3),
        "age_days": row.get("token_age_days"), "pool": q.pair_address,
        "parts": {k: round(v, 1) for k, v in parts.items()}, "score": score, "fails": fails,
    }
    if fails:
        return None, fails, feats
    c = Candidate(q.token, q.symbol, nf_win, traders, float(row.get("token_age_days") or 0), q,
                  score, score >= e.get("min_entry_score", 0), feats)
    return c, [], feats


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
