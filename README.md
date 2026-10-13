# Memebot — smart money divergence trader (Solana)

Buys young memecoins (≤30 days old) that Nansen smart money is **accumulating while the price is falling**, then exits on a **trailing stop** or when **smart money starts selling**. Starts in paper mode. Live trades go through GMGN.

## How it decides

Looks for coins that smart money is **quietly accumulating while price pulls back into the 61.8–76% zone below its all-time high**, and only buys if that zone is holding.

All price levels use **1-minute candle body closes** (free GeckoTerminal data), never wicks.

**Entry** (every Nansen scan, every 4h). All must be true:
1. Token ≤ 30 days old, ≥ 3 distinct smart wallets, smart money still net buying over 24h
2. **ATH market cap ≥ $10M** (highest 1m close × supply)
3. **Latest 1m close is 61.8–76% below the ATH close**
4. **Support holds:** no 1m close below the 76% line since the ATH, and price has been in the zone ≥ 1 hour
5. **Divergence:** price fell over the past 7 days while smart money net bought **≥ 0.5% of total supply** in that window (USD flow ÷ the window's average price, so the dip doesn't inflate it)
6. Liquidity ≥ $75k and ≥ 2% of market cap

Coins that pass are ranked by a 0–100 score: % of supply bought (25), number of wallets (20), persistence across scans (20), time holding the zone (15), still buying in the last hour (10), not dumping now (5), liquidity (5).

**Exit**, first hit wins:
- **Support broken:** any 1m candle closes below the 76%-from-ATH line
- Trailing stop: 25% below the highest price since entry
- Smart money exits: smart money net flow over 24h below −$5k
- Hard stop −35% (backstop) · max hold 14 days

**Why the bot bought:** the dashboard has a section for every trade showing the bot's reasoning in plain English, a checklist of each entry rule (what it required, what the coin showed, met or not met), the score breakdown, and what the bot watched for after buying. New buys save their checklist and the rule thresholds in force at the time. Buys made before this existed are rebuilt from the signal log and use today's thresholds (the page says so); if neither exists, only the original one-line summary is shown.

**Signal lab:** every coin that gets a candle check is followed for 3 days, bought or not. The dashboard compares coins that passed every rule with near-misses, so you can see whether the rules are adding value.

Risk limits: $50 per trade, max 5 open, stops new entries after −$150 in a day, 24h cooldown before re-buying a coin.

Every number above is in `config.toml`.

## Setup (Mac, or a Linux VPS)

Needs Python 3.11+.

```bash
cd memebot
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env        # then put your NANSEN_API_KEY in .env
python main.py doctor       # checks Nansen + prices, shows what passes the screen right now
python main.py run          # paper trade continuously
```

Check results any time: `python main.py report`. Every signal, including rejected ones, is saved in `data/memebot.db`; the log is `data/memebot.log`.

### Keep it running on a VPS
Any $5/month Ubuntu server works (DigitalOcean, Hetzner, Vultr). After setup, run it in the background:

```bash
nohup .venv/bin/python main.py run > /dev/null 2>&1 &
```

Or run one pass every 5 minutes from cron instead: `*/5 * * * * cd ~/memebot && .venv/bin/python main.py once`

### Or run it on GitHub (paper mode only)
1. Create a **private** repo and upload this folder (the hidden `.github` folder must come with it).
2. Repo → Settings → Secrets and variables → Actions → add `NANSEN_API_KEY` (plus the Telegram ones if you want alerts).
3. Actions tab → enable workflows → "memebot" → Run workflow to test it.

It then runs every 30 minutes and commits the trade database back to the repo, so `python main.py report` works on any copy you pull. Every 30 min uses roughly 1,500 of the 2,000 free monthly minutes a private repo gets. Positions are only checked every 30 min, so trailing-stop exits are slower and rougher than on an always-on server. Live trading can't run here, because GMGN needs a fixed whitelisted IP.

### Dashboard password
The dashboard page and the saved trade database are encrypted with the `DASHBOARD_PASSWORD` secret (AES-256). The page asks for it and decrypts in your browser. Run logs only show warnings, so trades stay private even though the repo is public. Changing the password later needs the saved state re-encrypted, so ask before changing it.

### Phone alerts (optional)
Create a Telegram bot with @BotFather, put the token and your chat ID in `.env`. You'll get a message on every buy and sell.

## Nansen credits

Hard cap: **2 Nansen calls per run** (`max_calls_per_run` in config.toml). One call checks smart money on all open positions together; one scans for new coins. With nothing open, a run uses 1. Nansen is only called every 4 hours (`nansen_every_hours`); the runs in between only check prices, which use DexScreener and cost nothing. Each call costs about 5 credits. The run log prints how many calls and credits each run used.

## Going live (only after paper results earn it)

Suggested bar: 30+ closed paper trades with positive total P&L after the built-in 4% round-trip slippage/fee haircut.

1. Make a **new Solana wallet just for this**. Fund it with only what you can lose.
2. `npm install -g gmgn-cli`, then `gmgn-cli config` and follow its steps (create the API key at gmgn.ai/ai with swap enabled; GMGN requires whitelisting your server's IPv4).
3. Run `gmgn-cli swap --help` and confirm the flags match `bot/gmgn.py` (`--amount` in lamports, `--percent`, `--condition-orders`). Do one tiny manual swap first.
4. In `.env`: `GMGN_WALLET_ADDRESS=<wallet>` and `LIVE_TRADING_CONFIRM=yes`. In `config.toml`: `trading_mode = "live"`.
5. Start with `position_size_usd` at $10–20.

## Limits to know

- Built against Nansen's and GMGN's published docs, but not yet run against the live APIs. `doctor` is the first real test; if a field name differs, the error will say which.
- Paper fills use DexScreener prices minus a slippage haircut. Real memecoin fills can be much worse on thin pools.
- The trailing stop runs in the bot, so it only works while the bot is running. The GMGN hard stop is the backup.
- Rug checks are limited to liquidity, age and Nansen labels. Honeypots and mint authority aren't checked yet.

## Tests

`python -m unittest discover tests` runs the strategy and a full paper buy→trail→sell cycle with fake data.
