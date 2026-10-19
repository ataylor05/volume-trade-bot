# Coinbase volume bot

## Current: range_bot.py (since 2026-10-16 22:17Z)
Goal (Allan, 22:13Z): create spot volume while protecting the portfolio's **USD value**; not tied to any coin.
- **Scanner** (every 30 min): scores a fixed large-cap allowlist (BTC ETH XRP SOL DOGE ADA LINK AVAX XLM SUI HBAR BCH LTC DOT; CoinGecko is blocked from this container, so market cap is a hand-kept list) on 24h of 5-min candles. Filters: Coinbase 24h volume ≥ $3M, tick < 0.05% of price, 24h range ≤ 8%, |net 24h move| ≤ 50% of range, |6h move| ≤ 60% of range. Score = swings of at least one round-trip step × (1 − trend) × market-cap weight (−4% per rank). It keeps the current coin unless another scores 25% higher. Output: `scan_latest.json`.
- **Trading**: USD split into 8 lots. Each lot places a post-only BUY at or slightly below the bid of the active coin, then a post-only SELL at max(ask, cost + both maker fees + 0.02%). Unfilled buys follow the price up and move when the active coin changes.
- **Stop**: if a held coin falls 4% below (Allan, 2026-10-17 17:06Z; was 1.5%) the lot's entry, the lot is abandoned and sold at the ask (a realized USD loss) so the money can move on.
- Run `python3 -u range_bot.py <seconds>` (resumes from `range_bot_state.json`). `touch STOP` cancels unfilled buys and exits; sells stay on the book.
- Files: `range_bot.log`, `range_bot_trades.csv`, `range_bot_value.csv` (USD value every 10 min), `range_bot_state.json`.

## Changes 2026-10-17 13:25Z
- Profit buffer 0.1% per round trip (Allan), so step = 2 x 0.15% maker + 0.1% = 0.40%. Allan first wrote 0.25%; that is below the 0.30% two-fee break-even, Allan confirmed 0.40% at 13:27Z.
- Trades the top 2 scanner coins at once (`N_MARKETS`); new USD lots go to whichever active coin holds fewer lots. State key `actives` replaces `active`.
- After a stop-loss the coin sits out 6h (`BAN_SECS`) and the scanner reruns for a replacement. Reason: on 2026-10-17 12:51-13:17Z NEAR trended down and the bot stopped out and rebought it 10 times, realizing about -$53.
- Restart loop must cd with absolute paths; the shared mount drops a long-lived working directory.

## Changes 2026-10-18 13:57Z (Allan: fewer stop-losses)
- Universe: top 10 non-stablecoins by CoinMarketCap (was 20); max 24h range 4% (was 8%).
- No new buys, and open buys are cancelled, while a coin is down 0.3% over 1h or 0.5% over 2h (`falling()`).
- At most 3 lots per coin (`MAX_LOTS_PER_COIN`); with 2 coins, up to 2 lots can wait in USD.
- Baseline before this change: 27 winning round trips (+$4.21), 17 stop-losses (-$105.55), value $2,241.56. `report.py` prints the counts.

## Retired: volume_bot.py (XLM, 2026-10-16 12:56Z to 22:17Z)
Sold XLM high and bought it back lower to grow the XLM count. It did $8,480 of volume over 11 round trips. It ended fully in USD ($2,371.64) after XLM rose away from the buy-backs; that USD is the new bot's starting value.

## Handoff (2026-10-16 22:30Z)
The bot was stopped in the original thread session so a fresh session, which loads `CMC_API_KEY`, can run it. At handoff: 7 lots hold AVAX with post-only sells at $9.999 to $10.015, and 1 lot holds $296.45 USD. Volume so far is $2,071.83. To resume: `cd /mnt/project-files/coinbase && python3 -u range_bot.py 6900` (run in the background, then restart each time it exits). Check `range_bot.log` first, and make sure no other session is running it, because two copies would fight over the same state.
