"""Memebot command line.

  python main.py doctor    check keys and API connections, show a sample scan
  python main.py once      one manage + scan pass, then exit (good for cron)
  python main.py run       run continuously
  python main.py report    paper/live P&L, win rate, open positions
  python main.py dashboard build the trade dashboard web page into site/index.html
"""
from __future__ import annotations

import logging
import shutil
import sys
from datetime import datetime

from bot.config import load_settings


def setup_logging(cfg) -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        handlers=[logging.StreamHandler(), logging.FileHandler(cfg.data_dir / "memebot.log")])


def doctor(cfg) -> None:
    from bot import strategy
    from bot.nansen import Nansen
    from bot.prices import get_quotes

    print(f"Mode: {'LIVE' if cfg.is_live else 'PAPER'}")
    n = Nansen(cfg.nansen_api_key)
    u = cfg["universe"]
    rows = n.smart_money_netflows("solana", u["max_token_age_days"], u["min_market_cap_usd"],
                                  u["max_market_cap_usd"], u["smart_money_labels"], 1, per_page=25)
    print(f"✓ Nansen OK — {len(rows)} young Solana tokens with smart money flows")
    quotes = get_quotes([r["token_address"] for r in rows])
    print(f"✓ DexScreener OK — prices for {len(quotes)}/{len(rows)}")
    passed, rejected = strategy.screen(rows, quotes, cfg)
    print(f"\nPassing divergence screen right now: {len(passed)}")
    for c in passed[:10]:
        print(f"  {c.symbol:10} SM +${c.sm_netflow_24h:>10,.0f}  price {c.quote.change_24h:+6.1f}%/24h  score {c.score:.2f}")
    print("\nTop rejects (why):")
    for r in rejected[:8]:
        print(f"  {r['symbol']:10} {', '.join(r['why'])}")
    if cfg["mode"]["trading_mode"] == "live":
        if shutil.which("gmgn-cli"):
            print("\n✓ gmgn-cli installed. Verify flags match with: gmgn-cli swap --help")
        else:
            print("\n✗ gmgn-cli missing: npm install -g gmgn-cli")
    print(f"\nNansen credits used by this check: {n.credits_used:.0f}")


def report(cfg) -> None:
    from bot.prices import get_quotes
    from bot.store import Store

    s = Store(cfg.data_dir / "memebot.db")
    sells = [t for t in s.trades() if t["side"] == "sell"]
    wins = [t for t in sells if t["pnl_usd"] > 0]
    total = sum(t["pnl_usd"] for t in sells)
    print(f"Closed trades: {len(sells)}   Win rate: {len(wins) / len(sells) * 100 if sells else 0:.0f}%   "
          f"Realized P&L: ${total:+.2f}")
    if sells:
        print(f"Avg win: ${sum(t['pnl_usd'] for t in wins) / max(len(wins), 1):+.2f}   "
              f"Avg loss: ${sum(t['pnl_usd'] for t in sells if t['pnl_usd'] <= 0) / max(len(sells) - len(wins), 1):+.2f}")
    print(f"Paper cash: ${s.get('cash', cfg['mode']['paper_starting_balance_usd']):.2f}")
    pos = s.positions()
    quotes = get_quotes([p.token for p in pos]) if pos else {}
    print(f"\nOpen positions: {len(pos)}")
    for p in pos:
        q = quotes.get(p.token)
        now = f"{(q.price_usd / p.entry_price - 1) * 100:+.1f}%" if q else "n/a"
        print(f"  {p.symbol:10} entry ${p.entry_price:.8g}  now {now}  opened {datetime.fromtimestamp(p.opened_at):%m-%d %H:%M}")
    print("\nLast 10 closed:")
    for t in sells[-10:]:
        print(f"  {datetime.fromtimestamp(t['ts']):%m-%d %H:%M} {t['symbol']:10} ${t['pnl_usd']:+7.2f}  {t['reason']}")


def main() -> None:
    cmd = sys.argv[1] if len(sys.argv) > 1 else "help"
    cfg = load_settings()
    setup_logging(cfg)
    if cmd == "doctor":
        doctor(cfg)
    elif cmd == "report":
        report(cfg)
    elif cmd == "dashboard":
        from bot.dashboard import build
        from bot.store import Store
        print(build(Store(cfg.data_dir / "memebot.db"), cfg, cfg.data_dir.parent / "site"))
    elif cmd in ("once", "run"):
        from bot.runner import Bot
        bot = Bot(cfg)
        bot.run_once() if cmd == "once" else bot.run_forever()
    else:
        print(__doc__)


if __name__ == "__main__":
    main()
