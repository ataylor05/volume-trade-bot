"""50/50 BTC grid bot (Allan, 2026-10-19 15:47Z; whole portfolio since 16:09Z) for "Claude - Volume".

Holds half its money in USD and half in BTC. At the start of every 30-minute candle it cancels its
unfilled orders, rebalances if BTC drifts outside 20-80% of its value, then posts two post-only
(maker) orders: a BUY GAP below the price and a SELL GAP above it, each ORDER_FRAC of its value.
When one fills it posts the exit on the other side TARGET away (0.30% = 2 x 0.10% maker + 0.10%).
Anything unfilled at the end of the candle is cancelled. No stop-loss.

It only spends its own ledger (state file). range_bot.py was retired 2026-10-19 16:09Z.
Start: python3 -u grid_bot.py <seconds> [alloc_usd]. The first start takes the whole portfolio's
USD + BTC (or only alloc_usd of cash) and splits it 50/50.
Stop: touch STOP in this folder (cancels its orders and exits). Files: grid_bot_state.json,
grid_bot.log, grid_bot_trades.csv, grid_bot_value.csv.
"""
import os, sys, time, json, uuid, secrets, base64, csv, math
import jwt, requests
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

DIR = os.path.dirname(os.path.abspath(__file__))
STATE = os.path.join(DIR, 'grid_bot_state.json')
LOG = os.path.join(DIR, 'grid_bot.log')
TRADES = os.path.join(DIR, 'grid_bot_trades.csv')
VALUE = os.path.join(DIR, 'grid_bot_value.csv')
STOP = os.path.join(DIR, 'STOP')
HOST = 'api.coinbase.com'
PID = 'BTC-USD'
CANDLE = 1800            # 30-minute candles (Allan, 2026-10-19 18:18Z; was 15)
GAP = 0.0025             # entry orders 0.25% above/below the candle's starting price (Allan 18:18Z; was 0.40%)
TARGET = 0.003           # exit 0.30% from the entry fill
ORDER_FRAC = 0.20        # each entry is this share of the bot's value (Allan 18:18Z: bigger orders; was 4.5%)
BAND = (0.2, 0.8)        # rebalance to 50/50 when BTC share leaves this band
LOOP_SECS = 10

NAME = os.environ['COINBASE_API_KEY_NAME']
KEY = Ed25519PrivateKey.from_private_bytes(base64.b64decode(os.environ['COINBASE_API_PRIVATE_KEY'].strip())[:32])
PROD = {}


def now():
    return time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())


def log(msg):
    line = now() + ' ' + msg
    print(line, flush=True)
    with open(LOG, 'a') as f:
        f.write(line + '\n')


def api(method, path, body=None):
    for attempt in range(4):
        t = int(time.time())
        tok = jwt.encode({'sub': NAME, 'iss': 'cdp', 'nbf': t, 'exp': t + 120,
                          'uri': f'{method} {HOST}{path.split("?")[0]}'},
                         KEY, algorithm='EdDSA', headers={'kid': NAME, 'nonce': secrets.token_hex()})
        try:
            r = requests.request(method, f'https://{HOST}{path}', json=body, timeout=20,
                                 headers={'Authorization': f'Bearer {tok}'})
            if r.status_code >= 500 or r.status_code == 429:
                raise RuntimeError(f'{r.status_code} {r.text[:200]}')
            return r.status_code, r.json()
        except Exception:
            if attempt == 3:
                raise
            time.sleep(2 ** (attempt + 1))


def product():
    if not PROD:
        PROD.update(api('GET', f'/api/v3/brokerage/products/{PID}')[1])
    return PROD


def _round(x, inc, up=False):
    inc = float(inc)
    n = math.ceil(x / inc - 1e-9) if up else math.floor(x / inc + 1e-9)
    dec = max(0, -int(math.floor(math.log10(inc))))
    return f'{n * inc:.{dec}f}'


def book():
    _, j = api('GET', f'/api/v3/brokerage/best_bid_ask?product_ids={PID}')
    b = j['pricebooks'][0]
    return float(b['bids'][0]['price']), float(b['asks'][0]['price'])


def maker_fee():
    _, j = api('GET', '/api/v3/brokerage/transaction_summary')
    return float(j['fee_tier']['maker_fee_rate'])


def place(side, qty, px):
    p = product()
    sz = _round(qty, p['base_increment'])
    pr = _round(px, p['price_increment'], up=(side == 'SELL'))
    if float(sz) * float(pr) < max(1.0, float(p.get('quote_min_size') or 1)):
        log(f'{side} {sz} @ {pr} below minimum, skipped')
        return None
    body = {'client_order_id': str(uuid.uuid4()), 'product_id': PID, 'side': side,
            'order_configuration': {'limit_limit_gtc': {'base_size': sz, 'limit_price': pr, 'post_only': True}}}
    _, j = api('POST', '/api/v3/brokerage/orders', body)
    if j.get('success'):
        oid = j['success_response']['order_id']
        log(f'placed {side} {sz} @ {pr} id={oid}')
        return {'id': oid, 'side': side, 'qty': float(sz), 'px': float(pr)}
    log(f'{side} {sz} @ {pr} rejected: {json.dumps(j.get("error_response", j))[:200]}')
    return None


def order(oid):
    return api('GET', f'/api/v3/brokerage/orders/historical/{oid}')[1]['order']


def cancel_wait(oid):
    api('POST', '/api/v3/brokerage/orders/batch_cancel', {'order_ids': [oid]})
    for _ in range(10):
        o = order(oid)
        if o['status'] != 'OPEN':
            return o
        time.sleep(1)
    return o


def save(st):
    tmp = STATE + '.tmp'
    with open(tmp, 'w') as f:
        json.dump(st, f, indent=1)
    os.replace(tmp, STATE)


def apply(st, o, kind):
    """Book an order's fills into the ledger (once per order)."""
    filled, val, fee = float(o.get('filled_size') or 0), float(o.get('filled_value') or 0), float(o.get('total_fees') or 0)
    done = st.setdefault('booked', [])
    if filled <= 0 or o['order_id'] in done:
        return 0.0
    done.append(o['order_id'])
    del done[:-50]
    if o['side'] == 'BUY':
        st['usd'] -= val + fee
        st['btc'] += filled
    else:
        st['usd'] += val - fee
        st['btc'] -= filled
    st['volume'] += val
    st['fees'] += fee
    new = not os.path.exists(TRADES)
    with open(TRADES, 'a', newline='') as f:
        w = csv.writer(f)
        if new:
            w.writerow(['time', 'kind', 'side', 'qty', 'avg_px', 'value', 'fee', 'status', 'order_id'])
        w.writerow([now(), kind, o['side'], filled, o.get('average_filled_price'), round(val, 4), round(fee, 4), o['status'], o['order_id']])
    log(f'{kind} {o["side"]} filled {filled} @ {o.get("average_filled_price")} ({o["status"]})')
    return filled


def value(st, bid):
    return st['usd'] + st['btc'] * bid


def end_candle(st):
    """Cancel everything unfilled and book what did fill."""
    for od in st['orders']:
        o = order(od['id'])
        if o['status'] == 'OPEN':
            o = cancel_wait(od['id'])
        apply(st, o, od['kind'])
    st['orders'] = []
    save(st)


def poll(st):
    """Entries that filled get their exit; exits that filled close the round trip."""
    keep = []
    for od in st['orders']:
        o = order(od['id'])
        if o['status'] == 'OPEN':
            keep.append(od)
            continue
        q = apply(st, o, od['kind'])
        if od['kind'] == 'entry' and q > 0:
            px = float(o['average_filled_price'])
            if o['side'] == 'BUY':
                ex = place('SELL', min(q, st['btc']), px * (1 + TARGET))
            else:
                ex = place('BUY', q, px * (1 - TARGET))
            if ex:
                keep.append({**ex, 'kind': 'exit', 'entry_px': px})
        elif od['kind'] == 'exit' and q > 0:
            st['round_trips'] += 1
            log(f'round trip done ({od["side"]} exit), {st["round_trips"]} so far')
    st['orders'] = keep
    save(st)


def start_candle(st, fee):
    bid, ask = book()
    mid = (bid + ask) / 2
    v = value(st, bid)
    share = st['btc'] * bid / v
    if not BAND[0] <= share <= BAND[1]:
        want = v / 2 / mid - st['btc']
        log(f'BTC share {share:.0%} outside {BAND}: rebalancing {want:+.8f} BTC')
        od = place('BUY', want, bid) if want > 0 else place('SELL', -want, ask)
        if od:
            st['orders'].append({**od, 'kind': 'rebalance'})
    size = v * ORDER_FRAC
    bp, sp = mid * (1 - GAP), mid * (1 + GAP)
    if st['usd'] >= size * (1 + fee) + sum(o['qty'] * o['px'] * (1 + fee) for o in st['orders'] if o['side'] == 'BUY'):
        od = place('BUY', size / bp, bp)
        if od:
            st['orders'].append({**od, 'kind': 'entry'})
    if st['btc'] - sum(o['qty'] for o in st['orders'] if o['side'] == 'SELL') >= size / sp:
        od = place('SELL', size / sp, sp)
        if od:
            st['orders'].append({**od, 'kind': 'entry'})
    st['candle'] = int(time.time()) // CANDLE * CANDLE
    save(st)


def record_value(st):
    bid, _ = book()
    v = value(st, bid)
    hold = st['hold_usd'] + st['start_btc_half'] * bid
    new = not os.path.exists(VALUE)
    with open(VALUE, 'a', newline='') as f:
        w = csv.writer(f)
        if new:
            w.writerow(['time', 'value_usd', 'start_usd', 'hold_5050_usd', 'usd', 'btc', 'btc_px', 'volume', 'fees', 'round_trips'])
        w.writerow([now(), round(v, 2), round(st['start_usd'], 2), round(hold, 2), round(st['usd'], 2), st['btc'], bid,
                    round(st['volume'], 2), round(st['fees'], 4), st['round_trips']])
    log(f'value {v:.2f} (start {st["start_usd"]:.2f}, 50/50 hold {hold:.2f}), volume {st["volume"]:.0f}, '
        f'round trips {st["round_trips"]}')


def init(alloc=None):
    """Take over the portfolio's USD and BTC (or only `alloc` USD of its cash) and set it 50/50.
    Other coins are left alone and logged. Refuses to start while any order is open."""
    _, j = api('GET', '/api/v3/brokerage/orders/historical/batch?order_status=OPEN')
    if j.get('orders'):
        sys.exit(f'{len(j["orders"])} open orders in the portfolio; cancel them first')
    _, j = api('GET', '/api/v3/brokerage/accounts?limit=250')
    bal = {a['currency']: float(a['available_balance']['value']) for a in j['accounts']}
    usd, btc = (alloc, 0.0) if alloc else (bal.get('USD', 0.0), bal.get('BTC', 0.0))
    others = {c: v for c, v in bal.items() if v > 0 and c not in ('USD', 'BTC')}
    st = {'start_time': now(), 'usd': usd, 'btc': btc, 'volume': 0.0, 'fees': 0.0,
          'round_trips': 0, 'orders': [], 'candle': 0}
    bid, ask = book()
    st['start_usd'] = usd + btc * bid
    save(st)
    log(f'init: {usd:.2f} USD + {btc:.8f} BTC = {st["start_usd"]:.2f}; other coins left alone: {others}')
    fee = maker_fee()
    for _ in range(40):           # get within 5% of 50/50 with maker orders at the touch
        bid, ask = book()
        v = value(st, bid)
        want = v / 2 / bid - st['btc']
        if abs(want) * bid < v * 0.05:
            break
        od = place('BUY', want / (1 + fee), bid) if want > 0 else place('SELL', -want, ask)
        if not od:
            time.sleep(5)
            continue
        time.sleep(30)
        o = order(od['id'])
        if o['status'] == 'OPEN':
            o = cancel_wait(od['id'])
        apply(st, o, 'setup')
    bid, _ = book()
    st['start_usd'] = value(st, bid)
    st['start_btc_half'] = st['btc']   # baseline: just holding this split
    st['hold_usd'] = st['usd']
    save(st)
    log(f'setup done: {st["usd"]:.2f} USD + {st["btc"]:.8f} BTC, value {st["start_usd"]:.2f}')
    return st


def main():
    secs = float(sys.argv[1]) if len(sys.argv) > 1 else 6900
    if os.path.exists(STATE):
        st = json.load(open(STATE))
    else:
        st = init(float(sys.argv[2]) if len(sys.argv) > 2 else None)
    fee, fee_t, val_t = maker_fee(), time.time(), 0
    end = time.time() + secs
    log(f'grid bot start for {secs:.0f}s: gap {GAP:.2%}, target {TARGET:.2%}, order {ORDER_FRAC:.1%} of value, '
        f'band {BAND}, maker fee {fee}')
    while time.time() < end:
        try:
            if os.path.exists(STOP):
                end_candle(st)
                log('STOP file found: orders cancelled, exiting')
                return
            if time.time() - fee_t > 600:
                fee, fee_t = maker_fee(), time.time()
            if int(time.time()) // CANDLE * CANDLE != st['candle']:
                end_candle(st)
                start_candle(st, fee)
            else:
                poll(st)
            if time.time() - val_t > CANDLE:
                record_value(st)
                val_t = time.time()
        except Exception as e:
            log(f'error: {e!r}')
            time.sleep(30)
        time.sleep(LOOP_SECS)
    log('run time up; orders stay on the book until the next start')


if __name__ == '__main__':
    main()
