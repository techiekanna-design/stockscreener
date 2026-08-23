"""
Conviction data refresher — zero-touch after first run.

What it does on every run (GitHub Actions, nightly):
  1. Universe  : Nifty 500 constituents from NSE (falls back to tickers.txt if NSE is unreachable).
  2. Prices    : incremental. Existing prices.json is extended with NSE bhavcopy files for every
                 trading day since the last stored date (exchange-official bars). First run, or any
                 symbol with <260 bars, is bootstrapped from Yahoo 1y daily. Bhavcopy failure falls
                 back to Yahoo for that day.
  3. Pre-filter: cheap price tests (above 50-DMA, or near 52w high, or 20>50 DMA cross) so we only
                 spend Yahoo fundamentals calls on names that could possibly score.
  4. Fundamentals: yfinance statements + key stats -> fundamentals.json (TTM/last FY, with asOf).
  5. Writes fundamentals.json, prices.json, and data_status.json (what succeeded, what fell back).

    pip install yfinance pandas requests
    python update_data.py
"""
import io, json, math, sys, time, zipfile, datetime as dt
from pathlib import Path
import requests, pandas as pd, yfinance as yf

HERE = Path(__file__).parent
CR = 1e7
UA = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/124 Safari/537.36',
      'Accept': '*/*', 'Referer': 'https://www.nseindia.com/'}
NIFTY500 = 'https://archives.nseindia.com/content/indices/ind_nifty500list.csv'
BHAV = 'https://nsearchives.nseindia.com/content/cm/BhavCopy_NSE_CM_0_0_0_{d}_F_0000.csv.zip'
MIN_BARS = 260
status = {'run': dt.datetime.now(dt.timezone.utc).isoformat(timespec='seconds'), 'notes': []}
def note(m): print(m); status['notes'].append(m)

# ---------------- universe ----------------
def universe():
    try:
        r = requests.get(NIFTY500, headers=UA, timeout=30); r.raise_for_status()
        df = pd.read_csv(io.StringIO(r.text))
        syms = df['Symbol'].str.strip().str.upper().tolist()
        sectors = dict(zip(syms, df['Industry'].astype(str)))
        note(f'universe: Nifty 500 from NSE ({len(syms)})'); return syms, sectors
    except Exception as e:
        note(f'universe: NSE index list failed ({e}); using tickers.txt')
        syms = [l.strip().upper() for l in (HERE/'tickers.txt').read_text().splitlines() if l.strip() and not l.startswith('#')]
        return syms, {}

# ---------------- prices ----------------
def load_prices():
    p = HERE/'prices.json'
    return json.loads(p.read_text()) if p.exists() else {}

def yahoo_bootstrap(symbols):
    out = {}
    for i in range(0, len(symbols), 100):
        chunk = symbols[i:i+100]
        df = yf.download([s+'.NS' for s in chunk], period='1y', interval='1d', group_by='ticker',
                         auto_adjust=False, progress=False, threads=True)
        for s in chunk:
            try: d = df[s+'.NS'].dropna(subset=['Close'])
            except KeyError: continue
            out[s] = [[ix.strftime('%Y-%m-%d'), round(float(r.Open),2), round(float(r.High),2),
                       round(float(r.Low),2), round(float(r.Close),2), int(r.Volume)] for ix, r in d.iterrows()]
    return out

def bhavcopy(day):
    r = requests.get(BHAV.format(d=day.strftime('%Y%m%d')), headers=UA, timeout=30)
    if r.status_code == 404: return None                     # holiday / weekend
    r.raise_for_status()
    z = zipfile.ZipFile(io.BytesIO(r.content))
    df = pd.read_csv(z.open(z.namelist()[0]))
    df = df[df['SctySrs'].astype(str).str.strip() == 'EQ']
    return {str(t).strip(): [day.strftime('%Y-%m-%d'), float(o), float(h), float(l), float(c), int(v)]
            for t, o, h, l, c, v in zip(df['TckrSymb'], df['OpnPric'], df['HghPric'], df['LwPric'], df['ClsPric'], df['TtlTradgVol'])}

def update_prices(symbols):
    prices = {s: v for s, v in load_prices().items() if s in symbols}
    need_boot = [s for s in symbols if len(prices.get(s, [])) < MIN_BARS - 30]
    if need_boot:
        note(f'prices: bootstrapping {len(need_boot)} symbols from Yahoo (1y)')
        prices.update(yahoo_bootstrap(need_boot))
    last = max((v[-1][0] for v in prices.values() if v), default=None)
    if last is None: return prices
    day = dt.date.fromisoformat(last) + dt.timedelta(days=1)
    today = dt.date.today()
    added = 0
    while day <= today:
        if day.weekday() < 5:
            try:
                bars = bhavcopy(day)
                if bars:
                    for s in symbols:
                        if s in bars:
                            prices.setdefault(s, []).append(bars[s])
                    added += 1
            except Exception as e:
                note(f'prices: bhavcopy {day} failed ({e}); Yahoo will backfill next run')
                break
        day += dt.timedelta(days=1)
    note(f'prices: appended {added} bhavcopy sessions after {last}')
    # keep ~1.2y, dedupe by date
    for s in prices:
        seen, out = set(), []
        for b in prices[s]:
            if b[0] not in seen: seen.add(b[0]); out.append(b)
        prices[s] = out[-320:]
    return prices

def prefilter(prices):
    keep = []
    for s, bars in prices.items():
        if len(bars) < 210: continue
        c = [b[4] for b in bars]
        s20, s50 = sum(c[-20:])/20, sum(c[-50:])/50
        s20p, s50p = sum(c[-25:-5])/20, sum(c[-55:-5])/50
        hi52 = max(b[2] for b in bars[-252:])
        if c[-1] > s50 or c[-1] >= 0.9*hi52 or (s20 > s50 and s20p <= s50p):
            keep.append(s)
    return keep

# ---------------- fundamentals ----------------
def num(x):
    try: v = float(x); return None if math.isnan(v) else v
    except (TypeError, ValueError): return None
def row(df, *names):
    if df is None or df.empty: return []
    for n in names:
        if n in df.index: return [num(v) for v in df.loc[n].tolist()]
    return []
def growth(cur, prev):
    return None if cur is None or prev is None or prev <= 0 else round((cur/prev-1)*100, 1)
def cagr(series, years):
    s = [v for v in series if v is not None]
    if len(s) <= years or s[years] <= 0 or s[0] <= 0: return None
    return round(((s[0]/s[years])**(1/years)-1)*100, 1)

def fundamentals(sym, sector_hint, overrides):
    t = yf.Ticker(sym+'.NS'); info = t.info or {}
    inc, bs, cf = t.income_stmt, t.balance_sheet, t.cashflow
    rev, ebitda = row(inc, 'Total Revenue', 'Operating Revenue'), row(inc, 'EBITDA', 'Normalized EBITDA')
    eps, ni, fcf = row(inc, 'Diluted EPS', 'Basic EPS'), row(inc, 'Net Income', 'Net Income Common Stockholders'), row(cf, 'Free Cash Flow')
    debt, eq = row(bs, 'Total Debt'), row(bs, 'Stockholders Equity', 'Common Stock Equity')
    opm = round(ebitda[0]/rev[0]*100, 1) if len(rev) and len(ebitda) and rev[0] and ebitda[0] is not None else None
    opm_prev = round(ebitda[1]/rev[1]*100, 1) if len(rev) > 1 and len(ebitda) > 1 and rev[1] and ebitda[1] is not None else None
    eps_g = growth(eps[0], eps[1]) if len(eps) > 1 else None
    pe = num(info.get('trailingPE'))
    de = round(debt[0]/eq[0], 2) if debt and eq and debt[0] is not None and eq[0] else (round(info['debtToEquity']/100, 2) if num(info.get('debtToEquity')) is not None else None)
    roe = num(info.get('returnOnEquity'))
    o = overrides.get(sym, {})
    return {'ticker': sym, 'name': info.get('longName') or info.get('shortName') or sym,
            'sector': o.get('sector') or sector_hint or info.get('sector') or 'Unknown',
            'mcap': round(info['marketCap']/CR) if num(info.get('marketCap')) else None,
            'revG3y': cagr(rev, 3) if len(rev) >= 4 else (cagr(rev, 2) if len(rev) >= 3 else None),
            'epsG': eps_g, 'opm': opm, 'opmPrev': opm_prev,
            'roe': round(roe*100, 1) if roe is not None else None, 'de': de,
            'fcf': round(fcf[0]/CR) if fcf and fcf[0] is not None else None,
            'pat': round(ni[0]/CR) if ni and ni[0] is not None else None,
            'pe': round(pe, 1) if pe else None,
            'peg': round(pe/eps_g, 2) if pe and eps_g and eps_g > 0 else None,
            'moat': o.get('moat', 2), 'leader': o.get('leader', False),
            'asOf': ('FY ending ' + str(inc.columns[0])[:10]) if inc is not None and not inc.empty else None,
            'fetched': dt.datetime.now(dt.timezone.utc).isoformat(timespec='seconds')}

def main():
    syms, sectors = universe()
    overrides = json.loads((HERE/'overrides.json').read_text()) if (HERE/'overrides.json').exists() else {}
    overrides.pop('_comment', None)
    prices = update_prices(syms)
    (HERE/'prices.json').write_text(json.dumps(prices, separators=(',', ':')))
    cand = prefilter(prices)
    note(f'fundamentals: {len(cand)} of {len(prices)} passed the price pre-filter')
    # reuse fundamentals younger than 7 days for candidates, refresh the rest
    old = {}
    fp = HERE/'fundamentals.json'
    if fp.exists():
        for s in json.loads(fp.read_text()).get('stocks', []):
            if s.get('fetched') and (dt.datetime.now(dt.timezone.utc) - dt.datetime.fromisoformat(s['fetched'])).days < 7:
                old[s['ticker']] = s
    stocks, failed = [], []
    for i, s in enumerate(cand, 1):
        if s in old: stocks.append(old[s]); continue
        try:
            stocks.append(fundamentals(s, sectors.get(s), overrides)); time.sleep(0.4)
        except Exception as e:
            failed.append(s); print('FAIL', s, e, file=sys.stderr)
        if i % 50 == 0: print(f'  {i}/{len(cand)}')
    fp.write_text(json.dumps({'fetched': status['run'], 'source': 'NSE bhavcopy (prices) + Yahoo Finance via yfinance (fundamentals)',
                              'universe': 'Nifty 500', 'failed': failed, 'stocks': stocks}, indent=1))
    status['fundamentals'] = len(stocks); status['failed'] = failed; status['prices'] = len(prices)
    (HERE/'data_status.json').write_text(json.dumps(status, indent=1))
    note(f'done: {len(stocks)} fundamentals, {len(prices)} price series, {len(failed)} failed')

if __name__ == '__main__':
    main()
