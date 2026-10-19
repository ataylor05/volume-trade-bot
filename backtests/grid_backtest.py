"""Backtest Allan's 50/50 two-sided idea on 1-minute candles.
Each candle start: post a maker BUY at open*(1-off) and a maker SELL at open*(1+off), LOT usd each.
Unfilled entries are cancelled at the next candle start. A filled BUY gets an exit SELL at +TGT;
a filled SELL gets an exit BUY back at -TGT. Exits wait until filled, or stop at STOP against them.
Fills need price to trade through the limit (low < buy, high > sell); new orders fill from the next minute.
Rebalance to 50/50 when crypto share leaves [lo, hi] (free balances only, maker fee).
"""
import sys, pandas as pd, numpy as np

FEE = 0.001

def sim(df, period=5, off=0.0, tgt=0.003, lot=100, stop=0.04, band=(0.35, 0.65), start=2250.0, max_open=20, cancel_exits=False, flatten=False):
    o, h, l, c = (df[x].values for x in ('open', 'high', 'low', 'close'))
    p0 = o[0]
    usd, coin = start / 2, start / 2 / p0
    entries, exits = [], []      # dict(side, px, qty, [entry])
    vol = fees = 0.0
    wins = stops = rebal = 0
    pnl_w = pnl_s = 0.0
    def fill(side, px, qty):
        nonlocal usd, coin, vol, fees
        n = px * qty; f = n * FEE
        if side == 'buy': usd -= n + f; coin += qty
        else: usd += n - f; coin -= qty
        vol += n; fees += f
    for i in range(len(df)):
        # fills on this minute (orders placed before this minute)
        for e in entries[:]:
            if (e['side'] == 'buy' and l[i] < e['px']) or (e['side'] == 'sell' and h[i] > e['px']):
                fill(e['side'], e['px'], e['qty']); entries.remove(e)
                ex = {'side': 'sell' if e['side'] == 'buy' else 'buy', 'entry': e['px'], 'qty': e['qty'], 'new': True,
                      'px': e['px'] * (1 + tgt) if e['side'] == 'buy' else e['px'] * (1 - tgt)}
                exits.append(ex)
        for x in exits[:]:
            if x.pop('new', False):
                continue
            if (x['side'] == 'sell' and h[i] > x['px']) or (x['side'] == 'buy' and l[i] < x['px']):
                fill(x['side'], x['px'], x['qty']); exits.remove(x); wins += 1
                pnl_w += x['qty'] * abs(x['px'] - x['entry']) - x['qty'] * (x['px'] + x['entry']) * FEE
            elif (x['side'] == 'sell' and c[i] <= x['entry'] * (1 - stop)) or (x['side'] == 'buy' and c[i] >= x['entry'] * (1 + stop)):
                fill(x['side'], c[i], x['qty']); exits.remove(x); stops += 1
                pnl_s += x['qty'] * (-abs(c[i] - x['entry'])) - x['qty'] * (c[i] + x['entry']) * FEE
        if i % period == 0:
            entries.clear()
            if cancel_exits or flatten:     # end of candle: drop unfilled exits; flatten closes the leg at the open
                for x in exits:
                    if flatten:
                        fill(x['side'], o[i], x['qty'])
                        pnl_s += x['qty'] * ((o[i] - x['entry']) if x['side'] == 'sell' else (x['entry'] - o[i])) - x['qty'] * (o[i] + x['entry']) * FEE
                        stops += 1
                exits.clear()
            res_usd = sum(x['px'] * x['qty'] * (1 + FEE) for x in exits if x['side'] == 'buy')
            res_coin = sum(x['qty'] for x in exits if x['side'] == 'sell')
            total = usd + coin * o[i]
            share = coin * o[i] / total
            if not band[0] <= share <= band[1]:
                want = total / 2 / o[i] - coin
                if want > 0: q = min(want, max(0, (usd - res_usd) / o[i] / (1 + FEE)))
                else: q = -min(-want, max(0, coin - res_coin))
                if abs(q) * o[i] > 10:
                    fill('buy' if q > 0 else 'sell', o[i], abs(q)); rebal += 1
                    res_usd = sum(x['px'] * x['qty'] * (1 + FEE) for x in exits if x['side'] == 'buy')
                    res_coin = sum(x['qty'] for x in exits if x['side'] == 'sell')
            if len(exits) < max_open:
                bp, sp = o[i] * (1 - off), o[i] * (1 + off)
                if usd - res_usd >= lot * (1 + FEE):
                    entries.append({'side': 'buy', 'px': bp, 'qty': lot / bp})
                if coin - res_coin >= lot / sp:
                    entries.append({'side': 'sell', 'px': sp, 'qty': lot / sp})
    end = usd + coin * c[-1]
    hold = start / 2 + start / 2 / p0 * c[-1]
    return {'end': round(end, 2), 'pnl_vs_usd': round(end - start, 2), 'hold_5050': round(hold - start, 2),
            'vs_hold': round(end - hold, 2), 'round_trips': wins, 'win_pnl': round(pnl_w, 2), 'stops': stops,
            'stop_pnl': round(pnl_s, 2), 'stuck_open': len(exits), 'rebalances': rebal, 'volume': round(vol), 'fees': round(fees, 2),
            'price_move_pct': round((c[-1] / p0 - 1) * 100, 2)}

if __name__ == '__main__':
    rows = []
    for pid in ('BTC-USD', 'ETH-USD', 'XRP-USD', 'SOL-USD'):
        df = pd.read_csv(f'candles_{pid}_1m.csv')
        for period in (5, 15):
            for off in (0.0, 0.001):
                for tgt in (0.003, 0.0025):
                    r = sim(df, period, off, tgt)
                    rows.append({'coin': pid, 'candle': f'{period}m', 'offset%': off * 100, 'target%': tgt * 100, **r})
    t = pd.DataFrame(rows)
    pd.set_option('display.width', 250); pd.set_option('display.max_columns', 30)
    print(t.to_string(index=False))
    t.to_csv('grid_results.csv', index=False)
