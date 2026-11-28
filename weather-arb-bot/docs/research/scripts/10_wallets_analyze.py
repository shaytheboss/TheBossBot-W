"""Per wallet on settled November temperature markets: P&L, return, YES/NO,
sells, price paid vs the minute mid, drift after the buy, price bands, timing.
Needs <DATA>/gamma and <DATA>/ph. Usage (in the folder with trades_nov.json):
python -I 10_wallets_analyze.py <DATA>
"""
import json, glob, sys, numpy as np, pandas as pd
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
S = sys.argv[1]
src = open(__import__('os').path.join(__import__('os').path.dirname(__import__('os').path.abspath(__file__)), '04_fetch_minute_prices.py')).read(); TZ = eval(src[src.index("TZ = {") + 5: src.index("}\n", src.index("TZ = {")) + 1])
# token -> (won, is_yes, yes_token, event day) for every closed November market (both sides)
tok = {}
for f in glob.glob(S + '/gamma/closed_*.json'):
    for e in (json.load(open(f)) or []):
        if not (isinstance(e, dict) and 'highest-temperature' in e.get('slug', '')): continue
        day = datetime.strptime(e['slug'].rsplit('-on-', 1)[1], '%B-%d-%Y').date()
        for m in e.get('markets', []):
            try: ts = json.loads(m['clobTokenIds']); outs = json.loads(m['outcomes']); pr = json.loads(m['outcomePrices'])
            except Exception: continue
            yi = [o.lower() for o in outs].index('yes')
            for i, t in enumerate(ts):
                tok[t] = dict(won=float(pr[i]) >= 0.99, yes=(i == yi), yes_tok=ts[yi], day=day, slug=e['slug'])
z = np.load(S + '/ph/ph_series.npz'); series = {}
def mid_at(yes_tok, ts, is_yes):
    k = f"t_{yes_tok}"
    if k not in z.files: return None
    if yes_tok not in series: series[yes_tok] = (z[k], z[f"p_{yes_tok}"].astype(float))
    t, p = series[yes_tok]; i = np.searchsorted(t, ts, side='right') - 1
    if i < 0 or ts - t[i] > 180: return None
    return p[i] if is_yes else 1 - p[i]
W = json.load(open('trades_nov.json'))
rows = []
for w, v in W.items():
    for x in v['trades']:
        info = tok.get(x['token_id'])
        if info is None: continue
        city = x['event_slug'].split('highest-temperature-in-')[1].split('-on-')[0]
        loc = datetime.fromtimestamp(x['timestamp'], ZoneInfo(TZ.get(city, 'UTC')))
        m0 = mid_at(info['yes_tok'], x['timestamp'], info['yes']); m5 = mid_at(info['yes_tok'], x['timestamp'] + 300, info['yes'])
        m60 = mid_at(info['yes_tok'], x['timestamp'] + 3600, info['yes'])
        rows.append(dict(w=v['name'], side=x['side'], yes=info['yes'], price=x['price'], qty=x['size'], won=info['won'],
                         lead=(info['day'] - loc.date()).days, lh=loc.hour, m0=m0, m5=m5, m60=m60, slug=info['slug'],
                         half=1 if info['day'] < datetime(2026, 11, 14).date() else 2))
T = pd.DataFrame(rows)
T['cash'] = np.where(T.side == 'BUY', -T.qty * T.price, T.qty * T.price)
T['shares'] = np.where(T.side == 'BUY', T.qty, -T.qty)
T['payout'] = T.shares * T.won.astype(float)
T['usd'] = T.qty * T.price
print(f"settled-market trades analysed: {len(T):,} across {T.w.nunique()} wallets\n")
g = T.groupby('w')
S1 = pd.DataFrame({
    'trades': g.size(), 'buy_$': g.apply(lambda x: x.usd[x.side == 'BUY'].sum()),
    'pnl_$': g.apply(lambda x: (x.cash + x.payout).sum()),
    'sell%': g.apply(lambda x: 100 * (x.side == 'SELL').mean()),
    'YES%': g.apply(lambda x: 100 * x.yes[x.side == 'BUY'].mean()),
    'avg_buy¢': g.apply(lambda x: 100 * x.price[x.side == 'BUY'].mean()),
    'pnl_h1': g.apply(lambda x: (x.cash + x.payout)[x.half == 1].sum()),
    'pnl_h2': g.apply(lambda x: (x.cash + x.payout)[x.half == 2].sum()),
})
S1['ret%'] = 100 * S1['pnl_$'] / S1['buy_$']
print(S1.sort_values('pnl_$', ascending=False).round(1).to_string())
B = T[(T.side == 'BUY') & T.m0.notna()].copy()
B['vs_mid¢'] = 100 * (B.price - B.m0)
B['d5¢'] = 100 * (B.m5 - B.m0); B['d60¢'] = 100 * (B.m60 - B.m0)
B['edge¢'] = 100 * (B.won.astype(float) - B.price)
print("\nBUYS — price paid vs the mid at that minute, and what the mid did next (in the bought side's favour = +):")
g = B.groupby('w')
S2 = pd.DataFrame({'buys': g.size(), 'paid-mid¢ (median)': g['vs_mid¢'].median(), 'below mid %': g.apply(lambda x: 100 * (x['vs_mid¢'] < -0.3).mean()),
                   'mid +5m ¢': g['d5¢'].mean(), 'mid +60m ¢': g['d60¢'].mean(), 'win-price ¢/share': g['edge¢'].mean()})
print(S2.round(2).to_string())
print("\nBUYS by price paid — all these wallets together:")
B['band'] = pd.cut(B.price, [0, .05, .2, .5, .8, .95, 1.0], labels=['<5¢', '5-20¢', '20-50¢', '50-80¢', '80-95¢', '>95¢'])
print(B.groupby('band', observed=True).agg(buys=('edge¢', 'size'), usd=('usd', 'sum'), won=('won', 'mean'), paid=('price', 'mean'), edge=('edge¢', 'mean')).round(3).to_string())
print("\nBUYS by days before the event / local hour:")
print(B.groupby(B.lead.clip(-1, 3)).agg(buys=('edge¢', 'size'), edge=('edge¢', 'mean'), d60=('d60¢', 'mean')).round(2).to_string())
print(B.groupby(pd.cut(B.lh, [-1, 5, 9, 13, 17, 23])).agg(buys=('edge¢', 'size'), edge=('edge¢', 'mean'), d60=('d60¢', 'mean')).round(2).to_string())
T.to_pickle('trades_analysed.pkl')
