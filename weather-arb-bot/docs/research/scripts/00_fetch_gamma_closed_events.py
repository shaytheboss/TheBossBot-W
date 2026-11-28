"""Download closed Polymarket weather events from Gamma (newest first) into
<DATA>/gamma/closed_<offset>.json. Gamma serves pages with limit > 50 only
gzip-compressed, so the request asks for gzip.

Usage: python 00_fetch_gamma_closed_events.py <DATA> [pages=25]
"""
import gzip, json, os, sys, time, urllib.request
DATA = sys.argv[1]; pages = int(sys.argv[2]) if len(sys.argv) > 2 else 25
os.makedirs(DATA + '/gamma', exist_ok=True)
for k in range(pages):
    off = 100 * k
    url = ("https://gamma-api.polymarket.com/events?tag_slug=weather&closed=true&limit=100"
           f"&offset={off}&order=endDate&ascending=false")
    req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0', 'Accept-Encoding': 'gzip'})
    raw = urllib.request.urlopen(req, timeout=60).read()
    try: raw = gzip.decompress(raw)
    except OSError: pass
    open(f"{DATA}/gamma/closed_{off}.json", 'wb').write(raw)
    print(off, len(json.loads(raw)), flush=True); time.sleep(0.3)
