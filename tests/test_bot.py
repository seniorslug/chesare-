"""Offline tests: fake Nansen + DexScreener data, real strategy and paper-trading code.
Run: python -m unittest discover tests
"""
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from bot import nansen as nansen_mod
from bot import runner, strategy
from bot.config import load_settings
from bot.prices import Quote, parse_pairs
from bot.store import Position

GOOD, PUMPING, OLD, THIN = "GoodMint", "PumpMint", "OldMint", "ThinMint"


def q(token, price=1.0, h6=-5, h24=-30, liq=200_000, mcap=2_000_000, age=10):
    return Quote(token, token[:4].upper(), price, -1, h6, h24, liq, mcap, age, f"https://dex/{token}")


NETFLOWS = [
    {"token_address": GOOD, "token_symbol": "GOOD", "net_flow_24h_usd": 60_000, "trader_count": 6, "token_age_days": 10, "market_cap_usd": 2e6},
    {"token_address": PUMPING, "token_symbol": "PUMP", "net_flow_24h_usd": 90_000, "trader_count": 9, "token_age_days": 5, "market_cap_usd": 3e6},
    {"token_address": OLD, "token_symbol": "OLD", "net_flow_24h_usd": 50_000, "trader_count": 5, "token_age_days": 45, "market_cap_usd": 5e6},
    {"token_address": THIN, "token_symbol": "THIN", "net_flow_24h_usd": 20_000, "trader_count": 4, "token_age_days": 3, "market_cap_usd": 5e5},
]
QUOTES = {GOOD: q(GOOD), PUMPING: q(PUMPING, h24=+40, h6=+12), OLD: q(OLD, age=45), THIN: q(THIN, liq=20_000)}


class StrategyTests(unittest.TestCase):
    def setUp(self):
        self.cfg = load_settings()

    def test_screen_keeps_only_divergence(self):
        passed, rejected = strategy.screen(NETFLOWS, QUOTES, self.cfg)
        self.assertEqual([c.token for c in passed], [GOOD])
        why = {r["token"]: " ".join(r["why"]) for r in rejected}
        self.assertIn("not dropping", why[PUMPING])
        self.assertIn("age 45", why[OLD])
        self.assertIn("liquidity", why[THIN])

    def test_confirmation(self):
        ok, _ = strategy.confirmed({"smart_trader_net_flow_usd": 8000, "smart_trader_wallet_count": 4}, self.cfg)
        bad, _ = strategy.confirmed({"smart_trader_net_flow_usd": -2000, "smart_trader_wallet_count": 4}, self.cfg)
        self.assertTrue(ok)
        self.assertFalse(bad)

    def _pos(self):
        return Position(GOOD, "GOOD", 1.0, 50, 50, time.time(), 1.0)

    def test_trailing_stop_after_run_up(self):
        p = self._pos()
        for price in (1.2, 1.8, 2.0):
            strategy.update_peak(p, price)
            self.assertIsNone(strategy.exit_reason(p, price, time.time(), self.cfg))
        strategy.update_peak(p, 1.55)  # 22.5% off peak: hold
        self.assertIsNone(strategy.exit_reason(p, 1.55, time.time(), self.cfg))
        strategy.update_peak(p, 1.45)  # 27.5% off peak: sell, still +45%
        self.assertIn("trailing", strategy.exit_reason(p, 1.45, time.time(), self.cfg))

    def test_hard_stop(self):
        p = self._pos()
        self.assertIn("hard stop", strategy.exit_reason(p, 0.6, time.time(), self.cfg))

    def test_follow_smart_money_out(self):
        p = self._pos()
        strategy.update_peak(p, 1.1)
        why = strategy.exit_reason(p, 1.1, time.time(), self.cfg, -12_000)
        self.assertIn("smart money exiting", why)
        self.assertIsNone(strategy.exit_reason(p, 1.1, time.time(), self.cfg, 3_000))
        self.assertIsNone(strategy.exit_reason(p, 1.1, time.time(), self.cfg, None))

    def test_max_hold(self):
        p = self._pos()
        p.opened_at -= 15 * 86400
        self.assertIn("max hold", strategy.exit_reason(p, 1.0, time.time(), self.cfg))

    def test_dexscreener_parse_picks_deepest_pool(self):
        now_ms = time.time() * 1000
        pairs = [
            {"baseToken": {"address": GOOD, "symbol": "GOOD"}, "priceUsd": "0.01", "liquidity": {"usd": 5000},
             "priceChange": {"h24": -10}, "pairCreatedAt": now_ms - 5 * 86_400_000},
            {"baseToken": {"address": GOOD, "symbol": "GOOD"}, "priceUsd": "0.012", "liquidity": {"usd": 90000},
             "priceChange": {"h1": -2, "h6": -4, "h24": -25}, "marketCap": 1.5e6, "pairCreatedAt": now_ms - 2 * 86_400_000},
            {"baseToken": {"address": "Other"}, "priceUsd": "9", "liquidity": {"usd": 1e9}},
        ]
        x = parse_pairs(GOOD, pairs)
        self.assertEqual(x.price_usd, 0.012)
        self.assertEqual(x.change_24h, -25)
        self.assertAlmostEqual(x.pair_age_days, 5, places=1)  # oldest pool counts for age


class PaperLoopTest(unittest.TestCase):
    """Full cycle: scan -> buy -> price runs up -> trailing stop sells -> P&L recorded."""

    def test_end_to_end_paper(self):
        cfg = load_settings()
        cfg.nansen_api_key = "test"
        cfg.live_confirm = False
        cfg.data_dir = Path(tempfile.mkdtemp())
        prices = {"p": 1.0}
        sm_on_good = {"flow": 1000.0}
        http_calls = []

        def fake_post(self, path, body, retries=3):
            # Mirrors the real budget check, then returns canned API data.
            if self.calls >= self.max_calls:
                raise runner.NansenError("cap")
            self.calls += 1
            http_calls.append(path)
            if body["filters"].get("token_address"):
                return {"data": [{"token_address": GOOD, "net_flow_24h_usd": sm_on_good["flow"]}]}
            return {"data": NETFLOWS}

        def quotes(tokens):
            base = dict(QUOTES)
            base[GOOD] = q(GOOD, price=prices["p"])
            return {t: base[t] for t in tokens if t in base}

        with mock.patch.object(nansen_mod.Nansen, "_post", fake_post), \
             mock.patch.object(runner, "get_quotes", side_effect=quotes):
            bot = runner.Bot(cfg)
            bot.notify = lambda m: None

            bot.run_once(force_nansen=True)  # nothing open: 1 call (scan), buys GOOD
            self.assertEqual(http_calls, ["/smart-money/netflow"])
            self.assertEqual([p.token for p in bot.store.positions()], [GOOD])
            self.assertEqual(bot.store.get("cash"), 950)

            for price in (1.5, 2.2):  # holding: exactly 2 calls per run, no duplicate buy
                http_calls.clear()
                prices["p"] = price
                bot.run_once(force_nansen=True)
                self.assertEqual(len(http_calls), 2)
                self.assertEqual(len(bot.store.positions()), 1)

            prices["p"] = 1.6  # 27% off the 2.2 peak -> trailing stop
            bot.run_once(force_nansen=True)
            self.assertEqual(bot.store.positions(), [])
            sell = [t for t in bot.store.trades() if t["side"] == "sell"][0]
            self.assertIn("trailing", sell["reason"])
            self.assertGreater(sell["pnl_usd"], 20)  # ~+50% minus fees on $50

            bot.run_once(force_nansen=True)  # cooldown blocks re-buying right away
            self.assertEqual(bot.store.positions(), [])

    def test_smart_money_exit_end_to_end(self):
        cfg = load_settings()
        cfg.nansen_api_key = "test"
        cfg.data_dir = Path(tempfile.mkdtemp())

        def fake_post(self, path, body, retries=3):
            self.calls += 1
            if body["filters"].get("token_address"):
                return {"data": [{"token_address": GOOD, "net_flow_24h_usd": -20_000}]}
            return {"data": NETFLOWS}

        with mock.patch.object(nansen_mod.Nansen, "_post", fake_post), \
             mock.patch.object(runner, "get_quotes", side_effect=lambda ts: {t: QUOTES[t] for t in ts if t in QUOTES}):
            bot = runner.Bot(cfg)
            bot.notify = lambda m: None
            bot.run_once(force_nansen=True)
            bot.run_once(force_nansen=True)
            sell = [t for t in bot.store.trades() if t["side"] == "sell"][0]
            self.assertIn("smart money exiting", sell["reason"])


class NansenScheduleTest(unittest.TestCase):
    def test_price_only_runs_between_nansen_runs(self):
        cfg = load_settings()
        cfg.nansen_api_key = "test"
        cfg.data_dir = Path(tempfile.mkdtemp())
        calls = []

        def fake_post(self, path, body, retries=3):
            self.calls += 1
            calls.append(path)
            if body["filters"].get("token_address"):
                return {"data": []}
            return {"data": NETFLOWS}

        with mock.patch.object(nansen_mod.Nansen, "_post", fake_post), \
             mock.patch.object(runner, "get_quotes", side_effect=lambda ts: {t: QUOTES[t] for t in ts if t in QUOTES}):
            bot = runner.Bot(cfg)
            bot.notify = lambda m: None
            bot.run_once()          # first run: Nansen
            self.assertEqual(len(calls), 1)
            for _ in range(5):      # next runs within 4h: prices only
                bot.run_once()
            self.assertEqual(len(calls), 1)
            bot.store.set("last_nansen_run", time.time() - 5 * 3600)
            bot.run_once()          # 4h later: Nansen again (2 calls, position open)
            self.assertEqual(len(calls), 3)

            from bot.dashboard import build
            html = build(bot.store, cfg, cfg.data_dir / "site").read_text()
            self.assertIn('"symbol": "GOOD"', html)
            self.assertNotIn("__DATA__", html)


class ScoringTests(unittest.TestCase):
    def setUp(self):
        self.cfg = load_settings()

    def _row(self, **kw):
        r = {"token_address": GOOD, "token_symbol": "GOOD", "net_flow_24h_usd": 60_000,
             "trader_count": 6, "token_age_days": 10, "market_cap_usd": 2e6}
        r.update(kw)
        return r

    def test_more_wallets_persistence_and_buying_now_score_higher(self):
        base, _ = strategy.entry_score(self._row(), q(GOOD), streak=0)
        better, f = strategy.entry_score(
            self._row(trader_count=20, net_flow_1h_usd=2_000, net_flow_7d_usd=150_000), q(GOOD), streak=2)
        self.assertGreater(better, base + 25)
        self.assertEqual(f["parts"]["persistence"], 20)
        self.assertEqual(f["parts"]["buying_now"], 10)

    def test_still_dumping_and_smart_money_selling_now_score_lower(self):
        calm, _ = strategy.entry_score(self._row(net_flow_1h_usd=500), q(GOOD), 1)
        knife, _ = strategy.entry_score(self._row(net_flow_1h_usd=-3_000), q(GOOD, h6=-15), 1)
        k2 = q(GOOD); k2.change_1h = -12
        knife2, _ = strategy.entry_score(self._row(net_flow_1h_usd=-3_000), k2, 1)
        self.assertGreater(calm, knife)
        self.assertGreater(knife, knife2)

    def test_rug_collapse_and_thin_pool_filtered(self):
        rows = [self._row(), self._row(token_address=THIN, token_symbol="THIN")]
        quotes = {GOOD: q(GOOD, h24=-90), THIN: q(THIN, liq=30_000, mcap=5e6)}
        passed, rejected = strategy.screen(rows, quotes, self.cfg)
        self.assertEqual(passed, [])
        why = " ".join(w for r in rejected for w in r["why"])
        self.assertIn("rug", why)
        self.assertIn("thin pool", why)

    def test_below_min_score_is_scored_but_not_qualified(self):
        weak = self._row(trader_count=3, net_flow_24h_usd=10_500, market_cap_usd=4e7)
        passed, _ = strategy.screen([weak], {GOOD: q(GOOD, mcap=4e7, liq=3e6, h24=-16)}, self.cfg)
        self.assertEqual(len(passed), 1)
        self.assertFalse(passed[0].qualified)
        self.assertLess(passed[0].score, self.cfg["entry"]["min_entry_score"])


class MemoryAndLabTests(unittest.TestCase):
    def test_streaks_and_lab_returns(self):
        cfg = load_settings()
        cfg.nansen_api_key = "test"
        cfg.data_dir = Path(tempfile.mkdtemp())
        prices = {"p": 1.0}

        def fake_post(self, path, body, retries=3):
            self.calls += 1
            if body["filters"].get("token_address"):
                return {"data": []}
            return {"data": NETFLOWS}

        def quotes(ts):
            base = dict(QUOTES); base[GOOD] = q(GOOD, price=prices["p"])
            return {t: base[t] for t in ts if t in base}

        with mock.patch.object(nansen_mod.Nansen, "_post", fake_post), \
             mock.patch.object(runner, "get_quotes", side_effect=quotes):
            bot = runner.Bot(cfg)
            bot.notify = lambda m: None
            s = bot.store
            # Two earlier scans saw GOOD accumulating
            s.record_scan(NETFLOWS, time.time() - 8 * 3600)
            s.record_scan(NETFLOWS, time.time() - 4 * 3600)
            self.assertEqual(s.streaks(time.time())[GOOD], 2)

            bot.run_once(force_nansen=True)
            lab = s.lab_all()
            self.assertEqual([r["token"] for r in lab], [GOOD])
            self.assertTrue(lab[0]["bought"])
            self.assertEqual(lab[0]["features"]["streak"], 2)
            pos = s.positions()[0]
            self.assertIn("seen in 2 earlier scans", pos.entry_reason)

            # 25h later price is up 30%: 4h and 24h returns get filled, 72h still open
            s.db.execute("UPDATE lab SET ts = ts - 25*3600")
            prices["p"] = 1.3
            bot.update_lab()
            r = s.lab_all()[0]
            self.assertAlmostEqual(r["r24h"], 30.0, places=3)
            self.assertAlmostEqual(r["r4h"], 30.0, places=3)
            self.assertIsNone(r["r72h"])
            self.assertAlmostEqual(r["max72"], 30.0, places=3)

            s.db.execute("UPDATE lab SET ts = ts - 48*3600")
            prices["p"] = 0.9
            bot.update_lab()
            r = s.lab_all()[0]
            self.assertAlmostEqual(r["r72h"], -10.0, places=3)
            self.assertEqual(r["done"], 1)


if __name__ == "__main__":
    unittest.main()
