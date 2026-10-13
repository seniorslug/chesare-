"""Offline tests: fake Nansen + DexScreener data, real strategy and paper-trading code.
Run: python -m unittest discover tests
"""
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

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
        why = strategy.exit_reason(p, 1.1, time.time(), self.cfg, {"smart_trader_net_flow_usd": -12_000})
        self.assertIn("smart money exiting", why)
        self.assertIsNone(strategy.exit_reason(p, 1.1, time.time(), self.cfg, {"smart_trader_net_flow_usd": 3_000}))

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
        fake = mock.MagicMock()
        fake.smart_money_netflows.return_value = NETFLOWS
        fake.flow_intelligence.return_value = {"smart_trader_net_flow_usd": 9000, "smart_trader_wallet_count": 5}
        fake.credits_used = 0
        prices = {"p": 1.0}

        def quotes(tokens):
            base = dict(QUOTES)
            base[GOOD] = q(GOOD, price=prices["p"])
            return {t: base[t] for t in tokens if t in base}

        with mock.patch.object(runner, "Nansen", return_value=fake), \
             mock.patch.object(runner, "get_quotes", side_effect=quotes):
            bot = runner.Bot(cfg)
            bot.notify = lambda m: None
            bot.scan()
            pos = bot.store.positions()
            self.assertEqual([p.token for p in pos], [GOOD])
            self.assertEqual(bot.store.get("cash"), 950)

            bot.scan()  # already holding: no duplicate buy
            self.assertEqual(len(bot.store.positions()), 1)

            fake.flow_intelligence.return_value = {"smart_trader_net_flow_usd": 1000}  # SM still fine
            for price in (1.5, 2.2, 1.6):
                prices["p"] = price
                bot.manage()
            self.assertEqual(bot.store.positions(), [])
            sell = [t for t in bot.store.trades() if t["side"] == "sell"][0]
            self.assertIn("trailing", sell["reason"])
            self.assertGreater(sell["pnl_usd"], 20)  # ~+50% minus fees on $50

            bot.scan()  # cooldown blocks re-buying right away
            self.assertEqual(bot.store.positions(), [])


if __name__ == "__main__":
    unittest.main()
