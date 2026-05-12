#!/usr/bin/env python3
"""
update_market_data.py
─────────────────────
Actualizează automat market_data.json cu indicatori macro curenți:
  - CAPE Shiller (de pe multpl.com/shillerdata.com)
  - Fear & Greed Index (de pe CNN API)
  - VIX (de pe Yahoo Finance)
  - Fed Funds Rate (de pe FRED St. Louis)
  - S&P 500 spot price (de pe Yahoo Finance)
  - VWCE.DE price + SMA200 (de pe Yahoo Finance)

Output: market_data.json în root-ul repo-ului.

Rulare manuală locală:
    python update_market_data.py

Rulare în GitHub Actions:
    Vezi .github/workflows/daily-update.yml

Dependencies: requests, beautifulsoup4 (instalate de workflow YAML)
"""

import json
import os
import sys
import time
import re
from datetime import datetime, timezone
from pathlib import Path

import requests
from bs4 import BeautifulSoup


# ═══════════════════════════════════════════════════════════════════
# Configuration
# ═══════════════════════════════════════════════════════════════════
OUTPUT_FILE = "market_data.json"
REQUEST_TIMEOUT = 30  # seconds
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)
HEADERS = {"User-Agent": USER_AGENT, "Accept-Language": "en-US,en;q=0.9"}


def log(msg, level="INFO"):
    """Simple logger with timestamps."""
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    print(f"[{ts}] [{level}] {msg}", flush=True)


def safe_float(value, min_val=None, max_val=None):
    """Parse float with range validation. Returns None if invalid."""
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
# Source 1: CAPE Shiller from multpl.com
# ═══════════════════════════════════════════════════════════════════
def fetch_cape():
    """Fetch current CAPE Shiller ratio from multpl.com.

    Returns float (e.g. 36.5) or None on failure.
    """
    url = "https://www.multpl.com/shiller-pe"
    log(f"Fetching CAPE from {url}")
    try:
        r = requests.get(url, headers=HEADERS, timeout=REQUEST_TIMEOUT)
        r.raise_for_status()
        soup = BeautifulSoup(r.text, "html.parser")

        # Page structure: <div id="current"> contains "Current Shiller PE Ratio: 36.48"
        current = soup.find(id="current")
        if not current:
            log("CAPE: #current div not found on multpl.com", "WARN")
            return None

        text = current.get_text(" ", strip=True)
        # Match number with optional decimal (e.g. "36.48")
        m = re.search(r"(\d{1,3}(?:\.\d+)?)", text)
        if not m:
            log(f"CAPE: no number found in '{text}'", "WARN")
            return None

        cape = safe_float(m.group(1), min_val=5, max_val=100)
        if cape is None:
            log(f"CAPE: value out of range: {m.group(1)}", "WARN")
            return None

        log(f"CAPE: {cape}")
        return cape
    except Exception as e:
        log(f"CAPE fetch failed: {e}", "ERROR")
        return None


# ═══════════════════════════════════════════════════════════════════
# Source 2: Fear & Greed Index from CNN
# ═══════════════════════════════════════════════════════════════════
def fetch_fear_greed():
    """Fetch CNN Fear & Greed Index.

    Returns tuple (score: float, rating: str) or (None, None) on failure.
    """
    url = "https://production.dataviz.cnn.io/index/fearandgreed/graphdata"
    log(f"Fetching F&G from {url}")
    try:
        r = requests.get(url, headers=HEADERS, timeout=REQUEST_TIMEOUT)
        r.raise_for_status()
        data = r.json()

        fg = data.get("fear_and_greed", {})
        score = safe_float(fg.get("score"), min_val=0, max_val=100)
        rating = fg.get("rating", "")

        if score is None:
            log("F&G: invalid score from CNN", "WARN")
            return None, None

        # Capitalize rating (e.g. "greed" → "Greed")
        rating_clean = rating.strip().title() if rating else _classify_fg(score)
        log(f"F&G: {score:.1f} ({rating_clean})")
        return score, rating_clean
    except Exception as e:
        log(f"F&G fetch failed: {e}", "ERROR")
        return None, None


def _classify_fg(score):
    """Fallback classification if CNN doesn't return a rating string."""
    if score < 25:
        return "Extreme Fear"
    if score < 45:
        return "Fear"
    if score < 55:
        return "Neutral"
    if score < 75:
        return "Greed"
    return "Extreme Greed"


# ═══════════════════════════════════════════════════════════════════
# Source 3: Yahoo Finance (VIX, S&P 500, VWCE)
# ═══════════════════════════════════════════════════════════════════
def fetch_yahoo_quote(symbol, range_str="1mo"):
    """Fetch Yahoo Finance quote.

    Returns dict with keys: current, close_history (list of last 200 closes).
    """
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
    params = {"interval": "1d", "range": range_str}
    log(f"Fetching Yahoo: {symbol} ({range_str})")
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

        return {
            "current": current,
            "close_history": closes,
            "high_52w": meta.get("fiftyTwoWeekHigh"),
        }
    except Exception as e:
        log(f"Yahoo {symbol} fetch failed: {e}", "ERROR")
        return None


def fetch_vix():
    """Current VIX value."""
    q = fetch_yahoo_quote("^VIX", "1mo")
    if q and q["current"] is not None:
        v = safe_float(q["current"], 5, 100)
        log(f"VIX: {v}")
        return v
    return None


def fetch_sp500():
    """Current S&P 500 spot + 52w high + correction pct."""
    q = fetch_yahoo_quote("^GSPC", "1y")
    if not q or q["current"] is None:
        return None, None
    price = safe_float(q["current"], 100, 20000)
    high = safe_float(q["high_52w"], 100, 20000) if q["high_52w"] else None

    corr = 0.0
    if price and high and high > price:
        corr = round((high - price) / high * 100, 2)

    log(f"S&P 500: {price} | 52w high: {high} | correction: {corr}%")
    return price, corr


def fetch_vwce_and_sma():
    """VWCE.DE current + 200d SMA + above_sma boolean."""
    # Need 250 days to compute 200-day SMA reliably
    q = fetch_yahoo_quote("VWCE.DE", "1y")
    if not q or q["current"] is None:
        log("VWCE: trying VWCE.MI fallback", "WARN")
        q = fetch_yahoo_quote("VWCE.MI", "1y")
    if not q or q["current"] is None:
        return None, None, None

    price = safe_float(q["current"], 10, 1000)
    closes = q["close_history"]

    sma200 = None
    if len(closes) >= 200:
        sma200 = round(sum(closes[-200:]) / 200, 2)
    elif len(closes) >= 50:
        # Fallback: use what we have (warn user)
        sma200 = round(sum(closes) / len(closes), 2)
        log(f"VWCE: only {len(closes)} days available, SMA approximated", "WARN")

    above = bool(price and sma200 and price > sma200)
    log(f"VWCE: {price} | SMA200: {sma200} | above: {above}")
    return price, sma200, above


# ═══════════════════════════════════════════════════════════════════
# Source 4: Fed Funds Rate from FRED
# ═══════════════════════════════════════════════════════════════════
def fetch_fed_rate():
    """Effective Fed Funds Rate from FRED (no API key needed for HTML scrape).

    Series: DFF (daily effective). We scrape the public series page.
    """
    url = "https://fred.stlouisfed.org/graph/fredgraph.csv?id=DFF"
    log(f"Fetching Fed Rate from FRED ({url})")
    try:
        r = requests.get(url, headers=HEADERS, timeout=REQUEST_TIMEOUT)
        r.raise_for_status()
        lines = r.text.strip().split("\n")
        # CSV: header line + data lines "YYYY-MM-DD,value"
        if len(lines) < 2:
            return None
        # Take last non-empty line
        for line in reversed(lines[1:]):
            parts = line.split(",")
            if len(parts) >= 2 and parts[1].strip() not in ("", "."):
                rate = safe_float(parts[1], -1, 20)
                if rate is not None:
                    log(f"Fed Rate: {rate}%")
                    return rate
        return None
    except Exception as e:
        log(f"Fed Rate fetch failed: {e}", "ERROR")
        return None


# ═══════════════════════════════════════════════════════════════════
# Composite score (your app's logic)
# ═══════════════════════════════════════════════════════════════════
def compute_score(cape, fg_score, vix, sp_correction):
    """Compute composite 0-100 market score, mirroring app's logic.

    Higher = more bullish/buy-friendly. Lower = more defensive.
    """
    components = []

    # CAPE: lower is better (capped at 50)
    if cape is not None:
        cape_score = max(0, min(100, 100 - (cape / 50 * 100)))
        components.append(("cape", cape_score, 0.40))

    # F&G: contrarian — high F&G = bearish signal
    if fg_score is not None:
        fg_inverted = 100 - fg_score  # contrarian
        components.append(("fg", fg_inverted, 0.20))

    # VIX: higher = opportunity (fear)
    if vix is not None:
        vix_score = max(0, min(100, (vix / 40) * 100))
        components.append(("vix", vix_score, 0.20))

    # Correction: higher correction = better buying opportunity
    if sp_correction is not None:
        corr_score = max(0, min(100, (sp_correction / 30) * 100))
        components.append(("corr", corr_score, 0.20))

    if not components:
        return None

    total_weight = sum(w for _, _, w in components)
    weighted = sum(s * w for _, s, w in components) / total_weight
    return round(weighted, 1)


# ═══════════════════════════════════════════════════════════════════
# Main orchestrator
# ═══════════════════════════════════════════════════════════════════
def build_market_data():
    """Fetch all sources and build the market_data dict."""
    log("=" * 60)
    log("Starting market data refresh")
    log("=" * 60)

    cape = fetch_cape()
    fg_score, fg_rating = fetch_fear_greed()
    vix = fetch_vix()
    sp500_price, sp_correction = fetch_sp500()
    vwce_price, vwce_sma200, vwce_above_sma = fetch_vwce_and_sma()
    fed_rate = fetch_fed_rate()

    composite_score = compute_score(cape, fg_score, vix, sp_correction)

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
        "score": composite_score,
        "intraday_pct": 0,  # placeholder, computed by app if needed
        "source": "automated GitHub Action",
    }

    # Drop None values from output (cleaner JSON)
    data_clean = {k: v for k, v in data.items() if v is not None}
    return data_clean


def load_previous():
    """Load existing market_data.json if it exists."""
    path = Path(OUTPUT_FILE)
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def has_significant_change(old, new):
    """Check if data changed enough to warrant a commit.

    Avoid trivial commits when only timestamps change.
    """
    if old is None:
        return True

    fields_to_compare = [
        "vix", "cape", "fg_score", "fed_rate",
        "sp500_price", "vwce_price", "score"
    ]
    for f in fields_to_compare:
        old_v = old.get(f)
        new_v = new.get(f)
        if old_v is None or new_v is None:
            if old_v != new_v:
                return True
            continue
        # Numeric comparison with 0.1% tolerance
        try:
            if abs(float(old_v) - float(new_v)) / max(abs(float(old_v)), 1) > 0.001:
                return True
        except (TypeError, ValueError):
            if old_v != new_v:
                return True
    return False


def main():
    """Entry point."""
    try:
        new_data = build_market_data()
    except Exception as e:
        log(f"FATAL: build_market_data failed: {e}", "ERROR")
        sys.exit(1)

    # Validate minimum required fields
    required = ["cape", "fg_score", "vix"]
    missing = [f for f in required if f not in new_data]
    if missing:
        log(f"FATAL: required fields missing: {missing}", "ERROR")
        log("Refusing to write incomplete data — keeping previous file.", "ERROR")
        sys.exit(2)

    # Compare with previous
    old_data = load_previous()
    if not has_significant_change(old_data, new_data):
        log("No significant change since last run — skipping write.")
        # Still write timestamp update so we have a heartbeat
        if old_data:
            old_data["updated_at"] = new_data["updated_at"]
            old_data["updated_at_display"] = new_data["updated_at_display"]
            Path(OUTPUT_FILE).write_text(
                json.dumps(old_data, indent=2) + "\n", encoding="utf-8"
            )
            log(f"Heartbeat written to {OUTPUT_FILE}")
        sys.exit(0)

    # Write new file
    Path(OUTPUT_FILE).write_text(
        json.dumps(new_data, indent=2) + "\n", encoding="utf-8"
    )
    log(f"✓ {OUTPUT_FILE} updated successfully")
    log("Summary:")
    for k, v in new_data.items():
        log(f"   {k}: {v}")


if __name__ == "__main__":
    main()
