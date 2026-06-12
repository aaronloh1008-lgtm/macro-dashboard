#!/usr/bin/env python3
"""Telegram alert checker for the macro dashboard.

Runs every 5 minutes via GitHub Actions, completely independent of the dashboard
build/deploy — it never touches Netlify, so it uses zero Netlify credits.

For each tracked release it scrapes the Trading Economics calendar, reads the
release time from the GMT column, and sends Telegram messages:
  • nudges at  T-60 / T-15 / T-5 minutes before a scheduled release/decision
  • an "actual released" alert when the figure prints (Actual cell fills in)

Dedup is via alerts_state.json, persisted across runs by the GitHub Actions
cache (see .github/workflows/alerts.yml) — so each nudge fires exactly once.

Secrets required (set as GitHub repo secrets, never in this file):
  TELEGRAM_BOT_TOKEN   from @BotFather
  TELEGRAM_CHAT_ID     your channel/chat id
"""

import json, os, re, sys, urllib.request, urllib.parse
from datetime import datetime, timezone, timedelta

UTC = timezone.utc
NOW = datetime.now(UTC)
SGT = timezone(timedelta(hours=8))                       # dashboard's clock
HERE = os.path.dirname(os.path.abspath(__file__))
STATE_FILE = os.path.join(HERE, "alerts_state.json")
TE_BASE = "https://tradingeconomics.com/"
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124 Safari/537.36")

# (display name, TE slug, is_central_bank). Central-bank pages mix in projections /
# minutes / votes, so those are filtered to genuine "interest rate decision" rows.
EVENTS = [
    ("US CPI",              "united-states/inflation-cpi",                 False),
    ("US PCE",              "united-states/pce-price-index-annual-change", False),
    ("US GDP",              "united-states/gdp-growth",                    False),
    ("US Nonfarm Payrolls", "united-states/non-farm-payrolls",             False),
    ("Fed decision",        "united-states/interest-rate",                 True),
    ("ECB decision",        "euro-area/interest-rate",                     True),
    ("BoE decision",        "united-kingdom/interest-rate",                True),
    ("BoJ decision",        "japan/interest-rate",                         True),
    ("BoK decision",        "south-korea/interest-rate",                   True),
]

THRESHOLDS = [(60, "1 hour"), (15, "15 minutes"), (5, "5 minutes")]
GRACE_MIN = 6          # fire a threshold only within ~one cron step past it
RELEASE_WINDOW_H = 6   # send the "released" alert only within this long after print


# ----------------------------------------------------------------- scraping
def fetch(url):
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    return urllib.request.urlopen(req, timeout=25).read().decode("utf-8", "replace")


def calendar(url):
    """Parse TE's calendar table into rows:
    (date_iso, time_str, event, reference, actual, previous, consensus). '' for blanks."""
    raw = fetch(url)
    i = raw.find('id="calendar-table"')
    if i < 0:
        return []
    seg = raw[i:raw.find("</table>", i) + 8]
    out = []
    for tr in re.findall(r"<tr[^>]*>(.*?)</tr>", seg, re.S)[1:]:      # skip header
        c = [re.sub(r"<[^>]+>", " ", x).strip()
             for x in re.findall(r"<td[^>]*>(.*?)</td>", tr, re.S)]
        # columns: date, GMT time, event, reference, actual, previous, consensus, forecast
        if len(c) >= 5 and re.match(r"20\d\d-[01]\d-[0-3]\d", c[0]):
            out.append((c[0], c[1], c[2], c[3],
                        c[4], c[5] if len(c) > 5 else "", c[6] if len(c) > 6 else ""))
    return out


def event_rows(name, slug, is_cb):
    rows = calendar(TE_BASE + slug)
    if is_cb:
        rows = [r for r in rows if "interest rate decision" in r[2].lower()]
    return rows


_TIME_RE = re.compile(r"(\d{1,2}):(\d{2})\s*(AM|PM)", re.I)


def release_dt(date_iso, time_str):
    """TE date + GMT time -> aware UTC datetime, plus whether a real time was given.
    Rows like 'All Day' / 'Tentative' have no usable time -> (midnight, False)."""
    try:
        d = datetime.strptime(date_iso, "%Y-%m-%d")
    except ValueError:
        return None, False
    m = _TIME_RE.match(time_str.strip())
    if not m:
        return d.replace(tzinfo=UTC), False
    hh, mm, ap = int(m.group(1)), int(m.group(2)), m.group(3).upper()
    if ap == "PM" and hh != 12:
        hh += 12
    if ap == "AM" and hh == 12:
        hh = 0
    return d.replace(hour=hh, minute=mm, tzinfo=UTC), True


# ----------------------------------------------------------------- telegram
def esc(s):
    return (s or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def telegram(text):
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat = os.environ.get("TELEGRAM_CHAT_ID")
    if not token or not chat:
        sys.stderr.write("[telegram] TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID not set; "
                         "would have sent:\n" + text + "\n")
        return False
    data = urllib.parse.urlencode({
        "chat_id": chat, "text": text,
        "parse_mode": "HTML", "disable_web_page_preview": "true",
    }).encode()
    try:
        req = urllib.request.Request(f"https://api.telegram.org/bot{token}/sendMessage",
                                     data=data)
        with urllib.request.urlopen(req, timeout=20) as r:
            return r.status == 200
    except Exception as e:
        sys.stderr.write(f"[telegram] send failed: {e}\n")
        return False


def fmt_when(dt):
    sgt = dt.astimezone(SGT)
    return f"{dt:%b %-d, %H:%M} GMT ({sgt:%b %-d, %H:%M} SGT)"


def fmt_eta(dt):
    secs = (dt - NOW).total_seconds()
    if secs < 0:
        return "now"
    d, rem = divmod(int(secs), 86400)
    h, rem = divmod(rem, 3600)
    m = rem // 60
    if d:
        return f"in {d}d {h}h"
    if h:
        return f"in {h}h {m}m"
    return f"in {m}m"


def nudge_msg(name, lbl, dt, reference, previous, consensus):
    head = f"⏰ <b>{esc(name)}</b> in {lbl}"
    if reference:
        head += f" — {esc(reference)}"
    lines = [head, fmt_when(dt)]
    bits = []
    if consensus:
        bits.append(f"consensus {esc(consensus)}")
    if previous:
        bits.append(f"prev {esc(previous)}")
    if bits:
        lines.append(" · ".join(bits))
    return "\n".join(lines)


def release_msg(name, dt, reference, actual, previous, consensus):
    head = f"✅ <b>{esc(name)}</b> released: <b>{esc(actual)}</b>"
    if reference:
        head += f" ({esc(reference)})"
    lines = [head]
    bits = []
    if consensus:
        bits.append(f"consensus {esc(consensus)}")
    if previous:
        bits.append(f"prev {esc(previous)}")
    if bits:
        lines.append(" · ".join(bits))
    return "\n".join(lines)


# ----------------------------------------------------------------- state
def load_state():
    try:
        with open(STATE_FILE) as f:
            return json.load(f)
    except Exception:
        return {"sent": []}


def save_state(order):
    with open(STATE_FILE, "w") as f:
        json.dump({"sent": order[-300:]}, f, indent=0)


# ----------------------------------------------------------------- main
def run_checks(order, seen):
    """Send any due nudges / release alerts. Mutates order+seen; returns # sent."""
    sent_count = 0
    for name, slug, is_cb in EVENTS:
        try:
            rows = event_rows(name, slug, is_cb)
        except Exception as e:
            sys.stderr.write(f"[{name}] fetch failed: {e}\n")
            continue
        for date_iso, time_str, event, reference, actual, previous, consensus in rows:
            dt, has_time = release_dt(date_iso, time_str)
            if not dt:
                continue
            age_h = (NOW - dt).total_seconds() / 3600.0

            # --- released: Actual has printed within the recent window ---
            if actual and 0 <= age_h <= RELEASE_WINDOW_H:
                key = f"{name}|{date_iso}|release"
                if key not in seen:
                    if telegram(release_msg(name, dt, reference, actual, previous, consensus)):
                        seen.add(key); order.append(key); sent_count += 1

            # --- upcoming: nudge before an unreleased, time-stamped event ---
            if has_time and not actual:
                mins = (dt - NOW).total_seconds() / 60.0
                for thr, lbl in THRESHOLDS:
                    if (thr - GRACE_MIN) < mins <= thr:
                        key = f"{name}|{date_iso}|{thr}"
                        if key not in seen:
                            if telegram(nudge_msg(name, lbl, dt, reference, previous, consensus)):
                                seen.add(key); order.append(key); sent_count += 1
    return sent_count


def send_digest():
    """Manual-trigger connectivity test: the next upcoming item per tracked event."""
    items = []
    for name, slug, is_cb in EVENTS:
        try:
            rows = event_rows(name, slug, is_cb)
        except Exception:
            continue
        upcoming = []
        for date_iso, time_str, event, reference, actual, previous, consensus in rows:
            dt, _ = release_dt(date_iso, time_str)
            if dt and dt > NOW and not actual:
                upcoming.append((dt, reference))
        if upcoming:
            dt, reference = min(upcoming, key=lambda x: x[0])
            label = f"{name}" + (f" ({reference})" if reference else "")
            items.append((dt, f"• <b>{esc(name)}</b> — {fmt_when(dt)}, {fmt_eta(dt)}"))
    items.sort(key=lambda x: x[0])
    body = "\n".join(t for _, t in items) or "No upcoming releases found."
    telegram("🔔 <b>Macro alert bot connected.</b>\nUpcoming:\n" + body)


def main():
    state = load_state()
    order = list(state.get("sent", []))
    seen = set(order)

    if os.environ.get("GITHUB_EVENT_NAME") == "workflow_dispatch" or os.environ.get("ALERT_PING"):
        send_digest()

    n = run_checks(order, seen)
    save_state(order)
    print(f"alerts: {n} sent" if n else "alerts: none due")


if __name__ == "__main__":
    main()
