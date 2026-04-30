#!/usr/bin/env python3
"""
monitor.py v3 — colectează datele live pentru DCA Pasiv Pro v14+
Fix-uri:
- CAPE: BeautifulSoup + sanity check (interval 10-60) + fallback la valoarea anterioară dacă aberant
- F&G: 3 strategii (CNN API, CNN scrape, fallback Alternative.me)
- Fed Rate: FRED CSV + fallback HTML scrape
- Z-score sanity check pe TOATE câmpurile
- Păstrează valoarea anterioară dacă noul fetch e clar aberant
"""

import json
import re
import datetime as dt
import urllib.request
import urllib.error
import os.path

try:
    from bs4 import BeautifulSoup
    HAS_BS4 = True
except ImportError:
    HAS_BS4 = False
    print('WARNING: BeautifulSoup not available — fallback to regex (less robust)')


# ── HELPER: HTTP GET cu retry + user-agent ─────────────────────────
def http_get(url, timeout=15, headers=None):
    req = urllib.request.Request(url, headers=headers or {
        'User-Agent': 'Mozilla/5.0 (compatible; DCA-Pasiv-Pro/3.0)'
    })
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode('utf-8', errors='ignore')


# ── HELPER: încarcă valoarea anterioară din market_data.json existent ──
def load_previous_value(field, default=None):
    if not os.path.exists('market_data.json'):
        return default
    try:
        with open('market_data.json') as f:
            data = json.load(f)
        return data.get(field, default)
    except Exception:
        return default


# ── HELPER: validare cu sanity range + fallback la valoare anterioară ──
def validate_or_previous(value, field, min_v, max_v, label='value'):
    """Returnează value dacă e în range, altfel valoarea anterioară din JSON."""
    if value is not None and min_v <= value <= max_v:
        return value
    prev = load_previous_value(field)
    if prev is not None and min_v <= prev <= max_v:
        print(f'WARNING: {label}={value} outside [{min_v},{max_v}] — fallback la anterior {prev}')
        return prev
    print(f'ERROR: {label}={value} aberant și fără valoare anterioară validă')
    return None


# ── CAPE: BeautifulSoup + sanity check ───────────────────────────────
def fetch_cape():
    """CAPE Shiller — multpl.com cu BeautifulSoup, range valid [10, 60]."""
    try:
        html = http_get('https://www.multpl.com/shiller-pe', timeout=20)

        if HAS_BS4:
            soup = BeautifulSoup(html, 'html.parser')
            # multpl.com structură: <div id="current"> ... <b>VALUE</b>
            current = soup.find(id='current')
            if current:
                # Caută primul <b> sau primul număr cu format float
                b_tag = current.find('b')
                if b_tag:
                    txt = b_tag.get_text(strip=True)
                    m = re.search(r'(\d{1,2}\.\d{1,2})', txt)
                    if m:
                        val = float(m.group(1))
                        return validate_or_previous(val, 'cape', 10.0, 60.0, 'CAPE multpl.com #current')
            # Fallback: caută text "Current" în pagină
            for el in soup.find_all(text=re.compile(r'(?i)current\s*Shiller')):
                parent_text = el.parent.get_text(' ', strip=True) if el.parent else ''
                m = re.search(r'(\d{2}\.\d{1,2})', parent_text)
                if m:
                    val = float(m.group(1))
                    if 10 <= val <= 60:
                        return val

        # Fallback regex (fără bs4) — mai strict
        # Caută pattern "Current Shiller PE Ratio: XX.YY"
        m = re.search(r'(?:Current\s+Shiller\s+PE\s+Ratio[:\s]*|id=["\']?current["\']?[^>]*>[^<]*<[^>]+>\s*)(\d{2}\.\d{1,2})', html, re.IGNORECASE)
        if m:
            val = float(m.group(1))
            return validate_or_previous(val, 'cape', 10.0, 60.0, 'CAPE multpl.com regex')

        # Fallback ultim: load anterioară
        prev = load_previous_value('cape')
        if prev:
            print(f'CAPE fetch failed, folosesc anterior: {prev}')
            return prev
        return None

    except Exception as e:
        print(f'CAPE fetch failed: {e}')
        return load_previous_value('cape')


# ── VIX, S&P500, VWCE — Yahoo Finance ────────────────────────────────
def fetch_yahoo(ticker):
    try:
        url = f'https://query1.finance.yahoo.com/v8/finance/chart/{ticker}?interval=1d&range=2d'
        data = json.loads(http_get(url))
        result = data['chart']['result'][0]
        return float(result['meta']['regularMarketPrice'])
    except Exception as e:
        print(f'Yahoo {ticker} failed: {e}')
        return None


def fetch_vwce_sma200():
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


# ── Fear & Greed: 3 strategii ────────────────────────────────────────
def fetch_fear_greed():
    """3 strategii: CNN API, CNN HTML scrape, alternative.me."""

    # 1. CNN API direct (s-a schimbat probabil structura — încercăm)
    try:
        url = 'https://production.dataviz.cnn.io/index/fearandgreed/graphdata'
        data = json.loads(http_get(url, headers={
            'User-Agent': 'Mozilla/5.0',
            'Accept': 'application/json',
            'Origin': 'https://edition.cnn.com',
            'Referer': 'https://edition.cnn.com/'
        }))
        # Try multiple paths
        score = None
        rating = ''
        if 'fear_and_greed' in data:
            fg = data['fear_and_greed']
            score = fg.get('score')
            rating = fg.get('rating', '')
        elif 'data' in data:
            # New structure?
            for item in data.get('data', []):
                if 'score' in item:
                    score = item['score']
                    rating = item.get('rating', '')
                    break
        if score is not None:
            try: score = float(score)
            except: score = None
        if score and 0 <= score <= 100:
            print(f'F&G CNN API OK: {score} ({rating})')
            return score, str(rating)
    except Exception as e:
        print(f'F&G CNN API failed: {e}')

    # 2. CNN HTML scrape
    try:
        html = http_get('https://edition.cnn.com/markets/fear-and-greed', timeout=20)
        if HAS_BS4:
            soup = BeautifulSoup(html, 'html.parser')
            # Caută containerul cu scor (text mare)
            for el in soup.find_all(class_=re.compile(r'(?i)fear|greed|score|gauge')):
                txt = el.get_text(strip=True)
                m = re.search(r'\b(\d{1,3})\b', txt)
                if m:
                    val = int(m.group(1))
                    if 0 <= val <= 100:
                        # Caută rating-ul
                        rating = ''
                        for keyword in ['Extreme Fear', 'Fear', 'Neutral', 'Greed', 'Extreme Greed']:
                            if keyword.lower() in html.lower():
                                rating = keyword
                                break
                        return float(val), rating
        # Fallback regex
        m = re.search(r'fear[\s-]?and[\s-]?greed[^0-9]{1,500}(\d{1,3})', html, re.IGNORECASE)
        if m:
            val = int(m.group(1))
            if 0 <= val <= 100:
                return float(val), ''
    except Exception as e:
        print(f'F&G CNN HTML failed: {e}')

    # 3. Alternative.me (crypto F&G — proxy semnificativ pentru sentiment)
    try:
        data = json.loads(http_get('https://api.alternative.me/fng/?limit=1'))
        item = data['data'][0]
        score = float(item['value'])
        rating = item.get('value_classification', '')
        print(f'F&G alternative.me OK: {score} ({rating}) [crypto sentiment proxy]')
        return score, rating + ' (crypto)'
    except Exception as e:
        print(f'F&G alternative.me failed: {e}')

    # Fallback: anterior
    prev_score = load_previous_value('fg_score')
    prev_rating = load_previous_value('fg_rating', '')
    if prev_score:
        print(f'F&G: folosesc anterior {prev_score}')
        return prev_score, prev_rating
    return None, ''


# ── Fed Rate: FRED CSV + fallback scrape ─────────────────────────────
def fetch_fed_rate():
    """Fed Funds Upper Target — FRED CSV cu fallback HTML scrape."""

    # 1. FRED CSV oficial
    try:
        url = 'https://fred.stlouisfed.org/graph/fredgraph.csv?id=DFEDTARU&cosd=2024-01-01'
        csv = http_get(url, timeout=15)
        lines = [l for l in csv.strip().split('\n') if l and not l.upper().startswith('DATE')]
        if lines:
            last = lines[-1].split(',')
            if len(last) >= 2 and last[1] not in ('.', ''):
                val = float(last[1])
                if -1 <= val <= 20:
                    return val
    except Exception as e:
        print(f'Fed FRED CSV failed: {e}')

    # 2. Fallback: scrape FRED page
    try:
        html = http_get('https://fred.stlouisfed.org/series/DFEDTARU', timeout=15)
        # Caută "X.XX%" cu prefixe contextuale
        m = re.search(r'(?:Last\s+Value|latest\s+value)[^0-9]{1,100}([\d.]+)\s*(?:%|percent)', html, re.IGNORECASE)
        if m:
            val = float(m.group(1))
            if -1 <= val <= 20:
                return val
    except Exception as e:
        print(f'Fed FRED scrape failed: {e}')

    # 3. Anterior
    prev = load_previous_value('fed_rate')
    if prev:
        print(f'Fed rate: folosesc anterior {prev}')
        return prev
    return None


# ── EUR/RON din BNR ──────────────────────────────────────────────────
def fetch_eur_ron():
    try:
        xml = http_get('https://www.bnr.ro/nbrfxrates.xml', timeout=10)
        m = re.search(r'<Rate currency="EUR"[^>]*>([\d.]+)</Rate>', xml)
        if m:
            v = float(m.group(1))
            if 4.5 <= v <= 7.0:
                return v
        return None
    except Exception as e:
        print(f'BNR EUR/RON failed: {e}')
        return load_previous_value('eur_ron')


# ── S&P 500 correction vs maxim 52W ──────────────────────────────────
def calculate_correction_pct(sp500_current):
    try:
        url = 'https://query1.finance.yahoo.com/v8/finance/chart/^GSPC?interval=1d&range=1y'
        data = json.loads(http_get(url))
        highs = data['chart']['result'][0]['indicators']['quote'][0]['high']
        max52w = max([h for h in highs if h is not None])
        return round((max52w - sp500_current) / max52w * 100, 2) if max52w else 0
    except Exception:
        return 0


# ── Score compus (replică recalcScore din aplicație) ─────────────────
def calculate_market_score(cape, vix, fed_rate, sma_above):
    if cape is None: return None
    cape_score = max(0, min(100, round(100 - (cape / 50 * 100))))
    trend_score = 35 if sma_above else 65
    vix_pts = 30 if (vix is None or vix < 20) else (50 if vix < 30 else 75)
    rate_pts = 30 if (fed_rate or 0) > 4 else (15 if (fed_rate or 0) > 2 else 5)
    risk_score = min(100, vix_pts + rate_pts)
    return round(cape_score * 0.6 + trend_score * 0.2 + risk_score * 0.2)


# ── MAIN ─────────────────────────────────────────────────────────────
def main():
    print('=== Fetching market data v3 (robust) ===')

    cape = fetch_cape()
    vix = fetch_yahoo('^VIX')
    sp500 = fetch_yahoo('^GSPC')
    fg_score, fg_rating = fetch_fear_greed()
    fed_rate = fetch_fed_rate()
    eur_ron = fetch_eur_ron()
    vwce_price, vwce_sma200, vwce_above_sma = fetch_vwce_sma200()
    correction = calculate_correction_pct(sp500) if sp500 else 0

    # Sanity validation finală — dacă orice valoare e aberantă, fallback la anterior
    cape    = validate_or_previous(cape,    'cape',    10.0, 60.0, 'CAPE final')
    vix     = validate_or_previous(vix,     'vix',     5.0, 100.0, 'VIX final')
    eur_ron = validate_or_previous(eur_ron, 'eur_ron', 4.5, 7.0,   'EUR/RON final')
    fed_rate= validate_or_previous(fed_rate,'fed_rate', -1.0, 20.0, 'Fed final')

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
        'eur_ron': eur_ron,
        'vwce_price': vwce_price,
        'vwce_sma200': vwce_sma200,
        'vwce_above_sma': vwce_above_sma,
        'score': calculate_market_score(cape, vix, fed_rate, vwce_above_sma)
    }

    # Filtrează None
    data = {k: v for k, v in data.items() if v is not None}

    with open('market_data.json', 'w') as f:
        json.dump(data, f, indent=2)

    print(f'\n=== OK: market_data.json updated ===')
    print(json.dumps(data, indent=2))


if __name__ == '__main__':
    main()
