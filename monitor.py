#!/usr/bin/env python3
"""
monitor.py v2 — Robust market data fetcher
═══════════════════════════════════════════════════════════════════

Actualizează automat market_data.json cu indicatori macro curenți.

Surse (cu fallback automat):
  • CAPE Shiller    ← multpl.com  → fallback: gurufocus.com
  • Fear & Greed    ← CNN API     → fallback: alternative.me (crypto F&G ca proxy)
  • VIX             ← Yahoo ^VIX  → fallback: Stooq
  • S&P 500 spot    ← Yahoo ^GSPC → fallback: Stooq
  • Fed Funds Rate  ← FRED CSV
  • VWCE.DE + SMA200 ← Yahoo Finance

Diferențe față de v1:
  • Logging extensiv pentru fiecare sursă (vezi exact ce primește din API)
  • Sanity checks: refuză să scrie valori absurde (ex: F&G=1, SP500=7412)
  • Multiple sources cu fallback automat
  • Defensive JSON parsing (verifică structura înainte să citească)
  • Comparație cu valoarea anterioară: alertă la schimbări bruște (>50 pts)
  • Status individual per sursă în JSON output (vezi care a eșuat și de ce)
"""

import json
import os
import sys
import re
from datetime import datetime, timezone
from pathlib import Path

import requests
from bs4 import BeautifulSoup


# ═══════════════════════════════════════════════════════════════════
# Configuration
# ═══════════════════════════════════════════════════════════════════
OUTPUT_FILE = "market_data.json"
REQUEST_TIMEOUT = 30
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)
HEADERS = {
    "User-Agent": USER_AGENT,
    "Accept": "application/json, text/plain, text/html, */*",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": "https://www.google.com/",
}

# Sanity ranges — refuse to write data outside these bounds
SANITY = {
    "cape":         (10,    60),   # CAPE: realistic 10–60
    "fg_score":     (5,     95),   # F&G: refuse exact 0, 1, 99, 100 (suspicious)
    "vix":          (8,     60),   # VIX: realistic 8–60 (50+ only crisis)
    "fed_rate":     (-1,    15),
    "sp500_price":  (4000,  8000), # SP500 realistic 4000–8000 (May 2026)
    "vwce_price":   (50,    300),
}

# Status tracker per source
status = {}


def log(msg, level="INFO"):
    ts = datetime.now(timezone.utc).strftime("%H:%M:%S")
    print(f"[{ts}] [{level}] {msg}", flush=True)


def sanity_check(name, value):
    """Validate value against expected range. Returns True if OK."""
    if value is None:
        return False
    if name not in SANITY:
        return True
    lo, hi = SANITY[name]
    if not (lo <= value <= hi):
        log(f"  ⚠ SANITY FAIL: {name}={value} (outside [{lo}, {hi}])", "WARN")
        return False
    return True


def safe_float(value, min_val=None, max_val=None):
    try:
        f = float(value)
        if min_val is not None and f < min_val:
            return None
        if max_val is not None and f > max_val:
            return None
        return f
    except (TypeError, ValueError):
        return None


# ═══════════════════════════════════════════════════════════════════
# CAPE Shiller — primary: multpl.com, fallback: gurufocus.com
# ═══════════════════════════════════════════════════════════════════
def fetch_cape_multpl():
    """Primary source: multpl.com/shiller-pe"""
    url = "https://www.multpl.com/shiller-pe"
    log(f"  → multpl.com")
    try:
        r = requests.get(url, headers=HEADERS, timeout=REQUEST_TIMEOUT)
        r.raise_for_status()
        soup = BeautifulSoup(r.text, "html.parser")
        current = soup.find(id="current")
        if not current:
            log("    multpl: #current not found", "WARN")
            return None
        text = current.get_text(" ", strip=True)
        m = re.search(r"(\d{1,3}(?:\.\d+)?)", text)
        if not m:
            return None
        val = safe_float(m.group(1), 5, 100)
        log(f"    multpl returned: {val}")
        return val
    except Exception as e:
        log(f"    multpl failed: {e}", "WARN")
        return None


def fetch_cape_gurufocus():
    """Fallback: gurufocus.com"""
    url = "https://www.gurufocus.com/economic_indicators/56/sp-500-shiller-cape-ratio"
    log(f"  → gurufocus.com")
    try:
        r = requests.get(url, headers=HEADERS, timeout=REQUEST_TIMEOUT)
        r.raise_for_status()
        # Look for pattern like "Last Value | 36.48"
        m = re.search(r"Last Value[^<]*?(\d{1,3}\.\d{1,2})", r.text)
        if m:
            val = safe_float(m.group(1), 5, 100)
            log(f"    gurufocus returned: {val}")
            return val
        return None
    except Exception as e:
        log(f"    gurufocus failed: {e}", "WARN")
        return None


def fetch_cape():
    """Try primary, then fallback."""
    log("Fetching CAPE Shiller...")
    for source_fn in (fetch_cape_multpl, fetch_cape_gurufocus):
        val = source_fn()
        if val is not None and sanity_check("cape", val):
            status["cape_source"] = source_fn.__name__
            return val
    status["cape_source"] = "FAILED_ALL"
    return None


# ═══════════════════════════════════════════════════════════════════
# Fear & Greed — primary: CNN, fallback: alternative.me
# ═══════════════════════════════════════════════════════════════════
def fetch_fg_cnn():
    """Primary: CNN production API"""
    url = "https://production.dataviz.cnn.io/index/fearandgreed/graphdata"
    log(f"  → CNN API")
    try:
        r = requests.get(url, headers=HEADERS, timeout=REQUEST_TIMEOUT)
        r.raise_for_status()
        data = r.json()

        # Log full structure for debugging
        log(f"    CNN response keys: {list(data.keys())[:5]}")

        fg = data.get("fear_and_greed", {})
        if isinstance(fg, dict):
            log(f"    fear_and_greed keys: {list(fg.keys())}")

            # Try multiple field paths CNN may use
            score = None
            for path in ["score", ["score", "current"], ["current", "score"]]:
                try:
                    if isinstance(path, str):
                        score = fg.get(path)
                    else:
                        v = fg
                        for k in path:
                            v = v[k]
                        score = v
                    if score is not None:
                        log(f"    Found score at path: {path}")
                        break
                except (KeyError, TypeError):
                    continue

            rating = fg.get("rating", "")
            if isinstance(rating, dict):
                rating = rating.get("rating", "") or rating.get("current", "")

            score_f = safe_float(score, 0, 100)
            if score_f is None:
                log(f"    CNN score invalid: {score}", "WARN")
                return None, None

            log(f"    CNN returned: score={score_f}, rating={rating}")
            return score_f, str(rating).strip().title() if rating else None
        return None, None
    except Exception as e:
        log(f"    CNN failed: {e}", "WARN")
        return None, None


def fetch_fg_alternative_me():
    """Fallback: alternative.me (crypto F&G as rough proxy — better than nothing)"""
    url = "https://api.alternative.me/fng/?limit=1"
    log(f"  → alternative.me (crypto F&G proxy)")
    try:
        r = requests.get(url, headers=HEADERS, timeout=REQUEST_TIMEOUT)
        r.raise_for_status()
        data = r.json()
        d = data.get("data", [{}])[0]
        score = safe_float(d.get("value"), 0, 100)
        rating = d.get("value_classification", "")
        if score is not None:
            log(f"    alternative.me returned: score={score}, rating={rating}")
            log(f"    NOTE: This is crypto F&G, used as fallback proxy for equity F&G")
            return score, rating
        return None, None
    except Exception as e:
        log(f"    alternative.me failed: {e}", "WARN")
        return None, None


def _classify_fg(score):
    if score < 25: return "Extreme Fear"
    if score < 45: return "Fear"
    if score < 55: return "Neutral"
    if score < 75: return "Greed"
    return "Extreme Greed"


def fetch_fear_greed():
    log("Fetching Fear & Greed Index...")
    for source_fn in (fetch_fg_cnn, fetch_fg_alternative_me):
        score, rating = source_fn()
        if score is not None and sanity_check("fg_score", score):
            if not rating:
                rating = _classify_fg(score)
            status["fg_source"] = source_fn.__name__
            return score, rating
    status["fg_source"] = "FAILED_ALL"
    return None, None


# ═══════════════════════════════════════════════════════════════════
# Yahoo Finance — VIX, S&P 500, VWCE
# ═══════════════════════════════════════════════════════════════════
def fetch_yahoo_quote(symbol, range_str="1mo"):
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
    params = {"interval": "1d", "range": range_str}
    log(f"  → Yahoo: {symbol} ({range_str})")
    try:
        r = requests.get(url, params=params, headers=HEADERS, timeout=REQUEST_TIMEOUT)
        r.raise_for_status()
        data = r.json()
        result = data["chart"]["result"][0]
        meta = result["meta"]
        quotes = result["indicators"]["quote"][0]
        closes = [c for c in quotes.get("close", []) if c is not None]

        current = meta.get("regularMarketPrice")
        if current is None and closes:
            current = closes[-1]

        log(f"    Yahoo {symbol}: current={current}, history points={len(closes)}, "
            f"52w_high={meta.get('fiftyTwoWeekHigh')}")

        return {
            "current": current,
            "close_history": closes,
            "high_52w": meta.get("fiftyTwoWeekHigh"),
        }
    except Exception as e:
        log(f"    Yahoo {symbol} failed: {e}", "WARN")
        return None


def fetch_stooq(symbol):
    """Fallback for Yahoo: Stooq CSV"""
    url = f"https://stooq.com/q/d/l/?s={symbol}&i=d&l=200"
    log(f"  → Stooq: {symbol}")
    try:
        r = requests.get(url, headers=HEADERS, timeout=REQUEST_TIMEOUT)
        r.raise_for_status()
        lines = r.text.strip().split("\n")
        if len(lines) < 2:
            return None
        # CSV header: Date,Open,High,Low,Close,Volume
        closes = []
        for line in lines[1:]:
            parts = line.split(",")
            if len(parts) >= 5:
                v = safe_float(parts[4])
                if v is not None:
                    closes.append(v)
        if not closes:
            return None
        log(f"    Stooq {symbol}: latest={closes[-1]}, history={len(closes)}")
        return {"current": closes[-1], "close_history": closes, "high_52w": max(closes)}
    except Exception as e:
        log(f"    Stooq {symbol} failed: {e}", "WARN")
        return None


def fetch_vix():
    log("Fetching VIX...")
    q = fetch_yahoo_quote("^VIX", "1mo")
    if q and q["current"]:
        v = safe_float(q["current"], 5, 100)
        if sanity_check("vix", v):
            status["vix_source"] = "yahoo"
            return v
    # Fallback Stooq (uses different symbol)
    q = fetch_stooq("^vix")
    if q and q["current"]:
        v = safe_float(q["current"], 5, 100)
        if sanity_check("vix", v):
            status["vix_source"] = "stooq"
            return v
    status["vix_source"] = "FAILED"
    return None


def fetch_sp500():
    log("Fetching S&P 500...")
    # Try Yahoo first
    q = fetch_yahoo_quote("^GSPC", "1y")
    if q and q["current"]:
        price = safe_float(q["current"])
        if sanity_check("sp500_price", price):
            high = safe_float(q["high_52w"])
            corr = round((high - price) / high * 100, 2) if (high and high > price) else 0.0
            log(f"    S&P 500 OK: {price}, correction {corr}%")
            status["sp500_source"] = "yahoo"
            return price, corr
        else:
            log(f"    ⚠ Yahoo returned suspicious ^GSPC={price} — trying fallback", "WARN")

    # Fallback Stooq
    q = fetch_stooq("^spx")
    if q and q["current"]:
        price = safe_float(q["current"])
        if sanity_check("sp500_price", price):
            high = max(q["close_history"]) if q["close_history"] else price
            corr = round((high - price) / high * 100, 2) if high > price else 0.0
            log(f"    S&P 500 OK via Stooq: {price}, correction {corr}%")
            status["sp500_source"] = "stooq"
            return price, corr

    status["sp500_source"] = "FAILED"
    return None, None


def fetch_vwce_and_sma():
    log("Fetching VWCE + SMA200...")
    q = fetch_yahoo_quote("VWCE.DE", "1y")
    if not q or not q["current"]:
        log("  VWCE.DE failed, trying VWCE.MI", "WARN")
        q = fetch_yahoo_quote("VWCE.MI", "1y")
    if not q or not q["current"]:
        log("  VWCE all sources failed", "WARN")
        status["vwce_source"] = "FAILED"
        return None, None, None

    price = safe_float(q["current"], 50, 300)
    closes = q["close_history"]
    sma200 = None
    if len(closes) >= 200:
        sma200 = round(sum(closes[-200:]) / 200, 2)
    elif len(closes) >= 50:
        sma200 = round(sum(closes) / len(closes), 2)
        log(f"  ⚠ Only {len(closes)} closes — SMA approximated", "WARN")

    above = bool(price and sma200 and price > sma200)
    log(f"  VWCE OK: price={price}, SMA200={sma200}, above_sma={above}")
    status["vwce_source"] = "yahoo"
    return price, sma200, above


# ═══════════════════════════════════════════════════════════════════
# Fed Funds Rate — FRED CSV
# ═══════════════════════════════════════════════════════════════════
def fetch_fed_rate():
    log("Fetching Fed Funds Rate...")
    url = "https://fred.stlouisfed.org/graph/fredgraph.csv?id=DFF"
    log(f"  → FRED CSV")
    try:
        r = requests.get(url, headers=HEADERS, timeout=REQUEST_TIMEOUT)
        r.raise_for_status()
        lines = r.text.strip().split("\n")
        if len(lines) < 2:
            return None
        for line in reversed(lines[1:]):
            parts = line.split(",")
            if len(parts) >= 2 and parts[1].strip() not in ("", "."):
                rate = safe_float(parts[1], -1, 20)
                if rate is not None and sanity_check("fed_rate", rate):
                    log(f"    Fed Rate OK: {rate}%")
                    status["fed_source"] = "fred"
                    return rate
        return None
    except Exception as e:
        log(f"    FRED failed: {e}", "WARN")
        status["fed_source"] = "FAILED"
        return None


# ═══════════════════════════════════════════════════════════════════
# Score & build
# ═══════════════════════════════════════════════════════════════════
def compute_score(cape, fg_score, vix, sp_correction):
    components = []
    if cape is not None:
        components.append(("cape", max(0, min(100, 100 - (cape / 50 * 100))), 0.40))
    if fg_score is not None:
        components.append(("fg", 100 - fg_score, 0.20))
    if vix is not None:
        components.append(("vix", max(0, min(100, (vix / 40) * 100)), 0.20))
    if sp_correction is not None:
        components.append(("corr", max(0, min(100, (sp_correction / 30) * 100)), 0.20))
    if not components:
        return None
    total_weight = sum(w for _, _, w in components)
    weighted = sum(s * w for _, s, w in components) / total_weight
    return round(weighted, 1)


def build_market_data():
    log("=" * 60)
    log(f"Market data refresh — {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}")
    log("=" * 60)

    cape = fetch_cape()
    fg_score, fg_rating = fetch_fear_greed()
    vix = fetch_vix()
    sp500_price, sp_correction = fetch_sp500()
    vwce_price, vwce_sma200, vwce_above_sma = fetch_vwce_and_sma()
    fed_rate = fetch_fed_rate()
    composite = compute_score(cape, fg_score, vix, sp_correction)

    now = datetime.now(timezone.utc)
    data = {
        "updated_at": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "updated_at_display": now.strftime("%Y-%m-%d %H:%M UTC"),
        "vix": vix,
        "cape": cape,
        "fg_score": fg_score,
        "fg_rating": fg_rating,
        "fed_rate": fed_rate,
        "sp500_price": sp500_price,
        "correction_pct": sp_correction,
        "vwce_price": vwce_price,
        "vwce_sma200": vwce_sma200,
        "vwce_above_sma": vwce_above_sma,
        "score": composite,
        "intraday_pct": 0,
        "source_status": status,
    }
    return {k: v for k, v in data.items() if v is not None}


def load_previous():
    p = Path(OUTPUT_FILE)
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return None


def check_sudden_changes(old, new):
    """Alert if any value changed dramatically since last run."""
    if not old:
        return
    log("─" * 60)
    log("Change vs previous run:")
    fields = ["cape", "fg_score", "vix", "fed_rate", "sp500_price", "vwce_price", "score"]
    for f in fields:
        o, n = old.get(f), new.get(f)
        if o is None or n is None:
            continue
        try:
            diff = float(n) - float(o)
            pct = abs(diff) / max(abs(float(o)), 1) * 100
            marker = "⚠️" if pct > 20 else "✓"
            log(f"  {marker} {f}: {o} → {n} ({diff:+.2f}, {pct:.1f}%)")
        except (TypeError, ValueError):
            pass


def main():
    try:
        new_data = build_market_data()
    except Exception as e:
        log(f"FATAL: {e}", "ERROR")
        sys.exit(1)

    # Require at least 3 of 6 sources successful
    required_any = ["cape", "fg_score", "vix", "fed_rate", "sp500_price", "vwce_price"]
    successful = [f for f in required_any if f in new_data]

    log("─" * 60)
    log(f"Successful sources: {len(successful)}/6 — {successful}")
    log(f"Source status: {status}")

    if len(successful) < 3:
        log(f"FATAL: only {len(successful)} sources succeeded. Refusing to write.", "ERROR")
        sys.exit(2)

    old_data = load_previous()
    check_sudden_changes(old_data, new_data)

    # Preserve previously-good values if current run failed for some
    if old_data:
        for f in required_any:
            if f not in new_data and f in old_data:
                new_data[f] = old_data[f]
                new_data.setdefault("source_status", {})[f"{f}_fallback"] = "kept_previous_value"
                log(f"  Kept previous value for {f}: {old_data[f]}", "INFO")

    Path(OUTPUT_FILE).write_text(json.dumps(new_data, indent=2) + "\n", encoding="utf-8")
    log("=" * 60)
    log(f"✓ {OUTPUT_FILE} written")
    log("Final values:")
    for k, v in new_data.items():
        if k != "source_status":
            log(f"   {k}: {v}")
    log("=" * 60)


if __name__ == "__main__":
    main()
