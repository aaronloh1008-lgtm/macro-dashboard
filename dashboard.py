#!/usr/bin/env python3
"""
Macro & Markets Dashboard — free, no-key edition.

Every value comes from a FREE structured data feed (no API key, no AI, no cost):
  • Markets  -> Yahoo Finance
  • US data  -> FRED (US Federal Reserve)
  • Eurozone -> Eurostat (+ DBnomics mirror for unemployment)
  • UK       -> ONS
  • Policy rates (BoJ/BoK/BoE) -> BIS (via DBnomics);  Fed/ECB -> FRED
  • Japan CPI -> IMF (via DBnomics)

Each cell is FRESHNESS-CHECKED. The rule (per the user): if a number is not
current, it is NOT shown — the cell goes blank rather than display a stale or
wrong value. So every cell is one of:
  🟢 live   — fetched and current
  🟠 blank  — lagged / unavailable / no free feed (shown empty, with the reason)

Dates: markets show the quote date; indicators show the release / decision date.
Remarks are COMPUTED from each series (direction, records, streaks, next release).

Usage:  python3 dashboard.py [--open]
Refresh: see com.cgsi.macro-dashboard.plist (every 12h).
"""

import csv, io, json, os, re, ssl, sys, urllib.request, urllib.error, urllib.parse
from datetime import datetime, timezone, date

HERE = os.path.dirname(os.path.abspath(__file__))
OUT_HTML = os.environ.get("DASH_OUT", os.path.join(HERE, "macro_dashboard.html"))
UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko)"
TIMEOUT = int(os.environ.get("DASH_TIMEOUT", "25"))
RETRIES = int(os.environ.get("DASH_RETRIES", "2"))
_SSL = ssl.create_default_context()
from datetime import timedelta
NOW = datetime.now(tz=timezone.utc) + timedelta(hours=8)  # SGT = UTC+8 (timezone-independent)
NOW_ISO = NOW.strftime("%Y-%m-%d %H:%M")
NOW_TS  = NOW.strftime("%Y-%m-%dT%H:%M:%S")  # WebKit-safe ISO (countdown anchor)
TODAY = date.today()

# ------------------------------------------------------------------ http
def _get(url, retries=RETRIES, headers=None, encoding="utf-8"):
    h = {"User-Agent": UA, "Accept": "*/*"}
    if headers: h.update(headers)
    last = None
    for _ in range(retries + 1):
        try:
            req = urllib.request.Request(url, headers=h)
            with urllib.request.urlopen(req, timeout=TIMEOUT, context=_SSL) as r:
                return r.read().decode(encoding, "replace")
        except urllib.error.HTTPError:
            raise
        except Exception as e:
            last = e
    raise last

# ------------------------------------------------------------------ dates
_MON = ["Jan","Feb","Mar","Apr","May","Jun","Jul","Aug","Sep","Oct","Nov","Dec"]
_MON3 = {m.upper(): i+1 for i, m in enumerate(_MON)}
def parse_iso(d):
    try: return datetime.strptime(d[:10], "%Y-%m-%d").date()
    except Exception: return None
def to_iso(p):
    """Normalise a period label from any feed to YYYY-MM-DD (first of period)."""
    if not p: return ""
    p = p.strip()
    if re.match(r"^\d{4}-\d{2}-\d{2}$", p): return p
    if re.match(r"^\d{4}-\d{2}$", p): return p + "-01"
    m = re.match(r"^(\d{4})[- ]?Q([1-4])$", p)
    if m: return f"{m.group(1)}-{(int(m.group(2))-1)*3+1:02d}-01"
    m = re.match(r"^(\d{4})\s+([A-Za-z]{3})", p)
    if m and m.group(2)[:3].upper() in _MON3:
        return f"{m.group(1)}-{_MON3[m.group(2)[:3].upper()]:02d}-01"
    return ""
def month_tag(iso):  p = parse_iso(iso); return _MON[p.month-1] if p else ""
def quarter_tag(iso):p = parse_iso(iso); return f"Q{(p.month-1)//3+1}" if p else ""
def pretty_date(iso):p = parse_iso(iso); return f"{_MON[p.month-1]} {p.day}, {p.year}" if p else ""
def pretty_month(iso):p = parse_iso(iso); return f"{_MON[p.month-1]} {p.year}" if p else ""
def pretty_period(iso, quarterly=False):
    p = parse_iso(iso)
    if not p: return ""
    return f"Q{(p.month-1)//3+1} {p.year}" if quarterly else f"{_MON[p.month-1]} {p.year}"
def age_days(iso):
    p = parse_iso(iso); return (TODAY - p).days if p else 10**9

# ------------------------------------------------------------------ formatters
def money0(v): return f"${v:,.0f}"
def money2(v): return f"${v:,.2f}"
def num2(v):   return f"{v:,.2f}"
def pct1(v):   return f"{v:.1f}%"
def pct2(v):   return f"{v:.2f}%"
def pctc(v):   # comment %: 1dp, but keep a meaningful 2nd dp (e.g. 0.75 / 3.75)
    s = f"{v:.2f}"
    return (f"{v:.1f}%" if s.endswith("0") else s + "%")
def signed(v): return f"{v:+,.0f}"

# ------------------------------------------------------------------ feeds
def yahoo_quote(symbol):
    """(price, iso_date, change_pct_vs_prev_close) or (None, None, None).

    The % change is the current price vs the close of the *previous distinct
    trading day* for that symbol's own market — so US, Asia and Japan each get
    their own session's move. We compute it from the daily candle array rather
    than Yahoo's `chartPreviousClose`, which returns the close at the *start of
    the range window* (days ago), not yesterday — that bug was flipping signs
    (e.g. BTC -2% when it was really +3%, Nikkei +3% when it actually fell)."""
    url = ("https://query1.finance.yahoo.com/v8/finance/chart/"
           + urllib.parse.quote(symbol) + "?interval=1d&range=7d")
    try:
        r = json.loads(_get(url))["chart"]["result"][0]
        m = r["meta"]
        ts = m.get("regularMarketTime")
        iso = datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d") if ts else ""
        price = m.get("regularMarketPrice")
        # Daily closes paired with their UTC calendar date, dropping null bars.
        rows = [(datetime.fromtimestamp(t, tz=timezone.utc).strftime("%Y-%m-%d"), c)
                for t, c in zip(r.get("timestamp", []),
                                r["indicators"]["quote"][0]["close"]) if c is not None]
        chg = None
        if price is not None and rows:
            last_date = rows[-1][0]
            prev = next((c for d, c in reversed(rows) if d < last_date), None)
            if prev is None and len(rows) >= 2:      # all bars same date — fall back
                prev = rows[-2][1]
            if prev:
                chg = round((price/prev - 1)*100, 2)
        if price is not None:
            return float(price), iso, chg
    except Exception as e:
        sys.stderr.write(f"[yahoo {symbol}] {e}\n")
    return None, None, None

def fred_series(series_id, cosd="2015-01-01"):
    """[(iso, value)] from FRED public CSV. `cosd` bounds history so daily
    series (DGS2 etc.) return a small, fast file instead of decades of data."""
    try:
        out = []
        url = f"https://fred.stlouisfed.org/graph/fredgraph.csv?id={series_id}&cosd={cosd}"
        for i, line in enumerate(_get(url).splitlines()):
            if i == 0 or not line.strip(): continue
            d, v = (line.split(",") + [""])[:2]
            v = v.strip()
            if v in (".", "", "NaN"): continue
            try: out.append((d.strip(), float(v)))
            except ValueError: pass
        return out
    except Exception as e:
        sys.stderr.write(f"[fred {series_id}] {e}\n"); return []

def eurostat_series(dataset, params):
    """[(iso, value)] from the Eurostat dissemination API (JSON-stat)."""
    try:
        url = f"https://ec.europa.eu/eurostat/api/dissemination/statistics/1.0/data/{dataset}?format=JSON&{params}"
        d = json.loads(_get(url))
        inv = {v: k for k, v in d["dimension"]["time"]["category"]["index"].items()}
        vals = d.get("value", {})
        out = [(to_iso(inv[int(k)]), v) for k, v in vals.items() if isinstance(v, (int, float))]
        out.sort(); return out
    except Exception as e:
        sys.stderr.write(f"[eurostat {dataset}] {e}\n"); return []

def dbnomics_series(path):
    """[(iso, value)] from DBnomics (provider/dataset/series_code)."""
    try:
        s = json.loads(_get(f"https://api.db.nomics.world/v22/series/{path}?observations=1"))["series"]["docs"][0]
        per, val = s["period"], s["value"]
        return [(to_iso(per[i]), val[i]) for i in range(len(per)) if isinstance(val[i], (int, float))]
    except Exception as e:
        sys.stderr.write(f"[dbnomics {path}] {e}\n"); return []

def ons_series(url):
    """[(iso, value)] from an ONS timeseries .../data endpoint."""
    try:
        d = json.loads(_get(url))
        arr = d.get("months") or d.get("quarters") or []
        out = []
        for x in arr:
            iso = to_iso(x.get("date", ""))
            try: out.append((iso, float(x.get("value"))))
            except (TypeError, ValueError): pass
        out.sort(); return out
    except Exception as e:
        sys.stderr.write(f"[ons] {e}\n"); return []

def bis_series(area):
    """Central-bank policy rate [(iso,value)] from the BIS SDMX API (current, fast)."""
    try:
        url = f"https://stats.bis.org/api/v1/data/WS_CBPOL/M.{area}?lastNObservations=48"
        txt = _get(url, headers={"Accept": "application/vnd.sdmx.data+csv;version=1.0.0"})
        rows = list(csv.reader(io.StringIO(txt)))
        if not rows: return []
        hdr = rows[0]
        ti, vi = hdr.index("TIME_PERIOD"), hdr.index("OBS_VALUE")
        out = []
        for r in rows[1:]:
            try: out.append((to_iso(r[ti]), float(r[vi])))
            except (ValueError, IndexError): pass
        out.sort(); return out
    except Exception as e:
        sys.stderr.write(f"[bis {area}] {e}\n"); return []

# ------------------------------------------------------------------ transforms
def yoy_from_index(series, lag=12):
    # Calendar-aware: match each point to the SAME month `lag` months earlier by
    # date, not by row position. FRED series occasionally drop a month (e.g. a
    # delayed release); a positional lag would then silently compute a 13-month
    # change and overstate YoY. Keying on (year, month) is gap-proof.
    by_ym = {}
    for dt, v in series:
        y, m = int(dt[:4]), int(dt[5:7])
        by_ym[(y, m)] = v
    out = []
    for dt, v in series:
        y, m = int(dt[:4]), int(dt[5:7])
        ty = y - lag // 12
        tm = m - lag % 12
        if tm <= 0: tm += 12; ty -= 1
        base = by_ym.get((ty, tm))
        if base: out.append((dt, round((v/base - 1)*100, 1)))
    return out
def mom_change(series, scale=1000):
    return [(series[i][0], round((series[i][1]-series[i-1][1])*scale)) for i in range(1, len(series))]

# ------------------------------------------------------------------ remark engine
def direction(latest, prev, fmt):
    # The current value is already shown in the chip, so the remark only needs
    # the direction and the prior print: "up from 3.3%" / "down from 1.5%".
    if prev is None or fmt(latest) == fmt(prev): return "unchanged"
    return f"{'up' if latest>prev else 'down'} from {fmt(prev)}"
def streak(vals):
    if not vals: return 0
    last = round(vals[-1],1); n = 1
    for v in reversed(vals[:-1]):
        if round(v,1) == last: n += 1
        else: break
    return n
def _short_month(iso):
    p = parse_iso(iso)
    return f"{_MON[p.month-1]} '{p.year % 100:02d}" if p else ""   # e.g. "Nov '23"
def extreme(series, latest, band_frac=0.12):
    """Flag an extreme only when it's *narratively significant* — i.e. the
    reading has returned to the upper/lower extreme of the whole multi-year
    regime (e.g. inflation back near its cycle peak), not merely a recent local
    high. Significance is judged by level, not by how long ago it last occurred:
    the value must sit within the top/bottom `band_frac` of the full historical
    range. A mid-cycle wiggle earns no remark."""
    prior = series[:-1]
    if len(prior) < 6: return ""
    vals = [v for _, v in prior]
    hi, lo = max(vals), min(vals)
    rng = hi - lo
    if rng <= 0: return ""
    band = band_frac * rng
    if latest >= hi - band:                       # back at cycle-high levels
        j = next((i for i in range(len(prior)-1, -1, -1) if prior[i][1] >= latest-1e-9), None)
        return "multi-year high" if j is None else f"highest since {_short_month(prior[j][0])}"
    if latest <= lo + band:                        # back at cycle-low levels
        k = next((i for i in range(len(prior)-1, -1, -1) if prior[i][1] <= latest+1e-9), None)
        return "multi-year low" if k is None else f"lowest since {_short_month(prior[k][0])}"
    return ""
def target_phrase(v, target):
    if target is None: return ""
    if v > target+0.2: return ""   # above-target is the obvious case — omit
    if v < target-0.2: return f"below {target:.0f}% target"
    return f"near {target:.0f}% target"

# ------------------------------------------------------------------ release calendar
# Every release / decision date is sourced live from Trading Economics — no
# hand-maintained date lists (which drift and silently go wrong). Each key maps
# to a TE page whose calendar table carries the actual past & scheduled dates.
_SCHED_SLUG = {
    "cpi":   "united-states/inflation-cpi",
    "pce":   "united-states/pce-price-index-annual-change",
    "gdp":   "united-states/gdp-growth",
    "jobs":  "united-states/non-farm-payrolls",
    "fomc":  "united-states/interest-rate",
    # ECB dates come from the DEPOSIT-facility page: its calendar lists the next
    # scheduled decisions (Jul/Sep/Oct), whereas the main-refi "interest-rate"
    # page only carries past decisions + speeches, so it never yields a "Next:".
    "ecb":   "euro-area/deposit-interest-rate",
    "boe":   "united-kingdom/interest-rate",
    "boj":   "japan/interest-rate",
    "bok":   "south-korea/interest-rate",
    "ukcpi": "united-kingdom/inflation-cpi",
    "ezgdp": "euro-area/gdp-growth-annual",
}
_CB_KEYS = {"fomc", "ecb", "boe", "boj", "bok"}
# Event-row keyword used to isolate genuine rate-decision rows on a CB page.
# Default is "interest rate decision"; ECB's deposit page labels them differently.
_CB_DECISION_KW = {"ecb": "deposit facility"}
# Rate rows whose decision is spliced from a TE page into the slower BIS value when a
# decision posts before BIS updates. Maps schedule key -> (TE slug, event keyword).
# ECB is special: its release DATES come from the main-refi "interest-rate" page, but
# the value we DISPLAY is the deposit facility rate, so the value is read from the
# deposit-rate page (keyword "deposit facility"). Fed is handled in fed_rate().
_RATE_SPLICE = {
    "boj": ("japan/interest-rate",            "interest rate"),
    "bok": ("south-korea/interest-rate",      "interest rate"),
    "boe": ("united-kingdom/interest-rate",   "interest rate"),
    "ecb": ("euro-area/deposit-interest-rate", "deposit facility"),
}
_sched_cache = {}
def sched_dates(key):
    """Sorted release/decision dates for a key, parsed from its TE calendar.
    Central-bank pages mix in minutes/votes/forums, so those are filtered to
    genuine 'Interest Rate Decision' rows; data pages are release-only already."""
    if key not in _SCHED_SLUG: return []
    if key not in _sched_cache:
        rows = te_calendar(_TE_BASE + _SCHED_SLUG[key])
        if key in _CB_KEYS:
            kw = _CB_DECISION_KW.get(key, "interest rate decision")
            rows = [r for r in rows if kw in (r[6] or "").lower()]
        _sched_cache[key] = sorted({r[0] for r in rows})
    return _sched_cache[key]
def release_info(key):
    """Return (last_released_iso, next_scheduled_iso).
    A date scheduled for TODAY counts as 'last' only once an actual has posted;
    until then it is treated as 'next' so the date column stays on the previous release."""
    if key not in _SCHED_SLUG: return ("", "")
    today_iso = TODAY.isoformat()
    rows = te_calendar(_TE_BASE + _SCHED_SLUG[key])
    if key in _CB_KEYS:
        kw = _CB_DECISION_KW.get(key, "interest rate decision")
        rows = [r for r in rows if kw in (r[6] or "").lower()]
    last = nxt = ""
    for r in rows:
        d, actual = r[0], r[2]
        if d < today_iso:
            if d > last: last = d
        elif d == today_iso:
            if actual:           # released today -> treat as last
                if d > last: last = d
            elif not nxt:        # scheduled today, not yet out -> treat as next
                nxt = d
        elif not nxt:
            nxt = d
    return last, nxt
def next_note(key):
    _, n = release_info(key); return f"Next: {pretty_date(n)}" if n else ""

# ------------------------------------------------------------------ row builders
def amber(label, reason="no free feed"):
    return {"label": label, "disp": "", "tag": "", "date": "", "comment": reason,
            "source": "amber"}

_CG = {}
def coingecko_quote(cg_id):
    """(price, change_24h_pct, iso) for a crypto asset from CoinGecko. Crypto
    trades 24/7 with no daily close, so the meaningful move is the rolling 24-hour
    change — the same number shown on CoinGecko / Google / exchange apps — not a
    'since 00:00 UTC' daily candle delta."""
    if cg_id in _CG: return _CG[cg_id]
    res = (None, None, "")
    try:
        url = ("https://api.coingecko.com/api/v3/simple/price?ids=" + cg_id +
               "&vs_currencies=usd&include_24hr_change=true")
        d = json.loads(_get(url))[cg_id]
        res = (float(d["usd"]), round(float(d["usd_24h_change"]), 2), TODAY.isoformat())
    except Exception as e:
        sys.stderr.write(f"[coingecko {cg_id}] {e}\n")
    _CG[cg_id] = res
    return res

def market(label, symbol, fmt, *, fred_id="", comment="", cg_id=""):
    """Market price/yield from Yahoo (or FRED for a yield), with quote date + daily % change.
    For crypto (cg_id set) the change is the 24h rolling move from CoinGecko."""
    if cg_id:
        p, chg, iso = coingecko_quote(cg_id)
        if p is None:                                  # CoinGecko down → Yahoo fallback
            p, iso, chg = yahoo_quote(symbol)
        if p is None: return amber(label, "source unavailable")
        return {"label": label, "disp": fmt(p), "tag": "", "date": pretty_date(iso),
                "comment": comment, "chg": chg, "chgpct": True, "source": "live"}
    if fred_id:
        s = fred_series(fred_id)
        if s and age_days(s[-1][0]) <= 12:
            chg = round((s[-1][1]-s[-2][1])/s[-2][1]*100, 2) if len(s) >= 2 and s[-2][1] else None
            return {"label": label, "disp": fmt(s[-1][1]), "tag": "", "date": pretty_date(s[-1][0]),
                    "comment": comment, "chg": chg, "source": "live"}
        return amber(label, "source unavailable")
    p, iso, chg = yahoo_quote(symbol)
    if p is None: return amber(label, "source unavailable")
    return {"label": label, "disp": fmt(p), "tag": "", "date": pretty_date(iso),
            "comment": comment, "chg": chg, "chgpct": True, "source": "live"}

def vol_index(label, symbol, *, comment=""):
    """A volatility / risk gauge (VIX, VVIX, SKEW, MOVE) from Yahoo. Shown like a
    market quote but with INVERTED change colour — rising vol = red (risk-off /
    stress), falling vol = green (calm) — since for these gauges 'up' is the
    risk-negative move, not a gain."""
    p, iso, chg = yahoo_quote(symbol)
    if p is None:
        return amber(label, "source unavailable")
    return {"label": label, "disp": num2(p), "tag": "", "date": pretty_date(iso),
            "comment": comment, "chg": chg, "chgpct": True, "invert": True, "source": "live"}

_CNBC = {}
def cnbc_yield(symbol):
    """Latest market-close yield for a CNBC bond symbol -> (latest, prev, iso).
    Uses CNBC's free, key-less JSON quote service. `last` is the yield (e.g.
    '4.532%'); `change` is the day's move, so prev = last - change. This is the
    same number shown on cnbc.com/quotes/<symbol>, i.e. the last market close."""
    if symbol in _CNBC: return _CNBC[symbol]
    res = (None, None, "")
    try:
        url = ("https://quote.cnbc.com/quote-html-webservice/restQuote/symbolType/symbol?"
               f"symbols={symbol}&requestMethod=itv&noform=1&partnerId=2&fund=1"
               "&exthrs=1&output=json&events=1")
        r = json.loads(_get(url, headers={"User-Agent": "Mozilla/5.0"}))
        q = r["FormattedQuoteResult"]["FormattedQuote"][0]
        last = float(str(q["last"]).replace("%", "").replace(",", ""))
        raw_chg = str(q.get("change", "")).strip()
        if raw_chg.upper() == "UNCH":          # CNBC's literal flat-day token
            chg = 0.0
        else:
            try: chg = float(raw_chg.replace("%", "").replace("+", ""))
            except (TypeError, ValueError): chg = None
        prev = round(last - chg, 3) if chg is not None else None
        iso = str(q.get("last_time") or q.get("last_timedate") or "")[:10]
        res = (last, prev, iso)
    except Exception as e:
        sys.stderr.write(f"[cnbc {symbol}] {e}\n")
    _CNBC[symbol] = res
    return res

_UST = None
def treasury_yields():
    """Latest US Treasury par yields {tenor: (latest, prev, iso)} — official daily feed."""
    global _UST
    if _UST is not None: return _UST
    _UST = {}
    for yr in (TODAY.year, TODAY.year - 1):
        try:
            url = ("https://home.treasury.gov/resource-center/data-chart-center/interest-rates/"
                   f"daily-treasury-rates.csv/{yr}/all?type=daily_treasury_yield_curve"
                   f"&field_tdr_date_value={yr}&page&_format=csv")
            rows = list(csv.reader(io.StringIO(_get(url))))
            if len(rows) < 2: continue
            h = rows[0]; di = h.index("Date")
            def col(name):
                i = h.index(name); d = rows[1][di]                 # MM/DD/YYYY, newest first
                iso = f"{d[6:10]}-{d[0:2]}-{d[3:5]}"
                return float(rows[1][i]), (float(rows[2][i]) if len(rows) > 2 else None), iso
            _UST = {"2Y": col("2 Yr"), "10Y": col("10 Yr")}
            break
        except Exception as e:
            sys.stderr.write(f"[treasury {yr}] {e}\n")
    return _UST

def yield_row(label, latest, prev, iso, *, comment="", fresh_days=12):
    """A bond-yield row: shows the level + the yield CHANGE with INVERTED colour
    (yield up = red/bad for the bondholder, yield down = green)."""
    if latest is None or age_days(iso) > fresh_days:
        return amber(label, "source unavailable")
    chg = round(latest - prev, 2) if prev is not None else None
    return {"label": label, "disp": pct2(latest), "tag": "", "date": pretty_date(iso),
            "comment": comment, "chg": chg, "chgpct": False, "invert": True,
            "bps": True, "source": "live"}

_MOF = None
def mof_jgb():
    """Daily JGB yields {tenor: (latest, prev, iso)} from Japan MOF (3dp, Shift-JIS CSV).
    Columns after the date are 1Y,2Y,...,10Y,15Y,... so 2Y=index 2, 10Y=index 10."""
    global _MOF
    if _MOF is not None: return _MOF
    _MOF = {}
    try:
        txt = _get("https://www.mof.go.jp/jgbs/reference/interest_rate/data/jgbcm_all.csv",
                   encoding="shift_jis")
        rows = [r.split(",") for r in txt.splitlines()]
        data = [r for r in rows if r and re.match(r"^[SHR]\d+\.\d+\.\d+", r[0].strip())]
        if len(data) < 2: return _MOF
        def jiso(s):                                   # Japanese era date -> iso
            m = re.match(r"^([SHR])(\d+)\.(\d+)\.(\d+)", s.strip())
            base = {"S": 1925, "H": 1988, "R": 2018}[m.group(1)]   # era 1 = base+1
            return f"{base+int(m.group(2))}-{int(m.group(3)):02d}-{int(m.group(4)):02d}"
        last, prev, iso = data[-1], data[-2], jiso(data[-1][0])
        def col(i):
            try: return (float(last[i]), float(prev[i]), iso)
            except (ValueError, IndexError): return (None, None, "")
        _MOF = {"2Y": col(2), "10Y": col(10)}
    except Exception as e:
        sys.stderr.write(f"[mof] {e}\n")
    return _MOF

def macro(label, fetch, fmt, *, kind="rate", target=None, sched="", quarterly=False,
          editorial="", max_age=0, est_url=""):
    """
    kind: rate | yoy | index_yoy | nfp_change
    Fetched series is freshness-checked; stale/missing -> amber blank.
    """
    try: series = fetch() or []
    except Exception as e:
        sys.stderr.write(f"[{label}] {e}\n"); series = []
    if kind == "index_yoy": series = yoy_from_index(series)
    elif kind == "nfp_change": series = mom_change(series)
    # If the release calendar already carries a newer actual than the value feed
    # (FRED etc. lag the official press release by hours), splice it in so the value,
    # month tag, "up from", estimate and release date all advance together. Auto-heals:
    # once the feed catches up, its own point matches and this becomes a no-op.
    if est_url and series:
        try:
            te_pt = te_latest_actual(est_url)
            if te_pt and te_pt[0] > series[-1][0]:
                series = series + [te_pt]
        except Exception as e:
            sys.stderr.write(f"[{label} te-splice] {e}\n")
    # Policy-rate rows: splice a just-decided rate from the TE decision calendar when
    # it post-dates the BIS value feed, so a hike/cut shows the moment it's decided.
    if kind == "rate" and sched in _RATE_SPLICE and series:
        try:
            slug, kw = _RATE_SPLICE[sched]
            dec = te_decision_actual(_TE_BASE + slug, kw)
            if dec and dec[0] > series[-1][0]:
                series = series + [dec]
        except Exception as e:
            sys.stderr.write(f"[{label} rate-splice] {e}\n")
    if not max_age:
        max_age = 200 if quarterly else 100   # strict: stale data is hidden, not shown

    if not series:
        return amber(label, "source unavailable")
    iso, val = series[-1]
    if age_days(iso) > max_age:
        return amber(label, f"source lagged (last {pretty_month(iso)})")

    facts = []
    if kind == "nfp_change":
        disp = signed(val)
    else:
        disp = fmt(val)
        prev = series[-2][1] if len(series) >= 2 else None
        facts.append(direction(val, prev, pctc))
    if est_url:
        cons = te_consensus(est_url, iso)
        if cons:
            if kind == "nfp_change" and not cons.startswith(("+", "-")): cons = "+" + cons
            facts.append(f"est {cons}")
    if kind in ("yoy", "index_yoy"):
        ex = extreme(series, val)
        if ex: facts.append(ex)
        tp = target_phrase(val, target)
        if tp: facts.append(tp)
    if kind == "rate":
        st = streak([v for _, v in series])
        if st >= 3:
            if "unchanged" in facts: facts.remove("unchanged")   # streak supersedes
            facts.append(f"unchanged {st} mo")

    last_rel, _ = release_info(sched) if sched else ("", "")
    # date column: release/decision date if we have a calendar, else the data's reference period
    date_col = pretty_date(last_rel) if (sched and last_rel) else pretty_period(iso, quarterly)
    nn = next_note(sched) if sched else ""
    if nn: facts.append(nn)
    if editorial: facts.append(editorial)
    tag = (quarter_tag(iso) if quarterly else month_tag(iso)) if sched else ""
    return {"label": label, "disp": disp, "tag": tag, "date": date_col,
            "comment": "; ".join(facts), "source": "live"}

_TE_UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
          "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")

_TE_PAGES = {}
def _te_page(url):
    """Fetch & cache a Trading Economics page (so the same URL used for both the
    headline value and the consensus estimate is only fetched once)."""
    if url not in _TE_PAGES:
        try:
            _TE_PAGES[url] = _get(url, headers={"User-Agent": _TE_UA})
        except Exception as e:
            sys.stderr.write(f"[te {url}] {e}\n"); _TE_PAGES[url] = ""
    return _TE_PAGES[url]

def te_calendar(url):
    """Parse TE's release calendar table into rows:
    (date_iso, period, actual, previous, consensus, forecast, event). Empty cells stay ''."""
    raw = _te_page(url)
    i = raw.find('id="calendar-table"')
    if i < 0: return []
    seg = raw[i:raw.find("</table>", i) + 8]
    out = []
    for tr in re.findall(r"<tr[^>]*>(.*?)</tr>", seg, re.S)[1:]:   # skip header row
        c = [re.sub(r"<[^>]+>", "", x).strip() for x in re.findall(r"<td[^>]*>(.*?)</td>", tr, re.S)]
        # columns: date, GMT, event, period, actual, previous, consensus, forecast
        if len(c) >= 7 and re.match(r"20\d\d-[01]\d-[0-3]\d", c[0]):
            out.append((c[0], c[3], c[4], c[5], c[6], c[7] if len(c) > 7 else "", c[2]))
    return out

def te_consensus(url, ref_iso=""):
    """Market consensus (the estimate) for the release that matches ref_iso's
    month — else the most recent already-released row. Returns TE's own string
    (e.g. '85K', '3.7%') or '' when unavailable."""
    rows = te_calendar(url)
    if not rows: return ""
    today = TODAY.isoformat()
    released = [r for r in rows if r[0] <= today and r[2]]   # has an Actual
    want = ""
    if ref_iso:
        p = parse_iso(ref_iso)
        if p: want = p.strftime("%b")                        # 'Apr', 'May', ...
    for r in reversed(released):                             # newest first
        if not want or r[1] == want:
            return r[4]                                      # consensus cell
    return released[-1][4] if released else ""

def _period_month(period):
    """First calendar month (1-12) of a TE reference-period label:
    'May'->5, 'Q1'->1, 'Q2'->4, 'Q4'->10. None if unrecognised."""
    p = (period or "").strip().upper()
    if len(p) >= 2 and p[0] == "Q" and p[1].isdigit():
        q = int(p[1])
        return (q - 1) * 3 + 1 if 1 <= q <= 4 else None
    return _MON3.get(p[:3])

def te_latest_actual(url):
    """Latest already-released calendar row as (ref_iso, value), or None.
    Used to splice a just-published figure into a slower value feed (FRED/Eurostat
    lag the official press release by hours). ref_iso is the reference period's
    first-of-month (monthly) or first-of-quarter (quarterly 'Q1'/'Q2'…), derived
    from the release date + period label; value is parsed from the Actual cell
    (handles %, K/M suffixes, commas, signs)."""
    rows = te_calendar(url)
    if not rows: return None
    today = TODAY.isoformat()
    released = [r for r in rows if r[0] <= today and r[2]]   # has an Actual
    if not released: return None
    rel_date, period, actual = released[-1][0], released[-1][1], released[-1][2]
    m = re.search(r"-?\d+(?:\.\d+)?", actual.replace(",", ""))
    rd = parse_iso(rel_date)
    mon = _period_month(period)
    if not (m and rd and mon): return None
    val = float(m.group())
    up = actual.upper()
    if "K" in up: val *= 1_000
    elif "M" in up: val *= 1_000_000
    yr = rd.year - (1 if mon > rd.month else 0)
    return (f"{yr}-{mon:02d}-01", val)

def te_decision_actual(url, keyword="interest rate"):
    """Latest already-occurred rate decision as (date_iso, rate), or None. Keyed on
    the decision DATE (rate decisions carry no monthly reference period). Used to
    splice a just-decided policy rate into the slower BIS value feed. `keyword`
    selects the decision rows on the page — ECB's displayed value is the 'deposit
    facility' rate, not the main-refi 'interest rate' row."""
    rows = te_calendar(url)
    if not rows: return None
    today = TODAY.isoformat()
    dec = [r for r in rows if keyword in (r[6] or "").lower()
           and r[0] <= today and r[2]]                       # decided, has an Actual
    if not dec: return None
    date_iso, actual = dec[-1][0], dec[-1][2]
    m = re.search(r"-?\d+(?:\.\d+)?", actual.replace(",", ""))
    return (date_iso, float(m.group())) if m else None

def te_indicator(url):
    """Parse a Trading Economics headline indicator from the page's meta
    description, e.g. 'Inflation Rate in Japan decreased to 1.40 percent in April
    from 1.50 percent in March of 2026.' -> (latest, prev, iso, next_iso). The
    reference month is read from the text; its year is taken from the trailing
    'of YYYY' (rolling back across a Jan/Dec boundary if needed), or inferred
    from today when TE omits it. From the page's calendar table, rel_iso is the
    release date of the current figure (latest date before today) and next_iso is
    the next scheduled release (earliest date on/after today)."""
    raw = _te_page(url)
    if not raw: return (None, None, "", "", "")
    m = re.search(r'name="description" content="(.*?)"', raw)
    if not m: return (None, None, "", "", "")
    desc = m.group(1)
    m1 = re.search(r"(?:increased|decreased|rose|fell|remained unchanged|was|stood) "
                   r"(?:to|at) ([\d.]+) (?:percent|points) in ([A-Za-z]+)", desc)
    if not m1: return (None, None, "", "", "")
    latest = float(m1.group(1))
    lmon = _MON3.get(m1.group(2)[:3].upper())
    if not lmon: return (None, None, "", "", "")
    m2 = re.search(r"from ([\d.]+) (?:percent|points) in ([A-Za-z]+)(?: of (\d{4}))?", desc)
    prev = float(m2.group(1)) if m2 else None
    if m2 and m2.group(3):
        pmon = _MON3.get(m2.group(2)[:3].upper())
        ly = int(m2.group(3)) + (1 if (pmon and pmon > lmon) else 0)
    else:
        ly = TODAY.year - (1 if lmon > TODAY.month else 0)
    # calendar table: current release = latest date with actual (or before today);
    # next = first future date, or today if today's actual hasn't posted yet.
    rel_iso = next_iso = ""
    rows_cal = te_calendar(url)   # already cached via _te_page
    today_iso = TODAY.isoformat()
    past_d = sorted({r[0] for r in rows_cal if r[0] < today_iso or (r[0] == today_iso and r[2])})
    fut_d  = sorted({r[0] for r in rows_cal if r[0] > today_iso or (r[0] == today_iso and not r[2])})
    if past_d: rel_iso  = past_d[-1]
    if fut_d:  next_iso = fut_d[0]
    return (latest, prev, f"{ly}-{lmon:02d}-01", rel_iso, next_iso)

def te_previous(url, ref_iso=""):
    """Previous-period value from the calendar's Previous column — used when the
    meta sentence omits the 'from X%' clause (e.g. 'remained unchanged at …'),
    so direction() never has to *assume* unchanged from a missing prior."""
    rows = te_calendar(url)
    if not rows: return None
    want = parse_iso(ref_iso).strftime("%b") if ref_iso else ""
    released = [r for r in rows if r[2]]
    for r in reversed(released):
        if not want or r[1] == want:
            m = re.search(r"-?\d+(?:\.\d+)?", r[3] or "")
            return float(m.group()) if m else None
    return None

def te_row(label, url, *, target=None, max_age=130):
    """A monthly macro row sourced from Trading Economics (value + reference
    month). Blanked to amber if the latest reported month is older than max_age
    (catches a feed that has stopped updating, while tolerating each series'
    natural release lag — e.g. UK unemployment prints ~3 months behind)."""
    latest, prev, iso, rel_iso, next_iso = te_indicator(url)
    if latest is None or not iso:
        return amber(label, "source unavailable")
    if age_days(iso) > max_age:
        return amber(label, f"source lagged (last {pretty_month(iso)})")
    if prev is None:                       # meta lacked "from X%" → use calendar
        prev = te_previous(url, iso)
    facts = [direction(latest, prev, pctc)]
    cons = te_consensus(url, iso)          # same page, already cached
    if cons: facts.append(f"est {cons}")
    tp = target_phrase(latest, target)
    if tp: facts.append(tp)
    if next_iso: facts.append(f"Next: {pretty_date(next_iso)}")
    # date column = release date of the current figure; tag = reference month
    date_col = pretty_date(rel_iso) if rel_iso else pretty_period(iso)
    return {"label": label, "disp": pct2(latest), "tag": month_tag(iso),
            "date": date_col, "comment": "; ".join(facts), "source": "live"}

def pmi_index(label, url, *, max_age=70):
    """A diffusion-index PMI in points (not a percent) from Trading Economics —
    e.g. US ISM Manufacturing (TE 'business-confidence') or the euro-area PMIs.
    >50 signals expansion, <50 contraction. Two paths:
      • Pages WITH a release-calendar widget (US ISM) → full row with release date,
        consensus and 'Next:'. Today counts as released only once its Actual prints.
      • Pages WITHOUT one (TE's euro-area composite/services/mfg PMI pages have no
        calendar) → fall back to the meta-description sentence for the value +
        reference month; the date column shows that month and there is no 'Next:'."""
    pm = lambda v: f"{v:.1f}"
    def num(s):
        m = re.search(r"-?\d+(?:\.\d+)?", (s or "").replace(",", ""))
        return float(m.group()) if m else None
    rows = te_calendar(url)
    if rows:
        today_iso = TODAY.isoformat()
        released = sorted((r for r in rows if r[0] <= today_iso and r[2]), key=lambda r: r[0])
        upcoming = sorted((r for r in rows if r[0] > today_iso or (r[0] == today_iso and not r[2])),
                          key=lambda r: r[0])
        val = num(released[-1][2]) if released else None
        if val is not None:
            rel_iso, period = released[-1][0], released[-1][1]
            previous, consensus = released[-1][3], released[-1][4]
            rd, mon = parse_iso(rel_iso), _period_month(period)   # tag = reference month, not release month
            refp = f"{rd.year - (1 if mon > rd.month else 0)}-{mon:02d}-01" if (rd and mon) else rel_iso
            if age_days(rel_iso) > max_age:
                return amber(label, f"source lagged (last {pretty_month(refp)})")
            facts = []
            prevv = num(previous)
            if prevv is not None:
                facts.append(direction(val, prevv, pm))
            if consensus.strip():
                facts.append(f"est {consensus.strip()}")
            facts.append("expansion" if val > 50 else "contraction" if val < 50 else "at 50")
            next_iso = upcoming[0][0] if upcoming else ""
            if next_iso:
                facts.append(f"Next: {pretty_date(next_iso)}")
            return {"label": label, "disp": pm(val), "tag": month_tag(refp),
                    "date": pretty_date(rel_iso), "comment": "; ".join(facts), "source": "live"}
    # No calendar widget on this TE page → meta-description fallback (value + month only).
    latest, prev, iso, _r, _n = te_indicator(url)
    if latest is None or not iso:
        return amber(label, "source unavailable")
    if age_days(iso) > max_age:
        return amber(label, f"source lagged (last {pretty_month(iso)})")
    facts = []
    if prev is not None:
        facts.append(direction(latest, prev, pm))
    facts.append("expansion" if latest > 50 else "contraction" if latest < 50 else "at 50")
    # No release calendar on this page → no real release date; show a dash (the
    # reference month already rides in the tag next to the value).
    return {"label": label, "disp": pm(latest), "tag": month_tag(iso),
            "date": "—", "comment": "; ".join(facts), "source": "live"}

def tankan(label, url, *, max_age=200):
    """BoJ Tankan — large-manufacturers business-conditions index, which Trading
    Economics files under Japan's 'business-confidence' slug. Unlike the PMIs it's
    a QUARTERLY diffusion index centered on ZERO (signed: positive = more firms
    judge conditions good than bad), so it has its own builder: signed display, a
    quarter tag, and the quarterly freshness window. Today counts as released only
    once its Actual prints."""
    rows = te_calendar(url)
    if not rows:
        return amber(label, "source unavailable")
    today_iso = TODAY.isoformat()
    released = sorted((r for r in rows if r[0] <= today_iso and r[2]), key=lambda r: r[0])
    upcoming = sorted((r for r in rows if r[0] > today_iso or (r[0] == today_iso and not r[2])),
                      key=lambda r: r[0])
    if not released:
        return amber(label, "source unavailable")
    rel_iso, period, actual, previous, consensus = released[-1][:5]
    def num(s):
        m = re.search(r"-?\d+(?:\.\d+)?", (s or "").replace(",", ""))
        return float(m.group()) if m else None
    val = num(actual)
    if val is None:
        return amber(label, "source unavailable")
    rd, mon = parse_iso(rel_iso), _period_month(period)   # tag = reference quarter, not release month
    refp = f"{rd.year - (1 if mon > rd.month else 0)}-{mon:02d}-01" if (rd and mon) else rel_iso
    if age_days(rel_iso) > max_age:
        return amber(label, f"source lagged (last {pretty_period(refp, quarterly=True)})")
    sg = lambda v: f"{v:+.0f}"
    facts = []
    prevv = num(previous)
    if prevv is not None:
        facts.append(direction(val, prevv, sg))
    cons = num(consensus)
    if cons is not None:
        facts.append(f"est {sg(cons)}")
    next_iso = upcoming[0][0] if upcoming else ""
    if next_iso:
        facts.append(f"Next: {pretty_date(next_iso)}")
    return {"label": label, "disp": sg(val), "tag": quarter_tag(refp),
            "date": pretty_date(rel_iso), "comment": "; ".join(facts), "source": "live"}

def fed_rate():
    """Fed funds TARGET RANGE, reconstructed from the BIS midpoint (the FOMC range
    is always 25bp wide, so midpoint ± 0.125 gives the bounds) — no slow FRED call."""
    last, _ = release_info("fomc")
    b = bis_series("US")
    mid = bis_date = None
    if b and age_days(b[-1][0]) <= 120:
        mid, bis_date = b[-1][1], b[-1][0]
    # Splice a just-decided FOMC rate if it post-dates BIS (TE Actual = upper bound;
    # the range is always 25bp wide, so midpoint = upper − 0.125).
    series = list(b) if b else []
    try:
        dec = te_decision_actual(_TE_BASE + _SCHED_SLUG["fomc"])
        if dec and (bis_date is None or dec[0] > bis_date):
            mid = dec[1] - 0.125
            series = series + [(dec[0], mid)]
    except Exception as e:
        sys.stderr.write(f"[Fed rate-splice] {e}\n")
    if mid is not None:
        facts = []
        st = streak([v for _, v in series])
        if st >= 3:
            facts.append(f"unchanged {st} mo")
        nn = next_note("fomc")
        if nn: facts.append(nn)
        return {"label": "Fed Policy Rate", "disp": f"{mid-0.125:.2f}–{mid+0.125:.2f}%", "tag": "",
                "date": pretty_date(last), "comment": "; ".join(facts), "source": "live"}
    return amber("Fed Policy Rate", "source unavailable")

# ------------------------------------------------------------------ build
_TE_BASE = "https://tradingeconomics.com/"
_TE_USED = [
    "united-states/core-pce-price-index-annual-change",
    "united-states/pce-price-index-annual-change",
    "united-states/core-inflation-rate",
    "united-states/inflation-cpi",
    "united-states/unemployment-rate",
    "united-states/non-farm-payrolls",
    "united-states/business-confidence",  # ISM Manufacturing PMI
    "japan/business-confidence",          # Tankan Large Manufacturers Index
    "japan/inflation-cpi",
    "euro-area/inflation-cpi",
    "euro-area/unemployment-rate",
    "euro-area/composite-pmi",            # EZ Composite PMI (no calendar widget; meta fallback)
    "united-kingdom/inflation-cpi",
    "united-kingdom/unemployment-rate",
    "united-states/gdp-growth",         # release-date calendars + QoQ-ann actual splice
    "euro-area/gdp-growth-annual",       # EZ GDP YoY actual splice
    "united-states/interest-rate",      # central-bank decision calendars
    "euro-area/interest-rate",
    "euro-area/deposit-interest-rate",
    "united-kingdom/interest-rate",
    "japan/interest-rate",
    "south-korea/interest-rate",
]
def _prefetch_te():
    """Warm the TE page cache concurrently so the ~11 indicator/estimate pages
    don't get fetched one-by-one (cuts a ~30s sequential crawl to a few sec)."""
    try:
        from concurrent.futures import ThreadPoolExecutor
        with ThreadPoolExecutor(max_workers=8) as ex:
            list(ex.map(_te_page, [_TE_BASE + s for s in _TE_USED]))
    except Exception as e:
        sys.stderr.write(f"[te prefetch] {e}\n")

def build():
    _prefetch_te()
    return [
        ("Markets", [
            market("Gold",          "GC=F",    money0, comment="COMEX front-month"),
            market("Silver",        "SI=F",    money2, comment="COMEX front-month"),
            market("S&P 500",       "^GSPC",   num2),
            market("Bitcoin",       "BTC-USD", money0, comment="24h"),
            market("Brent Crude",   "BZ=F",    money2),
            yield_row("10Y UST Yield", *cnbc_yield("US10Y")),
            yield_row("2Y UST Yield",  *cnbc_yield("US2Y")),
        ]),
        ("Volatility", [
            vol_index("VIX",  "^VIX",  comment="S&P 500 implied vol (30d)"),
            vol_index("VVIX", "^VVIX", comment="vol of VIX"),
            vol_index("SKEW", "^SKEW", comment="S&P 500 tail risk (100 = none)"),
            vol_index("MOVE", "^MOVE", comment="US Treasury implied vol"),
        ]),
        ("United States", [
            fed_rate(),
            macro("Core PCE (YoY)", lambda: fred_series("PCEPILFE"), pct2, kind="index_yoy", target=2.0, sched="pce", est_url="https://tradingeconomics.com/united-states/core-pce-price-index-annual-change"),
            macro("Headline PCE (YoY)", lambda: fred_series("PCEPI"), pct2, kind="index_yoy", target=2.0, sched="pce", est_url="https://tradingeconomics.com/united-states/pce-price-index-annual-change"),
            macro("Core CPI (YoY)", lambda: fred_series("CPILFENS"), pct2, kind="index_yoy", target=2.0, sched="cpi", est_url="https://tradingeconomics.com/united-states/core-inflation-rate"),
            macro("Headline CPI (YoY)", lambda: fred_series("CPIAUCNS"), pct2, kind="index_yoy", target=2.0, sched="cpi", est_url="https://tradingeconomics.com/united-states/inflation-cpi"),
            macro("Unemployment Rate", lambda: fred_series("UNRATE"), pct2, kind="rate", sched="jobs", est_url="https://tradingeconomics.com/united-states/unemployment-rate"),
            macro("Nonfarm Payrolls (MoM)", lambda: fred_series("PAYEMS"), signed, kind="nfp_change", sched="jobs", est_url="https://tradingeconomics.com/united-states/non-farm-payrolls"),
            macro("Real GDP (QoQ ann.)", lambda: fred_series("A191RL1Q225SBEA"), pct2, kind="yoy", quarterly=True, sched="gdp", est_url="https://tradingeconomics.com/united-states/gdp-growth"),
            pmi_index("ISM Mfg PMI", "https://tradingeconomics.com/united-states/business-confidence"),
        ]),
        ("Japan", [
            macro("BoJ Policy Rate", lambda: bis_series("JP"), pct2, kind="rate", sched="boj"),
            te_row("Inflation (YoY)", "https://tradingeconomics.com/japan/inflation-cpi", target=2.0),
            yield_row("10Y JGB Yield", *cnbc_yield("JP10Y-JP")),
            yield_row("2Y JGB Yield",  *cnbc_yield("JP2Y-JP")),
            tankan("Tankan (Lg Mfg)", "https://tradingeconomics.com/japan/business-confidence"),
        ]),
        ("South Korea", [
            macro("BoK Policy Rate", lambda: bis_series("KR"), pct2, kind="rate", sched="bok"),
        ]),
        ("Eurozone", [
            macro("ECB Policy Rate", lambda: bis_series("XM"), pct2, kind="rate", sched="ecb"),
            te_row("Inflation HICP (YoY)", "https://tradingeconomics.com/euro-area/inflation-cpi", target=2.0),
            te_row("Unemployment Rate", "https://tradingeconomics.com/euro-area/unemployment-rate"),
            macro("Real GDP (YoY)", lambda: eurostat_series("namq_10_gdp", "unit=CLV_PCH_SM&s_adj=SCA&na_item=B1GQ&geo=EA20"), pct2, kind="yoy", quarterly=True, sched="ezgdp", est_url="https://tradingeconomics.com/euro-area/gdp-growth-annual"),
            pmi_index("Composite PMI", "https://tradingeconomics.com/euro-area/composite-pmi"),
        ]),
        ("United Kingdom", [
            macro("BOE Bank Rate", lambda: bis_series("GB"), pct2, kind="rate", sched="boe"),
            macro("Inflation CPI (YoY)", lambda: ons_series("https://www.ons.gov.uk/economy/inflationandpriceindices/timeseries/d7g7/mm23/data"), pct2, kind="yoy", target=2.0, sched="ukcpi", est_url="https://tradingeconomics.com/united-kingdom/inflation-cpi"),
            te_row("Unemployment Rate", "https://tradingeconomics.com/united-kingdom/unemployment-rate"),
        ]),
    ]

# ------------------------------------------------------------------ render
THEMES = {
 "slate": dict(bg="#0f172a", border="#1e293b", text="#e2e8f0", muted="#94a3b8",
   accent="#38bdf8", live="#22c55e", amber="#f59e0b", chip="#1e293b", tag="#64748b",
   down="#ef4444", cmt_amber="#c2870b", nextc="#c084fc"),
 "black": dict(bg="#000000", border="#1b1b1b", text="#f0f0f0", muted="#8a8a8a",
   accent="#ffa028", live="#00d26a", amber="#ffb000", chip="#121212", tag="#777777",
   down="#ff453a", cmt_amber="#d99100", nextc="#c792ea"),
 "light": dict(bg="#f6f7f9", border="#e2e6ec", text="#1a2230", muted="#5c6675",
   accent="#2563eb", live="#15803d", amber="#b45309", chip="#ffffff", tag="#8a93a2",
   down="#dc2626", cmt_amber="#b45309", nextc="#7c3aed"),
 "navy": dict(bg="#0a1929", border="#15324c", text="#e7f1ff", muted="#8aa3bd",
   accent="#34d2ff", live="#2ee6a0", amber="#f0b429", chip="#0f2336", tag="#6f8aa6",
   down="#ff6b6b", cmt_amber="#d9a520", nextc="#c792ea"),
 "github": dict(bg="#0d1117", border="#21262d", text="#e6edf3", muted="#8b949e",
   accent="#58a6ff", live="#3fb950", amber="#d29922", chip="#1f2630", tag="#7d8590",
   down="#f85149", cmt_amber="#a87b1f", nextc="#bc8cff"),
}
def root_css(theme):
    t = THEMES.get(theme, THEMES["slate"])
    return ":root{" + "".join(f"--{k.replace('_','-')}:{v};" for k, v in t.items()) + "}"

CSS = """
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--text);-webkit-font-smoothing:antialiased;
font:14px/1.4 ui-sans-serif,-apple-system,BlinkMacSystemFont,"SF Pro Text",Segoe UI,Roboto,sans-serif;
letter-spacing:.1px;}
.wrap{max-width:1000px;margin:0 auto;padding:0 22px 56px;}
header{display:flex;align-items:center;justify-content:space-between;gap:14px;
margin:0 -22px 18px;padding:15px 22px 14px;
border-top:2px solid var(--accent);border-bottom:1px solid var(--border);}
h1{font-size:18px;margin:0;font-weight:600;letter-spacing:.4px;text-transform:uppercase;
display:flex;align-items:center;gap:11px;white-space:nowrap;}
h1::before{content:"";width:9px;height:17px;background:var(--accent);border-radius:1px;flex:none;}
.clock{font-family:ui-monospace,"SF Mono",Menlo,monospace;font-size:12px;text-align:right;line-height:1.7;}
.clock .live{display:inline-block;background:var(--live);color:#04130a;padding:2px 7px;border-radius:3px;
font-weight:700;font-size:10px;letter-spacing:.6px;vertical-align:middle;animation:livepulse 2.4s ease-in-out infinite;}
#clk{color:var(--text);font-variant-numeric:tabular-nums;}
.built{margin-top:3px;color:var(--muted);font-size:11px;}
@keyframes livepulse{0%,100%{opacity:1}50%{opacity:.6}}
.legend{color:var(--muted);font-size:11px;margin:2px 0 22px;
font-family:ui-monospace,"SF Mono",Menlo,monospace;letter-spacing:.3px;}
.dot{display:inline-block;width:7px;height:7px;border-radius:50%;margin:0 5px 0 0;vertical-align:middle;}
.dot.live{background:var(--live);} .dot.amber{background:var(--amber);}
.legend .dot{margin-left:16px;}
section{margin-bottom:22px;}
h2{font:600 11px/1 ui-monospace,"SF Mono",Menlo,monospace;text-transform:uppercase;letter-spacing:1.8px;
color:var(--accent);margin:0 0 4px;padding:0 0 7px;border-bottom:1px solid var(--border);}
table{width:100%;border-collapse:collapse;table-layout:fixed;}
td{padding:7px 12px 7px 0;border-bottom:1px solid var(--border);vertical-align:baseline;overflow:hidden;text-overflow:ellipsis;}
tr:last-child td{border-bottom:none;}
tr:nth-child(even) td{background:rgba(255,255,255,.016);}
.label{width:27%;white-space:nowrap;}
.val{width:16%;text-align:right;font-variant-numeric:tabular-nums;white-space:nowrap;}
.chip{font-family:ui-monospace,"SF Mono",Menlo,monospace;font-size:14px;font-weight:600;color:var(--text);letter-spacing:.2px;}
.tag{color:var(--tag);font-size:11px;margin-left:6px;font-family:ui-monospace,"SF Mono",Menlo,monospace;}
.date{width:14%;color:var(--muted);font-size:12px;white-space:nowrap;font-family:ui-monospace,"SF Mono",Menlo,monospace;}
.comment{color:var(--muted);font-size:12.5px;line-height:1.35;padding-left:2px;}
.amber .comment{color:var(--cmt-amber);font-style:italic;}
.up{color:var(--live);font-variant-numeric:tabular-nums;font-weight:600;} .down{color:var(--down);font-variant-numeric:tabular-nums;font-weight:600;}
.next{color:var(--nextc);font-weight:600;}
@media(max-width:640px){.wrap{padding:0 16px 48px;}.comment{font-size:11.5px}.label{width:38%}.date{display:none}}
"""
CLOCK_JS = """<script>
// Live clock
function t(){document.getElementById('clk').textContent=
new Date().toLocaleTimeString(undefined,{hour:'2-digit',minute:'2-digit',second:'2-digit'});}
t();setInterval(t,1000);
// Freshness indicator — two honest modes:
//  • Übersicht widget (inFrame): the widget rebuilds locally on a fixed 5-min
//    clock (refreshFrequency), so a real countdown is accurate there. Holds at
//    "refreshing…" when done; Übersicht swaps the iframe itself.
//  • Web / PWA (not inFrame): NO countdown — a countdown can only guess when
//    GitHub will finish publishing. Instead we poll the server every 15s (and
//    immediately when the app returns to the foreground) and reload THE MOMENT
//    a newer build exists. The reload is caused by the data landing, so it is
//    synced by construction. The label shows true data age: "live — updated Xs ago".
(function(){
  var builtEl=document.getElementById('built-ts');
  var el=document.getElementById('countdown');
  var label=document.getElementById('cd-label');
  var inFrame=window.self!==window.top;
  var myTs=builtEl.dataset.ts;
  var built=new Date(myTs);
  if(inFrame){
    var INTERVAL=300;   // matches the widget's 5-min refreshFrequency
    function fmt(rem){var m=Math.floor(rem/60),s=rem%60;return m+':'+(s<10?'0':'')+s;}
    function cd(){
      var rem=INTERVAL-Math.floor((Date.now()-built.getTime())/1000);
      el.textContent=rem<=0?'refreshing…':fmt(rem);
    }
    cd();setInterval(cd,1000);
    return;
  }
  // Web/PWA: show the data's true age, ticking every second.
  if(label){label.textContent='live —';}
  function age(){
    var s=Math.floor((Date.now()-built.getTime())/1000);
    if(s<0)s=0;
    el.textContent='updated '+(s<60?s+'s':Math.floor(s/60)+'m '+(s%60)+'s')+' ago';
  }
  age();setInterval(age,1000);
  // Poll for a newer build; reload the instant one is published. busy-guard
  // prevents overlapping fetches (e.g. interval tick + visibility wake).
  var busy=false;
  function check(){
    if(busy) return;
    busy=true;
    fetch(location.pathname+'?_='+Date.now(),{cache:'no-store'})
      .then(function(r){return r.text();})
      .then(function(html){
        var m=html.match(/id="built-ts"[^>]*data-ts="([^"]+)"/);
        if(m && m[1]!==myTs){ location.reload(); return; }
        busy=false;
      })
      .catch(function(){ busy=false; });
  }
  setInterval(check,15000);
  // Coming back to the PWA after a while? Check right away, don't wait 15s.
  document.addEventListener('visibilitychange',function(){ if(!document.hidden) check(); });
})();
</script>"""

def render(sections, theme="slate"):
    parts = []
    live = total = 0
    for title, rows in sections:
        parts.append(f'<section><h2>{title}</h2><table>')
        for r in rows:
            total += 1
            amberrow = r["source"] != "live"
            if not amberrow: live += 1
            dot = r["source"]
            chip = f'<span class="chip">{r["disp"]}</span>' if r["disp"] else '<span class="chip">—</span>'
            tag = f'<span class="tag">{r["tag"]}</span>' if r.get("tag") else ""
            cbits = []
            chg = r.get("chg")
            if chg is not None:
                # A genuinely flat day reads as "unch", not "▲ 0 bps" / "▲ 0.00".
                if r.get("bps"):
                    flat = round(abs(chg) * 100) == 0
                    body = f"{abs(chg)*100:.0f} bps"     # yield move in basis points
                else:
                    flat = round(abs(chg), 2) == 0
                    body = f"{abs(chg):.2f}" + ("%" if r.get("chgpct") else "")
                if flat:
                    cbits.append(f'<span class="tag">unch</span>')
                else:
                    rising = chg >= 0
                    good = rising != bool(r.get("invert"))   # price up = good; yield up = bad
                    cbits.append(f'<span class="{"up" if good else "down"}">'
                                 f'{"▲" if rising else "▼"} {body}</span>')
            if r.get("comment"):
                # bold the upcoming-release date, e.g. "Next: Jun 18, 2026"
                cbits.append(re.sub(r"Next: ([A-Z][a-z]{2} \d{1,2}, \d{4})",
                                    r'<span class="next">Next: \1</span>', r["comment"]))
            parts.append(
                f'<tr class="{"amber" if amberrow else ""}">'
                f'<td class="label"><span class="dot {dot}"></span>{r["label"]}</td>'
                f'<td class="val">{chip}{tag}</td>'
                f'<td class="date">{r.get("date","")}</td>'
                f'<td class="comment">{" ".join(cbits)}</td></tr>'
            )
        parts.append('</table></section>')
    return f"""<!doctype html><html lang="en"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<meta http-equiv="refresh" content="900"><title>Macro &amp; Markets Dashboard</title>
<link rel="manifest" href="manifest.webmanifest">
<meta name="theme-color" content="#000000">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-status-bar-style" content="black-translucent">
<meta name="apple-mobile-web-app-title" content="Macro">
<link rel="apple-touch-icon" href="icon-192.png">
<link rel="icon" type="image/png" href="icon-192.png">
<style>{root_css(theme)}{CSS}</style></head>
<body><div class="wrap">
<header><h1>Macro &amp; Markets Dashboard</h1>
<div class="clock"><span class="live">●&nbsp;LIVE</span>&nbsp;<span id="clk">--:--:--</span>
<div class="built" id="built-ts" data-ts="{NOW_TS}">data as of {NOW_ISO} &nbsp;·&nbsp; <span id="cd-label">next refresh</span> <span id="countdown">5:00</span></div></div></header>
<div class="legend"><span class="dot live"></span>live
<span class="dot amber"></span>manual input / verify data</div>
{''.join(parts)}
</div>{CLOCK_JS}</body></html>"""

def main():
    sections = build()
    theme = os.environ.get("DASH_THEME", "black")
    with open(OUT_HTML, "w") as f:
        f.write(render(sections, theme))
    # terminal readout — verify the real values YOUR feeds returned (no browser needed)
    print(f"\n  Macro & Markets — extracted {NOW_ISO}\n  " + "-" * 52)
    for title, rows in sections:
        print(f"  [{title}]")
        for r in rows:
            print(f"    {r['label']:26} {(r['disp'] or '—'):14} {r.get('date','')}")
    print(f"\n  Wrote {OUT_HTML}")
    if "--open" in sys.argv:
        os.system(f'open "{OUT_HTML}"')

if __name__ == "__main__":
    main()
