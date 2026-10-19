"""2-hourly report for Allan: wins vs stop-losses, portfolio value, change since last report.
Run: python3 -I report.py   (updates report_last.json)"""
import os, re, json, csv, time
D = os.path.dirname(os.path.abspath(__file__))
LAST = os.path.join(D, 'report_last.json')
wins = stops = 0
pnl_w = pnl_s = 0.0
for line in open(os.path.join(D, 'range_bot.log')):
    m = re.search(r'round trip done, pnl ([+-][\d.]+)', line)
    if m:
        v = float(m.group(1))
        if v >= 0:
            wins += 1; pnl_w += v
        else:
            stops += 1; pnl_s += v
rows = list(csv.DictReader(open(os.path.join(D, 'range_bot_value.csv'))))
r = rows[-1]
cur = {'time': r['time'], 'wins': wins, 'stops': stops, 'value': float(r['value_usd']),
       'volume': float(r['volume_usd']), 'start': float(r['start_value_usd']), 'active': r['active'],
       'pnl_w': round(pnl_w, 2), 'pnl_s': round(pnl_s, 2)}
prev = json.load(open(LAST)) if os.path.exists(LAST) else None
cur['prev'] = {k: prev[k] for k in ('time', 'wins', 'stops', 'value', 'volume')} if prev else None
print(json.dumps(cur))
json.dump({k: v for k, v in cur.items() if k != 'prev'}, open(LAST, 'w'))
