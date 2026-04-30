#!/usr/bin/env python3
"""
monitor.py — colectează datele live pentru DCA Pasiv Pro v13+
Versiune cu EUR/RON din BNR (server-side, fără CORS).

Surse:
- CAPE Shiller: multpl.com/shiller-pe (scraping)
- VIX: Yahoo Finance ^VIX
- Fear & Greed: CNN Money index
- S&P500: Yahoo ^GSPC
- Fed Funds Rate: FRED CSV (DFEDTARU)
- VWCE.DE: Yahoo (preț + SMA200)
- EUR/RON: BNR XML (server-side, mereu funcționează)

Output: market_data.json
"""

import json
import re
import datetime as dt
import urllib.request


def http_get(url, timeout=15, headers=None):
    """GET cu timeout și user-agent corect."""
    req = urllib.request.Request(url, headers=headers or {
        'User-Agent': 'Mozilla/5.0 (compatible; DCA-Pasiv-Pro/1.0)'
    })
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode('utf-8', errors='ignore')


def fetch_cape():
    """CAPE Shiller PE Ratio — scrape multpl.com."""
    try:
        html = http_get('https://www.multpl.com/shiller-pe')
        m = re.search(r'class=["\']?current["\']?[^>]*>.*?<[^>]+>\s*([\d.]+)', html, re.IGNORECASE | re.DOTALL)
        if not m:
            m = re.search(r'>(\d{2}\.\d{1,2})<', html)
        return float(m.group(1)) if m else None
    except Exception as e:
        print(f'CAPE fetch failed: {e}')
        return None


def fetch_yahoo(ticker):
    """Yahoo Finance — preț regular curent."""
    try:
        url = f'https://query1.finance.yahoo.com/v8/finance/chart/{ticker}?interval=1d&range=2d'
        data = json.loads(http_get(url))
        result = data['chart']['result'][0]
        return float(result['meta']['regularMarketPrice'])
    except Exception as e:
        print(f'Yahoo {ticker} failed: {e}')
        return None


def fetch_vwce_sma200():
    """VWCE.DE preț + SMA200 = avg ultim 200 zile."""
    try:
        url = 'https://query1.finance.yahoo.com/v8/finance/chart/VWCE.DE?interval=1d&range=1y'
        data = json.loads(http_get(url))
        result = data['chart']['result'][0]
        closes = [c for c in result['indicators']['quote'][0]['close'] if c is not None]
        price = closes[-1]
        sma200 = sum(closes[-200:]) / min(200, len(closes))
        return price, sma200, price > sma200
    except Exception as e:
        print(f'VWCE SMA fetch failed: {e}')
        return None, None, None


def fetch_fear_greed():
    """CNN Fear & Greed Index."""
    try:
        url = 'https://production.dataviz.cnn.io/index/fearandgreed/graphdata'
        data = json.loads(http_get(url, headers={
            'User-Agent': 'Mozilla/5.0',
            'Accept': 'application/json'
        }))
        score = data.get('fear_and_greed', {}).get('score')
        rating = data.get('fear_and_greed', {}).get('rating')
        return float(score) if score else None, rating or ''
    except Exception as e:
        print(f'F&G fetch failed: {e}')
        return None, ''


def fetch_fed_rate():
    """Fed Funds Rate — FRED CSV (DFEDTARU)."""
    try:
        url = 'https://fred.stlouisfed.org/graph/fredgraph.csv?id=DFEDTARU&cosd=2024-01-01'
        csv = http_get(url)
        lines = [l for l in csv.strip().split('\n') if l and not l.startswith('DATE')]
        last = lines[-1].split(',')
        return float(last[1]) if len(last) >= 2 else None
    except Exception as e:
        print(f'Fed rate fetch failed: {e}')
        return None


# ━━━ NOU în v2: EUR/RON din BNR (server-side, fără CORS) ━━━
def fetch_eur_ron():
    """EUR/RON din BNR XML — server-side, mereu funcționează."""
    try:
        xml = http_get('https://www.bnr.ro/nbrfxrates.xml', timeout=10)
        m = re.search(r'<Rate currency="EUR"[^>]*>([\d.]+)</Rate>', xml)
        if m:
            v = float(m.group(1))
            if 4.5 <= v <= 7.0:
                return v
        return None
    except Exception as e:
        print(f'BNR EUR/RON fetch failed: {e}')
        return None


def calculate_correction_pct(sp500_current):
    """Corecție vs maxim 52W S&P500."""
    try:
        url = 'https://query1.finance.yahoo.com/v8/finance/chart/^GSPC?interval=1d&range=1y'
        data = json.loads(http_get(url))
        highs = data['chart']['result'][0]['indicators']['quote'][0]['high']
        max52w = max([h for h in highs if h is not None])
        return round((max52w - sp500_current) / max52w * 100, 2) if max52w else 0
    except Exception:
        return 0


def calculate_market_score(cape, vix, fed_rate, sma_above):
    """Replică logica recalcScore() din aplicație."""
    if cape is None: return None
    cape_score = max(0, min(100, round(100 - (cape / 50 * 100))))
    trend_score = 35 if sma_above else 65
    vix_pts = 30 if (vix is None or vix < 20) else (50 if vix < 30 else 75)
    rate_pts = 30 if (fed_rate or 0) > 4 else (15 if (fed_rate or 0) > 2 else 5)
    risk_score = min(100, vix_pts + rate_pts)
    return round(cape_score * 0.6 + trend_score * 0.2 + risk_score * 0.2)


def main():
    print('Fetching market data...')

    cape = fetch_cape()
    vix = fetch_yahoo('^VIX')
    sp500 = fetch_yahoo('^GSPC')
    fg_score, fg_rating = fetch_fear_greed()
    fed_rate = fetch_fed_rate()
    eur_ron = fetch_eur_ron()  # ← NOU în v2
    vwce_price, vwce_sma200, vwce_above_sma = fetch_vwce_sma200()
    correction = calculate_correction_pct(sp500) if sp500 else 0

    now = dt.datetime.utcnow()

    data = {
        'updated_at': now.isoformat() + 'Z',
        'updated_at_display': now.strftime('%Y-%m-%d %H:%M UTC'),
        'cape': cape,
        'vix': vix,
        'sp500_price': sp500,
        'correction_pct': correction,
        'fg_score': fg_score,
        'fg_rating': fg_rating,
        'fed_rate': fed_rate,
        'eur_ron': eur_ron,  # ← NOU în v2 (citit de aplicație v13)
        'vwce_price': vwce_price,
        'vwce_sma200': vwce_sma200,
        'vwce_above_sma': vwce_above_sma,
        'score': calculate_market_score(cape, vix, fed_rate, vwce_above_sma)
    }

    # Filtrează valorile None
    data = {k: v for k, v in data.items() if v is not None}

    with open('market_data.json', 'w') as f:
        json.dump(data, f, indent=2)

    print(f'OK: market_data.json updated')
    print(json.dumps(data, indent=2))


if __name__ == '__main__':
    main()
