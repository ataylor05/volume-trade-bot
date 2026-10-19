"""Maker-only XLM volume bot for the "Claude - Volume" portfolio.

Goal: create spot volume without reducing the XLM count.
Each tranche of XLM cycles: post-only SELL near the ask -> once filled, post-only BUY
back with all proceeds at a price low enough that the XLM bought exceeds the XLM sold
after maker fees on both legs. Buys are never moved up and no market orders are used.

State: volume_bot_state.json, fills log: volume_bot_trades.csv (same folder).
Create a file named STOP in this folder to make the bot cancel unfilled SELLs and exit.
"""
import os, sys, time, json, uuid, secrets, base64, csv, math, traceback
import jwt, requests
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

DIR = os.path.dirname(os.path.abspath(__file__))
STATE = os.path.join(DIR, 'volume_bot_state.json')
TRADES = os.path.join(DIR, 'volume_bot_trades.csv')
LOG = os.path.join(DIR, 'volume_bot.log')
STOP = os.path.join(DIR, 'STOP')
PRODUCT = 'XLM-USD'
HOST = 'api.coinbase.com'
N_TRANCHES = 8
MARGIN = 0.0002          # XLM gain per round trip on top of fees (Allan: 0.02%)
LADDER = 0.003           # initial spacing between tranche sells
LOOP_SECS = 15

NAME = os.environ['COINBASE_API_KEY_NAME']
_seed = base64.b64decode(os.environ['COINBASE_API_PRIVATE_KEY'].strip())[:32]
KEY = Ed25519PrivateKey.from_private_bytes(_seed)


def log(msg):
    line = time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()) + ' ' + msg
    print(line, flush=True)
    with open(LOG, 'a') as f:
        f.write(line + '\n')


def api(method, path, body=None):
    now = int(time.time())
    tok = jwt.encode({'sub': NAME, 'iss': 'cdp', 'nbf': now, 'exp': now + 120,
                      'uri': f'{method} {HOST}{path.split("?")[0]}'},
                     KEY, algorithm='EdDSA', headers={'kid': NAME, 'nonce': secrets.token_hex()})
    for attempt in range(4):
        try:
            r = requests.request(method, f'https://{HOST}{path}', json=body, timeout=20,
                                 headers={'Authorization': f'Bearer {tok}'})
            if r.status_code >= 500 or r.status_code == 429:
                raise RuntimeError(f'{r.status_code} {r.text[:200]}')
            return r.status_code, r.json()
        except Exception as e:
            if attempt == 3:
                raise
            time.sleep(2 ** (attempt + 1))


def fmt_px(p):
    return f'{p:.6f}'


def fmt_sz(s):
    return f'{math.floor(s * 100) / 100:.2f}'


def maker_fee():
    _, j = api('GET', '/api/v3/brokerage/transaction_summary')
    return float(j['fee_tier']['maker_fee_rate']), float(j['total_volume'])


def book():
    _, j = api('GET', f'/api/v3/brokerage/best_bid_ask?product_ids={PRODUCT}')
    b = j['pricebooks'][0]
    return float(b['bids'][0]['price']), float(b['asks'][0]['price'])


def balances():
    _, j = api('GET', '/api/v3/brokerage/accounts?limit=250')
    return {a['currency']: float(a['available_balance']['value']) for a in j['accounts']}


def place(side, size, price):
    body = {'client_order_id': str(uuid.uuid4()), 'product_id': PRODUCT, 'side': side,
            'order_configuration': {'limit_limit_gtc': {'base_size': fmt_sz(size),
                                                        'limit_price': fmt_px(price),
                                                        'post_only': True}}}
    _, j = api('POST', '/api/v3/brokerage/orders', body)
    if j.get('success'):
        oid = j['success_response']['order_id']
        log(f'placed {side} {fmt_sz(size)} @ {fmt_px(price)} id={oid}')
        return oid
    log(f'place {side} {fmt_sz(size)} @ {fmt_px(price)} rejected: {json.dumps(j.get("error_response", j))[:300]}')
    return None


def cancel(oid):
    _, j = api('POST', '/api/v3/brokerage/orders/batch_cancel', {'order_ids': [oid]})
    ok = j.get('results', [{}])[0].get('success', False)
    log(f'cancel {oid}: {ok}')
    return ok


def order(oid):
    _, j = api('GET', f'/api/v3/brokerage/orders/historical/{oid}')
    return j['order']


def save(st):
    tmp = STATE + '.tmp'
    with open(tmp, 'w') as f:
        json.dump(st, f, indent=1)
    os.replace(tmp, STATE)


def record(t, side, o):
    new = not os.path.exists(TRADES)
    with open(TRADES, 'a', newline='') as f:
        w = csv.writer(f)
        if new:
            w.writerow(['time', 'tranche', 'side', 'xlm', 'avg_price', 'usd_value', 'fee_usd', 'order_id'])
        w.writerow([time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()), t['id'], side, o['filled_size'],
                    o['average_filled_price'], o['filled_value'], o['total_fees'], o['order_id']])


def init_state():
    bal = balances()
    xlm = bal.get('XLM', 0)
    per = math.floor(xlm / N_TRANCHES * 100) / 100
    st = {'start_xlm': xlm, 'start_time': time.time(), 'volume_usd': 0.0, 'fees_usd': 0.0,
          'tranches': [{'id': i, 'state': 'holding', 'xlm': per, 'usd': 0.0, 'anchor': None,
                        'order_id': None, 'order_px': None, 'cycles': 0} for i in range(N_TRANCHES)]}
    log(f'init: {xlm} XLM split into {N_TRANCHES} tranches of {per}')
    return st


def step(st, fee, bid, ask):
    s = 2 * fee + MARGIN
    for t in st['tranches']:
        if t['order_id']:
            o = order(t['order_id'])
            status = o['status']
            if status == 'FILLED':
                record(t, t['state'], o)
                val, fees = float(o['filled_value']), float(o['total_fees'])
                st['volume_usd'] += val
                st['fees_usd'] += fees
                px = float(o['average_filled_price'])
                if t['state'] == 'selling':
                    t.update(state='usd', usd=val - fees, xlm=0.0, sold_px=px, sold_xlm=float(o['filled_size']))
                else:
                    t.update(state='holding', xlm=float(o['filled_size']), usd=0.0, anchor=px, cycles=t['cycles'] + 1)
                    log(f'tranche {t["id"]} cycle done: sold {t.get("sold_xlm")} bought {t["xlm"]} XLM')
                t['order_id'] = None
            elif status in ('CANCELLED', 'EXPIRED', 'FAILED'):
                filled = float(o.get('filled_size') or 0)
                if filled > 0:
                    log(f'tranche {t["id"]} order {status} with partial fill {filled}; needs manual review')
                    t['state'] = 'review'
                else:
                    t['state'] = 'holding' if t['state'] == 'selling' else 'usd'
                t['order_id'] = None
            elif status == 'OPEN' and t['state'] == 'buying' and float(o.get('filled_size') or 0) == 0:
                # Fee tier dropped: a closer buy-back still gains XLM, so move it up.
                target = buy_target(t, fee, bid)
                if target > t['order_px'] * 1.001 and cancel(t['order_id']):
                    t['order_id'] = None
                    t['state'] = 'usd'
            elif status == 'OPEN' and t['state'] == 'selling' and float(o.get('filled_size') or 0) == 0 \
                    and t['order_px'] > ask * (1 + s / 2):
                # Allan (2026-10-16 20:06Z): sells follow the market down; the XLM count is
                # protected by the buy-back rule, not by a sell price floor.
                if cancel(t['order_id']):
                    t['order_id'] = None
                    t['state'] = 'holding'
        if t['order_id'] is None and t['state'] == 'holding' and t['xlm'] > 0:
            px = sell_target(t, s, fee, ask)
            oid = place('SELL', t['xlm'], px)
            if oid:
                t.update(state='selling', order_id=oid, order_px=px)
        elif t['order_id'] is None and t['state'] == 'usd' and t['usd'] > 1:
            px = buy_target(t, fee, bid)
            size = t['usd'] / (px * (1 + fee)) * 0.9999
            if size <= t['sold_xlm']:
                log(f'tranche {t["id"]} buy-back would not gain XLM ({size} <= {t["sold_xlm"]}); skipping')
                continue
            oid = place('BUY', size, px)
            if oid:
                t.update(state='buying', order_id=oid, order_px=px)
    save(st)


def buy_target(t, fee, bid):
    # Highest price at which the actual sell proceeds buy back sold_xlm * (1 + MARGIN) after the buy fee.
    return min(math.floor(t['usd'] / (t['sold_xlm'] * (1 + MARGIN) * (1 + fee)) * 1e6) / 1e6, bid)


def sell_target(t, s, fee, ask):
    return ask


def stop_all(st):
    for t in st['tranches']:
        if t['order_id'] and t['state'] == 'selling':
            o = order(t['order_id'])
            if float(o.get('filled_size') or 0) == 0 and cancel(t['order_id']):
                t['order_id'] = None
                t['state'] = 'holding'
    save(st)
    log('STOP file found: unfilled sells cancelled, buy-backs left working. Exiting.')


def main():
    run_secs = float(sys.argv[1]) if len(sys.argv) > 1 else 6900
    st = json.load(open(STATE)) if os.path.exists(STATE) else init_state()
    fee, vol, fee_t = None, None, 0
    end = time.time() + run_secs
    log(f'bot start, run for {run_secs:.0f}s')
    while time.time() < end:
        try:
            if os.path.exists(STOP):
                stop_all(st)
                return
            if time.time() - fee_t > 600:
                fee, vol = maker_fee()
                fee_t = time.time()
                log(f'maker fee {fee}, 30d volume {vol:.0f}, step {(2 * fee + MARGIN) * 100:.2f}%')
            bid, ask = book()
            step(st, fee, bid, ask)
            for t in st['tranches']:
                t['fresh'] = False
        except Exception:
            log('error: ' + traceback.format_exc().replace('\n', ' | ')[:800])
        time.sleep(LOOP_SECS)
    log('run window over; orders stay on the book')


if __name__ == '__main__':
    main()
