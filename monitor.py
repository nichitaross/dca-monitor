# -*- coding: utf-8 -*-
"""
DCA Pasiv Pro — Monitor Cloud v2
Rulează pe GitHub Actions la fiecare 30 min în timpul ședinței NYSE.

CONDIȚII DE ALERTĂ:
  1. Scor estimat > 70  (CAPE 50% + corecție 30% + VIX 10% + F&G 10%)
  2. CAPE Shiller < 20  (piață subevaluată/neutră)
  3. Corecție față de maxim 52W > 15%
  4. Drop intraday > 2%
  5. VIX > 20  (agitație/frică/panică)
  6. Fear & Greed < 35  (frică pe piață)

ÎMBUNĂTĂȚIRI v2:
  ★ VIX — indicele fricii, cel mai predictiv semnal de cumpărare
  ★ Fear & Greed Index (CNN) — sentiment global de piață
  ★ Alerte pe 3 niveluri: 🟡 Atenție / 🟠 Oportunitate / 🔴 Acțiune urgentă
  ★ Filtru de confirmare — semnal trimis doar după 2 verificări consecutive
    (elimină zgomotul intraday și false pozitive)
"""

import os, re, json, datetime, requests, smtplib
from pathlib import Path
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

# ── Config ──────────────────────────────────────────────────────────────────
TOKEN           = os.environ.get("TELEGRAM_TOKEN", "")
CHAT_ID         = os.environ.get("TELEGRAM_CHAT_ID", "")
CAPE_MANUAL     = float(os.environ.get("CAPE_VALUE", "0"))
GMAIL_USER      = os.environ.get("GMAIL_USER", "")
GMAIL_PASSWORD  = os.environ.get("GMAIL_APP_PASSWORD", "")
ANTHROPIC_KEY   = os.environ.get("ANTHROPIC_API_KEY", "")
EMAIL_TO        = os.environ.get("REPORT_EMAIL", "nichitaross@yahoo.com")
MONTHLY_BUDGET  = float(os.environ.get("MONTHLY_BUDGET", "200"))

THRESHOLDS = {
    "score_buy"      : 70,    # Scor > 70
    "cape_neutral"   : 20,    # CAPE < 20
    "correction_pct" : 15.0,  # corecție față de maxim 52W
    "intraday_drop"  : 2.0,   # drop intraday
    "vix_yellow"     : 20,    # VIX 20-30 → agitație
    "vix_orange"     : 30,    # VIX 30-40 → frică
    "vix_red"        : 40,    # VIX > 40  → panică istorică
    "fg_fear"        : 35,    # Fear & Greed < 35 → frică
    "fg_extreme"     : 20,    # Fear & Greed < 20 → frică extremă
}

# Intervalul de confirmare: 75 min (2.5× ciclul de 30 min — toleranță pentru întârzieri GitHub)
CONFIRM_WINDOW_SEC = 75 * 60

CACHE_FILE = Path("alert_cache.json")

# ── Cache ────────────────────────────────────────────────────────────────────

def load_cache():
    if CACHE_FILE.exists():
        try:
            return json.loads(CACHE_FILE.read_text())
        except Exception:
            pass
    return {}

def save_cache(cache):
    CACHE_FILE.write_text(json.dumps(cache, indent=2))

def _today_ro():
    """Data de azi în fusul orar România (UTC+2), nu UTC."""
    return (datetime.datetime.utcnow() + datetime.timedelta(hours=2)).date().isoformat()

def alerted_today(cache, key):
    return cache.get(f"alerted_{key}") == _today_ro()

def mark_alerted(cache, key):
    cache[f"alerted_{key}"] = _today_ro()
    # Șterge pending după ce am alertat
    cache.pop(f"pending_{key}", None)

def is_confirmed(cache, key):
    """
    Filtru de confirmare: returnează True dacă semnalul a fost activ
    și în verificarea anterioară (în fereastra de 45 min).
    La prima declanșare marchează 'pending' și returnează False.
    La a doua declanșare consecutivă returnează True → trimite alertă.
    """
    now_ts = datetime.datetime.utcnow().timestamp()
    pending_key = f"pending_{key}"

    if pending_key in cache:
        elapsed = now_ts - cache[pending_key]
        if elapsed <= CONFIRM_WINDOW_SEC:
            return True   # confirmat — al doilea check consecutiv
        else:
            # Prea mult timp a trecut, resetăm
            del cache[pending_key]

    # Prima declanșare — marcăm pending, nu trimitem încă
    cache[pending_key] = now_ts
    return False

# ── Telegram ─────────────────────────────────────────────────────────────────

def send_telegram(msg):
    if not TOKEN or not CHAT_ID:
        print("⚠️  Secrete Telegram lipsesc — mesaj afișat local:")
        print(msg)
        return False
    url = f"https://api.telegram.org/bot{TOKEN}/sendMessage"
    resp = requests.post(url, json={
        "chat_id"   : CHAT_ID,
        "text"      : msg,
        "parse_mode": "HTML",
    }, timeout=10)
    ok = resp.status_code == 200
    print("Telegram:", "✅ trimis" if ok else f"❌ {resp.status_code}: {resp.text[:200]}")
    return ok

# ── Fetch date de piață ───────────────────────────────────────────────────────

def fetch_yahoo(ticker, range_="1y"):
    """Fetch Yahoo Finance chart data pentru un ticker."""
    url = (f"https://query1.finance.yahoo.com/v8/finance/chart/{ticker}"
           f"?interval=1d&range={range_}")
    r = requests.get(url, headers={"User-Agent": "Mozilla/5.0",
                                   "Accept": "application/json"}, timeout=10)
    r.raise_for_status()
    result = r.json().get("chart", {}).get("result") or []
    if not result:
        raise Exception(f"Yahoo API: result gol pentru {ticker}")
    return result[0]

def fetch_fed_rate():
    """
    Rata dobânzii Fed.
    Sursă 1: FRED CSV (oficial) — timeout scurt ca să nu blocheze
    Sursă 2: Yahoo Finance ^IRX (Bon Trezorerie 13S ≈ rata Fed, același provider ca VWCE)
    """
    # Sursă 1: FRED
    try:
        url = "https://fred.stlouisfed.org/graph/fredgraph.csv?id=DFEDTARU"
        r = requests.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=8)
        if r.status_code == 200:
            for line in reversed(r.text.strip().split("\n")):
                parts = line.split(",")
                raw = parts[1].strip()
                if len(parts) == 2 and raw not in (".", "DFEDTARU", ""):
                    try:
                        val = float(raw)
                        if 0 < val < 30:   # sanity check: rata Fed realistă
                            print(f"Fed Rate (FRED): {val}%")
                            return val
                    except ValueError:
                        continue
    except Exception as e:
        print(f"Fed Rate FRED: {e}")

    # Sursă 2: Yahoo Finance ^IRX (Bon Trezorerie 13S — proxy excelent pentru rata Fed)
    try:
        res = fetch_yahoo("%5EIRX", range_="5d")
        closes = [c for c in res["indicators"]["quote"][0]["close"] if c]
        if closes:
            val = round(closes[-1], 2)
            print(f"Fed Rate (^IRX fallback): {val}%")
            return val
    except Exception as e:
        print(f"Fed Rate ^IRX: {e}")

    return None

def fetch_vwce_sma():
    """Prețul curent VWCE.DE și dacă e deasupra SMA 200d."""
    for ticker in ["VWCE.DE", "VWCE.L"]:
        try:
            res = fetch_yahoo(ticker, range_="1y")
            closes = [c for c in res["indicators"]["quote"][0]["close"] if c]
            if len(closes) < 50:
                continue
            price_raw = res["meta"].get("regularMarketPrice")
            price     = float(price_raw) if price_raw is not None else None
            if not isinstance(price, float) or price <= 0:
                price = float(closes[-1])
            sma200 = sum(closes[-200:]) / min(len(closes), 200)
            return round(price, 2), bool(price > sma200), round(sma200, 2)
        except Exception as e:
            print(f"VWCE({ticker}): {e}")
    return None, None, None

def fetch_market_data():
    data = {
        "cape"           : CAPE_MANUAL or None,
        "sp500_price"    : None,
        "sp500_high52"   : None,
        "sp500_open"     : None,
        "vix"            : None,
        "fg_score"       : None,
        "fg_rating"      : None,
        "fed_rate"       : None,
        "vwce_price"     : None,
        "vwce_sma200"    : None,
        "vwce_above_sma" : None,
        "errors"         : [],
    }

    # 1. CAPE Shiller — scrape pagina HTML multpl.com (API-ul lor e blocat pe GitHub)
    if not data["cape"]:
        for cape_url in [
            "https://www.multpl.com/shiller-pe",
            "https://www.multpl.com/",
        ]:
            try:
                r = requests.get(cape_url,
                    headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                             "AppleWebKit/537.36 (KHTML, like Gecko) "
                             "Chrome/120.0.0.0 Safari/537.36"},
                    timeout=10)
                if r.status_code == 200:
                    # <div id="current">37.00</div>
                    m = re.search(r'id=["\']current["\'][^>]*>\s*([\d.]+)', r.text)
                    if not m:
                        # fallback: primul număr mare (>5) lângă "Shiller"
                        m = re.search(r'Shiller[^>]*>[\s\S]{0,200}?([\d]{2,3}\.\d{1,2})', r.text)
                    if not m:
                        # fallback generic: orice număr între 10 și 60
                        nums = re.findall(r'\b([1-5]\d\.\d{1,2})\b', r.text)
                        if nums:
                            data["cape"] = float(nums[0])
                            break
                    if m:
                        data["cape"] = float(m.group(1))
                        break
            except Exception as e:
                data["errors"].append(f"CAPE({cape_url[-20:]}): {e}")

    # 2. S&P500
    try:
        res = fetch_yahoo("%5EGSPC")
        meta   = res["meta"]
        closes = [c for c in res["indicators"]["quote"][0]["close"] if c]
        data["sp500_price"]  = float(meta["regularMarketPrice"]) if meta.get("regularMarketPrice") else (closes[-1] if closes else None)
        data["sp500_open"]   = float(meta["regularMarketOpen"])  if meta.get("regularMarketOpen")  else (closes[-1] if closes else None)
        # fiftyTwoWeekHigh direct din meta (mai precis decât max(closes[-252:]))
        h52 = meta.get("fiftyTwoWeekHigh")
        data["sp500_high52"] = float(h52) if h52 else (max(closes) if closes else None)
    except Exception as e:
        data["errors"].append(f"SP500: {e}")

    # 3. VIX — indicele fricii
    try:
        res = fetch_yahoo("%5EVIX", range_="5d")
        meta = res["meta"]
        data["vix"] = meta.get("regularMarketPrice")
        if not data["vix"]:
            closes = [c for c in res["indicators"]["quote"][0]["close"] if c]
            data["vix"] = closes[-1] if closes else None
    except Exception as e:
        data["errors"].append(f"VIX: {e}")

    # 4. Fear & Greed Index
    # Sursă primară: CNN (adesea blocat pe servere cloud)
    # Fallback: Alternative.me — index similar, corelat cu sentimentul general
    fg_ok = False
    for fg_url, fg_parser in [
        (
            "https://production.dataviz.cnn.io/index/fearandgreed/graphdata",
            "cnn"
        ),
        (
            "https://api.alternative.me/fng/?limit=1&format=json",
            "altme"
        ),
    ]:
        if fg_ok:
            break
        try:
            headers = {"User-Agent": "Mozilla/5.0"}
            if "cnn" in fg_url:
                headers["Referer"] = "https://edition.cnn.com/"
            r = requests.get(fg_url, headers=headers, timeout=8)
            if r.status_code == 200:
                js = r.json()
                if fg_parser == "cnn":
                    fg = js.get("fear_and_greed", {})
                    score = fg.get("score")
                    if score is not None:
                        data["fg_score"]  = float(score)
                        data["fg_rating"] = fg.get("rating", "")
                        fg_ok = True
                elif fg_parser == "altme":
                    entry = js.get("data", [{}])[0]
                    score = entry.get("value")
                    if score is not None:
                        data["fg_score"]  = float(score)
                        data["fg_rating"] = entry.get("value_classification", "")
                        # Notă: Alternative.me = crypto F&G, corelat cu sentimentul general
                        fg_ok = True
        except Exception as e:
            data["errors"].append(f"F&G({fg_parser}): {e}")

    # 5. Rata dobânzii Fed (FRED CSV — fără API key)
    fed = fetch_fed_rate()
    if fed is not None:
        data["fed_rate"] = fed
        print(f"Fed Rate: {fed}%")
    else:
        data["errors"].append("FedRate: fetch eșuat")

    # 6. VWCE — preț curent și SMA 200d (Xetra primar, LSE fallback)
    vwce_p, vwce_above, vwce_sma = fetch_vwce_sma()
    data["vwce_price"]     = vwce_p
    data["vwce_above_sma"] = vwce_above
    data["vwce_sma200"]    = vwce_sma
    if vwce_p is not None:
        print(f"VWCE: {vwce_p} | SMA200: {vwce_sma} | Deasupra: {vwce_above}")
    else:
        data["errors"].append("VWCE: fetch eșuat")

    return data

# ── Calcule ───────────────────────────────────────────────────────────────────

def estimate_score(cape, correction_pct, vix, fg_score):
    """Scor compozit 0-100: CAPE 50% + corecție 30% + VIX 10% + F&G 10%.
    Type-safe: convertește forțat la float, returnează 50 pentru None/invalid."""
    # Conversie sigură — previne TypeError la comparații
    def _f(v):
        if v is None: return None
        try:    return float(v)
        except: return None
    cape           = _f(cape)
    correction_pct = _f(correction_pct)
    vix            = _f(vix)
    fg_score       = _f(fg_score)

    # CAPE component (0-100)
    if   cape is None : cs = 50
    elif cape < 15    : cs = 90
    elif cape < 20    : cs = 75
    elif cape < 25    : cs = 55
    elif cape < 30    : cs = 35
    elif cape < 35    : cs = 20
    else              : cs = 10

    # Corecție component (0-100)
    if   correction_pct is None  : rs = 45
    elif correction_pct >= 40    : rs = 95
    elif correction_pct >= 30    : rs = 85
    elif correction_pct >= 20    : rs = 75
    elif correction_pct >= 15    : rs = 65
    elif correction_pct >= 10    : rs = 55
    elif correction_pct >= 5     : rs = 45
    else                         : rs = 30

    # VIX component (0-100, VIX mare = oportunitate mai bună)
    if   vix is None : vs = 50
    elif vix >= 50   : vs = 95
    elif vix >= 40   : vs = 85
    elif vix >= 30   : vs = 70
    elif vix >= 20   : vs = 50
    else             : vs = 30

    # Fear & Greed component (0-100, scor mic F&G = frică = oportunitate)
    if   fg_score is None : fs = 50
    elif fg_score <= 10   : fs = 95
    elif fg_score <= 20   : fs = 85
    elif fg_score <= 35   : fs = 70
    elif fg_score <= 50   : fs = 50
    elif fg_score <= 65   : fs = 35
    else                  : fs = 20

    return round(cs * 0.50 + rs * 0.30 + vs * 0.10 + fs * 0.10)

def alert_level(n_triggered, vix, fg_score, correction_pct):
    """
    Determină nivelul alertei pe baza numărului de condiții + severitate.
    Returnează (emoji, label, multiplicator_DCA_recomandat)
    """
    # Condiții de severitate extremă
    extreme = (
        (vix and vix >= THRESHOLDS["vix_red"]) or
        (fg_score and fg_score <= THRESHOLDS["fg_extreme"]) or
        (correction_pct and correction_pct >= 30)
    )
    high = (
        (vix and vix >= THRESHOLDS["vix_orange"]) or
        (fg_score and fg_score <= THRESHOLDS["fg_fear"]) or
        (correction_pct and correction_pct >= 20)
    )

    if n_triggered == 0:
        return "⚪", "HOLD", 0.0
    if extreme or n_triggered >= 3:
        return "🔴", "ACȚIUNE URGENTĂ", 2.0
    elif high or n_triggered >= 2:
        return "🟠", "OPORTUNITATE", 1.5
    else:
        return "🟡", "ATENȚIE", 1.0

# ── Labels ────────────────────────────────────────────────────────────────────

CAPE_LABELS = [
    (0,  15, "🟢 Subevaluat"),
    (15, 20, "🟢 Neutru"),
    (20, 25, "🟡 Ușor supraevaluat"),
    (25, 30, "🟠 Supraevaluat"),
    (30, 35, "🔴 Puternic supraevaluat"),
    (35,999, "🔴 Extrem supraevaluat"),
]
def cape_lbl(v):
    for lo, hi, lbl in CAPE_LABELS:
        if lo <= v < hi: return lbl
    return "N/A"

VIX_LABELS = [
    (0,  20, "😴 Liniște"),
    (20, 30, "😟 Agitație"),
    (30, 40, "😨 Frică"),
    (40, 50, "😱 Panică"),
    (50,999, "💀 Panică istorică"),
]
def vix_lbl(v):
    for lo, hi, lbl in VIX_LABELS:
        if lo <= v < hi: return lbl
    return "N/A"

FG_LABELS = [
    (0,  25, "😱 Frică extremă"),
    (25, 45, "😨 Frică"),
    (45, 55, "😐 Neutru"),
    (55, 75, "😊 Lăcomie"),
    (75,101, "🤑 Lăcomie extremă"),
]
def fg_lbl(v):
    for lo, hi, lbl in FG_LABELS:
        if lo <= v < hi: return lbl
    return "N/A"

# ── Mesaj Telegram ────────────────────────────────────────────────────────────

def build_message(data, triggered, score, correction_pct, intraday_pct,
                  level_emoji, level_label, dca_mult):
    now_ro = datetime.datetime.utcnow() + datetime.timedelta(hours=2)
    ts     = now_ro.strftime("%d.%m.%Y %H:%M")

    cape     = data["cape"]
    vix      = data["vix"]
    fg       = data["fg_score"]
    sp_price = data["sp500_price"]

    lines = [
        f"{level_emoji} <b>DCA Pasiv Pro — {level_label}</b>",
        f"<i>{ts} (ora României) · semnal confirmat</i>",
        "",
    ]

    # Condiții declanșatoare
    lines.append("✅ <b>Semnale active:</b>")
    if "score"      in triggered:
        lines.append(f"  • Scor compozit <b>{score}/100</b> &gt; {THRESHOLDS['score_buy']}")
    if "cape"       in triggered:
        lines.append(f"  • CAPE <b>{cape:.1f}</b> &lt; {THRESHOLDS['cape_neutral']} — {cape_lbl(cape)}")
    if "correction" in triggered:
        lines.append(f"  • Corecție față de maxim: <b>-{correction_pct:.1f}%</b>")
    if "intraday"   in triggered:
        lines.append(f"  • Drop intraday: <b>-{intraday_pct:.1f}%</b> ⚡")
    if "vix"        in triggered:
        lines.append(f"  • VIX <b>{vix:.1f}</b> — {vix_lbl(vix)}")
    if "fg"         in triggered:
        lines.append(f"  • Fear &amp; Greed: <b>{fg:.0f}/100</b> — {fg_lbl(fg)}")
    lines.append("")

    # Tablou de bord
    lines.append("📊 <b>Tablou de bord:</b>")
    if cape:
        lines.append(f"  CAPE Shiller    {cape:.1f}   {cape_lbl(cape)}")
    if vix:
        lines.append(f"  VIX             {vix:.1f}   {vix_lbl(vix)}")
    if fg is not None:
        lines.append(f"  Fear &amp; Greed    {fg:.0f}/100  {fg_lbl(fg)}")
    if sp_price:
        lines.append(f"  S&amp;P500          ${sp_price:,.0f}")
    if correction_pct is not None:
        lines.append(f"  Corecție 52W    -{correction_pct:.1f}%")
    lines.append(f"  Scor compozit   {score}/100")
    lines.append("")

    # Acțiune recomandată
    if dca_mult > 1.0:
        lines.append(f"💡 <b>Acțiune recomandată:</b>")
        lines.append(f"  Multiplică DCA-ul lunar cu <b>×{dca_mult:.1f}</b>")
        if dca_mult >= 2.0:
            lines.append(f"  ⚠️ Condiții rare — consideră și lump sum parțial")
    else:
        lines.append("💡 <b>Acțiune:</b> DCA normal — o singură condiție îndeplinită.")

    if data["errors"]:
        lines.append("")
        lines.append(f"<i>⚠️ Date parțiale: {' · '.join(data['errors'])}</i>")

    return "\n".join(lines)

# ── Raport lunar — alocări ETF ───────────────────────────────────────────────

def get_cape_allocation(cape, vix):
    """
    Replica exactă a logicii din DCA_Pasiv_Pro_v4.html → recalcScore().
    Returnează (w_vwce, w_aggh, w_eqqq, allocation_multiplier, aggh_alert).
    """
    c = cape or 25.0  # fallback neutru dacă CAPE lipsește

    if   c < 15: w = (0.85, 0.10, 0.05); a = 2.0
    elif c < 20: w = (0.80, 0.13, 0.07); a = 1.5
    elif c < 25: w = (0.75, 0.17, 0.08); a = 1.0
    elif c < 30: w = (0.72, 0.18, 0.10); a = 0.7
    elif c < 35: w = (0.70, 0.20, 0.10); a = 0.5
    elif c < 40: w = (0.65, 0.25, 0.10); a = 0.3
    else:        w = (0.60, 0.30, 0.10); a = 0.3

    # VIX > 30 → bonus 20% alocare (capped la 2.0)
    if vix and vix > 30:
        a = min(2.0, a * 1.2)

    aggh_alert = (vix and vix > 30) or (c > 35)
    return w[0], w[1], w[2], round(a, 2), aggh_alert


# ── Raport lunar — narativă AI (Claude Sonnet) ────────────────────────────────

def generate_ai_narrative(data, w_vwce, w_aggh, w_eqqq, alloc, recommended_eur, aggh_alert):
    """Generează un paragraf narativ în română via Claude Sonnet 4."""
    if not ANTHROPIC_KEY:
        return ("Analiza AI nu este disponibilă. Sistemul operează pe reguli built-in CAPE + macro. "
                "Adaugă ANTHROPIC_API_KEY în secretele GitHub pentru a activa narativa automată.")

    cape     = data.get("cape") or 25
    vix      = data.get("vix")
    fg       = data.get("fg_score")
    fed      = data.get("fed_rate")
    sp500    = data.get("sp500_price")
    above    = data.get("vwce_above_sma")
    now_ro   = datetime.datetime.utcnow() + datetime.timedelta(hours=2)
    luna     = now_ro.strftime("%B %Y")

    aggh_note = ""
    if aggh_alert:
        aggh_pct = round(w_aggh * 100) + 10
        aggh_note = f"ALERTĂ: VIX sau CAPE extrem — se recomandă suplimentarea AGGH la ~{aggh_pct}%. "

    prompt = f"""Ești un analist DCA pasiv care scrie rapoarte lunare concise în română.

Date de piață — {luna}:
- CAPE Shiller: {cape:.1f} ({cape_lbl(cape)})
- VIX: {f'{vix:.1f} ({vix_lbl(vix)})' if vix else 'indisponibil'}
- Fear & Greed: {f'{fg:.0f}/100 ({fg_lbl(fg)})' if fg else 'indisponibil'}
- Rata Fed: {f'{fed:.2f}%' if fed else 'indisponibil'}
- S&P 500: {f'${sp500:,.0f}' if sp500 else 'indisponibil'}
- VWCE față de SMA200: {'DEASUPRA — trend bullish' if above else 'SUB — trend slab' if above is not None else 'indisponibil'}

Recomandare calculată:
- Alocare: {round(alloc * 100)}% din buget lunar ({recommended_eur}€)
- Portofoliu: VWCE {round(w_vwce*100)}% · AGGH {round(w_aggh*100)}% · EQQQ {round(w_eqqq*100)}%
{aggh_note}
Scrie EXACT 4 propoziții în română:
1. Evaluarea piețelor — ce spun indicatorii cheie
2. De ce această alocare este justificată acum
3. Principalul risc de urmărit luna viitoare
4. Un sfat practic specific pentru investitorul DCA pasiv

Fii direct și specific. Nu folosi formule generice."""

    try:
        r = requests.post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "x-api-key": ANTHROPIC_KEY,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
            json={
                "model": "claude-sonnet-4-6",
                "max_tokens": 350,
                "messages": [{"role": "user", "content": prompt}],
            },
            timeout=30,
        )
        d = r.json()
        return d["content"][0]["text"].strip()
    except Exception as e:
        print(f"⚠️ Claude API eroare: {e}")
        return (f"Analiza automată indisponibilă temporar ({e}). "
                "Recomandarea de {recommended_eur}€ a fost calculată pe baza regulilor CAPE built-in.")


# ── Raport lunar — template HTML email ───────────────────────────────────────

def build_html_email(data, narrative, w_vwce, w_aggh, w_eqqq, alloc,
                     recommended_eur, aggh_alert, score, correction_pct):
    """Construiește emailul HTML complet pentru raportul lunar."""
    now_ro  = datetime.datetime.utcnow() + datetime.timedelta(hours=2)
    luna_ro = {
        1:"Ianuarie",2:"Februarie",3:"Martie",4:"Aprilie",5:"Mai",6:"Iunie",
        7:"Iulie",8:"August",9:"Septembrie",10:"Octombrie",11:"Noiembrie",12:"Decembrie"
    }.get(now_ro.month, str(now_ro.month))
    data_str = f"{luna_ro} {now_ro.year}"

    cape  = data.get("cape")
    vix   = data.get("vix")
    fg    = data.get("fg_score")
    fed   = data.get("fed_rate")
    sp500 = data.get("sp500_price")
    above = data.get("vwce_above_sma")

    # Culoare recomandare bazată pe alocare
    if alloc >= 1.5:   rec_color="#16a34a"; rec_label="BUY AGRESIV"
    elif alloc >= 0.7: rec_color="#ca8a04"; rec_label="DCA NORMAL"
    else:              rec_color="#dc2626"; rec_label="DEFENSIV"

    # ETF sume individuale
    vwce_eur = round(recommended_eur * w_vwce)
    aggh_eur = round(recommended_eur * w_aggh)
    eqqq_eur = round(recommended_eur * w_eqqq)

    # AGGH alert block
    aggh_block = ""
    if aggh_alert:
        aggh_target = round(w_aggh * 100) + 10
        aggh_block = f"""
        <tr><td style="padding:0 24px 20px;">
          <table width="100%" cellpadding="16" style="background:#7c1d1d; border-radius:8px; border-left:4px solid #ef4444;">
            <tr><td>
              <div style="color:#fca5a5; font-size:13px; font-weight:bold; margin-bottom:6px;">
                ⚠️ ALERTĂ AGGH — Condiții de piață extreme
              </div>
              <div style="color:#fecaca; font-size:13px; line-height:1.5;">
                VIX ridicat sau CAPE extrem detectat. Se recomandă suplimentarea AGGH
                de la {round(w_aggh*100)}% la <strong>~{aggh_target}%</strong> din portofoliu,
                dacă ai capital suplimentar disponibil.
              </div>
            </td></tr>
          </table>
        </td></tr>"""

    # Rând indicator helper
    def ind_row(label, value, note=""):
        return f"""<tr>
          <td style="color:#94a3b8; font-size:13px; padding:8px 0; border-bottom:1px solid #1e3a5f;">{label}</td>
          <td style="color:#e2e8f0; font-size:13px; padding:8px 0; border-bottom:1px solid #1e3a5f; text-align:right; font-weight:bold;">{value}</td>
          <td style="color:#64748b; font-size:12px; padding:8px 0 8px 12px; border-bottom:1px solid #1e3a5f;">{note}</td>
        </tr>"""

    indicators = ""
    if cape:
        indicators += ind_row("CAPE Shiller", f"{cape:.1f}", cape_lbl(cape))
    if vix:
        indicators += ind_row("VIX", f"{vix:.1f}", vix_lbl(vix))
    if fg is not None:
        indicators += ind_row("Fear &amp; Greed", f"{fg:.0f}/100", fg_lbl(fg))
    if fed:
        indicators += ind_row("Rata Fed", f"{fed:.2f}%", "")
    if sp500:
        corr_str = f"-{correction_pct:.1f}% față de maxim 52W" if correction_pct else ""
        indicators += ind_row("S&amp;P 500", f"${sp500:,.0f}", corr_str)
    if above is not None:
        sma_lbl = "✅ Trend bullish" if above else "⚠️ Sub SMA200"
        indicators += ind_row("VWCE vs SMA200", sma_lbl, "")
    indicators += ind_row("Scor compozit", f"{score}/100", "")

    html = f"""<!DOCTYPE html>
<html lang="ro">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>Raport Lunar DCA — {data_str}</title>
</head>
<body style="margin:0; padding:0; background:#0a0f1e; font-family:Arial,Helvetica,sans-serif;">
<table width="100%" cellpadding="0" cellspacing="0" style="background:#0a0f1e; padding:32px 16px;">
<tr><td align="center">
<table width="600" cellpadding="0" cellspacing="0" style="max-width:600px; width:100%;">

  <!-- HEADER -->
  <tr><td style="background:linear-gradient(135deg,#0f3460,#16213e); border-radius:12px 12px 0 0; padding:32px 28px; text-align:center;">
    <div style="font-size:11px; letter-spacing:3px; color:#00c88c; text-transform:uppercase; margin-bottom:8px;">DCA Pasiv Pro</div>
    <div style="font-size:26px; font-weight:bold; color:#ffffff; margin-bottom:4px;">📊 Raport Lunar</div>
    <div style="font-size:15px; color:#8892a4;">{data_str}</div>
  </td></tr>

  <!-- AI NARRATIVE -->
  <tr><td style="background:#111827; padding:24px 28px;">
    <div style="font-size:11px; letter-spacing:2px; color:#00c88c; text-transform:uppercase; margin-bottom:12px;">🤖 Analiză AI — Claude Sonnet 4</div>
    <div style="background:#1e3a5f; border-left:3px solid #00c88c; border-radius:0 6px 6px 0; padding:16px 18px;">
      <p style="color:#e2e8f0; margin:0; line-height:1.7; font-size:14px;">{narrative}</p>
    </div>
  </td></tr>

  <!-- RECOMMENDATION -->
  <tr><td style="background:#111827; padding:0 28px 24px;">
    <table width="100%" cellpadding="0" cellspacing="0" style="background:{rec_color}; border-radius:10px; text-align:center; padding:24px;">
      <tr><td>
        <div style="font-size:44px; font-weight:bold; color:#ffffff; line-height:1;">{recommended_eur}€</div>
        <div style="font-size:13px; color:rgba(255,255,255,0.8); margin-top:6px;">
          recomandare DCA luna aceasta · {round(alloc*100)}% din buget · <strong>{rec_label}</strong>
        </div>
      </td></tr>
    </table>
  </td></tr>

  <!-- ETF ALLOCATION -->
  <tr><td style="background:#111827; padding:0 28px 24px;">
    <div style="font-size:11px; letter-spacing:2px; color:#00c88c; text-transform:uppercase; margin-bottom:12px;">📦 Alocare ETF-uri</div>
    <table width="100%" cellpadding="0" cellspacing="8">
      <tr>
        <td width="33%" style="text-align:center; background:#1e293b; border-radius:8px; padding:16px 8px;">
          <div style="font-size:18px; font-weight:bold; color:#60a5fa;">{vwce_eur}€</div>
          <div style="font-size:12px; color:#94a3b8; margin-top:4px;">VWCE</div>
          <div style="font-size:11px; color:#64748b;">{round(w_vwce*100)}% · Global</div>
        </td>
        <td width="4%"></td>
        <td width="30%" style="text-align:center; background:#1e293b; border-radius:8px; padding:16px 8px;">
          <div style="font-size:18px; font-weight:bold; color:#34d399;">{aggh_eur}€</div>
          <div style="font-size:12px; color:#94a3b8; margin-top:4px;">AGGH</div>
          <div style="font-size:11px; color:#64748b;">{round(w_aggh*100)}% · Obligațiuni</div>
        </td>
        <td width="4%"></td>
        <td width="29%" style="text-align:center; background:#1e293b; border-radius:8px; padding:16px 8px;">
          <div style="font-size:18px; font-weight:bold; color:#a78bfa;">{eqqq_eur}€</div>
          <div style="font-size:12px; color:#94a3b8; margin-top:4px;">EQQQ</div>
          <div style="font-size:11px; color:#64748b;">{round(w_eqqq*100)}% · Tech</div>
        </td>
      </tr>
    </table>
  </td></tr>

  {aggh_block}

  <!-- MARKET DATA -->
  <tr><td style="background:#111827; padding:0 28px 24px;">
    <div style="font-size:11px; letter-spacing:2px; color:#00c88c; text-transform:uppercase; margin-bottom:12px;">📈 Date de piață</div>
    <table width="100%" cellpadding="0" cellspacing="0">
      {indicators}
    </table>
  </td></tr>

  <!-- FOOTER -->
  <tr><td style="background:#0d1117; border-radius:0 0 12px 12px; padding:20px 28px; text-align:center;">
    <div style="font-size:11px; color:#4b5563; line-height:1.6;">
      DCA Pasiv Pro · Raport generat automat pe 14 ale lunii via GitHub Actions<br>
      Datele sunt informative. Aceasta nu constituie consultanță financiară.
    </div>
  </td></tr>

</table>
</td></tr>
</table>
</body>
</html>"""
    return html


# ── Raport lunar — trimitere email via Resend ─────────────────────────────────

def send_monthly_email(data, score, correction_pct):
    """Orchestrează generarea și trimiterea raportului lunar."""
    print("\n📧 RAPORT LUNAR — se pregătește trimiterea...")

    cape = data.get("cape") or 25.0
    vix  = data.get("vix")

    # 1. Calculează alocările
    w_vwce, w_aggh, w_eqqq, alloc, aggh_alert = get_cape_allocation(cape, vix)
    recommended_eur = round(MONTHLY_BUDGET * alloc)
    print(f"   Alocare: {round(alloc*100)}% → {recommended_eur}€")
    print(f"   ETF: VWCE={round(w_vwce*100)}% AGGH={round(w_aggh*100)}% EQQQ={round(w_eqqq*100)}%")
    print(f"   Alertă AGGH: {'DA' if aggh_alert else 'nu'}")

    # 2. Generează narativa AI
    print("   🤖 Se generează narativa AI...")
    narrative = generate_ai_narrative(
        data, w_vwce, w_aggh, w_eqqq, alloc, recommended_eur, aggh_alert)
    print(f"   Narativă: {narrative[:80]}...")

    # 3. Construiește HTML
    html = build_html_email(
        data, narrative, w_vwce, w_aggh, w_eqqq, alloc,
        recommended_eur, aggh_alert, score, correction_pct)

    # 4. Trimite via Gmail SMTP
    now_ro = datetime.datetime.utcnow() + datetime.timedelta(hours=2)
    luna_ro = {
        1:"Ianuarie",2:"Februarie",3:"Martie",4:"Aprilie",5:"Mai",6:"Iunie",
        7:"Iulie",8:"August",9:"Septembrie",10:"Octombrie",11:"Noiembrie",12:"Decembrie"
    }.get(now_ro.month, str(now_ro.month))
    subject = f"📊 Raport Lunar DCA — {luna_ro} {now_ro.year}"

    # Salvează HTML local (backup + debug)
    Path("monthly_report_preview.html").write_text(html, encoding="utf-8")
    print("   📄 Previzualizare salvată în monthly_report_preview.html")

    if not GMAIL_USER or not GMAIL_PASSWORD:
        print("⚠️  GMAIL_USER sau GMAIL_APP_PASSWORD lipsesc — emailul nu a fost trimis.")
        print("   Adaugă secretele în GitHub → Settings → Secrets → Actions.")
        return False

    try:
        msg = MIMEMultipart("alternative")
        msg["Subject"] = subject
        msg["From"]    = f"DCA Pasiv Pro <{GMAIL_USER}>"
        msg["To"]      = EMAIL_TO
        msg.attach(MIMEText(html, "html", "utf-8"))

        with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=20) as server:
            server.login(GMAIL_USER, GMAIL_PASSWORD)
            server.sendmail(GMAIL_USER, EMAIL_TO, msg.as_string())

        print(f"✅ Email trimis cu succes către {EMAIL_TO}")
        return True
    except Exception as e:
        print(f"❌ Eroare Gmail SMTP: {e}")
        return False


# ── Scrie market_data.json (pentru sincronizare cu aplicația HTML) ────────────

def write_market_data(data, correction_pct, intraday_pct, score):
    """Scrie market_data.json în repo — citit de aplicația HTML via GitHub raw."""
    now_ro = datetime.datetime.utcnow() + datetime.timedelta(hours=2)
    payload = {
        "updated_at":         now_ro.isoformat(),
        "updated_at_display": now_ro.strftime("%d.%m.%Y %H:%M") + " ora RO",
        "cape":         data.get("cape"),
        "vix":          data.get("vix"),
        "fg_score":     data.get("fg_score"),
        "fg_rating":    data.get("fg_rating"),
        "sp500_price":  data.get("sp500_price"),
        "sp500_high52": data.get("sp500_high52"),
        "correction_pct": round(correction_pct, 2) if correction_pct is not None else None,
        "intraday_pct":   round(intraday_pct,   2) if intraday_pct   is not None else None,
        "score":          score,
        "fed_rate":       data.get("fed_rate"),
        "vwce_price":     data.get("vwce_price"),
        "vwce_sma200":    data.get("vwce_sma200"),
        "vwce_above_sma": data.get("vwce_above_sma"),
        "errors":         data.get("errors", []),
    }
    Path("market_data.json").write_text(
        json.dumps(payload, indent=2, ensure_ascii=False)
    )
    print("📄 market_data.json actualizat.")

# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    sep = "=" * 52
    print(sep)
    print(f"DCA Monitor v2 — {datetime.datetime.utcnow().isoformat()} UTC")
    print(sep)

    cache = load_cache()
    data  = fetch_market_data()

    # Calcule de bază
    sp_price      = data["sp500_price"]
    sp_high52     = data["sp500_high52"]
    sp_open       = data["sp500_open"]
    cape          = data["cape"]
    vix           = data["vix"]
    fg            = data["fg_score"]

    correction_pct = ((sp_high52 - sp_price) / sp_high52 * 100
                      if sp_price and sp_high52 else None)
    intraday_pct   = ((sp_open - sp_price) / sp_open * 100
                      if sp_price and sp_open else None)

    score = estimate_score(cape, correction_pct, vix, fg)

    # Scrie datele live în market_data.json (pentru HTML sync)
    write_market_data(data, correction_pct, intraday_pct, score)

    # Log
    print(f"CAPE:      {cape}")
    print(f"VIX:       {vix}")
    print(f"F&G:       {fg} ({data['fg_rating']})")
    print(f"SP500:     {sp_price}  |  Max52W: {sp_high52}  |  Open: {sp_open}")
    print(f"Corecție:  {f'{correction_pct:.2f}%' if correction_pct else 'N/A'}")
    print(f"Intraday:  {f'{intraday_pct:.2f}%' if intraday_pct else 'N/A'}")
    print(f"Scor:      {score}/100")
    if data["errors"]:
        print(f"Erori:     {data['errors']}")
    print()

    # ── Evaluare condiții ────────────────────────────────────────────────────
    raw_triggered = []

    if score >= THRESHOLDS["score_buy"]:
        raw_triggered.append("score")
    if cape and cape < THRESHOLDS["cape_neutral"]:
        raw_triggered.append("cape")
    if correction_pct and correction_pct >= THRESHOLDS["correction_pct"]:
        raw_triggered.append("correction")
    if intraday_pct and intraday_pct >= THRESHOLDS["intraday_drop"]:
        raw_triggered.append("intraday")
    if vix and vix >= THRESHOLDS["vix_yellow"]:
        raw_triggered.append("vix")
    if fg is not None and fg <= THRESHOLDS["fg_fear"]:
        raw_triggered.append("fg")

    print(f"Semnale brute:     {raw_triggered if raw_triggered else 'niciunul'}")

    # ── Filtru confirmare ────────────────────────────────────────────────────
    # Semnalele intraday (zgomot) nu necesită confirmare
    NO_CONFIRM = {"intraday"}

    confirmed = []
    for key in raw_triggered:
        if alerted_today(cache, key):
            print(f"  [{key}] deja alertat azi — skip")
            continue
        if key in NO_CONFIRM or is_confirmed(cache, key):
            confirmed.append(key)
            print(f"  [{key}] ✅ confirmat")
        else:
            print(f"  [{key}] ⏳ pending — aștept confirmare la următoarea rulare")

    save_cache(cache)

    print(f"\nSemnale confirmate: {confirmed if confirmed else 'niciunul'}")

    if not confirmed:
        print("✓ Nicio alertă de trimis.")
        return

    # ── Verificare suficiență date ────────────────────────────────────────────
    n_missing = sum(x is None for x in [cape, vix, fg, sp_price])
    if n_missing >= 2:
        print(f"⚠️  Date insuficiente ({n_missing}/4 surse lipsă) — alertă anulată pentru siguranță.")
        return

    # ── Nivel alertă ─────────────────────────────────────────────────────────
    level_emoji, level_label, dca_mult = alert_level(
        len(confirmed), vix, fg, correction_pct)
    print(f"Nivel: {level_emoji} {level_label}  |  DCA ×{dca_mult}")

    # ── Trimite mesaj ────────────────────────────────────────────────────────
    msg = build_message(data, confirmed, score, correction_pct, intraday_pct,
                        level_emoji, level_label, dca_mult)
    if send_telegram(msg):
        for key in confirmed:
            mark_alerted(cache, key)
        save_cache(cache)

    # ── Raport lunar — rulează în fiecare lună pe 14 ─────────────────────────
    now_ro = datetime.datetime.utcnow() + datetime.timedelta(hours=2)
    if now_ro.day == 14:
        print(f"\n📅 Azi e ziua 14 — se generează raportul lunar...")
        send_monthly_email(data, score, correction_pct)
    else:
        print(f"\n📅 Ziua {now_ro.day} — raportul lunar se trimite pe 14.")

    print("Done.")

if __name__ == "__main__":
    main()
