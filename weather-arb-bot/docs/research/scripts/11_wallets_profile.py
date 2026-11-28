"""Profile of the top wallets' buys: YES/NO, days before the event, UTC/local
hour, price bands, the bot's own model vs price at that hour (shadow CSV),
bucket distance from the bot's forecast, drift 1h/3h after.
"""
import json, glob, sys, re, math, numpy as np, pandas as pd
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
S = sys.argv[1]
src = open(__import__('os').path.join(__import__('os').path.dirname(__import__('os').path.abspath(__file__)), '04_fetch_minute_prices.py')).read(); TZ = eval(src[src.index("TZ = {") + 5: src.index("}\n", src.index("TZ = {")) + 1])
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
                tok[t] = dict(won=float(pr[i]) >= 0.99, yes=(i == yi), day=str(day), label=m.get('groupItemTitle'))
SH = {'nyc': 'New York', 'sao-paulo': 'São Paulo'}
sh = pd.read_csv(S + '/shadow_in/shadow.csv', usecols=['taken_at', 'city', 'event_date', 'bucket', 'model_p', 'market_p', 'forecast_high_f', 'v2_p'])
sh['ts'] = (pd.to_datetime(sh.taken_at, utc=True) - pd.Timestamp('1970-01-01', tz='UTC')) // pd.Timedelta('1s')
sh = sh.sort_values('ts'); grp = {k: g[['ts', 'model_p', 'market_p', 'forecast_high_f', 'v2_p']].to_numpy() for k, g in sh.groupby(['city', 'event_date', 'bucket'])}
def parse(lab):
    unit = 'C' if '°C' in lab else 'F'; nums = [int(x) for x in re.findall(r'-?\d+', lab.replace('°', ' '))]
    if 'higher' in lab or 'above' in lab: return unit, nums[0], None
    if 'below' in lab or 'lower' in lab: return unit, None, nums[0]
    return (unit, nums[0], nums[1]) if (len(nums) == 2 and unit == 'F') else (unit, nums[0], nums[0])
def levels(lab, fc_f):
    unit, lo, hi = parse(lab); v = fc_f if unit == 'F' else (fc_f - 32) * 5 / 9; r = round(v); w = 2 if unit == 'F' else 1
    if lo is not None and r < lo: return math.ceil((lo - r) / w)
    if hi is not None and r > hi: return -math.ceil((r - hi) / w)
    return 0
W = json.load(open('trades_nov.json'))
for who in ('WrongBy1Degree', 'Bilberry', 'HighTempTation'):
    v = [x for x in W.values() if x['name'] == who][0]; rows = []
    for x in v['trades']:
        if x['side'] != 'BUY': continue
        info = tok.get(x['token_id'])
        if info is None: continue
        city = x['event_slug'].split('highest-temperature-in-')[1].split('-on-')[0]
        tz = ZoneInfo(TZ.get(city, 'UTC')); loc = datetime.fromtimestamp(x['timestamp'], tz); utc = datetime.fromtimestamp(x['timestamp'], timezone.utc)
        key = (SH.get(city, ' '.join(w.capitalize() for w in city.split('-'))), info['day'], info['label'])
        a = grp.get(key); snap = after1 = after3 = None
        if a is not None:
            i = np.searchsorted(a[:, 0], x['timestamp'], side='right') - 1
            if i >= 0 and x['timestamp'] - a[i, 0] <= 3700: snap = a[i]
            j1 = np.searchsorted(a[:, 0], x['timestamp'] + 3600); j3 = np.searchsorted(a[:, 0], x['timestamp'] + 3 * 3600)
            after1 = a[j1] if j1 < len(a) else None; after3 = a[j3] if j3 < len(a) else None
        side_p = lambda s: None if s is None else (s[2] if info['yes'] else 1 - s[2])
        rows.append(dict(yes=info['yes'], price=x['price'], qty=x['size'], won=float(info['won']),
                         lead=(datetime.fromisoformat(info['day']).date() - loc.date()).days, lh=loc.hour, uh=utc.hour,
                         model=None if snap is None else (snap[1] if info['yes'] else 1 - snap[1]),
                         v2=None if snap is None or pd.isna(snap[4]) else (snap[4] if info['yes'] else 1 - snap[4]),
                         lev=None if snap is None else levels(info['label'], snap[3]),
                         d1=None if after1 is None else side_p(after1) - x['price'], d3=None if after3 is None else side_p(after3) - x['price']))
    D = pd.DataFrame(rows); D['edge'] = D.won - D.price; D['usd'] = D.qty * D.price; D['pnl'] = D.qty * D.edge
    print(f"\n================ {who}: {len(D)} buys, ${D.usd.sum():,.0f}, P&L ${D.pnl.sum():,.0f} ({100*D.pnl.sum()/D.usd.sum():.1f}%)")
    print(" YES vs NO:"); print(D.groupby('yes').agg(buys=('edge','size'), usd=('usd','sum'), won=('won','mean'), paid=('price','mean'), pnl=('pnl','sum')).round(3).to_string())
    print(" days before the event (city's calendar):"); print(D.groupby(D.lead.clip(-1, 4)).agg(buys=('edge','size'), usd=('usd','sum'), pnl=('pnl','sum'), won=('won','mean'), paid=('price','mean')).round(2).to_string())
    print(" UTC hour of the buy (top 8 by count):", D.uh.value_counts().head(8).to_dict())
    print(" local hour of the buy (top 8):", D.lh.value_counts().head(8).to_dict())
    print(" price paid:"); print(D.groupby(pd.cut(D.price, [0, .1, .25, .4, .6, .8, .95, 1])).agg(buys=('edge','size'), pnl=('pnl','sum'), won=('won','mean'), paid=('price','mean')).round(3).to_string())
    k = D.dropna(subset=['model'])
    print(f" vs the bot's own model at that hour (n={len(k)}): bot's prob for the side bought minus price paid = {100*(k.model-k.price).mean():+.1f}c on average; "
          f"bought where bot's prob > price: {100*(k.model > k.price).mean():.0f}%")
    if k.v2.notna().any(): print(f"   v2's prob minus price: {100*(k.v2-k.price).mean():+.1f}c; v2 > price: {100*(k.v2 > k.price).mean():.0f}%")
    print(" bucket's distance from the bot's forecast (levels; + = bucket above the forecast) for YES buys:", k[k.yes].lev.value_counts().sort_index().to_dict())
    print(f" price move after the buy (bought side): +1h {100*D.d1.mean():+.2f}c, +3h {100*D.d3.mean():+.2f}c (n={D.d1.notna().sum()})")
