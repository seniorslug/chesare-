"""Offline tests: fake Nansen, DexScreener and candle data; real strategy and paper-trading code.
Run: python -m unittest discover tests
"""
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from bot import nansen as nansen_mod
from bot import runner, strategy
from bot.candles import Zone, analyze
from bot.config import load_settings
from bot.prices import Quote, parse_pairs
from bot.store import Position

GOOD, PUMPING, OLD, THIN, SMALL = "GoodMint", "PumpMint", "OldMint", "ThinMint", "SmallMint"


def q(token, price=1.0, h1=-1.0, h6=-5, h24=-30, liq=400_000, mcap=5_000_000, age=10):
    return Quote(token, token[:4].upper(), price, h1, h6, h24, liq, mcap, age, f"https://dex/{token}",
                 pair_address=f"pool-{token}")


def row(token, nf7d=60_000, nf24=8_000, nf1h=500, traders=8, age=10, sym=None):
    return {"token_address": token, "token_symbol": sym or token[:4].upper(), "net_flow_7d_usd": nf7d,
            "net_flow_24h_usd": nf24, "net_flow_1h_usd": nf1h, "trader_count": traders,
            "token_age_days": age, "market_cap_usd": 5e6}


def zone(ath=2.9, last=1.0, held=True, hours=6.0, avg7d=1.5, chg7d=-40.0, ok=True, deepest=None):
    return Zone(ok=ok, ath_close=ath, ath_ts=time.time() - 5 * 86400, last_close=last,
                drawdown_pct=(1 - last / ath) * 100,
                max_drawdown_pct=(1 - (deepest or last) / ath) * 100,
                support_price=ath * 0.30, zone_top_price=ath * 0.382, held_support=held,
                hours_in_zone=hours, avg_close={"24h": 1.05, "7d": avg7d, "30d": 1.8, "1h": 1.0},
                price_change={"24h": -5.0, "7d": chg7d, "30d": -20.0, "1h": 0.0})


NETFLOWS = [row(GOOD), row(PUMPING, nf24=-2_000), row(OLD, age=45), row(THIN), row(SMALL, nf7d=4_000)]
QUOTES = {GOOD: q(GOOD), PUMPING: q(PUMPING), OLD: q(OLD), THIN: q(THIN, liq=60_000), SMALL: q(SMALL)}


# ---------- synthetic candles ----------
class Market:
    """Minute-level price path -> hourly candles + a minute fetcher, like GeckoTerminal."""

    def __init__(self, hours, path, wick=0.0):
        self.now = 1_700_000_000 - (1_700_000_000 % 3600)
        self.start = self.now - hours * 3600
        self.mins = {t: path((t - self.start) / 3600, t) for t in range(self.start, self.now, 60)}
        self.wick = wick
        self.fetches = 0

    def hourly(self):
        out = []
        for hs in range(self.start, self.now, 3600):
            cs = [self.mins[t] for t in range(hs, hs + 3600, 60)]
            out.append((hs, cs[0], max(cs) + self.wick, min(cs) - self.wick, cs[-1]))
        return out

    def fetch(self, end, count):
        self.fetches += 1
        return [(t, 0, 0, 0, self.mins[t]) for t in range(int(end - count * 60) + 60, int(end) + 1, 60)
                if t in self.mins]


def pullback(hold_low=0.34, spike=None):
    """Rise to 1.0 by hour 50 (optional 1m spike close), fall to the zone by hour 200, then hold."""
    def path(h, t):
        if h < 50:
            return 0.2 + 0.8 * h / 50
        if h < 51:
            return spike if (spike and int((t % 3600) / 60) == 30) else 1.0
        if h < 200:
            return 1.0 - (1.0 - hold_low) * (h - 51) / 149
        return hold_low + 0.003 * ((t // 60) % 3)
    return path


class CandleTests(unittest.TestCase):
    def test_ath_uses_1m_close_and_wicks_dont_break_support(self):
        m = Market(240, pullback(spike=1.05), wick=0.03)  # hourly wicks dip below the 70% line
        z = analyze(m.hourly(), m.fetch, m.now, 61.8, 70)
        self.assertTrue(z.ok, z.error)
        self.assertAlmostEqual(z.ath_close, 1.05)          # spike close inside the peak hour
        self.assertAlmostEqual(z.support_price, 1.05 * 0.30)
        self.assertTrue(61.8 <= z.drawdown_pct <= 70, z.drawdown_pct)
        self.assertTrue(z.held_support)                      # only wicks went below; closes didn't
        self.assertGreater(z.hours_in_zone, 40)
        self.assertLess(z.price_change["7d"], 0)
        self.assertLessEqual(m.fetches, 6)

    def test_a_single_1m_close_below_the_line_breaks_support(self):
        m = Market(240, pullback())
        dip = m.now - 40 * 3600
        dip -= dip % 60
        m.mins[dip] = 0.29                                  # one 1m close below 0.30 (the 70% line)
        z = analyze(m.hourly(), m.fetch, m.now, 61.8, 70)
        self.assertTrue(z.ok, z.error)
        self.assertFalse(z.held_support)
        self.assertGreater(z.max_drawdown_pct, 70)

    def test_not_yet_in_zone(self):
        m = Market(240, pullback(hold_low=0.55))
        z = analyze(m.hourly(), m.fetch, m.now, 61.8, 70)
        self.assertLess(z.drawdown_pct, 61.8)
        self.assertEqual(z.hours_in_zone, 0.0)


class RuleTests(unittest.TestCase):
    def setUp(self):
        self.cfg = load_settings()

    def test_prefilter(self):
        keep, rejected = strategy.prefilter(NETFLOWS, QUOTES, self.cfg)
        self.assertEqual([r["token_address"] for r in keep], [GOOD])
        why = {r["token"]: " ".join(r["why"]) for r in rejected}
        self.assertIn("not buying", why[PUMPING])
        self.assertIn("age 45", why[OLD])
        self.assertIn("liquidity", why[THIN])
        self.assertIn("supply", why[SMALL])

    def test_all_rules_pass(self):
        c, fails, f = strategy.evaluate(row(GOOD), q(GOOD), zone(), 2, self.cfg)
        self.assertEqual(fails, [])
        self.assertTrue(c.qualified)
        # $60k at a $1.50 average = 40k tokens of 5M supply = 0.8%
        self.assertAlmostEqual(f["sm_supply_pct"], 0.8, places=3)
        self.assertAlmostEqual(f["ath_mcap"], 14.5e6, delta=1)

    def test_each_rule_can_fail(self):
        cases = {
            "ATH mcap": (row(GOOD), q(GOOD), zone(ath=1.8, last=0.65)),          # ATH $9M
            "needs 61.8": (row(GOOD), q(GOOD), zone(last=1.3)),                  # only 55% down
            "support at 70": (row(GOOD), q(GOOD), zone(held=False, deepest=0.8)),
            "in zone": (row(GOOD), q(GOOD), zone(hours=0.4)),
            "not dropping": (row(GOOD), q(GOOD), zone(chg7d=12)),
            "of supply": (row(GOOD, nf7d=40_000), q(GOOD), zone(avg7d=1.9)),     # 0.42%
            "candles": (row(GOOD), q(GOOD), Zone(ok=False, error="HTTP 404")),
        }
        for needle, (r, qq, z) in cases.items():
            c, fails, _ = strategy.evaluate(r, qq, z, 0, self.cfg)
            self.assertIsNone(c, needle)
            self.assertTrue(any(needle in f for f in fails), f"{needle}: {fails}")

    def test_score_rewards_more_supply_wallets_and_holding(self):
        _, _, weak = strategy.evaluate(row(GOOD, traders=3, nf1h=-100), q(GOOD), zone(hours=1.5), 0, self.cfg)
        _, _, strong = strategy.evaluate(row(GOOD, nf7d=150_000, traders=20), q(GOOD), zone(hours=20), 2, self.cfg)
        self.assertGreater(strong["score"], weak["score"] + 40)


class ExitTests(unittest.TestCase):
    def setUp(self):
        self.cfg = load_settings()

    def _pos(self):
        return Position(GOOD, "GOOD", 1.0, 50, 50, time.time(), 1.0)

    def test_trailing_stop_after_run_up(self):
        p = self._pos()
        for price in (1.2, 1.8, 2.0):
            strategy.update_peak(p, price)
            self.assertIsNone(strategy.exit_reason(p, price, time.time(), self.cfg))
        strategy.update_peak(p, 1.55)
        self.assertIsNone(strategy.exit_reason(p, 1.55, time.time(), self.cfg))
        strategy.update_peak(p, 1.45)
        self.assertIn("trailing", strategy.exit_reason(p, 1.45, time.time(), self.cfg))

    def test_hard_stop_smart_money_and_max_hold(self):
        p = self._pos()
        self.assertIn("hard stop", strategy.exit_reason(p, 0.6, time.time(), self.cfg))
        self.assertIn("smart money exiting", strategy.exit_reason(p, 1.0, time.time(), self.cfg, -12_000))
        self.assertIsNone(strategy.exit_reason(p, 1.0, time.time(), self.cfg, 3_000))
        p.opened_at -= 15 * 86400
        self.assertIn("max hold", strategy.exit_reason(p, 1.0, time.time(), self.cfg))

    def test_dexscreener_parse(self):
        now_ms = time.time() * 1000
        pairs = [
            {"baseToken": {"address": GOOD, "symbol": "GOOD"}, "priceUsd": "0.01", "liquidity": {"usd": 5000},
             "pairAddress": "small", "pairCreatedAt": now_ms - 5 * 86_400_000},
            {"baseToken": {"address": GOOD, "symbol": "GOOD"}, "priceUsd": "0.012", "liquidity": {"usd": 90000},
             "priceChange": {"h1": -2, "h6": -4, "h24": -25}, "marketCap": 1.5e6, "pairAddress": "deep",
             "txns": {"h1": {"buys": 10, "sells": 30}}, "pairCreatedAt": now_ms - 2 * 86_400_000},
        ]
        x = parse_pairs(GOOD, pairs)
        self.assertEqual((x.price_usd, x.pair_address, x.sells_1h), (0.012, "deep", 30))
        self.assertAlmostEqual(x.pair_age_days, 5, places=1)


class BotTests(unittest.TestCase):
    """Full paper cycle with fake APIs: scan -> buy -> support break / trailing -> lab -> dashboard."""

    def make_bot(self):
        cfg = load_settings()
        cfg.nansen_api_key = "test"
        cfg.data_dir = Path(tempfile.mkdtemp())
        self.calls = []
        self.sm_on_held = {"flow": 1000.0}
        self.prices = {"p": 1.0}
        self.minute_closes = []

        def fake_post(nansen, path, body, retries=3):
            if nansen.calls >= nansen.max_calls:
                raise runner.NansenError("cap")
            nansen.calls += 1
            self.calls.append(path)
            if body["filters"].get("token_address"):
                return {"data": [{"token_address": GOOD, "net_flow_24h_usd": self.sm_on_held["flow"]}]}
            return {"data": NETFLOWS}

        def quotes(tokens):
            base = dict(QUOTES); base[GOOD] = q(GOOD, price=self.prices["p"])
            return {t: base[t] for t in tokens if t in base}

        patches = [
            mock.patch.object(nansen_mod.Nansen, "_post", fake_post),
            mock.patch.object(runner, "get_quotes", side_effect=quotes),
            mock.patch.object(runner, "zone_for_pool", side_effect=lambda *a, **k: zone()),
            mock.patch.object(runner, "closes_since", side_effect=lambda *a, **k: self.minute_closes),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        bot = runner.Bot(cfg)
        bot.notify = lambda m: None
        return bot, cfg

    def test_buy_then_support_break_sells(self):
        bot, cfg = self.make_bot()
        bot.run_once(force_nansen=True)
        self.assertEqual(self.calls, ["/smart-money/netflow"])     # nothing open: 1 Nansen call
        pos = bot.store.positions()
        self.assertEqual([p.token for p in pos], [GOOD])
        p = pos[0]
        self.assertAlmostEqual(p.support_price, 2.9 * 0.30)
        self.assertIn("below ATH", p.entry_reason)
        self.assertIn("% of supply", p.entry_reason)
        lab = bot.store.lab_all()
        self.assertEqual(len(lab), 1)
        self.assertTrue(lab[0]["qualified"] and lab[0]["bought"])

        # A 1m candle closes below the 70% line -> sell, even though no stop was hit yet.
        t = time.time()
        self.minute_closes = [(t - 120, 0, 0, 0, 0.90), (t - 60, 0, 0, 0, 0.86)]
        self.prices["p"] = 0.86
        bot.run_once()
        self.assertEqual(bot.store.positions(), [])
        sell = [x for x in bot.store.trades() if x["side"] == "sell"][0]
        self.assertIn("support broken", sell["reason"])

    def test_two_calls_max_trailing_stop_and_dashboard(self):
        bot, cfg = self.make_bot()
        bot.run_once(force_nansen=True)
        self.calls.clear()
        self.prices["p"] = 2.2
        bot.run_once(force_nansen=True)                          # holding: exactly 2 calls
        self.assertEqual(len(self.calls), 2)
        self.calls.clear()
        for _ in range(3):                                        # within 4h: price-only runs
            bot.run_once()
        self.assertEqual(self.calls, [])
        self.prices["p"] = 1.6                                    # 27% off the 2.2 peak
        bot.run_once()
        sell = [x for x in bot.store.trades() if x["side"] == "sell"][0]
        self.assertIn("trailing", sell["reason"])
        self.assertGreater(sell["pnl_usd"], 20)

        from bot.dashboard import build
        html = build(bot.store, cfg, cfg.data_dir / "site").read_text()
        self.assertIn('"symbol": "GOOD"', html)
        enc = build(bot.store, cfg, cfg.data_dir / "site2", "pw").read_text()
        self.assertNotIn('"symbol": "GOOD"', enc)

    def test_smart_money_exit(self):
        bot, cfg = self.make_bot()
        bot.run_once(force_nansen=True)
        self.sm_on_held["flow"] = -20_000
        bot.run_once(force_nansen=True)
        sell = [x for x in bot.store.trades() if x["side"] == "sell"][0]
        self.assertIn("smart money exiting", sell["reason"])

    def test_lab_fills_forward_returns(self):
        bot, cfg = self.make_bot()
        bot.run_once(force_nansen=True)
        s = bot.store
        s.db.execute("UPDATE lab SET ts = ts - 25*3600")
        self.prices["p"] = 1.3
        bot.update_lab()
        r = s.lab_all()[0]
        self.assertAlmostEqual(r["r24h"], 30.0, places=3)
        self.assertIsNone(r["r72h"])


if __name__ == "__main__":
    unittest.main()
