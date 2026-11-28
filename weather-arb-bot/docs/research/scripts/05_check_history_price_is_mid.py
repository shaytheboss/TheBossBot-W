"""Is the CLOB history price the mid? Compares it with the hourly bid/ask in the
shadow CSV (<DATA>/shadow_in/shadow.csv). Result 27.11.2026: inside [bid,ask]
89%, median |p - mid| 0.05c -> it is the mid.
Usage: python -I 05_check_history_price_is_mid.py <DATA>
"""
import json, sys, numpy as np, pandas as pd
S = sys.argv[1]
meta = json.load(open(S + '/ph/ph_meta.json')); z = np.load(S + '/ph/ph_series.npz')
SH = {'New York': 'nyc', 'São Paulo': 'sao-paulo'}
key = {(m['city'], m['day'], m['bucket']): m['token'] for m in meta}
print('meta bucket examples:', [m['bucket'] for m in meta[:4]])
sh = pd.read_csv(S + '/shadow_in/shadow.csv', usecols=['taken_at', 'city', 'event_date', 'bucket', 'market_p', 'bid', 'ask', 'price_live'])
sh = sh[(sh.price_live == 1) & sh.bid.notna() & sh.ask.notna()].sample(30000, random_state=2)
print('shadow bucket examples:', sh.bucket.head(4).tolist())
sh['ts'] = (pd.to_datetime(sh.taken_at, utc=True) - pd.Timestamp('1970-01-01', tz='UTC')) // pd.Timedelta('1s')
res = []; miss = 0
for r in sh.itertuples():
    tok = key.get((SH.get(r.city, r.city.lower().replace(' ', '-')), r.event_date, r.bucket))
    if tok is None: miss += 1; continue
    t, p = z[f"t_{tok}"], z[f"p_{tok}"]
    i = np.searchsorted(t, r.ts, side='right') - 1
    if i < 0 or r.ts - t[i] > 120: continue
    res.append((float(p[i]), r.bid, r.ask))
a = np.array(res); print('matched', len(a), 'unmatched', miss)
if len(a):
    mid = (a[:, 1] + a[:, 2]) / 2; spr = a[:, 2] - a[:, 1]
    print(f"p inside [bid,ask]: {100*np.mean((a[:,0] >= a[:,1]-1e-6) & (a[:,0] <= a[:,2]+1e-6)):.1f}%   |p-mid| median {100*np.median(np.abs(a[:,0]-mid)):.2f}c, 90th pct {100*np.percentile(np.abs(a[:,0]-mid),90):.2f}c")
    w = spr > 0.04
    print(f"  when the spread is > 4c (n={w.sum()}): |p-mid| median {100*np.median(np.abs(a[w,0]-mid[w])):.2f}c; p equals bid or ask in {100*np.mean(np.isclose(a[w,0],a[w,1])|np.isclose(a[w,0],a[w,2])):.0f}%, equals mid in {100*np.mean(np.isclose(a[w,0],mid[w], atol=0.003)):.0f}%")
