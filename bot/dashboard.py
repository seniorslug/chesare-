"""Builds a single self-contained dashboard page (site/index.html) from the trade database."""
from __future__ import annotations

import base64
import hashlib
import json
import os
import time
from pathlib import Path

from . import strategy
from .reasoning import build_trade_cards
from .store import Store


BUCKETS = [("70+", 70, 101), ("55–70", 55, 70), ("40–55", 40, 55), ("< 40", -1, 40)]


def lab_stats(rows: list[dict], min_score: float) -> dict:
    def summarize(sub):
        d24 = [r["r24h"] for r in sub if r["r24h"] is not None]
        d72 = [r["r72h"] for r in sub if r["r72h"] is not None]
        mx = [r["max72"] for r in sub if r["r24h"] is not None]
        mn = [r["min72"] for r in sub if r["r24h"] is not None]
        avg = lambda xs: sum(xs) / len(xs) if xs else None
        return {"n": len(sub), "n24": len(d24), "avg24": avg(d24),
                "up24": (sum(1 for x in d24 if x > 0) / len(d24) * 100) if d24 else None,
                "n72": len(d72), "avg72": avg(d72), "best": avg(mx), "worst": avg(mn)}
    buckets = [{"label": lab, **summarize([r for r in rows if lo <= r["score"] < hi])}
               for lab, lo, hi in BUCKETS]
    recent = [{"ts": r["ts"], "token": r["token"], "symbol": r["symbol"], "score": r["score"],
               "bought": bool(r["bought"]), "r4h": r["r4h"], "r24h": r["r24h"], "r72h": r["r72h"],
               "parts": r["features"].get("parts", {}), "qualified": bool(r["qualified"]),
               "dd": r["features"].get("drawdown_pct"), "fails": r["features"].get("fails", [])}
              for r in rows[:12]]
    return {"buckets": buckets, "recent": recent, "total": len(rows), "min_score": min_score,
            "qualified": summarize([r for r in rows if r["qualified"]]),
            "below": summarize([r for r in rows if not r["qualified"]])}


def collect(store: Store, cfg) -> dict:
    start = cfg["mode"]["paper_starting_balance_usd"]
    cash = store.get("cash", start)
    trades = [dict(t) for t in store.trades()]
    positions = store.positions()

    # Pair each sell with the most recent buy of that token before it.
    last_buy: dict[str, dict] = {}
    closed = []
    for t in trades:
        if t["side"] == "buy":
            last_buy[t["token"]] = t
        else:
            b = last_buy.get(t["token"], {})
            entry = b.get("price") or 0
            closed.append({
                "symbol": t["symbol"], "token": t["token"],
                "opened": b.get("ts"), "closed": t["ts"],
                "entry": entry, "exit": t["price"],
                "cost": b.get("usd") or 0, "pnl": t["pnl_usd"],
                "pnl_pct": (t["price"] / entry - 1) * 100 if entry else 0,
                "entry_reason": b.get("reason", ""), "exit_reason": t["reason"],
                "mode": t["mode"],
            })

    open_rows = []
    unrealized = 0.0
    for p in positions:
        last = p.last_price or p.entry_price
        value = p.qty * last
        unrealized += value - p.size_usd
        open_rows.append({
            "symbol": p.symbol, "token": p.token, "opened": p.opened_at,
            "entry": p.entry_price, "last": last, "peak": p.peak_price,
            "cost": p.size_usd, "value": value, "pnl": value - p.size_usd,
            "pnl_pct": (last / p.entry_price - 1) * 100,
            "peak_pct": (p.peak_price / p.entry_price - 1) * 100,
            "drawdown_pct": (1 - last / p.peak_price) * 100 if p.peak_price else 0,
            "reason": p.entry_reason, "support": p.support_price, "ath": p.ath_price,
        })

    wins = [c for c in closed if c["pnl"] > 0]
    losses = [c for c in closed if c["pnl"] <= 0]
    realized = sum(c["pnl"] for c in closed)
    equity = cash + sum(r["value"] for r in open_rows)
    x = cfg["exit"]
    every_h = cfg["nansen"].get("nansen_every_hours", 0)
    return {
        "updated": time.time(),
        "mode": cfg["mode"]["trading_mode"] if cfg.is_live else "paper",
        "summary": {
            "start": start, "cash": cash, "equity": equity,
            "return_pct": (equity / start - 1) * 100 if start else 0,
            "realized": realized, "unrealized": unrealized,
            "closed": len(closed), "open": len(open_rows),
            "win_rate": len(wins) / len(closed) * 100 if closed else None,
            "avg_win": sum(c["pnl"] for c in wins) / len(wins) if wins else None,
            "avg_loss": sum(c["pnl"] for c in losses) / len(losses) if losses else None,
            "best": max((c["pnl"] for c in closed), default=None),
            "worst": min((c["pnl"] for c in closed), default=None),
            "next_nansen": (store.get("last_nansen_run", 0) + every_h * 3600) if every_h else None,
        },
        "rules": {
            "trail": x["trailing_stop_pct"], "stop": x["hard_stop_loss_pct"],
            "sm_exit": x["sm_exit_netflow_usd"], "sm_window": x["sm_exit_window"],
            "size": cfg["risk"]["position_size_usd"], "max_open": cfg["risk"]["max_open_positions"],
            "min_dd": cfg["entry"]["min_drawdown_pct"], "max_dd": cfg["entry"]["max_drawdown_pct"],
            "min_ath": cfg["entry"]["min_ath_mcap_usd"], "min_supply": cfg["entry"]["min_sm_supply_pct"],
        },
        "open": open_rows,
        "closed": list(reversed(closed)),
        "trade_log": build_trade_cards(trades, store.taken_signals(), positions, strategy.entry_thresholds(cfg)),
        "equity": store.get("equity_history", []),
        "signals": store.signals(30),
        "lab": lab_stats(store.lab_all(), cfg["entry"].get("min_entry_score", 0)),
    }


PBKDF2_ITERS = 310_000


def encrypt(plaintext: bytes, password: str) -> dict:
    """AES-256-GCM with a PBKDF2-SHA256 key; the browser reverses it with WebCrypto."""
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    salt, iv = os.urandom(16), os.urandom(12)
    key = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, PBKDF2_ITERS, 32)
    ct = AESGCM(key).encrypt(iv, plaintext, None)  # ciphertext + 16-byte tag
    b64 = lambda b: base64.b64encode(b).decode()
    return {"salt": b64(salt), "iv": b64(iv), "iter": PBKDF2_ITERS, "ct": b64(ct)}


def build(store: Store, cfg, out_dir: Path, password: str | None = None) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    data = json.dumps(collect(store, cfg), default=float)
    payload = {"enc": encrypt(data.encode(), password)} if password else {"plain": json.loads(data)}
    html = TEMPLATE.replace("__PAYLOAD__", json.dumps(payload).replace("</", "<\\/"))
    path = out_dir / "index.html"
    path.write_text(html)
    return path


TEMPLATE = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="robots" content="noindex">
<title>Memebot Trades</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&family=JetBrains+Mono:wght@400;500&display=swap" rel="stylesheet">
<style>
:root{
  color-scheme:light;
  --bg:#f4f4f2; --surface:#fcfcfb; --surface-2:#f0f0ed; --border:#e3e2de;
  --text:#0b0b0b; --text-2:#52514e; --muted:#8a8984;
  --series:#2a78d6; --series-fill:rgba(42,120,214,.10);
  --good:#0a7d0a; --good-mark:#0ca30c; --bad:#c23030; --bad-mark:#d03b3b; --warn:#9a6a00;
  --grid:#e9e8e4;
}
@media (prefers-color-scheme:dark){
  :root:where(:not([data-theme="light"])){
    color-scheme:dark;
    --bg:#111110; --surface:#1a1a19; --surface-2:#232321; --border:#2e2e2b;
    --text:#f0efec; --text-2:#c3c2b7; --muted:#8d8c84;
    --series:#3987e5; --series-fill:rgba(57,135,229,.14);
    --good:#3fc43f; --good-mark:#0ca30c; --bad:#ef7070; --bad-mark:#d03b3b; --warn:#fab219;
    --grid:#2a2a27;
  }
}
:root[data-theme="dark"]{
  color-scheme:dark;
  --bg:#111110; --surface:#1a1a19; --surface-2:#232321; --border:#2e2e2b;
  --text:#f0efec; --text-2:#c3c2b7; --muted:#8d8c84;
  --series:#3987e5; --series-fill:rgba(57,135,229,.14);
  --good:#3fc43f; --good-mark:#0ca30c; --bad:#ef7070; --bad-mark:#d03b3b; --warn:#fab219;
  --grid:#2a2a27;
}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--text);font:15px/1.45 Inter,system-ui,-apple-system,sans-serif;-webkit-font-smoothing:antialiased}
.wrap{max-width:1100px;margin:0 auto;padding:20px 16px 48px}
.mono{font-family:"JetBrains Mono",ui-monospace,monospace;font-variant-numeric:tabular-nums}
header{display:flex;justify-content:space-between;align-items:flex-start;gap:12px;flex-wrap:wrap;margin-bottom:18px}
h1{font-size:18px;font-weight:600;margin:0;letter-spacing:-.01em}
.sub{color:var(--text-2);font-size:13px;margin-top:2px}
.pill{display:inline-flex;align-items:center;gap:6px;font-size:12px;font-weight:600;padding:4px 10px;border-radius:999px;background:var(--surface-2);border:1px solid var(--border);color:var(--text-2);text-transform:uppercase;letter-spacing:.04em}
.pill .dot{width:7px;height:7px;border-radius:50%;background:var(--warn)}
.pill.live .dot{background:var(--good-mark)}
.card{background:var(--surface);border:1px solid var(--border);border-radius:14px;padding:16px}
.hero{display:grid;grid-template-columns:1fr;gap:12px;margin-bottom:12px}
.hero-num{font-size:40px;font-weight:700;letter-spacing:-.03em;line-height:1.05}
.hero-label{color:var(--text-2);font-size:13px;font-weight:500}
.delta{font-weight:600;font-size:15px;margin-top:4px}
.pos{color:var(--good)} .neg{color:var(--bad)} .flat{color:var(--text-2)}
.tiles{display:grid;grid-template-columns:repeat(2,1fr);gap:8px;margin-bottom:12px}
.tile .k{color:var(--text-2);font-size:12px;font-weight:500}
.tile .v{font-size:20px;font-weight:600;margin-top:2px;letter-spacing:-.01em}
.tile .s{color:var(--muted);font-size:12px;margin-top:1px}
@media(min-width:720px){.tiles{grid-template-columns:repeat(4,1fr)}.hero-num{font-size:48px}}
section{margin-top:20px}
h2{font-size:14px;font-weight:600;margin:0 0 10px;color:var(--text);display:flex;justify-content:space-between;align-items:baseline}
h2 small{font-weight:400;color:var(--muted);font-size:12px}
.chart{position:relative;width:100%;touch-action:pan-y}
.chart svg{display:block;width:100%;overflow:visible}
.tip{position:absolute;pointer-events:none;background:var(--surface);border:1px solid var(--border);border-radius:8px;padding:6px 9px;font-size:12px;box-shadow:0 4px 16px rgba(0,0,0,.12);white-space:nowrap;opacity:0;transition:opacity .1s;z-index:2}
.tip b{display:block;font-weight:600;font-size:13px}
.tip span{color:var(--text-2)}
.empty{color:var(--muted);font-size:13px;padding:18px 4px;text-align:center}
.pos-card{border:1px solid var(--border);border-radius:12px;padding:12px;background:var(--surface);margin-bottom:8px}
.pos-top{display:flex;justify-content:space-between;align-items:baseline;gap:8px}
.sym{font-weight:600;font-size:16px}
.sym a{color:inherit;text-decoration:none;border-bottom:1px dotted var(--muted)}
.row{display:flex;flex-wrap:wrap;gap:4px 14px;font-size:12px;color:var(--text-2);margin-top:6px}
.row b{color:var(--text);font-weight:500}
.why{font-size:12px;color:var(--muted);margin-top:6px}
.bar{height:6px;border-radius:3px;background:var(--surface-2);margin-top:8px;position:relative;overflow:hidden}
.bar i{position:absolute;top:0;bottom:0;border-radius:3px}
table{width:100%;border-collapse:collapse;font-size:13px}
th{text-align:left;font-weight:500;color:var(--text-2);font-size:12px;padding:8px 6px;border-bottom:1px solid var(--border);white-space:nowrap}
td{padding:9px 6px;border-bottom:1px solid var(--border);vertical-align:top}
td.n,th.n{text-align:right}
.tbl{overflow-x:auto;-webkit-overflow-scrolling:touch}
.reason{color:var(--muted);font-size:12px;margin-top:2px;max-width:340px}
.tag{display:inline-block;font-size:11px;padding:1px 7px;border-radius:999px;border:1px solid var(--border);color:var(--text-2)}
.verdict{font-size:13px;padding:10px 12px;border-radius:10px;background:var(--surface-2);margin-bottom:10px;color:var(--text-2)}
.verdict b{color:var(--text)}
.score{display:inline-block;min-width:30px;text-align:center;font-weight:600;font-size:12px;padding:2px 6px;border-radius:6px;background:var(--surface-2);color:var(--text)}
.score.hi{background:var(--series);color:#fff}
.legend{display:flex;gap:14px;font-size:12px;color:var(--text-2);margin-top:6px}
.legend i{display:inline-block;width:10px;height:10px;border-radius:2px;margin-right:5px;vertical-align:-1px}
.lock{min-height:100vh;display:flex;align-items:center;justify-content:center;padding:24px 16px}
.lock form{width:100%;max-width:340px}
.lock h1{font-size:22px;margin-bottom:4px}
.lock input[type=password]{width:100%;font:inherit;font-size:16px;padding:12px 14px;border-radius:10px;border:1px solid var(--border);background:var(--surface);color:var(--text);margin-top:16px}
.lock input:focus{outline:2px solid var(--series);outline-offset:1px}
.lock button{width:100%;font:inherit;font-weight:600;font-size:16px;padding:12px;border-radius:10px;border:0;background:var(--text);color:var(--bg);margin-top:10px;cursor:pointer}
.lock button:disabled{opacity:.6}
.lock label.rem{display:flex;align-items:center;gap:8px;font-size:13px;color:var(--text-2);margin-top:12px}
.lock .err{color:var(--bad);font-size:13px;min-height:18px;margin-top:8px}
[hidden]{display:none!important}
footer{margin-top:28px;color:var(--muted);font-size:12px;line-height:1.6}
@media(max-width:560px){.hide-sm{display:none}}
details.trade{padding:0;margin-bottom:10px}
details.trade>summary{list-style:none;cursor:pointer;padding:14px 16px;display:flex;justify-content:space-between;align-items:flex-start;gap:6px 14px;flex-wrap:wrap}
details.trade>summary::-webkit-details-marker{display:none}
details.trade>summary:focus-visible{outline:2px solid var(--series);outline-offset:-2px;border-radius:14px}
.t-head{display:flex;align-items:center;gap:8px;flex-wrap:wrap}
.t-head::before{content:"\25B8";color:var(--muted);display:inline-block;transition:transform .15s}
details.trade[open] .t-head::before{transform:rotate(90deg)}
.tag.good{color:var(--good);border-color:var(--good)} .tag.bad{color:var(--bad);border-color:var(--bad)}
.t-body{padding:0 16px 16px;border-top:1px solid var(--border)}
.t-body h3{font-size:12px;font-weight:600;color:var(--text-2);text-transform:uppercase;letter-spacing:.04em;margin:16px 0 6px}
.story p{margin:8px 0;font-size:14px;line-height:1.55;color:var(--text-2)}
.story p:last-child{color:var(--text);font-weight:500}
.chk{font-weight:600;font-size:12px;white-space:nowrap}
.chk.ok{color:var(--good)} .chk.no{color:var(--bad)} .chk.na{color:var(--muted)}
.sp{display:grid;grid-template-columns:minmax(120px,1.4fr) 2fr 44px;gap:4px 10px;align-items:center;font-size:12px;color:var(--text-2);margin:5px 0}
.sp .trk{height:6px;border-radius:3px;background:var(--surface-2);overflow:hidden}
.sp .trk i{display:block;height:100%;border-radius:3px;background:var(--series)}
.plan{margin:0;padding-left:18px;font-size:13px;color:var(--text-2)}
.plan li{margin:3px 0}
.note{font-size:12px;color:var(--muted);margin-top:10px}
.tl-tools{font-weight:400;font-size:12px}
.tl-tools a{color:var(--text-2);cursor:pointer;margin-left:10px}
</style>
</head>
<body>
<div class="lock" id="lock" hidden>
  <form id="lock-form" autocomplete="on">
    <h1>Memebot</h1>
    <div class="sub">Enter the dashboard password.</div>
    <input type="text" name="username" value="memebot" autocomplete="username" hidden>
    <input type="password" id="pw" autocomplete="current-password" placeholder="Password" required autofocus>
    <label class="rem"><input type="checkbox" id="remember" checked> Remember on this device</label>
    <button type="submit" id="unlock">Unlock</button>
    <div class="err" id="err"></div>
  </form>
</div>
<div class="wrap" id="app" hidden>
  <header>
    <div>
      <h1>Memebot</h1>
      <div class="sub">Solana smart money divergence &middot; updated <span id="updated"></span></div>
    </div>
    <span id="mode" class="pill"><span class="dot"></span><span></span></span>
  </header>

  <div class="hero card">
    <div>
      <div class="hero-label">Account value</div>
      <div class="hero-num mono" id="equity"></div>
      <div class="delta mono" id="ret"></div>
    </div>
  </div>

  <div class="tiles" id="tiles"></div>

  <section class="card">
    <h2>Account value over time <small id="eq-range"></small></h2>
    <div class="chart" id="eq-chart"></div>
  </section>

  <section class="card">
    <h2>Profit / loss per closed trade <small>oldest &rarr; newest</small></h2>
    <div class="chart" id="pnl-chart"></div>
    <div class="legend"><span><i style="background:var(--good-mark)"></i>Win</span><span><i style="background:var(--bad-mark)"></i>Loss</span></div>
  </section>

  <section>
    <h2>Open positions <small id="open-count"></small></h2>
    <div id="open"></div>
  </section>

  <section class="card">
    <h2>Closed trades <small id="closed-count"></small></h2>
    <div class="tbl"><table id="closed"></table></div>
  </section>

  <section>
    <h2>Why the bot bought <small class="tl-tools"><span id="tl-count"></span><a id="tl-open" role="button" tabindex="0">Expand all</a><a id="tl-close" role="button" tabindex="0">Collapse all</a></small></h2>
    <div class="sub" style="margin:-4px 0 10px">One section per trade. The bot follows fixed rules, so each section is its checklist at the moment it bought: what each rule required, what the coin actually showed, and whether it was met.</div>
    <div id="tl"></div>
  </section>

  <section class="card">
    <h2>Signal lab <small id="lab-sub"></small></h2>
    <div class="sub" style="margin:-4px 0 10px">Every coin that gets a candle check is followed for 3 days, bought or not. Compares coins that passed every rule with near-misses, and returns by score.</div>
    <div id="lab-verdict"></div>
    <div class="tbl"><table id="lab"></table></div>
    <h2 style="margin-top:16px">Recently scored</h2>
    <div class="tbl"><table id="lab-recent"></table></div>
  </section>

  <section class="card">
    <h2>Recent signals <small>coins that got a candle check</small></h2>
    <div class="tbl"><table id="signals"></table></div>
  </section>

  <footer id="rules"></footer>
</div>

<script>
const $ = id => document.getElementById(id);
const P = __PAYLOAD__;
function render(D) {
$("app").hidden = false; $("lock").hidden = true;
const usd = (v, sign) => v == null ? "—" : (sign && v > 0 ? "+" : v < 0 ? "−" : "") + "$" + Math.abs(v).toLocaleString(undefined,{minimumFractionDigits:2,maximumFractionDigits:2});
const pct = v => v == null ? "—" : (v > 0 ? "+" : v < 0 ? "−" : "") + Math.abs(v).toFixed(1) + "%";
const cls = v => v > 0 ? "pos" : v < 0 ? "neg" : "flat";
const price = v => v >= 1 ? "$" + v.toFixed(4) : "$" + Number(v.toPrecision(4)).toString();
const when = ts => ts ? new Date(ts*1000).toLocaleString(undefined,{month:"short",day:"numeric",hour:"numeric",minute:"2-digit"}) : "—";
const ago = ts => { const m = (Date.now()/1000 - ts)/60; return m < 60 ? Math.round(m)+"m ago" : m < 1440 ? Math.round(m/60)+"h ago" : Math.round(m/1440)+"d ago"; };
const held = (a,b) => { const h = ((b||Date.now()/1000) - a)/3600; return h < 24 ? h.toFixed(1)+"h" : (h/24).toFixed(1)+"d"; };
const esc = s => String(s ?? "").replace(/[&<>"]/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]));
const dex = t => "https://dexscreener.com/solana/" + t;
const S = D.summary;

$("updated").textContent = ago(D.updated) + " (" + when(D.updated) + ")";
$("mode").classList.toggle("live", D.mode === "live");
$("mode").lastElementChild.textContent = D.mode === "live" ? "Live" : "Paper";
$("equity").textContent = usd(S.equity);
$("ret").innerHTML = `<span class="${cls(S.return_pct)}">${usd(S.equity - S.start, true)} (${pct(S.return_pct)})</span> <span class="flat" style="font-weight:400;font-family:Inter,system-ui,sans-serif">since start (${usd(S.start)})</span>`;

const tiles = [
  ["Realized P&L", usd(S.realized, true), cls(S.realized), `${S.closed} closed trade${S.closed===1?"":"s"}`],
  ["Unrealized P&L", usd(S.unrealized, true), cls(S.unrealized), `${S.open} open of ${D.rules.max_open} max`],
  ["Win rate", S.win_rate == null ? "—" : S.win_rate.toFixed(0) + "%", "", S.closed ? `${Math.round(S.win_rate*S.closed/100)}W / ${S.closed - Math.round(S.win_rate*S.closed/100)}L` : "no closed trades yet"],
  ["Avg win", usd(S.avg_win, true), cls(S.avg_win), `avg loss ${usd(S.avg_loss,true)} · worst ${usd(S.worst,true)}`],
];
$("tiles").innerHTML = tiles.map(([k,v,c,s]) => `<div class="tile card"><div class="k">${k}</div><div class="v mono ${c}">${v}</div><div class="s">${s}</div></div>`).join("");

// ---------- tooltip helper ----------
function tipFor(box){ const t = document.createElement("div"); t.className = "tip"; box.appendChild(t); return t; }
function placeTip(tip, box, x, y, html){
  tip.innerHTML = html; tip.style.opacity = 1;
  const bw = box.clientWidth, tw = tip.offsetWidth;
  tip.style.left = Math.min(Math.max(x - tw/2, 0), bw - tw) + "px";
  tip.style.top = Math.max(y - tip.offsetHeight - 12, -8) + "px";
}
const NS = "http://www.w3.org/2000/svg";
const el = (n, a, p) => { const e = document.createElementNS(NS, n); for (const k in a) e.setAttribute(k, a[k]); p && p.appendChild(e); return e; };
function niceTicks(lo, hi, n){
  if (lo === hi) { lo -= 1; hi += 1; }
  const step0 = (hi - lo) / n, mag = Math.pow(10, Math.floor(Math.log10(step0)));
  const step = [1,2,2.5,5,10].map(s => s*mag).find(s => s >= step0);
  const a = Math.floor(lo/step)*step, b = Math.ceil(hi/step)*step, out = [];
  for (let v = a; v <= b + step/2; v += step) out.push(+v.toFixed(6));
  return out;
}
const short = v => Math.abs(v) >= 100000 ? "$" + Math.round(v/1000) + "k" : (v < 0 ? "−$" : "$") + Math.abs(Math.round(v)).toLocaleString();

// ---------- equity line ----------
function drawEquity(){
  const box = $("eq-chart"); box.innerHTML = "";
  let pts = D.equity.slice();
  if (!pts.length) pts = [[D.updated, S.equity]];
  if (pts.length === 1) pts = [[pts[0][0] - 1800, S.start], pts[0]];
  const W = box.clientWidth, H = 200, m = {l:52, r:10, t:10, b:24};
  const xs = pts.map(p=>p[0]), ys = pts.map(p=>p[1]).concat([S.start]);
  const ticks = niceTicks(Math.min(...ys), Math.max(...ys), 4);
  const y0 = ticks[0], y1 = ticks[ticks.length-1], x0 = xs[0], x1 = xs[xs.length-1] || x0 + 1;
  const X = v => m.l + (v - x0) / (x1 - x0 || 1) * (W - m.l - m.r);
  const Y = v => m.t + (1 - (v - y0) / (y1 - y0)) * (H - m.t - m.b);
  const svg = el("svg", {viewBox:`0 0 ${W} ${H}`, height:H, role:"img", "aria-label":"Account value over time"}, box);
  ticks.forEach(t => {
    el("line", {x1:m.l, x2:W-m.r, y1:Y(t), y2:Y(t), stroke:"var(--grid)", "stroke-width":1}, svg);
    const tx = el("text", {x:m.l-8, y:Y(t)+4, "text-anchor":"end", "font-size":11, fill:"var(--muted)", class:"mono"}, svg); tx.textContent = short(t);
  });
  // starting balance reference
  el("line", {x1:m.l, x2:W-m.r, y1:Y(S.start), y2:Y(S.start), stroke:"var(--muted)", "stroke-dasharray":"3 4", "stroke-width":1}, svg);
  const st = el("text", {x:W-m.r, y:Y(S.start)-5, "text-anchor":"end", "font-size":11, fill:"var(--muted)"}, svg); st.textContent = "start";
  const line = pts.map((p,i) => (i?"L":"M") + X(p[0]).toFixed(1) + " " + Y(p[1]).toFixed(1)).join(" ");
  el("path", {d: line + ` L${X(x1)} ${H-m.b} L${X(x0)} ${H-m.b} Z`, fill:"var(--series-fill)"}, svg);
  el("path", {d: line, fill:"none", stroke:"var(--series)", "stroke-width":2, "stroke-linejoin":"round", "stroke-linecap":"round"}, svg);
  const fmtD = t => new Date(t*1000).toLocaleDateString(undefined,{month:"short",day:"numeric"});
  [[x0,"start"],[x1,"end"]].forEach(([t,a]) => { const tx = el("text", {x:X(t), y:H-6, "text-anchor":a, "font-size":11, fill:"var(--muted)"}, svg); tx.textContent = fmtD(t); });
  $("eq-range").textContent = D.equity.length ? `${D.equity.length} snapshots` : "";
  // crosshair
  const cross = el("line", {y1:m.t, y2:H-m.b, stroke:"var(--text-2)", "stroke-width":1, opacity:0}, svg);
  const dot = el("circle", {r:5, fill:"var(--series)", stroke:"var(--surface)", "stroke-width":2, opacity:0}, svg);
  const tip = tipFor(box);
  const move = ev => {
    const r = svg.getBoundingClientRect(), cx = (ev.touches ? ev.touches[0].clientX : ev.clientX) - r.left;
    let best = 0; pts.forEach((p,i) => { if (Math.abs(X(p[0]) - cx) < Math.abs(X(pts[best][0]) - cx)) best = i; });
    const p = pts[best], px = X(p[0]), py = Y(p[1]);
    cross.setAttribute("x1", px); cross.setAttribute("x2", px); cross.setAttribute("opacity", .4);
    dot.setAttribute("cx", px); dot.setAttribute("cy", py); dot.setAttribute("opacity", 1);
    placeTip(tip, box, px, py, `<b class="mono">${usd(p[1])}</b><span class="${cls(p[1]-S.start)}">${pct((p[1]/S.start-1)*100)}</span> <span>· ${when(p[0])}</span>`);
  };
  const leave = () => { cross.setAttribute("opacity",0); dot.setAttribute("opacity",0); tip.style.opacity = 0; };
  svg.addEventListener("pointermove", move); svg.addEventListener("pointerdown", move); svg.addEventListener("pointerleave", leave);
}

// ---------- per-trade P&L bars ----------
function drawPnl(){
  const box = $("pnl-chart"); box.innerHTML = "";
  const rows = D.closed.slice().reverse();
  if (!rows.length) { box.innerHTML = '<div class="empty">No closed trades yet. Bars appear here as positions are sold.</div>'; return; }
  const W = box.clientWidth, H = 180, m = {l:52, r:10, t:10, b:10};
  const vals = rows.map(r=>r.pnl), ticks = niceTicks(Math.min(0,...vals), Math.max(0,...vals), 4);
  const y0 = ticks[0], y1 = ticks[ticks.length-1];
  const Y = v => m.t + (1 - (v - y0)/(y1 - y0)) * (H - m.t - m.b);
  const slot = (W - m.l - m.r) / rows.length, bw = Math.max(Math.min(slot - 2, 28), 2);
  const svg = el("svg", {viewBox:`0 0 ${W} ${H}`, height:H, role:"img", "aria-label":"Profit or loss per closed trade"}, box);
  ticks.forEach(t => {
    el("line", {x1:m.l, x2:W-m.r, y1:Y(t), y2:Y(t), stroke: t === 0 ? "var(--muted)" : "var(--grid)", "stroke-width":1}, svg);
    const tx = el("text", {x:m.l-8, y:Y(t)+4, "text-anchor":"end", "font-size":11, fill:"var(--muted)", class:"mono"}, svg); tx.textContent = short(t);
  });
  const tip = tipFor(box);
  rows.forEach((r,i) => {
    const cx = m.l + slot*i + slot/2, top = Y(Math.max(r.pnl,0)), bot = Y(Math.min(r.pnl,0)), h = Math.max(bot-top, 1);
    const up = r.pnl > 0, rad = Math.min(4, bw/2, h);
    const x = cx - bw/2;
    // rounded end away from the zero line
    const d = up
      ? `M${x} ${bot} V${top+rad} Q${x} ${top} ${x+rad} ${top} H${x+bw-rad} Q${x+bw} ${top} ${x+bw} ${top+rad} V${bot} Z`
      : `M${x} ${top} V${bot-rad} Q${x} ${bot} ${x+rad} ${bot} H${x+bw-rad} Q${x+bw} ${bot} ${x+bw} ${bot-rad} V${top} Z`;
    el("path", {d, fill: up ? "var(--good-mark)" : "var(--bad-mark)"}, svg);
    const hit = el("rect", {x: m.l + slot*i, y: m.t, width: slot, height: H-m.t-m.b, fill:"transparent"}, svg);
    const show = () => placeTip(tip, box, cx, top, `<b>${esc(r.symbol)} <span class="mono ${cls(r.pnl)}">${usd(r.pnl,true)}</span></b><span class="mono">${pct(r.pnl_pct)} · held ${held(r.opened, r.closed)}</span><br><span>${esc(r.exit_reason).slice(0,60)}</span>`);
    hit.addEventListener("pointerenter", show); hit.addEventListener("pointerdown", show);
    hit.addEventListener("pointerleave", () => tip.style.opacity = 0);
  });
}

// ---------- open positions ----------
$("open-count").textContent = `${D.open.length} open`;
$("open").innerHTML = D.open.length ? D.open.map(p => {
  const stopAt = -D.rules.stop, trailAt = p.peak * (1 - D.rules.trail/100);
  const range = Math.max(p.peak_pct, 5) - stopAt, at = (p.pnl_pct - stopAt) / range * 100;
  return `<div class="pos-card">
    <div class="pos-top"><span class="sym"><a href="${dex(p.token)}" target="_blank" rel="noopener">${esc(p.symbol)}</a></span>
      <span class="mono ${cls(p.pnl)}" style="font-weight:600">${usd(p.pnl,true)} (${pct(p.pnl_pct)})</span></div>
    <div class="row"><span>Entry <b class="mono">${price(p.entry)}</b></span><span>Now <b class="mono">${price(p.last)}</b></span><span>Peak <b class="mono">${pct(p.peak_pct)}</b></span><span>Held <b>${held(p.opened)}</b></span></div>
    <div class="row"><span>Trailing stop at <b class="mono">${price(trailAt)}</b> (${p.drawdown_pct.toFixed(1)}% of ${D.rules.trail}% used)</span>${p.support ? `<span>Support (${D.rules.max_dd}% below ATH) <b class="mono">${price(p.support)}</b> · ${pct((p.support / p.last - 1) * 100)} away</span>` : ""}</div>
    <div class="bar" title="Position between hard stop (left) and peak (right)"><i style="left:0;width:${Math.min(Math.max(at,2),100).toFixed(1)}%;background:${p.pnl >= 0 ? "var(--good-mark)" : "var(--bad-mark)"}"></i></div>
    <div class="why">${esc(p.reason)}</div>
  </div>`;
}).join("") : '<div class="card empty">No open positions. The bot is waiting for a smart money divergence signal.</div>';

// ---------- closed trades ----------
$("closed-count").textContent = `${D.closed.length} total`;
$("closed").innerHTML = D.closed.length ? `<thead><tr><th>Coin</th><th class="n">P&amp;L</th><th class="n hide-sm">Entry</th><th class="n hide-sm">Exit</th><th class="n">Held</th></tr></thead><tbody>` +
  D.closed.map(c => `<tr><td><a class="sym" style="font-size:14px;color:inherit" href="${dex(c.token)}" target="_blank" rel="noopener">${esc(c.symbol)}</a><div class="reason">${esc(c.exit_reason)}</div><div class="reason">${when(c.closed)}</div></td>
    <td class="n mono ${cls(c.pnl)}">${usd(c.pnl,true)}<div class="reason">${pct(c.pnl_pct)}</div></td>
    <td class="n mono hide-sm">${price(c.entry)}</td><td class="n mono hide-sm">${price(c.exit)}</td>
    <td class="n">${held(c.opened, c.closed)}</td></tr>`).join("") + "</tbody>"
  : '<tbody><tr><td class="empty">No closed trades yet.</td></tr></tbody>';

// ---------- why the bot bought (one section per trade) ----------
const TL = D.trade_log || [];
$("tl-count").textContent = TL.length ? `${TL.length} trade${TL.length === 1 ? "" : "s"}` : "";
const mark = m => m === true ? '<span class="chk ok">✓ Met</span>' : m === false ? '<span class="chk no">✗ Not met</span>' : '<span class="chk na">? Not recorded</span>';
$("tl").innerHTML = TL.length ? TL.map((c, i) => {
  const total = c.checks.length, ok = c.checks.filter(k => k.met === true).length, bad = c.checks.filter(k => k.met === false).length;
  const badge = !total ? "" : bad ? `<span class="tag bad">${ok}/${total} checks met</span>` : ok === total ? `<span class="tag good">All ${total} checks met</span>` : `<span class="tag">${ok}/${total} checks met</span>`;
  const state = c.status === "open" ? '<span class="tag">open</span>' : '<span class="tag">closed</span>';
  const pnl = c.pnl == null ? "" : `<span class="mono ${cls(c.pnl)}" style="font-weight:600">${usd(c.pnl,true)} (${pct(c.pnl_pct)})</span>`;
  const outcome = c.status === "closed"
    ? `Sold ${when(c.closed_at)} at ${price(c.exit_price)} after ${held(c.bought_at, c.closed_at)}: ${esc(c.exit_reason)}`
    : (c.last ? `Still open, now ${price(c.last)} after ${held(c.bought_at)}.` : "Still open.");
  const story = c.story.length ? `<h3>The reasoning</h3><div class="story">${c.story.map(p => `<p>${esc(p)}</p>`).join("")}</div>`
    : `<h3>The reasoning</h3><div class="story"><p>${esc(c.reason) || "No entry details were saved for this trade."}</p></div><div class="note">The full entry checklist wasn't saved for this trade.</div>`;
  const checks = total ? `<h3>Entry checklist</h3><div class="tbl"><table><thead><tr><th>Check</th><th>What the coin showed</th><th class="n">Result</th></tr></thead><tbody>` +
    c.checks.map(k => `<tr><td>${esc(k.label)}<div class="reason">Needs: ${esc(k.rule)}</div></td><td class="mono">${esc(k.actual)}</td><td class="n">${mark(k.met)}</td></tr>`).join("") + "</tbody></table></div>" : "";
  const parts = c.score_parts.length ? `<h3>Score breakdown</h3>` + c.score_parts.map(p =>
    `<div class="sp"><span>${esc(p.label)}</span><span class="trk" aria-hidden="true"><i style="width:${Math.min(100, p.value / p.max * 100).toFixed(0)}%"></i></span><span class="mono n">${p.value.toFixed(0)}/${p.max}</span></div>`).join("") : "";
  const plan = c.exit_plan.length ? `<h3>What it watched after buying</h3><ul class="plan">` +
    c.exit_plan.map(x => `<li>${esc(x.text)}${x.price ? ` <b class="mono">${price(x.price)}</b>` : ""}</li>`).join("") + "</ul>" : "";
  const src = c.source === "signal log" ? "Rebuilt from the bot's signal log for this buy" + (c.thresholds_today ? "; rule thresholds shown are today's settings." : ".") : "";
  return `<details class="trade card"${i < 3 ? " open" : ""}>
    <summary>
      <div><div class="t-head"><a class="sym" href="${dex(c.token)}" target="_blank" rel="noopener" onclick="event.stopPropagation()">${esc(c.symbol)}</a> ${state} ${badge}</div>
        <div class="reason" style="margin-left:18px">Bought ${when(c.bought_at)} at <span class="mono">${price(c.entry)}</span> · ${usd(c.size)}</div></div>
      <div>${pnl}</div>
    </summary>
    <div class="t-body">${story}${checks}${parts}${plan}
      <h3>Outcome</h3><div class="story"><p style="color:var(--text);font-weight:400">${outcome}</p></div>
      ${src ? `<div class="note">${esc(src)}</div>` : ""}</div>
  </details>`;
}).join("") : '<div class="card empty">No trades yet. A section appears here for every buy, with the checks it passed.</div>';
const setAll = open => document.querySelectorAll("details.trade").forEach(x => x.open = open);
[["tl-open", true], ["tl-close", false]].forEach(([id, v]) => { const a = $(id); a.addEventListener("click", () => setAll(v));
  a.addEventListener("keydown", e => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); setAll(v); } }); });

// ---------- signal lab ----------
const L = D.lab;
$("lab-sub").textContent = `${L.total} coins tracked`;
const pctOr = v => v == null ? '<span class="flat">—</span>' : `<span class="${cls(v)}">${pct(v)}</span>`;
const scoreTag = (v, hi) => `<span class="score ${hi ? "hi" : ""}">${Math.round(v)}</span>`;
(() => {
  const q = L.qualified, b = L.below;
  let msg;
  if (q.n24 < 10 || b.n24 < 10) msg = `<b>Still collecting data.</b> ${q.n24} coins that passed every rule and ${b.n24} that missed one have a 24h result. Give it about 30 of each before trusting the comparison.`;
  else if (q.avg24 > b.avg24) msg = `<b>Rules are working:</b> coins passing every rule averaged ${pct(q.avg24)} after 24h vs ${pct(b.avg24)} for coins that missed one (${q.n24} vs ${b.n24} coins).`;
  else msg = `<b>Rules aren't beating the near-misses yet:</b> passing coins averaged ${pct(q.avg24)} after 24h vs ${pct(b.avg24)} for coins that missed a rule. Worth revisiting the thresholds.`;
  $("lab-verdict").innerHTML = `<div class="verdict">${msg}</div>`;
})();
$("lab").innerHTML = `<thead><tr><th>Score</th><th class="n">Coins</th><th class="n">Avg 24h</th><th class="n">Up after 24h</th><th class="n hide-sm">Avg 72h</th><th class="n hide-sm">Best / worst</th></tr></thead><tbody>` +
  L.buckets.map(k => `<tr><td>${k.label}</td><td class="n">${k.n}${k.n24 < k.n ? `<div class="reason">${k.n24} with 24h</div>` : ""}</td>
    <td class="n mono">${pctOr(k.avg24)}</td><td class="n mono">${k.up24 == null ? "—" : Math.round(k.up24) + "%"}</td>
    <td class="n mono hide-sm">${pctOr(k.avg72)}</td><td class="n mono hide-sm">${pctOr(k.best)} / ${pctOr(k.worst)}</td></tr>`).join("") + "</tbody>";
$("lab-recent").innerHTML = L.recent.length ? `<thead><tr><th>Coin</th><th class="n">Score</th><th class="n">4h</th><th class="n">24h</th><th class="n hide-sm">72h</th></tr></thead><tbody>` +
  L.recent.map(r => `<tr><td><a href="${dex(r.token)}" target="_blank" rel="noopener" style="color:inherit">${esc(r.symbol)}</a> ${r.bought ? '<span class="tag">bought</span>' : r.qualified ? '<span class="tag">passed</span>' : ""}<div class="reason">${ago(r.ts)}${r.dd != null ? " · " + r.dd.toFixed(1) + "% below ATH" : ""}</div>${r.fails.length ? `<div class="reason">✗ ${esc(r.fails[0])}</div>` : ""}</td>
    <td class="n" title="${esc(Object.entries(r.parts).map(([k,v]) => k + " " + v).join(", "))}">${scoreTag(r.score, r.qualified)}</td>
    <td class="n mono">${pctOr(r.r4h)}</td><td class="n mono">${pctOr(r.r24h)}</td><td class="n mono hide-sm">${pctOr(r.r72h)}</td></tr>`).join("") + "</tbody>"
  : '<tbody><tr><td class="empty">Nothing scored yet. The next Nansen scan fills this in.</td></tr></tbody>';

// ---------- signals ----------
$("signals").innerHTML = D.signals.length ? `<thead><tr><th>Coin</th><th class="n">SM supply</th><th class="n">Below ATH</th><th class="n">Score</th><th class="n hide-sm">Wallets</th><th>When</th></tr></thead><tbody>` +
  D.signals.map(s => { const d = s.detail || {}; return `<tr><td><a href="${dex(s.token)}" target="_blank" rel="noopener" style="color:inherit">${esc(s.symbol)}</a> ${s.taken ? '<span class="tag">bought</span>' : '<span class="tag">skipped</span>'}</td>
    <td class="n mono">${d.sm_supply_pct != null ? d.sm_supply_pct.toFixed(2) + "%" : "—"}</td>
    <td class="n mono">${d.drawdown_pct != null ? d.drawdown_pct.toFixed(1) + "%" : "—"}</td><td class="n">${d.score != null ? scoreTag(d.score, !(d.fails || []).length) : "—"}</td><td class="n hide-sm">${d.sm_traders ?? "—"}</td><td>${ago(s.ts)}</td></tr>`; }).join("") + "</tbody>"
  : '<tbody><tr><td class="empty">No signals yet.</td></tr></tbody>';

$("rules").innerHTML = `Entry: ATH market cap ≥ $${(D.rules.min_ath/1e6).toFixed(0)}M, latest 1m close ${D.rules.min_dd}–${D.rules.max_dd}% below the ATH close, no close below the ${D.rules.max_dd}% line, smart money bought ≥ ${D.rules.min_supply}% of supply while price fell.<br>Exits: $${D.rules.size} per trade, max ${D.rules.max_open} open · 1m close below the ${D.rules.max_dd}% line · trailing stop ${D.rules.trail}% from peak · hard stop −${D.rules.stop}% · sell when smart money net flow over ${D.rules.sm_window} is below ${usd(D.rules.sm_exit)}.<br>` +
  `Prices refresh every run (about every 30 min). ${S.next_nansen ? "Next Nansen scan " + (S.next_nansen*1000 < Date.now() ? "on the next run" : "around " + when(S.next_nansen)) + "." : ""} ${D.mode === "paper" ? "Paper trades: fills include an assumed 4% round-trip slippage and fees." : ""}`;

if (!P.plain) { $("rules").insertAdjacentHTML("beforeend", '<br><a href="#" id="lockbtn" style="color:var(--text-2)">Lock on this device</a>');
  $("lockbtn").addEventListener("click", e => { e.preventDefault(); store.del(); location.reload(); }); }
drawEquity(); drawPnl();
let rt; addEventListener("resize", () => { clearTimeout(rt); rt = setTimeout(() => { drawEquity(); drawPnl(); }, 150); });
}

// ---------- password unlock ----------
const KEY = "memebot.pw";
const store = { get(){ try { return localStorage.getItem(KEY); } catch(e) { return null; } },
                set(v){ try { localStorage.setItem(KEY, v); } catch(e) {} },
                del(){ try { localStorage.removeItem(KEY); } catch(e) {} } };
const unb64 = s => Uint8Array.from(atob(s), c => c.charCodeAt(0));
async function decrypt(pw){
  const e = P.enc, enc = new TextEncoder();
  const base = await crypto.subtle.importKey("raw", enc.encode(pw), "PBKDF2", false, ["deriveKey"]);
  const key = await crypto.subtle.deriveKey({name:"PBKDF2", salt:unb64(e.salt), iterations:e.iter, hash:"SHA-256"},
    base, {name:"AES-GCM", length:256}, false, ["decrypt"]);
  const pt = await crypto.subtle.decrypt({name:"AES-GCM", iv:unb64(e.iv)}, key, unb64(e.ct));
  return JSON.parse(new TextDecoder().decode(pt));
}
async function tryUnlock(pw, remember){
  const D = await decrypt(pw);           // throws on a wrong password
  if (remember) store.set(pw); else store.del();
  render(D);
}
if (P.plain) render(P.plain);
else {
  const saved = store.get();
  (saved ? tryUnlock(saved, true) : Promise.reject()).catch(() => {
    store.del();
    $("lock").hidden = false;
    $("pw").focus();
  });
  $("lock-form").addEventListener("submit", async ev => {
    ev.preventDefault();
    const btn = $("unlock"); btn.disabled = true; btn.textContent = "Unlocking…"; $("err").textContent = "";
    try { await tryUnlock($("pw").value, $("remember").checked); }
    catch (e) { $("err").textContent = "Wrong password."; $("pw").select(); }
    btn.disabled = false; btn.textContent = "Unlock";
  });
}
</script>
</body>
</html>
"""
