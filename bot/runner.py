"""Main loop: scan for entries, manage open positions, log everything."""
from __future__ import annotations

import logging
import time

import requests

from . import strategy
from .gmgn import SOL_MINT, Gmgn, GmgnError
from .nansen import Nansen, NansenError
from .prices import get_quotes
from .store import Position, Store

log = logging.getLogger("memebot")


class Bot:
    def __init__(self, cfg):
        self.cfg = cfg
        self.store = Store(cfg.data_dir / "memebot.db")
        self.nansen = Nansen(cfg.nansen_api_key, cfg["nansen"]["max_calls_per_run"])
        self.mode = "live" if cfg.is_live else "paper"
        self.gmgn = None
        if self.mode == "live":
            g = cfg["gmgn"]
            self.gmgn = Gmgn(cfg.wallet_address, g["chain"], g["slippage_pct"], g["anti_mev"])
        elif cfg["mode"]["trading_mode"] == "live":
            log.warning("trading_mode is live but LIVE_TRADING_CONFIRM is not 'yes' — running PAPER")
        if self.store.get("cash") is None:
            self.store.set("cash", cfg["mode"]["paper_starting_balance_usd"])

    # ---------- notifications ----------
    def notify(self, msg: str) -> None:
        log.info(msg)
        if self.cfg.telegram_token and self.cfg.telegram_chat:
            try:
                requests.post(f"https://api.telegram.org/bot{self.cfg.telegram_token}/sendMessage",
                              json={"chat_id": self.cfg.telegram_chat, "text": msg}, timeout=10)
            except Exception as e:
                log.warning("Telegram failed: %s", e)

    # ---------- entries ----------
    def _day_start(self) -> float:
        t = time.localtime()
        return time.mktime((t.tm_year, t.tm_mon, t.tm_mday, 0, 0, 0, 0, 0, -1))

    def scan(self) -> None:
        cfg, r = self.cfg, self.cfg["risk"]
        open_pos = {p.token for p in self.store.positions()}
        # Even when we can't buy, scanning keeps the scan memory and signal lab fed.
        blocked = None
        if len(open_pos) >= r["max_open_positions"]:
            blocked = f"{len(open_pos)} positions open (max)"
        elif self.store.realized_pnl_since(self._day_start()) <= -r["max_daily_loss_usd"]:
            blocked = "daily loss limit hit"

        if self.nansen.calls_left < 1:
            log.info("Scan skipped: Nansen call cap for this run already used")
            return
        u = cfg["universe"]
        rows = self.nansen.smart_money_netflows(
            "solana", u["max_token_age_days"], u["min_market_cap_usd"], u["max_market_cap_usd"],
            u["smart_money_labels"], cfg["entry"]["min_sm_trader_count"])
        now = time.time()
        log.info("Nansen returned %d young tokens with smart money activity", len(rows))
        streaks = self.store.streaks(before=now)
        self.store.record_scan(rows, now)
        if not rows:
            return
        quotes = get_quotes([row["token_address"] for row in rows])
        candidates, rejected = strategy.screen(rows, quotes, cfg, streaks)
        qualified = [c for c in candidates if c.qualified]
        log.info("%d pass hard filters, %d score >= %s, %d rejected", len(candidates), len(qualified),
                 cfg["entry"]["min_entry_score"], len(rejected))

        bought: set[str] = set()
        cooldown = r["reentry_cooldown_hours"] * 3600
        confirm = cfg["entry"]["confirm_with_flow_intelligence"]
        for c in qualified:
            if blocked:
                log.info("Not buying: %s", blocked)
                break
            if len(open_pos) >= r["max_open_positions"]:
                break
            if c.token in open_pos or now - self.store.last_exit(c.token) < cooldown:
                continue
            if confirm:
                if self.nansen.calls_left < 1:
                    log.info("  %s not confirmed: no Nansen calls left this run", c.symbol)
                    break
                try:
                    flow = self.nansen.flow_intelligence("solana", c.token, cfg["entry"]["confirm_timeframe"])
                except NansenError as e:
                    log.warning("Confirm failed for %s: %s", c.symbol, e)
                    continue
                ok, note = strategy.confirmed(flow, cfg)
                c.detail["confirm"] = note
                if not ok:
                    self.store.log_signal(c.token, c.symbol, False, c.detail)
                    log.info("  %s rejected at confirmation (%s)", c.symbol, note)
                    continue
            if self.open_position(c):
                open_pos.add(c.token)
                bought.add(c.token)

        # Record signals and feed the lab: every scored coin, bought or not.
        tracked = {row["token"] for row in self.store.lab_open()}
        for c in candidates[: cfg["lab"]["track_per_scan"]]:
            self.store.log_signal(c.token, c.symbol, c.token in bought, c.detail)
            if c.token not in tracked:
                self.store.lab_add(c, c.token in bought)

    def update_lab(self) -> None:
        """Fill in each tracked coin's return 4h / 24h / 72h after it was scored. Free."""
        rows = self.store.lab_open()
        if not rows:
            return
        quotes = get_quotes(sorted({r["token"] for r in rows}))
        now = time.time()
        for r in rows:
            age_h = (now - r["ts"]) / 3600
            q = quotes.get(r["token"])
            if q and r["price0"]:
                ret = (q.price_usd / r["price0"] - 1) * 100
                if age_h <= 72:
                    r["max72"] = max(r["max72"] or 0.0, ret)
                    r["min72"] = min(r["min72"] or 0.0, ret)
                if r["r4h"] is None and age_h >= 4:
                    r["r4h"] = ret
                if r["r24h"] is None and age_h >= 24:
                    r["r24h"] = ret
                if r["r72h"] is None and age_h >= 72:
                    r["r72h"] = ret
                    r["done"] = 1
            elif age_h > 72 and q is None:
                # No price any more: pool gone. Count it as a total loss, which it usually is.
                for k in ("r4h", "r24h", "r72h"):
                    if r[k] is None:
                        r[k] = -100.0
                r["min72"], r["done"] = -100.0, 1
            if age_h > 96:
                r["done"] = 1
            self.store.lab_update(r)

    def open_position(self, c: strategy.Candidate) -> bool:
        r, x = self.cfg["risk"], self.cfg["exit"]
        size = r["position_size_usd"]
        cash = self.store.get("cash")
        if self.mode == "paper" and cash < size:
            log.info("Not enough paper cash for %s", c.symbol)
            return False
        price = c.quote.price_usd
        stop_id = ""
        if self.mode == "live":
            sol = get_quotes([SOL_MINT]).get(SOL_MINT)
            if not sol:
                log.warning("No SOL price; skipping buy")
                return False
            try:
                res = self.gmgn.buy(c.token, size / sol.price_usd, x["hard_stop_loss_pct"])
            except GmgnError as e:
                self.notify(f"❌ BUY FAILED {c.symbol}: {e}")
                return False
            stop_id = res.get("strategy_order_id", "") or ""
            fill = price  # refreshed by the next price poll; on-chain fill is in GMGN history
        else:
            fill = strategy.paper_fill(price, "buy", self.cfg)
            self.store.set("cash", cash - size)
        d = c.detail
        streak = d.get("streak", 0)
        reason = (f"Score {c.score:.0f}/100: SM +${c.sm_netflow_24h:,.0f}/24h "
                  f"({d.get('intensity_pct', 0):.1f}% of mcap) from {c.sm_traders} wallets"
                  f"{f', seen in {streak} earlier scan' + ('s' if streak != 1 else '') if streak else ''}"
                  f" while price {c.quote.change_24h:+.1f}%/24h"
                  f"{'; ' + d['confirm'] if d.get('confirm') else ''}")
        pos = Position(c.token, c.symbol, fill, size, size / fill, time.time(), fill,
                       reason, stop_id, time.time())
        self.store.save_position(pos)
        self.store.log_trade(c.token, c.symbol, "buy", fill, size, 0.0, reason, self.mode)
        self.notify(f"🟢 [{self.mode.upper()}] BUY {c.symbol} ${size:.0f} @ ${fill:.8g}\n{reason}\n{c.quote.pair_url}")
        return True

    # ---------- exits ----------
    def _sm_netflows(self, positions: list[Position]) -> dict[str, float]:
        """One Nansen call covering every open position."""
        x = self.cfg["exit"]
        if not x["exit_on_sm_outflow"] or self.nansen.calls_left < 1:
            return {}
        try:
            rows = self.nansen.netflows_for_tokens(
                "solana", [p.token for p in positions], self.cfg["universe"]["smart_money_labels"])
        except NansenError as e:
            log.warning("Smart money exit check failed: %s", e)
            return {}
        field = f"net_flow_{x['sm_exit_window']}_usd"
        return {t: float(r.get(field) or 0) for t, r in rows.items()}

    def manage(self, check_smart_money: bool = True) -> None:
        positions = self.store.positions()
        if not positions:
            return
        quotes = get_quotes([p.token for p in positions])
        sm = self._sm_netflows(positions) if check_smart_money else {}
        now = time.time()
        for p in positions:
            q = quotes.get(p.token)
            if not q:
                log.warning("No price for %s this round", p.symbol)
                continue
            strategy.update_peak(p, q.price_usd)
            p.last_price = q.price_usd
            if p.token in sm:
                p.last_sm_check = now
            why = strategy.exit_reason(p, q.price_usd, now, self.cfg, sm.get(p.token))
            if why:
                self.close_position(p, q.price_usd, why)
            else:
                self.store.save_position(p)

    def close_position(self, p: Position, price: float, why: str) -> None:
        if self.mode == "live":
            try:
                self.gmgn.sell_all(p.token)
            except GmgnError as e:
                self.notify(f"❌ SELL FAILED {p.symbol}: {e} — will retry next round")
                self.store.save_position(p)
                return
            if p.gmgn_stop_id:
                self.gmgn.cancel_strategy(p.gmgn_stop_id)
            fill = price
        else:
            fill = strategy.paper_fill(price, "sell", self.cfg)
        proceeds = p.qty * fill
        pnl = proceeds - p.size_usd
        if self.mode == "paper":
            self.store.set("cash", self.store.get("cash") + proceeds)
        self.store.remove_position(p.token)
        self.store.log_trade(p.token, p.symbol, "sell", fill, proceeds, pnl, why, self.mode)
        emoji = "✅" if pnl >= 0 else "🔴"
        self.notify(f"{emoji} [{self.mode.upper()}] SELL {p.symbol} {pnl:+.2f} USD ({(fill / p.entry_price - 1) * 100:+.1f}%)\n{why}")

    # ---------- loop ----------
    def equity(self) -> float:
        held = sum(p.qty * (p.last_price or p.entry_price) for p in self.store.positions())
        return self.store.get("cash", 0.0) + held

    def run_once(self, force_nansen: bool = False) -> None:
        try:
            self._run_once(force_nansen)
        finally:
            if self.mode == "paper":
                self.store.add_equity_point(self.equity())

    def _run_once(self, force_nansen: bool = False) -> None:
        try:
            self.update_lab()
        except Exception:
            log.exception("Signal lab update failed (continuing)")
        every = self.cfg["nansen"].get("nansen_every_hours", 0) * 3600
        last = self.store.get("last_nansen_run", 0)
        if not force_nansen and time.time() - last < every:
            self.manage(check_smart_money=False)
            log.info("Price-only run (next Nansen run in %.1fh)", (last + every - time.time()) / 3600)
            return
        self.store.set("last_nansen_run", time.time())
        self.nansen.new_run()
        self.manage()
        self.scan()
        log.info("Nansen calls this run: %d (credits reported: %.0f)",
                 self.nansen.calls, self.nansen.credits_used)

    def run_forever(self) -> None:
        t = self.cfg["timing"]
        self.notify(f"Memebot started in {self.mode.upper()} mode")
        last_scan = 0.0
        while True:
            try:
                if time.time() - last_scan >= t["scan_interval_min"] * 60:
                    self.run_once(force_nansen=True)  # Nansen calls only happen here, capped per run
                    last_scan = time.time()
                else:
                    self.manage(check_smart_money=False)  # free price checks in between
            except KeyboardInterrupt:
                raise
            except Exception:
                log.exception("Loop error (continuing)")
            time.sleep(t["manage_interval_min"] * 60)
