#!/usr/bin/env python3
"""Telegram alert checker for the macro dashboard.

Runs every 5 minutes via GitHub Actions, completely independent of the dashboard
build/deploy — it never touches Netlify, so it uses zero Netlify credits.

For each tracked release it scrapes the Trading Economics calendar, reads the
release time from the GMT column, and sends Telegram messages:
  • nudges at  T-24h / T-60min / T-5min before a scheduled release/decision
    (each carries the market consensus + previous figure when TE has them)
  • an "actual released" alert when the figure prints (Actual cell fills in)
  • a Monday 7:45am SGT "week ahead" digest — every tracked release in the next
    7 days, grouped by day (a manual run sends a per-event connectivity digest)

Resilience:
  • Every good scrape caches each event's upcoming schedule into the state file,
    so if Trading Economics goes down the T-60/T-5 nudges keep firing off the
    cache (release alerts need the live Actual, so they pause until TE recovers).
  • If a whole run scrapes nothing for every tracked event for OUTAGE_RUNS_BEFORE_ALERT
    consecutive runs, it sends one "TE unreachable" warning (a one-off blip stays
    silent), quiet thereafter for FAIL_ALERT_COOLDOWN_H hours. When TE recovers it
    posts a "resolved" note, deletes the warning, and deletes that note next run —
    so the channel self-cleans.

Dedup is via alerts_state.json, persisted across runs by the GitHub Actions
cache (see .github/workflows/alerts.yml) — so each nudge fires exactly once.

Secrets required (set as GitHub repo secrets, never in this file):
  TELEGRAM_BOT_TOKEN   from @BotFather
  TELEGRAM_CHAT_ID     your channel/chat id
"""

import json, os, re, sys, time, urllib.request, urllib.parse
from datetime import datetime, timezone, timedelta

UTC = timezone.utc
NOW = datetime.now(UTC)
SGT = timezone(timedelta(hours=8))                       # dashboard's clock
HERE = os.path.dirname(os.path.abspath(__file__))
STATE_FILE = os.path.join(HERE, "alerts_state.json")
TE_BASE = "https://tradingeconomics.com/"
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124 Safari/537.36")

# (display name, TE slug, decision_kw). decision_kw is None for data releases;
# for central-bank pages it's the event-row substring that isolates genuine rate
# decisions (the pages also mix in projections / minutes / votes / speeches).
# ECB is special: its main-refi "interest-rate" page lists no FUTURE decisions, so
# we read the DEPOSIT-facility page (which does) and match its "deposit facility" rows.
EVENTS = [
    ("US CPI",              "united-states/inflation-cpi",                 None),
    ("US PCE",              "united-states/pce-price-index-annual-change", None),
    ("US GDP",              "united-states/gdp-growth",                    None),
    ("US Nonfarm Payrolls", "united-states/non-farm-payrolls",             None),
    # Same jobs report as NFP, so both nudge at the identical minute — deliberate:
    # Aaron wants "unemployment" named explicitly, not shadowed under the NFP alert.
    ("US Unemployment Rate","united-states/unemployment-rate",             None),
    ("US ISM Mfg PMI",      "united-states/business-confidence",           None),
    ("Fed decision",        "united-states/interest-rate",                 "interest rate decision"),
    ("ECB decision",        "euro-area/deposit-interest-rate",             "deposit facility"),
    ("BoE decision",        "united-kingdom/interest-rate",                "interest rate decision"),
    ("BoJ decision",        "japan/interest-rate",                         "interest rate decision"),
    ("BoK decision",        "south-korea/interest-rate",                   "interest rate decision"),
    # Other dashboard data releases — full nudges + week-ahead.
    ("UK CPI",              "united-kingdom/inflation-cpi",                None),
    ("EZ HICP",             "euro-area/inflation-cpi",                     None),
    ("Japan CPI",           "japan/inflation-cpi",                         None),
    ("Japan Tankan",        "japan/business-confidence",                   None),
    ("EZ Unemployment",     "euro-area/unemployment-rate",                 None),
    ("UK Unemployment",     "united-kingdom/unemployment-rate",            None),
    ("EZ GDP",              "euro-area/gdp-growth-annual",                 None),
]

THRESHOLDS = [(1440, "24 hours"), (60, "1 hour"), (5, "5 minutes")]
GRACE_MIN = 6              # fire a threshold only within ~one cron step past it
RELEASE_WINDOW_H = 6       # send the "released" alert only within this long after print
FAIL_ALERT_COOLDOWN_H = 24 # at most one "TE unreachable" warning per this many hours
EVENT_DARK_H = 24          # warn if ONE event yields no rows this long (slug/label drift).
                           # A healthy TE page always returns recent history, so a single
                           # event going dark while others work means that page changed.
OUTAGE_RUNS_BEFORE_ALERT = 2  # only page on a TOTAL outage after this many consecutive
                              # all-fail runs (~10 min) — a one-off blip stays silent.


# ----------------------------------------------------------------- scraping
def fetch(url, retries=1):
    """GET with one quick retry, so a single transient blip doesn't count as a
    failure (keeps the bot quiet on momentary TE/network hiccups)."""
    last = None
    for attempt in range(retries + 1):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA})
            return urllib.request.urlopen(req, timeout=25).read().decode("utf-8", "replace")
        except Exception as e:
            last = e
            if attempt < retries:
                time.sleep(1.5)
    raise last


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


def event_rows(name, slug, decision_kw):
    rows = calendar(TE_BASE + slug)
    if decision_kw:
        rows = [r for r in rows if decision_kw in r[2].lower()]
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
            body = json.loads(r.read().decode("utf-8", "replace"))
            # Return the new message_id (truthy) on success so callers can later
            # delete it; None on failure. `if telegram(...)` still works (id is truthy).
            return body["result"]["message_id"] if body.get("ok") else None
    except Exception as e:
        sys.stderr.write(f"[telegram] send failed: {e}\n")
        return None


def telegram_delete(message_id):
    """Delete a message the bot posted (used to clean up the outage / resolved
    notices once TE recovers). Best-effort: failures are logged, not raised."""
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat = os.environ.get("TELEGRAM_CHAT_ID")
    if not token or not chat or not message_id:
        return False
    data = urllib.parse.urlencode({"chat_id": chat, "message_id": message_id}).encode()
    try:
        req = urllib.request.Request(f"https://api.telegram.org/bot{token}/deleteMessage",
                                     data=data)
        with urllib.request.urlopen(req, timeout=20) as r:
            return r.status == 200
    except Exception as e:
        sys.stderr.write(f"[telegram] delete failed: {e}\n")
        return False


def fmt_when(dt):
    sgt = dt.astimezone(SGT)
    return f"{sgt:%b %-d, %H:%M} SGT"


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
        return {}


def save_state(order, schedule, health, nudge_msg_ids):
    with open(STATE_FILE, "w") as f:
        json.dump({"sent": order[-300:], "schedule": schedule, "health": health,
                   "nudge_msg_ids": nudge_msg_ids},
                  f, indent=0)


def _parse_iso(s):
    try:
        return datetime.fromisoformat(s) if s else None
    except Exception:
        return None


# ----------------------------------------------------------------- main
def run_checks(order, seen, schedule, health=None, nudge_msg_ids=None):
    """Send any due nudges / release alerts.

    Mutates order/seen (dedup) and schedule (per-event forward cache); when a
    `health` dict is given, stamps health["events"][name] each time an event
    scrapes live rows (used for per-event "went dark" detection). Returns
    (sent_count, healthy_events), where healthy_events is how many tracked events
    were scraped live and yielded at least one row — 0 means a total TE outage.
    """
    sent_count = 0
    healthy_events = 0
    for name, slug, decision_kw in EVENTS:
        try:
            rows = event_rows(name, slug, decision_kw)
            live = True
        except Exception as e:
            sys.stderr.write(f"[{name}] fetch failed: {e}\n")
            rows, live = [], False

        if live:
            if rows:
                healthy_events += 1
                if health is not None:
                    health.setdefault("events", {})[name] = NOW.isoformat()
            # Refresh this event's forward cache from the good scrape: keep only
            # upcoming, time-stamped, not-yet-released rows for offline fallback.
            cache = []
            for d, t, _ev, ref, act, prev, cons in rows:
                dt, has_time = release_dt(d, t)
                if dt and has_time and not act and dt > NOW:
                    cache.append([d, t, ref, prev, cons])
            schedule[name] = cache
        else:
            # TE failed for this event — replay the cached forward schedule so the
            # T-60/T-5 nudges still fire. Actual is unknown (""), so only the nudge
            # branch runs; past entries are pruned as we go.
            kept, rows = [], []
            for d, t, ref, prev, cons in schedule.get(name, []):
                dt, _ = release_dt(d, t)
                if dt and dt > NOW:
                    kept.append([d, t, ref, prev, cons])
                    rows.append((d, t, "", ref, "", prev, cons))
            schedule[name] = kept

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
                        if nudge_msg_ids is not None:
                            ev_key = f"{name}|{date_iso}"
                            for mid in nudge_msg_ids.pop(ev_key, []):
                                telegram_delete(mid)

            # --- upcoming: nudge before an unreleased, time-stamped event ---
            if has_time and not actual:
                mins = (dt - NOW).total_seconds() / 60.0
                for thr, lbl in THRESHOLDS:
                    if (thr - GRACE_MIN) < mins <= thr:
                        key = f"{name}|{date_iso}|{thr}"
                        if key not in seen:
                            mid = telegram(nudge_msg(name, lbl, dt, reference, previous, consensus))
                            if mid:
                                seen.add(key); order.append(key); sent_count += 1
                                if nudge_msg_ids is not None:
                                    nudge_msg_ids.setdefault(f"{name}|{date_iso}", []).append(mid)
    return sent_count, healthy_events


def send_digest():
    """Manual-trigger connectivity test: the next upcoming item per tracked event."""
    items = []
    for name, slug, decision_kw in EVENTS:
        try:
            rows = event_rows(name, slug, decision_kw)
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


def fmt_clock(dt):
    """Just the time in SGT — for the day-grouped week-ahead list."""
    sgt = dt.astimezone(SGT)
    return f"{sgt:%H:%M} SGT"


def send_week_ahead(days=7):
    """Monday digest: every tracked release scheduled in the next `days` days,
    sorted chronologically and grouped by SGT calendar day — a single
    consolidated 'week ahead' rather than one line per event."""
    horizon = NOW + timedelta(days=days)
    items = []
    for name, slug, decision_kw in EVENTS:
        try:
            rows = event_rows(name, slug, decision_kw)
        except Exception:
            continue
        for date_iso, time_str, event, reference, actual, previous, consensus in rows:
            dt, has_time = release_dt(date_iso, time_str)
            if dt and not actual and NOW <= dt <= horizon:
                items.append((dt, has_time, name, reference, previous, consensus))
    items.sort(key=lambda x: x[0])
    if not items:
        telegram("🗓️ <b>Week ahead</b> — no tracked macro releases in the next 7 days.")
        return
    lines = ["🗓️ <b>Week ahead — macro releases (next 7 days)</b>"]
    cur_day = None
    for dt, has_time, name, reference, previous, consensus in items:
        day = dt.astimezone(SGT).strftime("%a %-d %b")
        if day != cur_day:
            lines.append(f"\n<b>{day}</b>")
            cur_day = day
        line = f"• <b>{esc(name)}</b>"
        if reference:
            line += f" ({esc(reference)})"
        line += f" — {fmt_clock(dt) if has_time else 'time TBC'}"
        extra = []
        if consensus:
            extra.append(f"est {esc(consensus)}")
        if previous:
            extra.append(f"prev {esc(previous)}")
        if extra:
            line += " · " + " · ".join(extra)
        lines.append(line)
    telegram("\n".join(lines))


def main():
    state = load_state()
    order = list(state.get("sent", []))
    seen = set(order)
    schedule = state.get("schedule", {})
    health = state.get("health", {})
    nudge_msg_ids = state.get("nudge_msg_ids", {})

    # Monday 7:45am SGT cron -> consolidated "week ahead". A manual run (or an
    # ALERT_PING) instead sends the per-event connectivity digest, so a manual
    # trigger still confirms every tracked page is scraping.
    if os.environ.get("WEEKLY_DIGEST"):
        send_week_ahead()
    elif os.environ.get("GITHUB_EVENT_NAME") == "workflow_dispatch" \
            or os.environ.get("ALERT_PING"):
        send_digest()

    n, healthy = run_checks(order, seen, schedule, health, nudge_msg_ids)

    # Total outage: nothing scraped for any tracked event this run. Only page after
    # OUTAGE_RUNS_BEFORE_ALERT consecutive all-fail runs (a one-off blip stays silent,
    # the cache fallback covers it), then stay quiet for the cooldown.
    if healthy == 0:
        streak = health.get("outage_streak", 0) + 1
        health["outage_streak"] = streak
        if streak >= OUTAGE_RUNS_BEFORE_ALERT:
            last = _parse_iso(health.get("last_fail_alert"))
            if last is None or (NOW - last) >= timedelta(hours=FAIL_ALERT_COOLDOWN_H):
                mid = telegram("⚠️ <b>Alert bot:</b> Trading Economics has been unreachable "
                               f"for {streak} runs (~{streak * 5} min). Nudges are running off "
                               "the cached schedule; release alerts pause until TE recovers.")
                if mid:
                    health["last_fail_alert"] = NOW.isoformat()
                    health["outage_msg_id"] = mid
        print(f"alerts: TE outage (healthy=0, streak={streak}); {n} sent")
    else:
        health["outage_streak"] = 0          # any healthy run clears the streak
        if health.get("outage_msg_id"):
            # Recovered from an alerted outage: post a 'resolved' note, delete the
            # original warning, and queue the resolved note itself for deletion next
            # run — so both vanish from the channel and it ends up clean.
            rid = telegram("✅ <b>Alert bot:</b> Trading Economics is back — alerts resumed.")
            telegram_delete(health.pop("outage_msg_id"))
            health.pop("last_fail_alert", None)        # clear cooldown for any future outage
            if rid:
                health["resolved_msg_id"] = rid
        elif health.get("resolved_msg_id"):
            telegram_delete(health.pop("resolved_msg_id"))
        # Check for a SINGLE event silently gone dark — a healthy TE page always
        # returns recent history, so one event yielding no rows for EVENT_DARK_H
        # means its slug/label likely changed and needs maintenance.
        check_dark_events(health)
        print(f"alerts: {n} sent" if n else "alerts: none due")

    save_state(order, schedule, health, nudge_msg_ids)


def check_dark_events(health):
    """Warn (once per cooldown, per event) about any tracked event that hasn't
    produced calendar rows for EVENT_DARK_H — the signal that one TE page changed
    while the rest are fine. A never-seen event is seeded 'now' so it gets a full
    grace window before it can warn (covers a freshly added or briefly-down page)."""
    events_h = health.setdefault("events", {})
    alerts_h = health.setdefault("event_alerts", {})
    dark = []
    for name, slug, decision_kw in EVENTS:
        last_ok = _parse_iso(events_h.get(name))
        if last_ok is None:
            events_h[name] = NOW.isoformat()
            continue
        if (NOW - last_ok) >= timedelta(hours=EVENT_DARK_H):
            last_warn = _parse_iso(alerts_h.get(name))
            if last_warn is None or (NOW - last_warn) >= timedelta(hours=FAIL_ALERT_COOLDOWN_H):
                dark.append(name)
    if dark:
        names = ", ".join(esc(d) for d in dark)
        if telegram(f"⚠️ <b>Alert bot:</b> no calendar rows for <b>{names}</b> in over "
                    f"{EVENT_DARK_H}h, while other events are fine — that page's slug or "
                    "event label likely changed on Trading Economics and needs a fix."):
            for d in dark:
                alerts_h[d] = NOW.isoformat()


if __name__ == "__main__":
    main()
