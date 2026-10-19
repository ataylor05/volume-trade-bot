# Kronos next-candle predictions (shadow mode)

Adds Kronos (github.com/shiyu-coder/Kronos, MIT; `model/` is vendored from commit 67b630e) to predict the next 5m or 15m candle for top-10 coins and keep right/wrong stats. It places **no orders**; trading on the predictions waits for Allan's OK.

## Setup
- Needs `torch` (CPU), `einops`, `huggingface_hub`, `safetensors`, `tqdm`, plus pandas/numpy/requests.
- Weights download from Hugging Face on first load: NeoQuasar/Kronos-small + Kronos-Tokenizer-base (default), or `mini` / `base`.
- Candles come from Coinbase's public endpoint `/api/v3/brokerage/market/products/{pid}/candles` (no key), oldest first, open candle excluded. Input per prediction: last 400 candles of open, high, low, close, volume, amount (= volume x mean price) plus UTC timestamps.

## How a prediction works
Kronos samples 10 possible next candles. Forecast close = mean of the samples; P(up) = share of samples closing above the last close. Direction is **right** when the actual close lands on the predicted side of the last close (flat closes are counted separately). A **signal** (what a trade would act on) is P(up) >= 60% and forecast close >= +0.05%; for signals we also record whether price reached the bot's +0.40% target within 12 candles.

## Commands (run from this folder)
- `python3 -u kronos_pred.py bench` times mini/small/base on CPU.
- `python3 -u kronos_pred.py backtest 5m small 300` walk-forward test; writes `kronos_backtest_5m_small.csv`.
- `python3 -u kronos_pred.py shadow 5m small 21600` predicts every candle close for BTC ETH XRP SOL DOGE and the live bot's active coins (read-only from `../range_bot_state.json`), scores each one after its candle closes. Writes `kronos_predictions.csv`, `kronos_stats.json`.
- `python3 -u kronos_pred.py stats` prints the counts.

Compare `accuracy_pct` against `always_up_baseline_pct`: Kronos is only useful if it beats that.
