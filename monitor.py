# -*- coding: utf-8 -*-
"""
DCA Pasiv Pro — Monitor Cloud
Rulează pe GitHub Actions la fiecare 30 min în timpul ședinței NYSE.
Trimite notificare Telegram dacă:
  1. Scor estimat > 70  (zona de cumpărare)
  2. CAPE Shiller < 20  (piață subevaluată/neutră)
  3. Corecție față de maxim 52W > 15%
  4. Drop intraday > 2%  (moment semnificativ)

Cooldown: max 1 notificare/zi per tip (fișier .last_alert pe Actions cache).
"""

import os, sys, json, time, datetime, requests
from pathlib import Path

# ── Config ─────────────────────────────────────────────────────────────────
TOKEN      = os.environ.get("TELEGRAM_TOKEN", "")
CHAT_ID    = os.environ.get("TELEGRAM_CHAT_ID", "")
CAPE_MANUAL = float(os.environ.get("CAPE_VALUE", "0"))   # fallback manual

THRESHOLDS = {
    "score_buy"       : 70,    # Scor Global > 70 → cumpărare
    "cape_neutral"    : 20,    # CAPE < 20 → subevaluat/neutru
    "correction_pct"  : 15.0,  # corecție față de maxim 52W
    "intraday_drop"   : 2.0,   # drop intraday semnificativ (%)
}

CACHE_FILE = Path("alert_cache.json")   # păstrat între rulări prin Actions cache

# ── Helpers ────────────────────────────────────────────────────────────────

def load_cache():
    if CACHE_FILE.exists():
        try:
            return json.loads(CACHE_FILE.read_text())
        except Exception:
            pass
    return {}

def save_cache(cache):
    CACHE_FILE.write_text(json.dumps(cache, indent=2))

def already_alerted_today(cache, key):
    today = datetime.date.today().isoformat()
    return cache.get(key) == today

def mark_alerted(cache, key):
    cache[key] = datetime.date.today().isoformat()

def send_telegram(msg):
    if not TOKEN or not CHAT_ID:
        print("⚠️  TELEGRAM_TOKEN sau TELEGRAM_CHAT_ID lipsesc — mesaj ignorat.")
        print("─── MESAJ ───")
        print(msg)
        return False
    url = f"https://api.telegram.org/bot{TOKEN}/sendMessage"
    resp = requests.post(url, json={
        "chat_id"    : CHAT_ID,
        "text"       : msg,
        "parse_mode" : "HTML",
    }, timeout=10)
    ok = resp.status_code == 200
    print("Telegram:", "✅ trimis" if ok else f"❌ eroare {resp.status_code}: {resp.text}")
    return ok

# ── Date de piață ───────────────────────────────────────────────────────────

def fetch_market_data():
    """Returnează dict cu CAPE, S&P500 curent/maxim52W/deschidere."""
    data = {
        "cape"        : CAPE_MANUAL or None,
        "sp500_price" : None,
        "sp500_high52": None,
        "sp500_open"  : None,
        "error"       : [],
    }

    # 1. CAPE Shiller — multpl.com
    if not data["cape"]:
        try:
            r = requests.get(
                "https://api.multpl.com/shiller-pe/table/monthly",
                headers={"User-Agent": "Mozilla/5.0"},
                timeout=8,
            )
            if r.status_code == 200:
                # Caută primul număr din răspuns
                import re
                nums = re.findall(r'"value"\s*:\s*"?([\d.]+)"?', r.text)
                if nums:
                    data["cape"] = float(nums[0])
        except Exception as e:
            data["error"].append(f"CAPE fetch: {e}")

    # 2. S&P500 — Yahoo Finance (fără bibliotecă externă)
    try:
        r = requests.get(
            "https://query1.finance.yahoo.com/v8/finance/chart/%5EGSPC"
            "?interval=1d&range=1y",
            headers={
                "User-Agent"  : "Mozilla/5.0",
                "Accept"      : "application/json",
            },
            timeout=10,
        )
        if r.status_code == 200:
            js = r.json()
            meta  = js["chart"]["result"][0]["meta"]
            closes = js["chart"]["result"][0]["indicators"]["quote"][0]["close"]
            closes = [c for c in closes if c is not None]

            data["sp500_price"]  = meta.get("regularMarketPrice") or closes[-1]
            data["sp500_open"]   = meta.get("regularMarketOpen") or closes[-1]
            data["sp500_high52"] = max(closes)
    except Exception as e:
        data["error"].append(f"SP500 fetch: {e}")

    return data

# ── Score estimat ────────────────────────────────────────────────────────────

def estimate_score(cape, correction_pct):
    """
    Estimare simplificată a Scorului Global din DCA Pasiv Pro.
    Folosește CAPE (60%) + Corecție (40% proxy pentru trend+risk).
    """
    # CAPE score (0-100, invers proporțional cu CAPE)
    if cape is None:
        cape_score = 50
    elif cape < 15:
        cape_score = 90
    elif cape < 20:
        cape_score = 75
    elif cape < 25:
        cape_score = 50
    elif cape < 30:
        cape_score = 35
    elif cape < 35:
        cape_score = 20
    else:
        cape_score = 10

    # Corecție score (0-100, cu cât corecția e mai mare cu atât scorul e mai mare)
    if correction_pct is None:
        corr_score = 50
    elif correction_pct >= 40:
        corr_score = 95
    elif correction_pct >= 30:
        corr_score = 85
    elif correction_pct >= 20:
        corr_score = 75
    elif correction_pct >= 15:
        corr_score = 65
    elif correction_pct >= 10:
        corr_score = 55
    elif correction_pct >= 5:
        corr_score = 45
    else:
        corr_score = 30

    return round(cape_score * 0.60 + corr_score * 0.40)

# ── Formatare notificare ─────────────────────────────────────────────────────

CAPE_LABELS = {
    (0,   15): ("🟢 Subevaluat",          "Oportunitate istorică"),
    (15,  20): ("🟢 Neutru",              "Condiții bune de cumpărare"),
    (20,  25): ("🟡 Ușor supraevaluat",   "Cumpărare prudentă"),
    (25,  30): ("🟠 Supraevaluat",        "DCA normal, fără accelerare"),
    (30,  35): ("🔴 Puternic supraevaluat","Precauție"),
    (35, 999): ("🔴 Extrem supraevaluat", "Evită lump sum"),
}

def cape_label(v):
    for (lo, hi), (lbl, sub) in CAPE_LABELS.items():
        if lo <= v < hi:
            return lbl, sub
    return "N/A", ""

def build_message(data, triggered, score, correction_pct, intraday_drop_pct):
    now_ro = datetime.datetime.utcnow() + datetime.timedelta(hours=2)  # EET approx
    ts = now_ro.strftime("%d.%m.%Y %H:%M")

    cape     = data["cape"]
    sp_price = data["sp500_price"]
    sp_high  = data["sp500_high52"]

    cape_lbl, cape_sub = cape_label(cape) if cape else ("N/A", "")

    lines = [
        f"🔔 <b>DCA Pasiv Pro — Alertă</b>",
        f"<i>{ts} (ora României)</i>",
        "",
    ]

    # Condiții îndeplinite
    if triggered:
        lines.append("✅ <b>Condiții îndeplinite:</b>")
        if "score"     in triggered: lines.append(f"  • Scor estimat <b>{score}/100</b> &gt; {THRESHOLDS['score_buy']}")
        if "cape"      in triggered: lines.append(f"  • CAPE <b>{cape:.1f}</b> &lt; {THRESHOLDS['cape_neutral']} → {cape_lbl}")
        if "correction"in triggered: lines.append(f"  • Corecție față de maxim: <b>-{correction_pct:.1f}%</b>")
        if "intraday"  in triggered: lines.append(f"  • Drop intraday: <b>-{intraday_drop_pct:.1f}%</b> ⚡ moment semnificativ")
        lines.append("")

    # Date piață
    lines.append("📊 <b>Date piață:</b>")
    if cape:
        lines.append(f"  CAPE Shiller: <b>{cape:.1f}</b> — {cape_lbl}")
    if sp_price:
        lines.append(f"  S&P500: <b>${sp_price:,.0f}</b>")
    if correction_pct is not None:
        lines.append(f"  Corecție față de maxim 52W: <b>-{correction_pct:.1f}%</b>")
    if intraday_drop_pct and abs(intraday_drop_pct) > 0.1:
        sign = "-" if intraday_drop_pct > 0 else "+"
        lines.append(f"  Mișcare intraday: <b>{sign}{abs(intraday_drop_pct):.1f}%</b>")
    lines.append(f"  Scor estimat: <b>{score}/100</b>")
    lines.append("")

    # Recomandare
    if score >= 70:
        lines.append("💡 <b>Acțiune:</b> Verifică planul DCA — condiții de cumpărare active.")
    elif score >= 50:
        lines.append("💡 <b>Acțiune:</b> DCA lunar normal. Fără accelerare.")
    else:
        lines.append("💡 <b>Acțiune:</b> Piață scumpă. Menține DCA minim.")

    if data["error"]:
        lines.append("")
        lines.append(f"⚠️ <i>Date parțiale: {'; '.join(data['error'])}</i>")

    return "\n".join(lines)

# ── Main ─────────────────────────────────────────────────────────────────────

def main():
    print("=" * 50)
    print(f"DCA Monitor — {datetime.datetime.utcnow().isoformat()} UTC")
    print("=" * 50)

    cache = load_cache()
    data  = fetch_market_data()
    print(f"CAPE: {data['cape']}")
    print(f"S&P500: {data['sp500_price']} | Max52W: {data['sp500_high52']} | Open: {data['sp500_open']}")
    if data["error"]:
        print("Erori:", data["error"])

    # Calcule
    sp_price  = data["sp500_price"]
    sp_high52 = data["sp500_high52"]
    sp_open   = data["sp500_open"]
    cape      = data["cape"]

    correction_pct   = None
    intraday_drop_pct = None

    if sp_price and sp_high52 and sp_high52 > 0:
        correction_pct = (sp_high52 - sp_price) / sp_high52 * 100

    if sp_price and sp_open and sp_open > 0:
        intraday_drop_pct = (sp_open - sp_price) / sp_open * 100  # pozitiv = scădere față de deschidere

    score = estimate_score(cape, correction_pct)

    print(f"Corecție față de max52W: {correction_pct:.2f}%" if correction_pct else "Corecție: N/A")
    print(f"Drop intraday: {intraday_drop_pct:.2f}%" if intraday_drop_pct else "Intraday: N/A")
    print(f"Scor estimat: {score}")

    # Evaluare triggere
    triggered = []

    if score >= THRESHOLDS["score_buy"] and not already_alerted_today(cache, "score"):
        triggered.append("score")

    if cape and cape < THRESHOLDS["cape_neutral"] and not already_alerted_today(cache, "cape"):
        triggered.append("cape")

    if correction_pct and correction_pct >= THRESHOLDS["correction_pct"] and not already_alerted_today(cache, "correction"):
        triggered.append("correction")

    if intraday_drop_pct and intraday_drop_pct >= THRESHOLDS["intraday_drop"] and not already_alerted_today(cache, "intraday"):
        triggered.append("intraday")

    print(f"Triggere active: {triggered if triggered else 'niciunul'}")

    if triggered:
        msg = build_message(data, triggered, score, correction_pct, intraday_drop_pct)
        if send_telegram(msg):
            for t in triggered:
                mark_alerted(cache, t)
            save_cache(cache)
    else:
        print("✓ Nicio condiție îndeplinită azi — fără notificare.")

    print("Done.")

if __name__ == "__main__":
    main()
