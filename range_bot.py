"""Maker-only, multi-asset volume bot for the "Claude - Volume" portfolio.

Goal (Allan, 2026-10-16 22:13Z): create spot volume while protecting the portfolio's USD value.
- Every SCAN_SECS it scores large-cap coins (ALLOWLIST) on 24h of 5-minute candles and picks the
  one trading sideways in a box with the most swings big enough to pay both maker fees.
- USD is split into lots. Each lot: post-only BUY at/near the bid of the active coin -> once filled,
  post-only SELL at the higher of the ask and its break-even floor (cost + both fees + MARGIN).
- Unfilled buys follow the price up and move to a new coin when the scanner switches.
- A lot whose coin falls STOP below its entry is abandoned: sold at the ask (accepting the loss)
  so the USD can move to a better coin.

Files (same folder): range_bot_state.json, range_bot.log, range_bot_trades.csv,
range_bot_value.csv, scan_latest.json. Create a file named STOP here to cancel unfilled BUYs
and exit; sells stay on the book.
"""
import os, sys, time, json, uuid, secrets, base64, csv, math, traceback
import jwt, requests
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

DIR = os.path.dirname(os.path.abspath(__file__))
STATE = os.path.join(DIR, 'range_bot_state.json')
TRADES = os.path.join(DIR, 'range_bot_trades.csv')
VALUE = os.path.join(DIR, 'range_bot_value.csv')
SCAN = os.path.join(DIR, 'scan_latest.json')
LOG = os.path.join(DIR, 'range_bot.log')
STOP = os.path.join(DIR, 'STOP')
HOST = 'api.coinbase.com'

# Large-cap coins listed on Coinbase, roughly in market-cap order. Edit to change the universe.
ALLOWLIST = ['BTC', 'ETH', 'XRP', 'SOL', 'DOGE', 'ADA', 'LINK', 'AVAX', 'XLM', 'SUI', 'HBAR',
             'BCH', 'LTC', 'DOT']
MIN_QUOTE_VOL = 3e6      # 24h USD volume on Coinbase
MAX_TICK = 0.0005        # price increment must be under 0.05% of price
N_LOTS = 8
MARGIN = 0.001           # profit buffer per round trip on top of both fees (Allan, 2026-10-17: 0.1%)
N_MARKETS = 2            # coins traded at once; USD lots are spread across them (Allan, 2026-10-17)
BAN_SECS = 6 * 3600      # after a stop-loss, the coin sits out this long
STOP_LOSS = 0.04         # abandon a held lot when its coin falls 4% below entry (Allan, 2026-10-17 17:06Z)
SCAN_SECS = 1800
LOOP_SECS = 15
MAX_RANGE = 0.04         # 24h high/low range above this is too wild (Allan, 2026-10-18: 4%, was 8%)
MAX_LOTS_PER_COIN = 3    # at most this many lots on one coin (Allan, 2026-10-18)
FALL_1H = 0.003          # no new buys while a coin is down this much over 1h ...
FALL_2H = 0.005          # ... or this much over 2h (Allan, 2026-10-18)
MAX_TREND = 0.5          # |net 24h move| / range above this is trending, not boxed
MAX_DRIFT6 = 0.6         # same over the last 6h

NAME = os.environ['COINBASE_API_KEY_NAME']
KEY = Ed25519PrivateKey.from_private_bytes(base64.b64decode(os.environ['COINBASE_API_PRIVATE_KEY'].strip())[:32])
PRODUCTS = {}
FALLING = {}  # pid -> (checked_at, is_falling)


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


def product(pid):
    if pid not in PRODUCTS:
        _, j = api('GET', f'/api/v3/brokerage/products/{pid}')
        PRODUCTS[pid] = j
    return PRODUCTS[pid]


def _round(x, inc, up=False):
    inc = float(inc)
    n = math.ceil(x / inc - 1e-9) if up else math.floor(x / inc + 1e-9)
    dec = max(0, -int(math.floor(math.log10(inc))))
    return f'{n * inc:.{dec}f}'


def books(pids):
    q = '&'.join(f'product_ids={p}' for p in pids)
    _, j = api('GET', f'/api/v3/brokerage/best_bid_ask?{q}')
    return {b['product_id']: (float(b['bids'][0]['price']), float(b['asks'][0]['price'])) for b in j['pricebooks']}


def maker_fee():
    _, j = api('GET', '/api/v3/brokerage/transaction_summary')
    return float(j['fee_tier']['maker_fee_rate']), float(j['total_volume'])


def place(pid, side, size, price):
    p = product(pid)
    sz = _round(size, p['base_increment'])
    px = _round(price, p['price_increment'], up=(side == 'SELL'))
    if float(sz) <= 0 or float(sz) * float(px) < float(p.get('quote_min_size') or 1):
        log(f'{pid} {side} {sz} @ {px} below minimum, skipped')
        return None, None
    body = {'client_order_id': str(uuid.uuid4()), 'product_id': pid, 'side': side,
            'order_configuration': {'limit_limit_gtc': {'base_size': sz, 'limit_price': px, 'post_only': True}}}
    _, j = api('POST', '/api/v3/brokerage/orders', body)
    if j.get('success'):
        oid = j['success_response']['order_id']
        log(f'placed {side} {sz} {pid} @ {px} id={oid}')
        return oid, float(px)
    log(f'place {side} {sz} {pid} @ {px} rejected: {json.dumps(j.get("error_response", j))[:250]}')
    return None, None


def cancel(oid):
    _, j = api('POST', '/api/v3/brokerage/orders/batch_cancel', {'order_ids': [oid]})
    ok = j.get('results', [{}])[0].get('success', False)
    log(f'cancel {oid}: {ok}')
    return ok


def order(oid):
    _, j = api('GET', f'/api/v3/brokerage/orders/historical/{oid}')
    return j['order']


def record(lot, o, note=''):
    new = not os.path.exists(TRADES)
    with open(TRADES, 'a', newline='') as f:
        w = csv.writer(f)
        if new:
            w.writerow(['time', 'lot', 'product', 'side', 'size', 'avg_price', 'usd_value', 'fee_usd', 'status', 'note', 'order_id'])
        w.writerow([now(), lot['id'], o['product_id'], o['side'], o['filled_size'], o['average_filled_price'],
                    o['filled_value'], o['total_fees'], o['status'], note, o['order_id']])


def save(st):
    tmp = STATE + '.tmp'
    with open(tmp, 'w') as f:
        json.dump(st, f, indent=1)
    os.replace(tmp, STATE)


# ---------------------------------------------------------------- scanner

def candles(pid, hours=24):
    end = int(time.time())
    _, j = api('GET', f'/api/v3/brokerage/products/{pid}/candles?start={end - hours * 3600}&end={end}&granularity=FIVE_MINUTE&limit=350')
    c = sorted(j.get('candles', []), key=lambda x: int(x['start']))
    return [(float(x['high']), float(x['low']), float(x['close'])) for x in c]


def reversals(c, step):
    hi = [x[0] for x in c]
    lo = [x[1] for x in c]
    n, mode, ext = 0, 'up', hi[0]
    for h, l in zip(hi, lo):
        if mode == 'up':
            ext = max(ext, h)
            if l <= ext * (1 - step):
                n, mode, ext = n + 1, 'down', l
        else:
            ext = min(ext, l)
            if h >= ext * (1 + step):
                n, mode, ext = n + 1, 'up', h
    return n


CMC_TOP = 10             # with CMC_API_KEY set: scan the top N non-stablecoins by market cap


def universe():
    """Large-cap coins in market-cap order: live from CoinMarketCap when CMC_API_KEY is set, else ALLOWLIST."""
    key = os.environ.get('CMC_API_KEY')
    if not key:
        return ALLOWLIST[:CMC_TOP], 'allowlist'
    try:
        r = requests.get('https://pro-api.coinmarketcap.com/v1/cryptocurrency/listings/latest',
                         params={'limit': 60, 'convert': 'USD'}, headers={'X-CMC_PRO_API_KEY': key}, timeout=20)
        data = r.json()['data']
        coins = [c['symbol'] for c in sorted(data, key=lambda c: c['cmc_rank'])
                 if 'stablecoin' not in (c.get('tags') or []) and c['symbol'] not in ('WBTC', 'WETH', 'STETH', 'WSTETH')]
        return coins[:CMC_TOP], 'coinmarketcap'
    except Exception as e:
        log(f'coinmarketcap lookup failed ({e}); using allowlist')
        return ALLOWLIST[:CMC_TOP], 'allowlist'


def scan(fee):
    step = 2 * fee + MARGIN
    rows = []
    coins, source = universe()
    for rank, coin in enumerate(coins):
        pid = f'{coin}-USD'
        try:
            p = product(pid)
            PRODUCTS.pop(pid, None)  # refresh volume next time
            price = float(p['price'])
            row = {'product': pid, 'rank': rank + 1, 'price': price,
                   'vol24_musd': round(float(p.get('approximate_quote_24h_volume') or 0) / 1e6, 1)}
            reasons = []
            if p.get('status') != 'online' or p.get('trading_disabled') or p.get('cancel_only'):
                reasons.append('not tradable')
            if float(p.get('approximate_quote_24h_volume') or 0) < MIN_QUOTE_VOL:
                reasons.append('low volume')
            if float(p['price_increment']) / price > MAX_TICK:
                reasons.append('tick too coarse')
            c = candles(pid)
            if len(c) < 200:
                reasons.append('not enough candles')
            else:
                H, L = max(x[0] for x in c), min(x[1] for x in c)
                rng = H - L
                c6 = c[-72:]
                row.update(range_pct=round(rng / ((H + L) / 2) * 100, 2),
                           trend=round(abs(c[-1][2] - c[0][2]) / rng, 2),
                           drift6=round(abs(c6[-1][2] - c6[0][2]) / rng, 2),
                           swings=reversals(c, step),
                           pos_in_range=round((c[-1][2] - L) / rng, 2))
                if rng / ((H + L) / 2) > MAX_RANGE:
                    reasons.append('range too wide')
                if row['trend'] > MAX_TREND:
                    reasons.append('trending 24h')
                if row['drift6'] > MAX_DRIFT6:
                    reasons.append('trending 6h')
                # sideways swings, nudged toward higher market cap
                row['score'] = round(row['swings'] * (1 - row['trend']) * (1 - 0.04 * rank), 2)
            row['ok'] = not reasons
            row['why_not'] = ', '.join(reasons)
            rows.append(row)
        except Exception as e:
            rows.append({'product': pid, 'rank': rank + 1, 'ok': False, 'why_not': f'not on Coinbase or error: {e}'[:120]})
    ok = sorted([r for r in rows if r['ok']], key=lambda r: -r['score'])
    with open(SCAN, 'w') as f:
        json.dump({'time': now(), 'universe': source, 'step_pct': round(step * 100, 3), 'pick': ok[0]['product'] if ok else None,
                   'rows': sorted(rows, key=lambda r: -r.get('score', -1))}, f, indent=1)
    return ok


def falling(pid):
    """True while the coin is sliding: down FALL_1H over the last hour or FALL_2H over two hours."""
    t, f = FALLING.get(pid, (0, False))
    if time.time() - t > 120:
        try:
            c = candles(pid, hours=3)
            px = c[-1][2]
            f = len(c) >= 25 and (px < c[-13][2] * (1 - FALL_1H) or px < c[-25][2] * (1 - FALL_2H))
            if f != FALLING.get(pid, (0, None))[1]:
                log(f'{pid} {"falling: buys paused" if f else "levelled off: buys allowed"}')
        except Exception as e:
            log(f'{pid} trend check failed ({e}); treating as falling')
            f = True
        FALLING[pid] = (time.time(), f)
    return f


def lots_on(st, pid):
    return sum(1 for l in st['lots'] if l['product'] == pid)


# ---------------------------------------------------------------- lots

def new_lot(st, **kw):
    lot = {'id': st['next_id'], 'state': 'usd', 'product': None, 'usd': 0.0, 'qty': 0.0, 'cost': 0.0,
           'entry_px': None, 'order_id': None, 'order_px': None, 'abandon': False, 'cycles': 0}
    lot.update(kw)
    st['next_id'] += 1
    st['lots'].append(lot)
    return lot


def settle(st, lot, o):
    """Apply a finished (FILLED or CANCELLED) order to a lot, splitting partial fills."""
    filled = float(o.get('filled_size') or 0)
    val, fees = float(o.get('filled_value') or 0), float(o.get('total_fees') or 0)
    if filled > 0:
        record(lot, o, 'abandon' if lot['abandon'] else '')
        st['volume_usd'] += val
        st['fees_usd'] += fees
    if lot['state'] == 'buying':
        spent = val + fees
        left = lot['usd'] - spent
        if filled > 0:
            lot.update(state='holding', qty=filled, cost=spent, usd=0.0, entry_px=float(o['average_filled_price']))
            if o['status'] != 'FILLED' and left > 1:
                new_lot(st, usd=left)
        else:
            lot.update(state='usd', product=None)
    else:  # selling
        if filled > 0:
            frac = min(filled / lot['qty'], 1.0)
            got = val - fees
            cost_part = lot['cost'] * frac
            st['realized_pnl'] += got - cost_part
            if frac < 0.999999:
                new_lot(st, usd=got)
                lot.update(state='holding', qty=lot['qty'] - filled, cost=lot['cost'] - cost_part)
            else:
                lot.update(state='usd', usd=got, qty=0.0, cost=0.0, product=None, entry_px=None,
                           abandon=False, cycles=lot['cycles'] + 1)
                log(f'lot {lot["id"]} round trip done, pnl {got - cost_part:+.4f} USD')
        else:
            lot['state'] = 'holding'
    lot['order_id'] = None
    lot['order_px'] = None


def floor_px(lot, fee):
    return lot['cost'] * (1 + MARGIN) / (lot['qty'] * (1 - fee))


def tick(st, fee, actives):
    step = 2 * fee + MARGIN
    pids = sorted({l['product'] for l in st['lots'] if l['product']} | set(actives))
    bk = books(pids) if pids else {}
    usd_lots = [l for l in st['lots'] if l['state'] in ('usd', 'buying')]
    for lot in list(st['lots']):
        pid = lot['product']
        if lot['order_id']:
            o = order(lot['order_id'])
            if o['status'] in ('FILLED', 'CANCELLED', 'EXPIRED', 'FAILED'):
                settle(st, lot, o)
            elif o['status'] == 'OPEN':
                bid, ask = bk[pid]
                if lot['state'] == 'buying':
                    k = usd_lots.index(lot) % 4 if lot in usd_lots else 0
                    target = bid * (1 - k * step / 4)
                    if pid not in actives or falling(pid) or target > lot['order_px'] * (1 + step / 4):
                        cancel(lot['order_id'])
                else:
                    stop = bid < lot['entry_px'] * (1 - STOP_LOSS)
                    if stop and not lot['abandon']:
                        log(f'lot {lot["id"]} {pid} fell {100 * (1 - bid / lot["entry_px"]):.2f}% below entry: abandoning at the ask')
                        lot['abandon'] = True
                        st.setdefault('banned', {})[pid] = time.time() + BAN_SECS
                        if pid in actives:
                            actives.remove(pid)
                            st['rescan'] = True
                            log(f'{pid} sits out {BAN_SECS // 3600}h after a stop-loss; active now {actives}')
                        cancel(lot['order_id'])
                    elif lot['abandon'] and lot['order_px'] > ask * (1 + step / 4):
                        cancel(lot['order_id'])
                    elif not lot['abandon'] and lot['order_px'] > max(ask, floor_px(lot, fee)) * (1 + step / 4):
                        cancel(lot['order_id'])
            continue
        open_coins = [a for a in actives if lots_on(st, a) < MAX_LOTS_PER_COIN and not falling(a)]
        if lot['state'] == 'usd' and open_coins and lot['usd'] > 2:
            # put the lot on the open active coin holding the fewest lots
            active = min(open_coins, key=lambda a: lots_on(st, a))
            bid, ask = bk[active]
            k = usd_lots.index(lot) % 4 if lot in usd_lots else 0
            px = bid * (1 - k * step / 4)
            oid, opx = place(active, 'BUY', lot['usd'] / (px * (1 + fee)) * 0.9999, px)
            if oid:
                lot.update(state='buying', product=active, order_id=oid, order_px=opx)
        elif lot['state'] == 'holding':
            bid, ask = bk[pid]
            px = ask if lot['abandon'] else max(ask, floor_px(lot, fee))
            oid, opx = place(pid, 'SELL', lot['qty'], px)
            if oid:
                lot.update(state='selling', order_id=oid, order_px=opx)
    # merge idle dust lots
    st['lots'] = [l for l in st['lots'] if not (l['state'] == 'usd' and l['usd'] < 0.01)]
    save(st)
    return bk


def value(st, bk):
    v = 0.0
    for l in st['lots']:
        if l['state'] in ('usd', 'buying'):
            v += l['usd']
        else:
            v += l['qty'] * bk[l['product']][0]
    return v


def init_state():
    _, j = api('GET', '/api/v3/brokerage/orders/historical/batch?order_status=OPEN')
    for o in j.get('orders', []):
        cancel(o['order_id'])
    time.sleep(3)
    _, j = api('GET', '/api/v3/brokerage/accounts?limit=250')
    st = {'start_time': now(), 'next_id': 0, 'lots': [], 'volume_usd': 0.0, 'fees_usd': 0.0,
          'realized_pnl': 0.0, 'active': None}
    usd = 0.0
    for a in j['accounts']:
        cur, amt = a['currency'], float(a['available_balance']['value'])
        if cur == 'USD':
            usd = amt
        elif amt > 0 and cur not in ('USDC',):
            pid = f'{cur}-USD'
            bid = books([pid])[pid][0]
            if amt * bid > 2:
                # Carried-in coins: cost basis is today's value, so they are sold at or above it.
                new_lot(st, state='holding', product=pid, qty=amt, cost=amt * bid, entry_px=bid)
    per = usd / N_LOTS
    for _ in range(N_LOTS):
        new_lot(st, usd=math.floor(per * 100) / 100)
    st['start_value_usd'] = usd + sum(l['cost'] for l in st['lots'] if l['state'] == 'holding')
    log(f'init: {usd:.2f} USD in {N_LOTS} lots, carried-in lots: '
        f'{[(l["product"], l["qty"]) for l in st["lots"] if l["state"] == "holding"]}')
    return st


def stop_all(st):
    for lot in st['lots']:
        if lot['order_id'] and lot['state'] == 'buying':
            cancel(lot['order_id'])
            settle(st, lot, order_wait(lot['order_id']))
    save(st)
    log('STOP file found: unfilled buys cancelled, sells left working. Exiting.')


def order_wait(oid):
    for _ in range(10):
        o = order(oid)
        if o['status'] != 'OPEN':
            return o
        time.sleep(1)
    return o


def main():
    run_secs = float(sys.argv[1]) if len(sys.argv) > 1 else 6900
    st = json.load(open(STATE)) if os.path.exists(STATE) else init_state()
    if 'actives' not in st:
        st['actives'] = [st['active']] if st.get('active') else []
    fee, fee_t, scan_t, val_t = None, 0, st.get('last_scan', 0), 0
    end = time.time() + run_secs
    log(f'bot start, run for {run_secs:.0f}s, active={st["actives"]}, margin {MARGIN * 100:.2f}%, markets {N_MARKETS}, '
        f'stop {STOP_LOSS * 100:.1f}%, top {CMC_TOP}, max range {MAX_RANGE * 100:.0f}%, max {MAX_LOTS_PER_COIN} lots/coin')
    while time.time() < end:
        try:
            if os.path.exists(STOP):
                stop_all(st)
                return
            if time.time() - fee_t > 600:
                fee, vol = maker_fee()
                fee_t = time.time()
                log(f'maker fee {fee}, 30d volume {vol:.0f}, step {(2 * fee + MARGIN) * 100:.2f}%')
            if time.time() - scan_t > SCAN_SECS:
                ok = scan(fee)
                scan_t = st['last_scan'] = time.time()
                banned = {k: v for k, v in st.get('banned', {}).items() if v > time.time()}
                st['banned'] = banned
                ok = [r for r in ok if r['product'] not in banned]
                old = st['actives']
                # keep a current coin while it passes the filters and no unused coin scores 25% better
                keep = [r for r in ok if r['product'] in old]
                rest = [r for r in ok if r['product'] not in old]
                picks = []
                for r in keep:
                    if not rest or rest[0]['score'] < r['score'] * 1.25:
                        picks.append(r)
                picks = (picks + [r for r in ok if r not in picks])[:N_MARKETS]
                new = [r['product'] for r in picks]
                if set(new) != set(old):
                    log(f'scanner: active coins {old} -> {new}')
                st['actives'] = new
                log('scan top: ' + ', '.join(f'{r["product"]} {r["score"]}' for r in ok[:5]) +
                    f' | active {st["actives"]} | sitting out {sorted(banned)}')
            if st.pop('rescan', False):
                scan_t = 0  # a coin was dropped after a stop-loss: rescan for a replacement
            bk = tick(st, fee, st['actives'])
            if time.time() - val_t > 600 and all(l['product'] in bk for l in st['lots'] if l['product']):
                val_t = time.time()
                new = not os.path.exists(VALUE)
                with open(VALUE, 'a', newline='') as f:
                    w = csv.writer(f)
                    if new:
                        w.writerow(['time', 'value_usd', 'start_value_usd', 'volume_usd', 'fees_usd', 'realized_pnl', 'active'])
                    w.writerow([now(), round(value(st, bk), 2), round(st['start_value_usd'], 2), round(st['volume_usd'], 2),
                                round(st['fees_usd'], 2), round(st['realized_pnl'], 4), ' '.join(st['actives'])])
        except Exception:
            log('error: ' + traceback.format_exc().replace('\n', ' | ')[:900])
        time.sleep(LOOP_SECS)
    log('run window over; orders stay on the book')


if __name__ == '__main__':
    main()
