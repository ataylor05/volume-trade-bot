"""Kronos next-candle predictions for the Coinbase volume bot (shadow mode: no orders).

Kronos (github.com/shiyu-coder/Kronos, MIT, vendored in ./model) is a transformer pre-trained on
OHLCV candles. Given the last LOOKBACK candles it samples the next candle; we draw SAMPLES paths and
use their mean close as the forecast and the share of paths closing above the last close as P(up).

Commands
  python3 -u kronos_pred.py bench                        time each model size on CPU
  python3 -u kronos_pred.py backtest [5m|15m] [model] [n] walk-forward test on recent Coinbase candles
  python3 -u kronos_pred.py shadow [5m|15m] [model] [secs] predict every new candle, score it when it closes
  python3 -u kronos_pred.py stats                        print right/wrong counts from the shadow log

A prediction is scored on direction: right when the next candle's close is on the side of the
last close that Kronos predicted. Candles that close exactly flat are counted apart. A "signal" is a
prediction with P(up) >= SIGNAL_P and a forecast close >= +SIGNAL_MOVE; for those we also check
whether price reached the bot's +0.40% sell target within HOLD candles (what a trade would need).
Files: kronos_predictions.csv (one row per prediction), kronos_stats.json, kronos_backtest_*.csv.
"""
import os, sys, json, time, csv
from datetime import datetime, timezone
import numpy as np, pandas as pd, requests

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

GRAN = {'5m': ('FIVE_MINUTE', 300), '15m': ('FIFTEEN_MINUTE', 900)}
MODELS = {'mini': ('NeoQuasar/Kronos-mini', 'NeoQuasar/Kronos-Tokenizer-2k', 2048),
          'small': ('NeoQuasar/Kronos-small', 'NeoQuasar/Kronos-Tokenizer-base', 512),
          'base': ('NeoQuasar/Kronos-base', 'NeoQuasar/Kronos-Tokenizer-base', 512)}
COINS = ['BTC-USD', 'ETH-USD', 'XRP-USD', 'SOL-USD', 'DOGE-USD']  # top-10 market cap, Coinbase-listed
LOOKBACK = 400        # candles of context (Kronos-small/base max 512)
SAMPLES = 10          # sampled paths per prediction
T, TOP_P = 1.0, 0.9
STEP = 0.0025         # bot's sell target above entry: 2 x 0.10% maker + 0.05% (Allan, 2026-10-19 Advanced 3)
SIGNAL_P, SIGNAL_MOVE = 0.6, 0.0005
HOLD = 12             # candles a signal trade may wait for the +0.40% target
PRED_CSV = os.path.join(HERE, 'kronos_predictions.csv')
STATS_JSON = os.path.join(HERE, 'kronos_stats.json')
BOT_STATE = os.path.join(HERE, '..', 'range_bot_state.json')


def log(msg):
    print(f"{datetime.now(timezone.utc):%Y-%m-%dT%H:%M:%SZ} {msg}", flush=True)


def candles(pid, gran, count, end=None):
    """Last `count` closed candles, oldest first, from Coinbase's public market endpoint."""
    name, secs = GRAN[gran]
    now = int(time.time()) // secs * secs   # start of the still-open candle, which we skip
    end = end or now
    rows = {}
    while len(rows) < count:
        start = end - 299 * secs
        for _ in range(4):
            try:
                r = requests.get(f'https://api.coinbase.com/api/v3/brokerage/market/products/{pid}/candles',
                                 params={'start': start, 'end': end, 'granularity': name, 'limit': 300}, timeout=20)
                r.raise_for_status()
                break
            except requests.RequestException:
                time.sleep(2)
        else:
            raise RuntimeError(f'candles {pid} failed')
        got = r.json().get('candles', [])
        if not got:
            break
        for c in got:
            if int(c['start']) < now:
                rows[int(c['start'])] = c
        end = start
    df = pd.DataFrame([rows[k] for k in sorted(rows)][-count:])
    df['ts'] = pd.to_datetime(df['start'].astype(int), unit='s', utc=True).dt.tz_localize(None)
    for k in ('open', 'high', 'low', 'close', 'volume'):
        df[k] = df[k].astype(float)
    df['amount'] = df['volume'] * df[['open', 'high', 'low', 'close']].mean(axis=1)
    return df[['ts', 'open', 'high', 'low', 'close', 'volume', 'amount']].reset_index(drop=True)


def load(size='small'):
    import torch
    from model import Kronos, KronosTokenizer
    torch.set_num_threads(os.cpu_count() or 4)
    m, t, ctx = MODELS[size]
    tok, mod = KronosTokenizer.from_pretrained(t).eval(), Kronos.from_pretrained(m).eval()
    return tok, mod, ctx


def predict(km, windows, gran):
    """windows: list of DataFrames of equal length. Returns one dict per window for the next candle."""
    import torch
    from model.kronos import auto_regressive_inference, calc_time_stamps
    tok, mod, ctx = km
    secs = GRAN[gran][1]
    xs, xst, yst, norms = [], [], [], []
    for w in windows:
        x = w[['open', 'high', 'low', 'close', 'volume', 'amount']].values.astype(np.float32)
        mu, sd = x.mean(0), x.std(0)
        xs.append((x - mu) / (sd + 1e-5))
        xst.append(calc_time_stamps(w['ts']).values.astype(np.float32))
        yst.append(calc_time_stamps(pd.Series([w['ts'].iloc[-1] + pd.Timedelta(seconds=secs)])).values.astype(np.float32))
        norms.append((mu, sd))
    rep = lambda a: torch.from_numpy(np.repeat(np.stack(a), SAMPLES, axis=0))  # each window SAMPLES times
    out = auto_regressive_inference(tok, mod, rep(xs), rep(xst), rep(yst), ctx, 1, 5, T, 0, TOP_P, 1)
    out = out[:, -1, :].reshape(len(windows), SAMPLES, -1)
    res = []
    for w, (mu, sd), o in zip(windows, norms, out):
        o = o * (sd + 1e-5) + mu
        last = float(w['close'].iloc[-1])
        p = o.mean(0)
        res.append({'last_close': last, 'pred_open': p[0], 'pred_high': p[1], 'pred_low': p[2], 'pred_close': p[3],
                    'p_up': float((o[:, 3] > last).mean())})
    return res


def score(pred, nxt, after=None):
    """Compare a prediction with the actual next candle (and the HOLD candles after it, if given)."""
    last, act = float(pred['last_close']), float(nxt['close'])
    pdir = 1 if pred['pred_close'] > last else -1
    adir = int(act > last) - int(act < last)
    signal = pred['p_up'] >= SIGNAL_P and pred['pred_close'] >= last * (1 + SIGNAL_MOVE)
    hit = None
    if signal and after is not None:
        hit = bool((after['high'] >= last * (1 + STEP)).any())
    return {'pred_dir': pdir, 'actual_dir': adir, 'correct': (adir == pdir) if adir else None,
            'close_err_pct': abs(pred['pred_close'] - act) / act * 100, 'signal': signal, 'target_hit': hit,
            'actual_close': act, 'actual_high': nxt['high'], 'actual_low': nxt['low']}


def summarize(rows):
    rows = [r for r in rows if r.get('actual_dir') not in (None, '')]
    dec = [r for r in rows if str(r['correct']) in ('True', 'False')]
    right = sum(str(r['correct']) == 'True' for r in dec)
    sig = [r for r in rows if str(r['signal']) == 'True']
    sig_dec = [r for r in sig if str(r['correct']) in ('True', 'False')]
    sig_hit = [r for r in sig if str(r.get('target_hit')) in ('True', 'False')]
    up = sum(int(r['actual_dir']) > 0 for r in dec)
    return {'predictions': len(rows), 'right': right, 'wrong': len(dec) - right, 'flat': len(rows) - len(dec),
            'accuracy_pct': round(100 * right / len(dec), 1) if dec else None,
            'always_up_baseline_pct': round(100 * up / len(dec), 1) if dec else None,
            'mean_close_err_pct': round(float(np.mean([float(r['close_err_pct']) for r in rows])), 3) if rows else None,
            'signals': len(sig),
            'signal_right': sum(str(r['correct']) == 'True' for r in sig_dec),
            'signal_wrong': sum(str(r['correct']) == 'False' for r in sig_dec),
            'signal_target_hit': sum(str(r['target_hit']) == 'True' for r in sig_hit),
            'signal_target_scored': len(sig_hit)}


def bench():
    df = candles('BTC-USD', '5m', LOOKBACK)
    for size in MODELS:
        t0 = time.time(); km = load(size); t1 = time.time()
        predict(km, [df], '5m'); t2 = time.time()
        predict(km, [df] * 5, '5m'); t3 = time.time()
        log(f'{size}: load {t1 - t0:.1f}s, 1 coin {t2 - t1:.1f}s, 5 coins {t3 - t2:.1f}s ({SAMPLES} samples each)')


def backtest(gran='5m', size='small', n=300):
    """Walk forward over the last n candles of each coin: predict candle i from the LOOKBACK before it."""
    km = load(size)
    out = os.path.join(HERE, f'kronos_backtest_{gran}_{size}.csv')
    rows = []
    for pid in COINS:
        df = candles(pid, gran, LOOKBACK + n + HOLD)
        t0 = time.time()
        idx = list(range(LOOKBACK, len(df) - HOLD))
        for b in range(0, len(idx), 8):
            batch = idx[b:b + 8]
            preds = predict(km, [df.iloc[i - LOOKBACK:i] for i in batch], gran)
            for i, p in zip(batch, preds):
                s = score(p, df.iloc[i], df.iloc[i:i + HOLD])
                rows.append({'product': pid, 'ts': df['ts'].iloc[i].isoformat(), **p, **s})
        log(f'{pid} {gran} {size}: {len(idx)} candles in {time.time() - t0:.0f}s {summarize([r for r in rows if r["product"] == pid])}')
    pd.DataFrame(rows).to_csv(out, index=False)
    s = summarize(rows)
    log(f'ALL {gran} {size}: {s}')
    return s


def bot_coins():
    try:
        with open(BOT_STATE) as f:
            return [p for p in json.load(f).get('actives', []) if p]
    except (OSError, ValueError):
        return []


def shadow(gran='5m', size='small', secs=6 * 3600):
    """Every candle close: score predictions whose candle has closed, then predict the next candle."""
    km = load(size)
    period = GRAN[gran][1]
    fields = ['made_at', 'product', 'gran', 'model', 'candle_ts', 'last_close', 'pred_open', 'pred_high', 'pred_low',
              'pred_close', 'p_up', 'pred_dir', 'actual_dir', 'correct', 'close_err_pct', 'signal', 'target_hit',
              'actual_close', 'actual_high', 'actual_low']
    rows = []
    if os.path.exists(PRED_CSV):
        with open(PRED_CSV) as f:
            rows = list(csv.DictReader(f))
    stop_at = time.time() + secs
    while time.time() < stop_at:
        coins = sorted(set(COINS) | set(bot_coins()))
        data = {pid: candles(pid, gran, LOOKBACK + HOLD + 2) for pid in coins}
        for r in rows:   # score pending rows (candle closed); target needs HOLD more candles
            if r['gran'] != gran or r['product'] not in data:
                continue
            df = data[r['product']]
            m = df.index[df['ts'] == pd.Timestamp(r['candle_ts'])]
            if not len(m):
                continue
            i = m[0]
            if r.get('actual_dir') in (None, ''):
                p = {k: float(r[k]) for k in ('last_close', 'pred_close', 'p_up')}
                r.update(score(p, df.iloc[i]))
            if str(r.get('signal')) == 'True' and r.get('target_hit') in (None, '') and len(df) - i >= HOLD:
                r['target_hit'] = bool((df['high'].iloc[i:i + HOLD] >= float(r['last_close']) * (1 + STEP)).any())
        nxt_ts = None
        windows = [data[p].iloc[-LOOKBACK:] for p in coins]
        for pid, w, p in zip(coins, windows, predict(km, windows, gran)):
            nxt_ts = w['ts'].iloc[-1] + pd.Timedelta(seconds=period)
            if any(r['product'] == pid and r['gran'] == gran and r['candle_ts'] == nxt_ts.isoformat() for r in rows):
                continue
            pdir = 1 if p['pred_close'] > p['last_close'] else -1
            rows.append({'made_at': datetime.now(timezone.utc).isoformat(timespec='seconds'), 'product': pid,
                         'gran': gran, 'model': size, 'candle_ts': nxt_ts.isoformat(), **p, 'pred_dir': pdir})
            log(f"{pid} next {gran} {nxt_ts:%H:%M}: close {p['pred_close']:.6g} vs {p['last_close']:.6g} "
                f"({'UP' if pdir > 0 else 'DOWN'}, P(up) {p['p_up']:.0%})")
        tmp = PRED_CSV + '.tmp'
        with open(tmp, 'w', newline='') as f:
            w = csv.DictWriter(f, fieldnames=fields, extrasaction='ignore')
            w.writeheader(); w.writerows(rows)
        os.replace(tmp, PRED_CSV)
        st = {'updated': datetime.now(timezone.utc).isoformat(timespec='seconds'), 'gran': gran, 'model': size,
              'all': summarize([r for r in rows if r['gran'] == gran]),
              'by_product': {pid: summarize([r for r in rows if r['gran'] == gran and r['product'] == pid])
                             for pid in sorted({r['product'] for r in rows})}}
        with open(STATS_JSON, 'w') as f:
            json.dump(st, f, indent=1)
        log(f"stats {st['all']}")
        wait = period - time.time() % period + 5    # just after the next candle closes
        time.sleep(max(1, min(wait, stop_at - time.time())))


def stats():
    with open(STATS_JSON) as f:
        print(json.dumps(json.load(f), indent=1))


if __name__ == '__main__':
    cmd, args = (sys.argv[1] if len(sys.argv) > 1 else 'stats'), sys.argv[2:]
    if cmd == 'bench':
        bench()
    elif cmd == 'backtest':
        backtest(args[0] if args else '5m', args[1] if len(args) > 1 else 'small', int(args[2]) if len(args) > 2 else 300)
    elif cmd == 'shadow':
        shadow(args[0] if args else '5m', args[1] if len(args) > 1 else 'small', int(args[2]) if len(args) > 2 else 6 * 3600)
    else:
        stats()
