"""Explains each buy in plain English: which entry checks it passed and what story they tell.

Pure functions with no I/O, so they can be tested without a database. Everything shown comes from
numbers the bot recorded when it bought (or from its signal log); nothing is guessed.
"""
from __future__ import annotations

import json
import logging
from typing import Any

from .strategy import SCORE_MAX

log = logging.getLogger("memebot")

SCORE_LABELS = {
    "supply": "Share of supply smart money bought", "breadth": "Number of smart wallets",
    "persistence": "Seen accumulating across scans", "holding": "Time holding the zone",
    "buying_now": "Still buying in the last hour", "stabilizing": "Not dumping right now",
    "liquidity": "Liquidity vs market cap",
}

# Signals are logged moments after the buy in the same scan; this is how far apart they may be.
SIGNAL_MATCH_BEFORE_S = 120
SIGNAL_MATCH_AFTER_S = 3600


def _num(v: Any) -> float | None:
    return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else None


def _usd(v: float, sign: bool = False) -> str:
    s = "+" if sign and v > 0 else "−" if v < 0 else ""
    return f"{s}${abs(v):,.0f}"


def _hours(v: float) -> str:
    return f"{round(v, 1):g}"


def entry_checks(d: dict, t: dict) -> list[dict]:
    """One row per entry rule: what was required, what the coin showed, and whether it was met.
    `met` is True / False, or None when the value wasn't recorded."""
    out: list[dict] = []
    win = t["accumulation_window"]

    def add(key, label, rule, value, show, ok):
        v = _num(value)
        out.append({"key": key, "label": label, "rule": rule,
                    "actual": "not recorded" if v is None else show(v),
                    "met": None if v is None else bool(ok(v))})

    add("age", "Young token", f"≤ {t['max_token_age_days']:g} days old", d.get("age_days"),
        lambda v: f"{v:.1f} days old", lambda v: v <= t["max_token_age_days"])
    add("wallets", "Several smart wallets", f"≥ {t['min_sm_trader_count']} distinct wallets", d.get("sm_traders"),
        lambda v: f"{int(v)} wallets", lambda v: v >= t["min_sm_trader_count"])
    if t["require_sm_buying_24h"]:
        add("buying24", "Smart money still buying", "net flow over 24h above $0", d.get("sm_netflow_24h"),
            lambda v: f"{_usd(v, True)} over 24h", lambda v: v > 0)
    add("ath", "Coin had a real peak", f"ATH market cap ≥ ${t['min_ath_mcap_usd'] / 1e6:g}M", d.get("ath_mcap"),
        lambda v: f"${v / 1e6:.1f}M", lambda v: v >= t["min_ath_mcap_usd"])
    lo, hi = t["min_drawdown_pct"], t["max_drawdown_pct"]
    add("zone", "Pulled back into the buy zone", f"{lo:g}–{hi:g}% below the ATH close", d.get("drawdown_pct"),
        lambda v: f"{v:.1f}% below ATH", lambda v: lo <= v <= hi)

    held = d.get("held_support")
    deepest = _num(d.get("max_drawdown_pct"))
    out.append({"key": "support", "label": "Support held", "met": held if isinstance(held, bool) else None,
                "rule": f"no 1-minute close below the {hi:g}% line since the ATH",
                "actual": ("not recorded" if not isinstance(held, bool) else
                           ("never closed below the line" if held else "a close fell below the line")
                           + (f" (deepest {deepest:.1f}% below ATH)" if deepest is not None else ""))})
    add("hours", "Held the zone for a while", f"≥ {t['min_hours_in_zone']:g}h inside the zone", d.get("hours_in_zone"),
        lambda v: f"{_hours(v)}h in the zone", lambda v: v >= t["min_hours_in_zone"])
    if t["require_price_dropping"]:
        add("dropping", "Price falling", f"price down over {win}", d.get("price_window_pct"),
            lambda v: f"{v:+.1f}% over {win}", lambda v: v < 0)
    add("supply", "Smart money accumulating", f"bought ≥ {t['min_sm_supply_pct']:g}% of supply over {win}",
        d.get("sm_supply_pct"), lambda v: f"{v:.2f}% of supply over {win}", lambda v: v >= t["min_sm_supply_pct"])
    add("liq", "Enough liquidity", f"≥ ${t['min_liquidity_usd']:,.0f}", d.get("liquidity"),
        lambda v: f"${v:,.0f}", lambda v: v >= t["min_liquidity_usd"])
    add("liqratio", "Pool deep enough for its size", f"≥ {t['min_liquidity_to_mcap'] * 100:g}% of market cap",
        d.get("liq_ratio"), lambda v: f"{v * 100:.1f}% of market cap", lambda v: v >= t["min_liquidity_to_mcap"])
    if t["min_entry_score"] > 0:
        add("score", "Entry score", f"score ≥ {t['min_entry_score']:g}", d.get("score"),
            lambda v: f"{v:.0f} / 100", lambda v: v >= t["min_entry_score"])
    if d.get("confirm"):   # only saved when the optional Nansen confirmation ran and passed
        out.append({"key": "confirm", "label": "Extra Nansen confirmation", "rule": "flow-intelligence check passed",
                    "actual": str(d["confirm"]), "met": True})
    return out


def score_parts(d: dict) -> list[dict]:
    parts = d.get("parts") or {}
    return [{"label": SCORE_LABELS[k], "value": float(parts[k]), "max": SCORE_MAX[k]}
            for k in SCORE_MAX if _num(parts.get(k)) is not None]


def entry_story(symbol: str, d: dict, t: dict, checks: list[dict]) -> list[str]:
    """A few short paragraphs of plain text (the page escapes it). Skips any sentence whose numbers are missing."""
    win, hi, lo = t["accumulation_window"], t["max_drawdown_pct"], t["min_drawdown_pct"]
    paras: list[str] = []

    sup, n, chg, flow = (_num(d.get("sm_supply_pct")), _num(d.get("sm_traders")),
                         _num(d.get("price_window_pct")), _num(d.get("sm_netflow_window")))
    if sup is not None and n is not None:
        s = (f"Over the past {win}, {int(n)} smart wallets net bought about {sup:.2f}% of {symbol}'s supply"
             + (f" ({_usd(flow)})" if flow is not None else ""))
        if chg is not None and chg < 0:
            s += (f" while its price fell {abs(chg):.0f}%. Smart money buying into a falling price is the "
                  "divergence this bot is built to find.")
        elif chg is not None:
            s += f" while its price moved {chg:+.0f}%."
        else:
            s += "."
        paras.append(s)

    dd, ath = _num(d.get("drawdown_pct")), _num(d.get("ath_mcap"))
    if dd is not None:
        s = f"The price had pulled back to {dd:.1f}% below its all-time-high close"
        s += f" (ATH market cap ${ath / 1e6:.1f}M)" if ath is not None else ""
        s += f", {'inside' if lo <= dd <= hi else 'outside'} the {lo:g}–{hi:g}% buy zone."
        held, hrs = d.get("held_support"), _num(d.get("hours_in_zone"))
        if held is True and hrs is not None:
            s += (f" It had spent {_hours(hrs)} hours there without a single 1-minute candle closing below the "
                  f"{hi:g}% support line, so the bot read the support as holding.")
        elif held is False:
            s += f" However, a 1-minute candle had closed below the {hi:g}% support line."
        paras.append(s)

    n24, n1h, streak = _num(d.get("sm_netflow_24h")), _num(d.get("sm_netflow_1h")), int(d.get("streak") or 0)
    bits = []
    if n24 is not None and n24 > 0:
        bits.append(f"{_usd(n24, True)} over the last 24 hours")
    if n1h is not None and n1h > 0:
        bits.append(f"{_usd(n1h, True)} over the last hour")
    seen = f"shown up as a buyer in {streak} earlier scan{'s' if streak != 1 else ''}"
    if bits:
        paras.append("Smart money was still buying: " + " and ".join(bits) + (f", and had also {seen}." if streak else "."))
    elif streak:
        paras.append(f"Smart money had {seen}.")

    liq, ratio = _num(d.get("liquidity")), _num(d.get("liq_ratio"))
    if liq is not None and ratio is not None:
        paras.append(f"The pool held ${liq:,.0f} of liquidity ({ratio * 100:.1f}% of market cap), against minimums of "
                     f"${t['min_liquidity_usd']:,.0f} and {t['min_liquidity_to_mcap'] * 100:g}%.")

    parts, score = score_parts(d), _num(d.get("score"))
    if parts and score is not None:
        top = sorted(parts, key=lambda p: p["value"] / p["max"], reverse=True)[:2]
        paras.append(f"It scored {score:.0f}/100. Strongest parts: "
                     + " and ".join(f"{p['label'].lower()} ({p['value']:.0f}/{p['max']})" for p in top) + ".")

    known = [c for c in checks if c["met"] is not None]
    met = sum(1 for c in known if c["met"])
    if not known:
        pass
    elif met == len(checks):
        paras.append(f"All {len(checks)} entry checks were met, so the bot bought.")
    else:
        failed = [c["label"].lower() for c in known if not c["met"]]
        unknown = len(checks) - len(known)
        paras.append(f"{met} of {len(checks)} entry checks are marked met"
                     + (f"; not met: {', '.join(failed)}" if failed else "")
                     + (f"; {unknown} not recorded" if unknown else "") + ".")
    return paras


def exit_plan(d: dict, t: dict, entry: float) -> list[dict]:
    """What the bot was watching once it owned the coin. `price` (when set) is formatted by the page."""
    plan = []
    support = _num(d.get("support_price"))
    if t["exit_on_support_break"] and support:
        plan.append({"text": f"Sell if a 1-minute candle closes below the {t['max_drawdown_pct']:g}% support line at", "price": support})
    plan.append({"text": f"Sell if price falls {t['trailing_stop_pct']:g}% from its highest price since entry", "price": None})
    plan.append({"text": f"Hard stop: sell at −{t['hard_stop_loss_pct']:g}% from entry, around",
                 "price": entry * (1 - t["hard_stop_loss_pct"] / 100) if entry else None})
    if t["exit_on_sm_outflow"]:
        plan.append({"text": f"Sell if smart money's net flow over {t['sm_exit_window']} drops below {_usd(t['sm_exit_netflow_usd'])}", "price": None})
    plan.append({"text": f"Sell after {t['max_hold_days']:g} days at the latest", "price": None})
    return plan


def _loads(raw: Any) -> dict:
    if isinstance(raw, dict):
        return raw
    try:
        v = json.loads(raw) if raw else {}
    except (TypeError, ValueError):
        return {}
    return v if isinstance(v, dict) else {}


def _match_signal(buy_ts: float, token: str, signals: list[dict], used: set[int]) -> dict | None:
    """For buys saved before trades kept their own detail: the signal the bot logged for that buy."""
    best, best_gap = None, None
    for i, s in enumerate(signals):
        gap = s["ts"] - buy_ts
        if i in used or s["token"] != token or not (-SIGNAL_MATCH_BEFORE_S <= gap <= SIGNAL_MATCH_AFTER_S):
            continue
        if best_gap is None or abs(gap) < best_gap:
            best, best_gap = i, abs(gap)
    if best is None:
        return None
    used.add(best)
    return signals[best]["detail"]


def build_trade_cards(trades: list[dict], signals: list[dict], positions: list, thresholds_now: dict) -> list[dict]:
    """One card per buy (newest first), joined to its sell if it has been closed."""
    pos = {p.token: p for p in positions}
    cards, still_open, used = [], {}, set()
    for t in sorted(trades, key=lambda r: r["ts"]):
        if t["side"] == "buy":
            card = {"symbol": t["symbol"], "token": t["token"], "bought_at": t["ts"], "entry": t["price"],
                    "size": t["usd"], "mode": t["mode"], "status": "open", "reason": t["reason"] or "",
                    "pnl": None, "pnl_pct": None, "closed_at": None, "exit_price": None, "exit_reason": None,
                    "source": "none", "thresholds_today": False, "checks": [], "story": [], "score_parts": [],
                    "exit_plan": []}
            try:
                d = _loads(t.get("detail"))
                thr = d.get("thresholds")
                if d:
                    card["source"] = "recorded"
                else:
                    d = _match_signal(t["ts"], t["token"], signals, used) or {}
                    card["source"] = "signal log" if d else "none"
                if d:
                    if not isinstance(thr, dict):
                        thr, card["thresholds_today"] = thresholds_now, True
                    card["checks"] = entry_checks(d, thr)
                    card["story"] = entry_story(t["symbol"], d, thr, card["checks"])
                    card["score_parts"] = score_parts(d)
                    card["exit_plan"] = exit_plan(d, thr, t["price"])
            except Exception as e:   # one odd trade must never stop the dashboard from building
                log.warning("Could not explain one trade (%s); showing its summary only", type(e).__name__)
                card.update(source="none", checks=[], story=[], score_parts=[], exit_plan=[])
            cards.append(card)
            still_open[t["token"]] = card
        else:
            card = still_open.pop(t["token"], None)
            if card:
                card.update(status="closed", closed_at=t["ts"], exit_price=t["price"], exit_reason=t["reason"],
                            pnl=t["pnl_usd"], pnl_pct=(t["price"] / card["entry"] - 1) * 100 if card["entry"] else 0)
    for card in cards:
        p = pos.get(card["token"])
        if card["status"] == "open" and p:
            last = p.last_price or p.entry_price
            card.update(pnl=p.qty * last - p.size_usd, pnl_pct=(last / p.entry_price - 1) * 100, last=last)
    return list(reversed(cards))
