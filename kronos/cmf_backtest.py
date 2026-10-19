# Backtest: trade Kronos predictions only when CMF(40) agrees. Reads kronos_backtest_{5m,15m}_small.csv.
# Run: python3 -I cmf_backtest.py
import sys; sys.path.insert(0,'/mnt/project-files/coinbase/kronos')
import kronos_pred as k, pandas as pd, numpy as np
TGT=0.0025; H=12; N=40
def cmf(df):
    rng=(df.high-df.low).replace(0,np.nan)
    mfv=(((df.close-df.low)-(df.high-df.close))/rng).fillna(0)*df.volume
    return mfv.rolling(N).sum()/df.volume.rolling(N).sum()
out={}
for g in ('5m','15m'):
    bt=pd.read_csv(f'/mnt/project-files/coinbase/kronos/kronos_backtest_{g}_small.csv'); bt['ts']=pd.to_datetime(bt.ts)
    rows=[]
    for pid in k.COINS:
        df=k.candles(pid,g,1100); df['cmf']=cmf(df).shift(1)   # CMF known at prediction time (through prior candle)
        n=len(df); hi=df.high.values; lo=df.low.values
        up=[(hi[i:i+H]>=df.close.iloc[i-1]*(1+TGT)).any() if i+H<=n else np.nan for i in range(n)]
        dn=[(lo[i:i+H]<=df.close.iloc[i-1]*(1-TGT)).any() if i+H<=n else np.nan for i in range(n)]
        df['up_hit']=up; df['dn_hit']=dn
        b=bt[bt['product']==pid].merge(df[['ts','cmf','up_hit','dn_hit']],on='ts')
        rows.append(b)
    d=pd.concat(rows); d=d[d.actual_dir!=0].dropna(subset=['cmf','up_hit'])
    d['ok']=d.correct.astype(str)=='True'
    d['tgt']=np.where(d.pred_dir>0,d.up_hit,d.dn_hit).astype(bool)
    al=d[((d.cmf>0)&(d.pred_dir>0))|((d.cmf<0)&(d.pred_dir<0))]
    na=d.drop(al.index) if False else d[~d.index.isin([])]
    mis=d[~(((d.cmf>0)&(d.pred_dir>0))|((d.cmf<0)&(d.pred_dir<0)))]
    cm=d[((d.cmf>0)&(d.actual_dir!=0))]
    def s(x): return f"{len(x)} trades, dir {x.ok.sum()}/{len(x)-x.ok.sum()} ({100*x.ok.mean():.1f}%), 0.25% target hit {100*x.tgt.mean():.1f}%"
    print(g,'ALL Kronos     :',s(d))
    print(g,'aligned (trade):',s(al))
    for side,x in (('  aligned BUY ',al[al.pred_dir>0]),('  aligned SELL',al[al.pred_dir<0])): print(g,side,':',s(x))
    print(g,'not aligned    :',s(mis))
    c=d.copy(); c['ok']=np.sign(c.cmf)==c.actual_dir; c['tgt']=np.where(c.cmf>0,c.up_hit,c.dn_hit).astype(bool)
    print(g,'CMF alone      :',s(c[c.cmf!=0]))
    print(g,'aligned by coin:',{p:f"{100*x.ok.mean():.0f}% of {len(x)}" for p,x in al.groupby('product')})
    print(g,'BASELINE any candle: up-target hit %.1f%%, down-target hit %.1f%%'%(100*d.up_hit.astype(float).mean(),100*d.dn_hit.astype(float).mean()))
