#!/usr/bin/env python3
"""Time Machine for india-timemachine (NSE / BSE). Standard library only, Python 3.9+.

Replays a planned intraday trade before you risk a rupee: 10,000 simulated runs
of the rest of the session from your entry, plus the same rule on every past
session in the data.

    python3 timemachine.py run                          # trader-in/plan.json, 10,000 replays, seed 19
    python3 timemachine.py run --write                  # also save trader-in/timemachine.json
    python3 timemachine.py run --until 15:30            # replay to the close instead of the flat-by time
    python3 timemachine.py run --qty 40                 # charges for 40 shares (default: Rs 500 risk)
    python3 timemachine.py run --seed 7 --replays 20000 --json
    python3 timemachine.py run --symbol RELIANCE --date 2026-10-19 --entry 1227.8 \\
        --stop 1215.3 --target 1240.3 --entry-time 09:50
    python3 timemachine.py chart                        # ASCII fan chart of the replays
    python3 timemachine.py chart --from trader-in/timemachine.json

How the replays work (the original trader-timemachine, same algorithm, same draws):
  1. Fetch 60 days of 5-minute bars (Yahoo v8 chart, SYMBOL.NS or SYMBOL.BO,
     range=60d interval=5m; one request). Keep only FULL sessions before the
     plan's date, judged by session_rules for THAT day: 75 bars (09:15 to
     15:30 IST), or 72 bars (09:15 to 15:15) for a CAS stock (one with F&O
     contracts) from 2026-08-03, when NSE's closing auction started. The
     15:15 to 15:30 auction bars are cut off, never filled. Up to 3 empty bars
     of real gaps in a session are filled flat at the previous close. Short
     sessions (Muhurat trading, bigger gaps), holidays and today are left out.
     --kite-bars FILE (Kite get_historical_data output) replaces only the plan
     day's bars; the history stays on Yahoo.
  2. The pool: for every bar slot 1..74 (1..71 on a CAS day) of the day, each past session's bar as
     ratios to the previous bar's close: (open, high, low, close) / prev close.
  3. One replay starts at the entry price and walks the rest of the session.
     For each bar i it picks a slot j = i + randint(-window, window), kept in
     1..74, then one past bar from pool[j] (random.choice), and scales it to
     the current price. The stop is checked before the target in every bar,
     so a bar that touches both counts as the stop (the conservative call).
  4. Outcomes: "target" or "stop" (whichever was touched first), or "neither"
     when the trade is still open at --until (default: the plan's flat_by, else
     15:00 for a CAS stock, 15:10 for the rest; Zerodha squares MIS off at
     15:12 / 15:25; continuous trading ends 15:15 / 15:30).
     "green" = target first, or above the entry at --until.
     R = the target's R at the target, -1 at the stop, otherwise
     (price at --until - entry) / risk. A gap through the stop still counts
     -1R here; a real fill can be worse.
     Net R takes off the Indian round-trip charges (brokerage, STT, exchange,
     SEBI, stamp duty, GST) for a stated quantity: R - charges / (risk x qty).
  5. The price at --until: median and the 5th to 95th percentile of all
     replays (pct(a, q) = sorted a at index int(q * n)). The fan chart uses a
     second run of 2,000 paths with the same seed. The histogram has 24 bins
     between the 1st and 99th percentile.
  6. History: the plan's own rule (an opening-range breakout with the same
     opening range, stop rule and target R) re-run on every one of those past
     full sessions, first close beyond the range = entry, same stop-first rule.

The verdict line looks at the replays that hit one side (target or stop) and
compares the target's share of them, target / (target + stop), with the share
that breaks even at the plan's target R: 1 / (1 + R), so 50 % at 1R and 33 % at
2R. The gap, in percentage points:
  less than 5 points    "Close to a coin flip" (1R) / "Close to break-even" (other R)
  5 to 15 points        "Leans toward the target" / "Leans toward the stop"
  15 points or more     "Strongly toward the target" / "Strongly toward the stop"
Below it: a note when more than 40 % of the replays hit neither (the price at
--until decides those), the average R when its sign goes against the lean, and
a note when the charges turn a positive average R negative.

5-minute bars only: Yahoo keeps 60 days of 5-min bars but only about 7 days of
1-min bars, too few sessions for a fair pool. The run refuses with a plain
message when there are fewer than 20 full sessions or the entry bar is not in
the data. Same plan, same data and same --seed give the same numbers on any
machine. A simulation built from the stock's own recent bars is not a
prediction: results, news and circuit limits can do what the last 60 days never did.

Data: https://query1.finance.yahoo.com/v8/finance/chart/RELIANCE.NS?range=60d&interval=5m
(unofficial and free, can lag; falls back to query2 on 429 or 5xx).
--offline FILE reads a saved chart JSON (.json or .json.gz) instead.
Times are IST (UTC+05:30, no daylight saving); --now "2026-10-19 09:55" pins the clock.
"""

import argparse
import datetime
import gzip
import json
import math
import os
import random
import re
import statistics
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

UA = "Mozilla/5.0 (compatible; india-day-trader-skill/1.0; +https://github.com/)"
PAUSE = 0.5          # seconds between requests: be polite to free endpoints
BACKOFF = 5.0        # seconds to wait once after a 429
HOSTS = ("query1", "query2")
INTERVAL = 300       # 5-minute bars, in seconds
SESSION_BARS = 75    # 09:15 to 15:30 in 5-minute bars
OPEN_MIN = 9 * 60 + 15
CLOSE_MIN = 15 * 60 + 30
DEFAULT_FLAT_BY = "15:10"   # used only when a result carries no until; the real default is session_rules
DEFAULT_RISK_INR = 500     # the pack's default max_loss_per_trade_inr
MIN_SESSIONS = 20
MAX_FILL = 3         # empty bars a session may have and still count as full (filled flat)
SAMPLE_PATHS = 160
BAND_PATHS = 2000
BAND_Q = (0.05, 0.25, 0.5, 0.75, 0.95)
HIST_BINS = 24
COIN_FLIP = 0.05     # verdict thresholds, see the docstring
STRONG = 0.15
MANY_NEITHER = 0.40
IST = datetime.timezone(datetime.timedelta(hours=5, minutes=30))
EPOCH = datetime.datetime(1970, 1, 1)
WEEKDAYS = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")
# NSE capital-market trading holidays 2026 (NSE holiday-master, read 2026-10-23). 2027 is not
# published yet: india-alerts caches the live list in trader-in/holidays.json, read when present.
NSE_HOLIDAYS_2026 = {
    "2026-01-15", "2026-01-26", "2026-02-15", "2026-03-03", "2026-03-21", "2026-03-26", "2026-03-31",
    "2026-04-03", "2026-04-14", "2026-05-01", "2026-05-28", "2026-06-26", "2026-08-15", "2026-09-14",
    "2026-10-02", "2026-10-20", "2026-11-08", "2026-11-10", "2026-11-24", "2026-12-25",
}
HOLIDAY_CACHE = os.path.join("trader-in", "holidays.json")
# Closing Auction Session: from 2026-08-03 stocks with F&O contracts ("CAS stocks") trade continuously
# only to 15:15, then go to a 15:15-15:35 auction. Zerodha squares MIS off at 15:12 for them, 15:25 for
# the rest (support.zerodha.com, read 2026-10-23).
CAS_START = datetime.date(2026, 8, 3)
FO_URL = "https://nsearchives.nseindia.com/content/fo/fo_mktlots.csv"
FO_CACHE = os.path.join("trader-in", "fo_list.txt")
FO_MAX_AGE = 7       # days before the cached F&O list is fetched again
BROWSER_UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) "
              "Chrome/130 Safari/537.36")


class TMError(Exception):
    pass


# ---------------------------------------------------------------- session rules (shared, see CAS_KITE_SPEC.md)

def session_rules(cas, day):
    """The day's session for a stock. cas: True / False / None (unknown -> the strict CAS rules)."""
    if isinstance(day, str):
        day = datetime.date.fromisoformat(day)
    strict = cas is not False and day >= CAS_START
    return {
        "open": "09:15",
        "close": "15:15" if strict else "15:30",        # end of continuous trading
        "square_off": "15:12" if strict else "15:25",   # Zerodha MIS auto square-off
        "flat_by_default": "15:00" if strict else "15:10",
        "no_entries_after_default": "14:45",
        "bars_5m": 72 if strict else 75,
        "cas": cas, "label": "CAS stock (F&O): continuous trading ends 15:15, Zerodha squares off MIS at 15:12" if strict
                 else "non-CAS stock: trades to 15:30, Zerodha squares off MIS at 15:25",
    }


def parse_fo_csv(text):
    """NSE fo_mktlots.csv -> the set of stock symbols with F&O contracts (index rows skipped)."""
    out = set()
    for line in text.splitlines()[1:]:
        cells = [c.strip() for c in line.split(",")]
        if len(cells) < 2 or not cells[1] or cells[0].upper().startswith("NIFTY") or cells[1].upper() == "SYMBOL":
            continue
        out.add(cells[1].upper())
    return out


def read_fo_cache(path):
    """(symbols, fetched date) from trader-in/fo_list.txt, or (None, None)."""
    try:
        with open(path, encoding="utf-8") as fh:
            lines = [l.strip() for l in fh if l.strip()]
    except OSError:
        return None, None
    fetched = None
    if lines and lines[0].startswith("#"):
        m = re.search(r"(\d{4}-\d{2}-\d{2})", lines[0])
        fetched = m.group(1) if m else None
    return {l.upper() for l in lines if not l.startswith("#")}, fetched


def fetch_fo_list(path, today):
    """Fetch NSE's F&O list (browser UA, compatible UA as fallback) and cache it. None on failure."""
    for ua in (BROWSER_UA, UA):
        try:
            req = urllib.request.Request(FO_URL, headers={"User-Agent": ua, "Accept": "text/csv,*/*"})
            with urllib.request.urlopen(req, timeout=20) as r:
                syms = parse_fo_csv(r.read().decode("utf-8", "replace"))
        except (urllib.error.URLError, OSError, ValueError):
            continue
        if syms:
            try:
                d = os.path.dirname(path)
                if d:
                    os.makedirs(d, exist_ok=True)
                with open(path, "w", encoding="utf-8") as fh:
                    fh.write("# fetched %s\n" % today + "\n".join(sorted(syms)) + "\n")
            except OSError:
                pass
            return syms
    return None


def cas_status(sym, flag=None, config=None, fo_path=FO_CACHE, today=None, online=True):
    """(True / False / None, how). --cas yes|no wins, then the config's cas_symbols_extra /
    non_cas_symbols, then NSE's F&O list (cached 7 days). Unknown -> None (strict CAS rules)."""
    if flag in ("yes", "no"):
        return flag == "yes", "--cas %s" % flag
    cfg = config or {}
    if sym in {str(s).upper() for s in cfg.get("cas_symbols_extra") or []}:
        return True, "config cas_symbols_extra"
    if sym in {str(s).upper() for s in cfg.get("non_cas_symbols") or []}:
        return False, "config non_cas_symbols"
    today = today or datetime.date.today()
    syms, fetched = read_fo_cache(fo_path)
    stale = not fetched or (today - datetime.date.fromisoformat(fetched)).days > FO_MAX_AGE
    if (syms is None or stale) and online:
        fresh = fetch_fo_list(fo_path, today.isoformat())
        if fresh:
            syms, fetched = fresh, today.isoformat()
    if syms is None:
        return None, "F&O list unavailable: using the stricter CAS rules"
    return sym in syms, "NSE F&O list (%s)" % (fetched or "undated")


def read_config(path):
    try:
        with open(path, encoding="utf-8") as fh:
            doc = json.load(fh)
        return doc if isinstance(doc, dict) else {}
    except (OSError, ValueError):
        return {}


# ---------------------------------------------------------------- time (IST is a fixed UTC+05:30)

def to_ist(utc):
    """Naive UTC -> naive IST wall-clock."""
    return utc.replace(tzinfo=None) + datetime.timedelta(hours=5, minutes=30)


def ist_to_utc(local):
    return local.replace(tzinfo=None) - datetime.timedelta(hours=5, minutes=30)


def parse_now(text):
    """ISO time -> naive UTC. A time without an offset is taken as IST."""
    if not text:
        return datetime.datetime.now(datetime.timezone.utc).replace(microsecond=0, tzinfo=None)
    t = text.strip().replace("Z", "+00:00")
    try:
        dt = datetime.datetime.fromisoformat(t)
    except ValueError:
        raise TMError("--now wants a time like \"2026-10-19 09:55\" (IST) or ...Z (UTC)")
    if dt.tzinfo is None:
        return ist_to_utc(dt)
    return (dt - dt.utcoffset()).replace(tzinfo=None)


def utc_of(ts):
    return EPOCH + datetime.timedelta(seconds=int(ts))


def parse_clock(text, what):
    """'09:50', '9:50 AM', '3:10 pm', '15:10' or '3:10' (a bare 1 to 8 o'clock means p.m.) -> minutes."""
    m = re.match(r"^\s*(\d{1,2}):(\d{2})\s*(?:([AaPp])\.?\s*[Mm]\.?)?\s*(?:IST)?\s*$", str(text or ""))
    if not m:
        raise TMError("%s wants an IST time like 09:50 or 15:10, not %r" % (what, text))
    h, mi, ap = int(m.group(1)), int(m.group(2)), (m.group(3) or "").lower()
    if mi > 59 or h > 23 or (ap and not 1 <= h <= 12):
        raise TMError("%s %r is not a clock time" % (what, text))
    if ap == "p" and h != 12:
        h += 12
    elif ap == "a" and h == 12:
        h = 0
    elif not ap and 1 <= h <= 8:
        h += 12
    return h * 60 + mi


def clock(minutes):
    return "%02d:%02d" % divmod(minutes, 60)


def holidays():
    """NSE trading holidays: the cached live list (trader-in/holidays.json) plus the 2026 fallback."""
    days = set(NSE_HOLIDAYS_2026)
    try:
        with open(HOLIDAY_CACHE, encoding="utf-8") as fh:
            doc = json.load(fh)
        rows = doc.get("CM", doc.get("holidays", [])) if isinstance(doc, dict) else doc
        for r in rows or []:
            v = r.get("date") or r.get("tradingDate") if isinstance(r, dict) else r
            try:
                days.add(datetime.date.fromisoformat(str(v)).isoformat())
            except ValueError:
                try:
                    days.add(datetime.datetime.strptime(str(v), "%d-%b-%Y").date().isoformat())
                except ValueError:
                    pass
    except (OSError, ValueError, AttributeError):
        pass
    return days


# ---------------------------------------------------------------- ticks and charges (shared, see PORTING.md)

def tick_size(price):
    """NSE tick size by price band (from Apr 15 2025; NSE reviews it monthly)."""
    if price < 250:
        return 0.01
    if price < 1000:
        return 0.05
    if price < 5000:
        return 0.10
    if price < 10000:
        return 0.50
    if price < 20000:
        return 1.00
    return 5.00


def round_to_tick(price, mode="nearest"):
    """Round to the stock's tick: 'down', 'up' or 'nearest'. Works in paise so 1215.35 stays exact."""
    t = int(round(tick_size(price) * 100))
    p = round(price * 100, 4)
    n = p / t
    k = math.floor(n + 1e-9) if mode == "down" else math.ceil(n - 1e-9) if mode == "up" else math.floor(n + 0.5)
    return round(k * t / 100.0, 2)


def charges(buy_value, sell_value, product="MIS", exchange="NSE"):
    """Indian equity round-trip charges in rupees, Zerodha's list as of 2026-10: check your broker's
    charge list. Brokerage: intraday min(20, 0.03 %) per executed order, delivery 0. STT: intraday
    0.025 % on the sell, delivery 0.1 % on both. Exchange: NSE 0.00297 %, BSE 0.00375 % of turnover.
    SEBI: Rs 10 a crore. Stamp duty on the buy: intraday 0.003 %, delivery 0.015 %. GST 18 % on
    brokerage + exchange + SEBI."""
    intraday = str(product).upper() != "CNC"
    turnover = buy_value + sell_value
    brokerage = sum(min(20.0, 0.0003 * v) for v in (buy_value, sell_value) if v > 0) if intraday else 0.0
    stt = 0.00025 * sell_value if intraday else 0.001 * turnover
    exch = (0.0000375 if str(exchange).upper() == "BSE" else 0.0000297) * turnover
    sebi = 0.000001 * turnover
    stamp = (0.00003 if intraday else 0.00015) * buy_value
    gst = 0.18 * (brokerage + exch + sebi)
    parts = {"brokerage": brokerage, "stt": stt, "exchange": exch, "sebi": sebi, "stamp": stamp, "gst": gst}
    out = {k: round(v, 2) for k, v in parts.items()}
    out["total"] = round(sum(parts.values()), 2)
    return out


def inr(x):
    """Rs amount with Indian digit grouping: 123456.78 -> '₹1,23,456.78'."""
    neg = x < 0
    whole, frac = ("%.2f" % abs(x)).split(".")
    head, tail = whole[:-3], whole[-3:]
    groups = []
    while len(head) > 2:
        groups.insert(0, head[-2:])
        head = head[:-2]
    if head:
        groups.insert(0, head)
    s = ",".join(groups + [tail]) if groups else tail
    return ("-" if neg else "") + "₹" + s + "." + frac


# ---------------------------------------------------------------- symbols and data

def parse_symbol(text, exchange=None):
    """'RELIANCE', 'RELIANCE.NS', 'NSE:RELIANCE', 'RELIANCE.BO', 'BSE:RELIANCE', '500325' -> (SYMBOL, EXCHANGE)."""
    s = str(text or "").strip().upper()
    ex = str(exchange).strip().upper() if exchange else None
    if ":" in s:
        pre, s = s.split(":", 1)
        if pre in ("NSE", "BSE"):
            ex = pre
    if s.endswith(".NS"):
        s, ex = s[:-3], "NSE"
    elif s.endswith(".BO"):
        s, ex = s[:-3], "BSE"
    if re.match(r"^\d{6}$", s):
        ex = "BSE"
    if not re.match(r"^[A-Z0-9][A-Z0-9&\-]{0,19}$", s):
        raise TMError("the plan needs an NSE/BSE symbol like RELIANCE, M&M or BAJAJ-AUTO, got %r" % text)
    if ex not in (None, "NSE", "BSE"):
        raise TMError("exchange must be NSE or BSE, got %r" % exchange)
    return s, ex or "NSE"


def yahoo_symbol(sym, exchange):
    return "%s.%s" % (sym, "BO" if exchange == "BSE" else "NS")


def chart_url(ysym, host="query1"):
    return "https://%s.finance.yahoo.com/v8/finance/chart/%s?range=60d&interval=5m" % (
        host, urllib.parse.quote(ysym, safe="."))


def _sleep(seconds):
    time.sleep(seconds)


def _get(url, timeout=20):
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode("utf-8", "replace")


def fetch_chart(ysym):
    """One request for 60 days of 5-min bars. query1 first, one retry after a 429,
    then query2 on 429 or 5xx."""
    last = None
    for n, host in enumerate(HOSTS):
        if n:
            _sleep(PAUSE)
        for attempt in (0, 1):
            try:
                return _get(chart_url(ysym, host))
            except urllib.error.HTTPError as e:
                last = e.code
                try:
                    e.close()
                except Exception:
                    pass
                if e.code == 429 and attempt == 0:
                    _sleep(BACKOFF)
                    continue
                if e.code == 429 or e.code >= 500:
                    break
                if e.code == 404:
                    raise TMError("Yahoo has no chart for %s (HTTP 404): check the symbol" % ysym)
                raise TMError("Yahoo answered HTTP %d for %s" % (e.code, ysym))
            except urllib.error.URLError as e:
                raise TMError("could not reach Yahoo: %s" % e.reason)
            except OSError as e:
                raise TMError("could not reach Yahoo: %s" % e)
    if last == 429:
        raise TMError("Yahoo is rate limiting (HTTP 429). Wait a minute and run it again.")
    raise TMError("Yahoo answered HTTP %s for %s on both hosts. Try again later." % (last, ysym))


def read_offline(path, sym):
    """A saved Yahoo chart file (.json or .json.gz), or a folder holding <SYM>-60d5m.json(.gz)."""
    if os.path.isdir(path):
        for name in ("%s-60d5m.json" % sym, "%s-60d5m.json.gz" % sym):
            if os.path.exists(os.path.join(path, name)):
                path = os.path.join(path, name)
                break
        else:
            raise TMError("no %s-60d5m.json(.gz) in %s" % (sym, path))
    if not os.path.exists(path):
        raise TMError("no offline chart file %s" % path)
    opener = gzip.open if path.endswith(".gz") else open
    try:
        with opener(path, "rt", encoding="utf-8") as fh:
            text = fh.read()
    except OSError as e:
        raise TMError("could not read %s: %s" % (path, e))
    if text.lstrip()[:1] == "[" or '"date":' in text[:200] and '"chart"' not in text[:200]:
        raise TMError("%s looks like Kite candles (get_historical_data): pass it with --kite-bars; the 60-day "
                      "history comes from Yahoo" % path)
    return text


def read_kite_bars(paths, now_utc):
    """Kite get_historical_data output (a JSON list of {date, open, high, low, close, volume, oi}, or
    {"data": [...]}) from one or more files, merged and de-duplicated by date -> 5-min bars, oldest first.
    Bars not complete at now are dropped."""
    rows = {}
    for path in paths or []:
        try:
            with open(path, encoding="utf-8") as fh:
                doc = json.load(fh)
        except (OSError, ValueError) as e:
            raise TMError("could not read the Kite candles %s: %s" % (path, e))
        if isinstance(doc, dict):
            doc = doc.get("data", doc.get("candles"))
            if isinstance(doc, dict):
                doc = doc.get("candles")
        if not isinstance(doc, list):
            raise TMError("%s is not Kite get_historical_data output (a list of candles)" % path)
        for r in doc:
            try:
                if isinstance(r, dict):
                    d, o, h, l, c, v = (r["date"], r["open"], r["high"], r["low"], r["close"], r.get("volume", 0))
                else:
                    d, o, h, l, c, v = r[0], r[1], r[2], r[3], r[4], (r[5] if len(r) > 5 else 0)
                dt = datetime.datetime.fromisoformat(str(d).replace("Z", "+00:00"))
            except (KeyError, IndexError, TypeError, ValueError):
                raise TMError("%s has a candle without date/open/high/low/close" % path)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=IST)
            utc = (dt - dt.utcoffset()).replace(tzinfo=None)
            rows[utc] = (float(o), float(h), float(l), float(c), int(v or 0))
    stamps = sorted(rows)
    if len(stamps) > 1:
        step = min((b - a).total_seconds() for a, b in zip(stamps, stamps[1:]))
        if step != INTERVAL:
            raise TMError("the Kite candles are %d-minute bars; the time machine needs 5minute" % (step // 60))
    bars = []
    for utc in stamps:
        local = to_ist(utc)
        mins = local.hour * 60 + local.minute - OPEN_MIN
        if mins < 0 or mins >= SESSION_BARS * 5 or mins % 5:
            continue
        if utc + datetime.timedelta(seconds=INTERVAL) > now_utc:
            continue
        o, h, l, c, v = rows[utc]
        bars.append({"date": local.date().isoformat(), "slot": mins // 5, "start": local.strftime("%H:%M"),
                     "o": o, "h": h, "l": l, "c": c, "v": v, "src": "kite"})
    return bars


def chart_result(text, sym):
    try:
        data = json.loads(text)
    except ValueError:
        raise TMError("Yahoo returned something that is not JSON for %s" % sym)
    chart = data.get("chart") if isinstance(data, dict) else None
    if not isinstance(chart, dict):
        raise TMError("Yahoo returned something unexpected for %s" % sym)
    res = chart.get("result")
    if not res:
        err = chart.get("error") or {}
        raise TMError("Yahoo has no data for %s: %s" % (sym, err.get("description") or "unknown symbol"))
    return res[0]


def parse_bars(result, now_utc):
    """Yahoo chart result -> (meta, bars, dropped). Regular-session 5-min bars only,
    oldest first. Dropped: bars with a missing price, bars off the 5-minute grid
    (the live tick, e.g. 11:24:35), bars not complete yet (now < start + 5 min,
    where now is the earlier of --now and the data's own regularMarketTime), and
    bars outside 09:15 to 15:30 IST."""
    meta = result.get("meta") or {}
    stamps = result.get("timestamp") or []
    q = ((result.get("indicators") or {}).get("quote") or [{}])[0] or {}
    cols = [q.get(k) or [] for k in ("open", "high", "low", "close", "volume")]
    as_of = now_utc
    rmt = meta.get("regularMarketTime")
    if isinstance(rmt, (int, float)) and utc_of(rmt) < as_of:
        as_of = utc_of(rmt)
    dropped = {"no_price": 0, "off_grid": 0, "not_complete": 0, "outside_hours": 0}
    bars = {}
    for i, ts in enumerate(stamps):
        o, h, l, c, v = [col[i] if i < len(col) else None for col in cols]
        if ts is None or None in (o, h, l, c):
            dropped["no_price"] += 1
            continue
        ts = int(ts)
        if ts % INTERVAL:
            dropped["off_grid"] += 1
            continue
        start = utc_of(ts)
        local = to_ist(start)
        mins = local.hour * 60 + local.minute - OPEN_MIN
        if mins < 0 or mins >= SESSION_BARS * 5:
            dropped["outside_hours"] += 1
            continue
        if start + datetime.timedelta(seconds=INTERVAL) > as_of:
            dropped["not_complete"] += 1
            continue
        bars[ts] = {"date": local.date().isoformat(), "slot": mins // 5, "start": local.strftime("%H:%M"),
                    "o": o, "h": h, "l": l, "c": c, "v": v or 0}
    return meta, [bars[t] for t in sorted(bars)], dropped


def split_sessions(bars):
    """{date: {slot: bar}}."""
    days = {}
    for b in bars:
        days.setdefault(b["date"], {})[b["slot"]] = b
    return days


def full_sessions(days, before, cas=None):
    """Full sessions strictly before the plan's date, oldest first, judged by THAT day's
    session_rules: 75 bars (09:15 to 15:30), or 72 bars (09:15 to 15:15) for a CAS stock
    from 2026-08-03 on. On a CAS day the 15:15 to 15:30 bars are the closing auction, not
    continuous trading: they are cut off and counted as auction bars, never filled.
    Within the session a real gap of at most MAX_FILL bars (and never the 09:15 bar) is
    filled flat at the previous close (open = high = low = close, volume 0) and counted;
    a bigger gap leaves the session out (Muhurat trading, outages).
    Returns (full, short, filled, auction) with filled = {date: bars filled} and
    auction = {date: auction slots cut}."""
    full, short, filled, auction = [], [], {}, {}
    for d in sorted(days):
        if d >= before:
            continue
        nb = session_rules(cas, d)["bars_5m"]
        s = {i: b for i, b in days[d].items() if i < nb}
        if nb < SESSION_BARS:
            auction[d] = SESSION_BARS - nb
        gaps = nb - len(s)
        if 0 in s and gaps <= MAX_FILL:
            rows = []
            for i in range(nb):
                if i in s:
                    rows.append(s[i])
                else:
                    pc = rows[-1]["c"]
                    rows.append({"date": d, "slot": i, "start": clock(OPEN_MIN + 5 * i),
                                 "o": pc, "h": pc, "l": pc, "c": pc, "v": 0, "filled": True})
            if gaps:
                filled[d] = gaps
            full.append((d, rows))
        else:
            short.append({"date": d, "bars": len(s)})
    return full, short, filled, auction


# ---------------------------------------------------------------- the simulation

def build_pool(sessions):
    """pool[i] = every past session's bar i as (o, h, l, c) / bar i-1's close, in date order."""
    pool = [[] for _ in range(SESSION_BARS)]
    for _, D in sessions:
        for i in range(1, len(D)):
            pc = D[i - 1]["c"]
            pool[i].append((D[i]["o"] / pc, D[i]["h"] / pc, D[i]["l"] / pc, D[i]["c"] / pc))
    return pool


def pct(a, q):
    return a[min(len(a) - 1, int(q * len(a)))]


def net_R(R, entry, exit_price, risk, qty, product, exchange):
    """R after the round-trip charges for qty shares bought at entry and sold at exit_price."""
    return R - charges(entry * qty, exit_price * qty, product, exchange)["total"] / (risk * qty)


def simulate(pool, entry, stop, target, k, last, replays=10000, seed=19, window=2,
             qty=1, product="MIS", exchange="NSE", nbars=SESSION_BARS):
    """Monte Carlo replays of bars k+1..last for a buy. Same draws and order as the original.
    nbars = the plan day's session length (72 for a CAS day): slots are kept in 1..nbars-1."""
    risk = entry - stop
    r_target = round((target - entry) / risk, 4)

    def slot(rng, i):
        return i if window == 0 else min(nbars - 1, max(1, i + rng.randint(-window, window)))

    rng = random.Random(seed)
    choice = rng.choice
    outcomes, closes, paths = [], [], []
    for n in range(replays):
        p = entry
        outcome = None
        keep = n < SAMPLE_PATHS
        path = [entry]
        for i in range(k + 1, last + 1):
            o, h, l, c = choice(pool[slot(rng, i)])
            hi, lo, cl = p * h, p * l, p * c
            if outcome is None:
                if lo <= stop:
                    outcome = "stop"        # both inside one bar: count the loss (conservative)
                elif hi >= target:
                    outcome = "target"
            p = cl
            if keep:
                path.append(round(p, 2))
        outcomes.append(outcome or "close")
        closes.append(p)
        if keep:
            paths.append(path)
    cnt = {x: outcomes.count(x) for x in ("target", "stop", "close")}
    green = sum(1 for o, p in zip(outcomes, closes) if o == "target" or (o == "close" and p > entry))
    Rs = [r_target if o == "target" else -1.0 if o == "stop" else (p - entry) / risk
          for o, p in zip(outcomes, closes)]
    exits = [target if o == "target" else stop if o == "stop" else p for o, p in zip(outcomes, closes)]
    Rn = [net_R(R, entry, x, risk, qty, product, exchange) for R, x in zip(Rs, exits)]
    srt = sorted(closes)
    # percentile bands per bar for the fan chart: a second run of 2,000 paths, same seed
    rng = random.Random(seed)
    choice = rng.choice
    allp = []
    for n in range(BAND_PATHS):
        p = entry
        path = [entry]
        for i in range(k + 1, last + 1):
            o, h, l, c = choice(pool[slot(rng, i)])
            p = p * c
            path.append(p)
        allp.append(path)
    bands = []
    for j in range(last - k + 1):
        col = sorted(pp[j] for pp in allp)
        bands.append([round(pct(col, q), 2) for q in BAND_Q])
    lo_, hi_ = pct(srt, 0.01), pct(srt, 0.99)
    W = (hi_ - lo_) / HIST_BINS
    bins = [0] * HIST_BINS
    for c in srt:
        b = int((c - lo_) / W) if W > 0 else 0
        if 0 <= b < HIST_BINS:
            bins[b] += 1
    return {
        "replays": replays, "seed": seed, "window": window, "bars_left": last - k,
        "p_target": round(cnt["target"] / replays, 4), "p_stop": round(cnt["stop"] / replays, 4),
        "p_neither": round(cnt["close"] / replays, 4), "p_green": round(green / replays, 4),
        "median_close": round(pct(srt, 0.5), 2), "p5_close": round(pct(srt, 0.05), 2),
        "p95_close": round(pct(srt, 0.95), 2), "avg_R": round(statistics.mean(Rs), 3),
        "avg_R_net": round(statistics.mean(Rn), 3),
        "r_at_target": r_target, "band_paths": BAND_PATHS, "bands": bands, "sample_paths": paths,
        "hist": {"lo": round(lo_, 2), "hi": round(hi_, 2), "bins": bins},
    }


# ---------------------------------------------------------------- stop rules and the history replay

def opening_range(D, n):
    """(high, low) of the first n bars of a session given as {slot: bar} or a list."""
    rows = [D[i] for i in range(n)]
    return max(b["h"] for b in rows), min(b["l"] for b in rows)


def rule_stop(rule, oh, ol, entry):
    """A buy's stop, rounded DOWN to the tick (away from the entry)."""
    r = rule["stop_rule"]
    if r == "or_mid":
        return round_to_tick((oh + ol) / 2, "down")
    if r == "or_low":
        return round_to_tick(ol, "down")
    return round_to_tick(entry * (1 - rule["stop_pct"] / 100.0), "down")


def history_replay(sessions, rule, last, qty_rule, product="MIS", exchange="NSE"):
    """The plan's rule on every past full session: entry = the first bar after the
    opening range to close above it (up to the --until bar), then stop first, then
    target, bar by bar, else the close at --until. qty_rule(entry, risk) -> shares,
    for the net R."""
    n = rule["or_minutes"] // 5
    trades = []
    for d, D in sessions:
        oh, ol = opening_range(D, n)
        L = min(last, len(D) - 1)      # a CAS day's session ends at 15:15
        kk = next((i for i in range(n, L + 1) if D[i]["c"] > oh), None)
        if kk is None:
            continue
        e = round_to_tick(D[kk]["c"])
        s = rule_stop(rule, oh, ol, e)
        risk = e - s
        if risk <= 0:
            continue
        t = round_to_tick(e + rule["target_r"] * risk, "down")
        oc = None
        for b in D[kk + 1:L + 1]:
            if b["l"] <= s:
                oc = "stop"
                break
            if b["h"] >= t:
                oc = "target"
                break
        cl = D[L]["c"]
        R = (t - e) / risk if oc == "target" else -1.0 if oc == "stop" else (cl - e) / risk
        x = t if oc == "target" else s if oc == "stop" else cl
        q = qty_rule(e, risk)
        trades.append({"date": d, "time": clock(OPEN_MIN + 5 * (kk + 1)), "entry": e, "stop": s, "target": t,
                       "outcome": oc or "close", "R": round(R, 2), "qty": q,
                       "R_net": round(net_R(R, e, x, risk, q, product, exchange), 2)})
    return {
        "sessions": len(sessions), "triggered": len(trades),
        "target": sum(1 for x in trades if x["outcome"] == "target"),
        "stop": sum(1 for x in trades if x["outcome"] == "stop"),
        "close": sum(1 for x in trades if x["outcome"] == "close"),
        "green": sum(1 for x in trades if x["R"] > 0),
        "avg_R": round(statistics.mean(x["R"] for x in trades), 3) if trades else None,
        "avg_R_net": round(statistics.mean(x["R_net"] for x in trades), 3) if trades else None,
        "rule": rule, "trades": trades,
    }


# ---------------------------------------------------------------- the plan

def _num(v, what):
    try:
        x = float(v)
    except (TypeError, ValueError):
        raise TMError("the plan needs a number for %s, got %r" % (what, v))
    if not x > 0 or math.isinf(x):
        raise TMError("the plan's %s must be a positive price, got %r" % (what, v))
    return x


def load_plan(a, now_utc):
    """The plan from --plan (trader-in/plan.json) or from the flags, checked and normalized."""
    if a.symbol:
        missing = [f for f in ("entry", "stop", "target", "entry_time") if getattr(a, f) is None]
        if missing:
            raise TMError("with --symbol also give %s" % ", ".join("--" + m.replace("_", "-") for m in missing))
        raw = {"symbol": a.symbol, "exchange": a.exchange, "side": "buy",
               "date": a.date or to_ist(now_utc).date().isoformat(),
               "entry": a.entry, "stop": a.stop, "target": a.target, "entry_time": a.entry_time,
               "setup": a.setup, "opening_range": {"minutes": a.or_minutes}, "stop_rule": a.stop_rule,
               "interval": "5m", "flat_by": a.flat_by, "product": a.product}
        src = "flags"
    else:
        if not os.path.exists(a.plan):
            raise TMError("no plan at %s: run /india-exit first, or pass --symbol --date --entry --stop "
                          "--target --entry-time" % a.plan)
        try:
            with open(a.plan, encoding="utf-8") as fh:
                raw = json.load(fh)
        except ValueError as e:
            raise TMError("%s is not valid JSON: %s" % (a.plan, e))
        if not isinstance(raw, dict):
            raise TMError("%s is not a plan" % a.plan)
        src = a.plan
    return normalize_plan(raw, src)


def normalize_plan(raw, src="flags"):
    """A plan dict (trader-in/plan.json shape) -> the checked plan the time machine runs."""
    sym, exchange = parse_symbol(raw.get("symbol"), raw.get("exchange"))
    side = str(raw.get("side") or "buy").lower()
    if side in ("sell", "short"):
        raise TMError("this pack is long only (allow_short is false): the time machine replays buys")
    if side != "buy":
        raise TMError("side must be buy, got %r" % raw.get("side"))
    currency = str(raw.get("currency") or "INR").upper()
    if currency != "INR":
        raise TMError("the plan is in %s; NSE/BSE plans are in INR" % currency)
    interval = str(raw.get("interval") or "5m")
    if interval != "5m":
        raise TMError("5-minute bars only (the plan says %s): Yahoo keeps 60 days of 5-min bars but only about "
                      "7 days of 1-min bars, too few sessions for a fair replay pool" % interval)
    try:
        date = datetime.date.fromisoformat(str(raw.get("date") or "")).isoformat()
    except ValueError:
        raise TMError("the plan needs a date like 2026-10-19, got %r" % raw.get("date"))
    entry, stop, target = (_num(raw.get(k), k) for k in ("entry", "stop", "target"))
    if not stop < entry < target:
        raise TMError("a buy needs stop < entry < target (got stop %s, entry %s, target %s)" % (stop, entry, target))
    k = None
    kb = None
    if raw.get("entry_bar") is not None:
        try:
            kb = int(raw["entry_bar"])
        except (TypeError, ValueError):
            raise TMError("entry_bar must be a bar number, got %r" % raw["entry_bar"])
    if raw.get("entry_time"):
        mins = parse_clock(raw["entry_time"], "entry_time")
        if (mins - OPEN_MIN) % 5:
            raise TMError("entry_time %s is not on the 5-minute grid (use the bar's close, e.g. 09:50)"
                          % raw["entry_time"])
        k = (mins - OPEN_MIN) // 5 - 1
        if kb is not None and kb != k:
            if kb == k + 1:      # the entry bar's START time was given: accept it, the bar number agrees
                k = kb
            else:
                raise TMError("the plan disagrees with itself: entry_bar %d but entry_time %s is bar %d"
                              % (kb, raw["entry_time"], k))
    if kb is not None:
        k = kb
    if k is None:
        raise TMError("the plan needs entry_time (the entry bar's close, e.g. 09:50) or entry_bar")
    if not 0 <= k <= SESSION_BARS - 2:
        raise TMError("the entry must close between 09:20 and 15:25 IST to leave bars to replay "
                      "(got %s)" % clock(OPEN_MIN + 5 * (k + 1)))
    orng = raw.get("opening_range") or {}
    or_min = orng.get("minutes", 15) if isinstance(orng, dict) else 15
    try:
        or_min = int(or_min)
    except (TypeError, ValueError):
        raise TMError("opening_range.minutes must be a number")
    if or_min < 5 or or_min % 5 or or_min >= 375:
        raise TMError("opening_range.minutes must be a multiple of 5 (got %s)" % or_min)
    product = str(raw.get("product") or "MIS").upper()
    if product not in ("MIS", "CNC"):
        raise TMError("product must be MIS or CNC, got %r" % raw.get("product"))
    qty = None
    for key in ("qty", "quantity", "shares"):
        if raw.get(key) is not None:
            try:
                qty = int(raw[key])
            except (TypeError, ValueError):
                raise TMError("the plan's %s must be a whole number of shares" % key)
            break
    risk = entry - stop
    out = {
        "symbol": sym, "exchange": exchange, "currency": "INR", "side": side, "date": date,
        "setup": str(raw.get("setup") or "orb"),
        "entry": entry, "entry_time": clock(OPEN_MIN + 5 * (k + 1)), "entry_bar": k,
        "stop": stop, "target": target, "risk_per_share": round(risk, 2),
        "target_r": round((target - entry) / risk, 2),
        "opening_range": dict(orng, minutes=or_min) if isinstance(orng, dict) else {"minutes": or_min},
        "stop_rule": str(raw.get("stop_rule") or "auto"), "interval": "5m",
        "tick": raw.get("tick") if raw.get("tick") is not None else tick_size(entry),
        "product": product, "flat_by": raw.get("flat_by"), "qty": qty, "from": src,
    }
    return out


def resolve_rule(plan, entry_day):
    """The rule the history replay uses: orb, the plan's opening range, its stop rule
    (or_mid, or_low, or 'pct' = the plan's stop distance as a percent of the entry),
    and the plan's target R. 'auto' works the stop rule out from the plan day's own
    opening range. None when the setup is not orb."""
    if plan["setup"].lower() != "orb":
        return None
    n = plan["opening_range"]["minutes"] // 5
    rule = {"setup": "orb", "or_minutes": plan["opening_range"]["minutes"], "stop_rule": plan["stop_rule"],
            "target_r": plan["target_r"],
            "stop_pct": round(100.0 * plan["risk_per_share"] / plan["entry"], 4)}
    half = tick_size(plan["stop"]) / 2
    if rule["stop_rule"] == "auto":
        rule["stop_rule"] = "pct"
        if all(i in entry_day for i in range(n)):
            oh, ol = opening_range(entry_day, n)
            if abs(round_to_tick((oh + ol) / 2, "down") - plan["stop"]) < half:
                rule["stop_rule"] = "or_mid"
            elif abs(round_to_tick(ol, "down") - plan["stop"]) < half:
                rule["stop_rule"] = "or_low"
    elif rule["stop_rule"] not in ("or_mid", "or_low"):
        rule["stop_rule"] = "pct"
    if rule["stop_rule"] != "pct":
        del rule["stop_pct"]
    return rule


def resolve_qty(plan, qty=None, risk_inr=None):
    """(shares, how). --qty, else --risk-inr, else the plan's qty, else Rs 500 of risk at the stop."""
    if qty is not None:
        if qty < 1:
            raise TMError("--qty must be at least 1 share")
        return qty, "%d shares (--qty)" % qty
    if risk_inr is None and plan.get("qty"):
        return plan["qty"], "%d shares (the plan's quantity)" % plan["qty"]
    budget = DEFAULT_RISK_INR if risk_inr is None else risk_inr
    if budget <= 0:
        raise TMError("--risk-inr must be a positive rupee amount")
    n = int(math.floor(budget / plan["risk_per_share"] + 1e-9))
    if n < 1:
        return 1, "1 share (%s of risk does not cover one share's %s risk)" % (inr(budget), inr(plan["risk_per_share"]))
    return n, "%d shares (%s risk at the stop / %s a share)" % (n, inr(budget), inr(plan["risk_per_share"]))


# ---------------------------------------------------------------- the run

def lean(mc):
    """(gap, share, break_even): the target's share of the replays that hit one side,
    minus the share that breaks even at the target's R. None when nothing hit a side."""
    pt, ps = mc["p_target"], mc["p_stop"]
    if pt + ps <= 0:
        return None
    r = mc.get("r_at_target") or 1.0
    share, even = pt / (pt + ps), 1.0 / (1.0 + r)
    return share - even, share, even


def verdict(mc):
    """One line from the numbers, see the docstring for the thresholds."""
    pt, ps = mc["p_target"], mc["p_stop"]
    until = mc.get("until") or DEFAULT_FLAT_BY
    lv = lean(mc)
    if lv is None:
        return "No replay reached the target or the stop by %s: the price at %s decides every one." % (until, until)
    d, share, even = lv
    r = mc.get("r_at_target") or 1.0
    one_r = abs(r - 1.0) < 0.005
    if abs(d) < COIN_FLIP:
        head = "Close to a coin flip" if one_r else "Close to break-even"
    elif abs(d) < STRONG:
        head = "Leans toward the target" if d > 0 else "Leans toward the stop"
    else:
        head = "Strongly toward the target" if d > 0 else "Strongly toward the stop"
    odds = "target first %.1f %% vs stop first %.1f %%" % (100 * pt, 100 * ps)
    if one_r:
        return "%s: %s." % (head, odds)
    return ("%s: at %gR the target has to come first in %.0f %% of the replays that hit one side; "
            "it did in %.0f %% (%s)." % (head, round(r, 2), 100 * even, 100 * share, odds))


def verdict_notes(mc):
    """The lines printed right below the verdict, so nothing reads as one-sided."""
    notes = []
    until = mc.get("until") or DEFAULT_FLAT_BY
    pn = mc.get("p_neither", 0)
    if pn > MANY_NEITHER and lean(mc) is not None:
        notes.append("%.1f %% of the replays hit neither by %s; the price at %s decides those."
                     % (100 * pn, until, until))
    lv = lean(mc)
    avg = mc.get("avg_R")
    if lv and abs(lv[0]) >= COIN_FLIP and avg is not None and avg != 0 and (avg > 0) != (lv[0] > 0):
        notes.append("Average R is %+.3f all the same: the replays that hit neither pull it the other way." % avg)
    net = mc.get("avg_R_net")
    if avg is not None and net is not None and avg > 0 and net <= 0:
        notes.append("After charges the average R is %+.3f: on %d shares the round trip costs about %.2fR a trade."
                     % (net, mc.get("qty", 0), mc.get("charges_R", 0)))
    return notes


def time_machine(text, plan, now_utc, replays=10000, seed=19, window=2, until=None, source="yahoo",
                 qty=None, risk_inr=None, cas=None, cas_how=None, kite_bars=None):
    """Chart JSON text + a normalized plan -> the timemachine.json document.
    cas: True / False / None (unknown: the stricter CAS rules). kite_bars: bars from
    read_kite_bars that replace the plan day's Yahoo bars; the history stays on Yahoo."""
    if cas_how is None:
        cas_how = {True: "given", False: "given", None: "unknown: using the stricter CAS rules"}[cas]
    if not 1 <= replays <= 1000000:
        raise TMError("--replays must be between 1 and 1,000,000")
    if not 0 <= window <= 10:
        raise TMError("--window must be between 0 and 10 bars")
    sym, exchange = plan["symbol"], plan["exchange"]
    ysym = yahoo_symbol(sym, exchange)
    result = chart_result(text, ysym)
    meta, bars, dropped = parse_bars(result, now_utc)
    msym = str(meta.get("symbol") or "").upper()
    if msym and msym != ysym:
        raise TMError("the data is for %s but the plan is for %s" % (msym, ysym))
    gran = meta.get("dataGranularity")
    if gran and gran != "5m":
        raise TMError("the data has %s bars; the time machine needs 5-minute bars" % gran)
    if not bars:
        raise TMError("no complete 5-minute bars for %s in the data" % ysym)
    k = plan["entry_bar"]
    rules = session_rules(cas, plan["date"])
    close_min = parse_clock(rules["close"], "close")
    sq_min = parse_clock(rules["square_off"], "square_off")
    nbars = rules["bars_5m"]
    warn = []
    if until is not None:
        until_min = parse_clock(until, "--until")
        until_from = "--until"
    else:
        fb = plan.get("flat_by")
        if fb in (None, "", "auto"):
            until_min, until_from = parse_clock(rules["flat_by_default"], "flat_by"), "default"
        else:
            until_min, until_from = parse_clock(fb, "flat_by"), "plan"
            if plan["product"] == "MIS" and until_min >= sq_min:
                warn.append("the plan's flat_by %s is at or after Zerodha's %s MIS square-off for %s: replaying to "
                            "%s instead" % (clock(until_min), rules["square_off"], sym, rules["flat_by_default"]))
                until_min, until_from = parse_clock(rules["flat_by_default"], "flat_by"), "default"
    if (until_min - OPEN_MIN) % 5 or until_min > close_min:
        raise TMError("--until must be on the 5-minute grid and no later than %s, the end of continuous trading "
                      "for %s on %s (%s)" % (rules["close"], sym, plan["date"], rules["label"]))
    last = (until_min - OPEN_MIN) // 5 - 1
    if k > nbars - 2:
        raise TMError("the entry at %s leaves no bars to replay: continuous trading ends at %s (%s)"
                      % (plan["entry_time"], rules["close"], rules["label"]))
    if last <= k:
        raise TMError("--until %s leaves nothing to replay after the %s entry" % (clock(until_min),
                                                                             plan["entry_time"]))
    if plan["product"] == "MIS" and until_min >= sq_min:
        warn.append("replaying to %s, at or after %s: Zerodha squares MIS positions off at %s for this stock "
                    "(Rs 50 + GST an order), so an MIS trade cannot be held that long"
                    % (clock(until_min), rules["square_off"], rules["square_off"]))
    days = split_sessions(bars)
    today_src = "Yahoo, free, can lag"
    if kite_bars:
        mine = [b for b in kite_bars if b["date"] == plan["date"]]
        other = sorted({b["date"] for b in kite_bars if b["date"] != plan["date"]})
        if other:
            warn.append("Kite candles for %s ignored: --kite-bars only replaces the plan day's bars, the "
                        "history stays on Yahoo" % ", ".join(other))
        if mine:
            days[plan["date"]] = {b["slot"]: b for b in mine}
            today_src = "Kite candles"
        else:
            warn.append("the Kite candles have no bars for %s: the plan day's bars come from Yahoo" % plan["date"])
    entry_day = days.get(plan["date"], {})
    if k not in entry_day:
        wd = datetime.date.fromisoformat(plan["date"]).weekday()
        if entry_day:
            reason = "the data stops at the %s bar" % entry_day[max(entry_day)]["start"]
        elif plan["date"] in holidays():
            reason = "an NSE trading holiday"
        elif wd >= 5:
            reason = "a %s, the market is closed" % WEEKDAYS[wd]
        else:
            reason = "no bars for that day"
        raise TMError("the entry is not in the data: no complete %s bar (closing %s) on %s for %s (%s)"
                      % (clock(OPEN_MIN + 5 * k), plan["entry_time"], plan["date"], ysym, reason))
    eb = entry_day[k]
    tk = tick_size(plan["entry"])
    if not eb["l"] - tk <= plan["entry"] <= eb["h"] + tk:
        warn.append("the plan's entry %.2f is outside that bar's range %.2f to %.2f (%s)"
                    % (plan["entry"], eb["l"], eb["h"], today_src))
    sessions, short, filled, auction = full_sessions(days, plan["date"], cas)
    if len(sessions) < MIN_SESSIONS:
        raise TMError("only %d full session(s) of %s before %s in the data; the time machine needs at least %d"
                      % (len(sessions), sym, plan["date"], MIN_SESSIONS))
    shares, how = resolve_qty(plan, qty, risk_inr)
    flat = charges(plan["entry"] * shares, plan["entry"] * shares, plan["product"], exchange)
    pool = build_pool(sessions)
    mc = simulate(pool, plan["entry"], plan["stop"], plan["target"], k, last, replays, seed, window,
                  shares, plan["product"], exchange, nbars)
    mc = dict(mc, until=clock(until_min), until_from=until_from, pool_sessions=len(sessions), pool_from=sessions[0][0],
              pool_to=sessions[-1][0], qty=shares, qty_from=how, product=plan["product"],
              charges_round_trip=flat["total"], charges_parts=flat,
              charges_R=round(flat["total"] / (plan["risk_per_share"] * shares), 3),
              band_times=[clock(OPEN_MIN + 5 * (j + 1)) for j in range(k, last + 1)])
    rule = resolve_rule(plan, entry_day)
    plan_out = {kk: v for kk, v in plan.items() if v is not None}
    if rule:
        plan_out["stop_rule"] = rule["stop_rule"]
        # the history sizes each past trade the same way: the stated qty, or the same rupee risk
        if qty is not None or (risk_inr is None and plan.get("qty")):
            qrule = lambda e, r: shares
        else:
            budget = DEFAULT_RISK_INR if risk_inr is None else risk_inr
            qrule = lambda e, r: max(1, int(math.floor(budget / r + 1e-9)))
        history = history_replay(sessions, rule, last, qrule, plan["product"], exchange)
    else:
        history = {"sessions": len(sessions), "triggered": 0, "rule": None, "trades": [],
                   "note": "skipped: the history replay knows the opening-range breakout (orb) only, "
                           "this plan's setup is %r" % plan["setup"]}
    last_bar = bars[-1]
    return {
        "created_by": "india-timemachine",
        "created_at": now_utc.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "source": source,
        "data": {"symbol": msym or ysym, "interval": "5m", "range": "60d",
                 "last_bar": "%s %s IST" % (last_bar["date"], last_bar["start"]),
                 "dropped": dropped, "short_sessions_left_out": short,
                 "filled_bars": sum(filled.values()), "filled_sessions": len(filled),
                 "auction_bars": sum(auction.values()), "auction_sessions": len(auction),
                 "history_source": "Yahoo, free, can lag", "plan_day_source": today_src},
        "session": dict(rules, cas_from=cas_how),
        "plan": plan_out,
        "mc": mc,
        "history": history,
        "verdict": verdict(mc),
        "verdict_notes": verdict_notes(mc),
        "warnings": warn,
        "note": ("A simulation built from %s's own last %d full sessions. It is not a prediction: results, "
                 "news and circuit limits can do things those sessions never did. Same seed, same numbers."
                 % (sym, len(sessions))),
    }


# ---------------------------------------------------------------- output

def pc(x):
    return "%.1f %%" % (100.0 * x)


def report(doc):
    p, m, h = doc["plan"], doc["mc"], doc["history"]
    until = m["until"]
    flat = {"plan": "your flat-by; ", "default": "the default flat-by; "}.get(m.get("until_from"), "")
    ses = doc.get("session") or {}
    wd = WEEKDAYS[datetime.date.fromisoformat(p["date"]).weekday()]
    r = p["target_r"]
    lines = [
        "%s (%s) buy, %s %s: entry %.2f at %s (bar %d), stop %.2f, target %.2f (%sR, risk %s a share)."
        % (p["symbol"], p["exchange"], wd, p["date"], p["entry"], p["entry_time"], p["entry_bar"], p["stop"],
           p["target"], ("%g" % r), inr(p["risk_per_share"])),
        "Session: %s (%s)." % (ses.get("label", "?"), ses.get("cas_from", "?")),
        "Data: the plan day's bars (%s); the %d-session history (%s)."
        % (doc["data"].get("plan_day_source", "Yahoo, free, can lag"), m["pool_sessions"],
           doc["data"].get("history_source", "Yahoo, free, can lag")),
        "%s replays of the rest of the session, %s to %s IST (%s%d five-minute bars), seed %d."
        % ("{:,}".format(m["replays"]), p["entry_time"], until, flat, m["bars_left"], m["seed"]),
        "Each bar is drawn whole from %s's own 5-min bars at the same time of day (+-%d bars) over its last "
        "%d full sessions (%s to %s)." % (p["symbol"], m["window"], m["pool_sessions"], m["pool_from"], m["pool_to"]),
        "",
        "  target first   %s" % pc(m["p_target"]),
        "  stop first     %s" % pc(m["p_stop"]),
        "  neither        %-8s still open at %s" % (pc(m["p_neither"]), until),
        "  green          %-8s target first, or above the entry at %s" % (pc(m["p_green"]), until),
        "",
        "  Price at %s: median %.2f, 90 %% of replays between %.2f and %.2f."
        % (until, m["median_close"], m["p5_close"], m["p95_close"]),
        "  Average R: %+.3f gross (R = the %s risk: +%gR at the target, -1R at the stop)."
        % (m["avg_R"], inr(p["risk_per_share"]), m["r_at_target"]),
        "  Average R: %+.3f net of charges, for %s: about %s a round trip (%s, %s) = %.3fR."
        % (m["avg_R_net"], m["qty_from"], inr(m["charges_round_trip"]), m["product"], p["exchange"],
           m["charges_R"]),
        "",
    ]
    if h.get("rule"):
        ru = h["rule"]
        stop_words = {"or_mid": "opening-range midpoint", "or_low": "opening-range low"}.get(
            ru["stop_rule"], "%.2f %% below the entry" % ru.get("stop_pct", 0))
        if h["triggered"]:
            lines.append("History: the same rule (%d-min opening range, stop at the %s, %gR target) on each of "
                         "those %d real sessions fired %d times: %d target, %d stop, %d neither. "
                         "Average R %+.3f gross, %+.3f net of charges."
                         % (ru["or_minutes"], stop_words, ru["target_r"], h["sessions"], h["triggered"],
                            h["target"], h["stop"], h["close"], h["avg_R"], h["avg_R_net"]))
        else:
            lines.append("History: the same rule never fired in those %d sessions." % h["sessions"])
    else:
        lines.append("History: " + h.get("note", "skipped."))
    lines += ["", doc["verdict"]] + list(doc.get("verdict_notes") or []) + [""]
    for w in doc.get("warnings") or []:
        lines.append("Note: " + w + ".")
    if doc["data"]["short_sessions_left_out"]:
        lines.append("Left out %d short session(s) (Muhurat trading or gaps): %s."
                     % (len(doc["data"]["short_sessions_left_out"]),
                        ", ".join(x["date"] for x in doc["data"]["short_sessions_left_out"])))
    if doc["data"].get("auction_sessions"):
        lines.append("Closing auction: on %d session(s) from 2026-08-03 the 15:15 to 15:30 bars are the auction, "
                     "not trading; they are left out, not filled." % doc["data"]["auction_sessions"])
    if doc["data"].get("filled_bars"):
        lines.append("Real gaps: filled %d empty 5-min bar(s) in %d session(s) flat at the previous close."
                     % (doc["data"]["filled_bars"], doc["data"]["filled_sessions"]))
    lines.append("Charges are Zerodha's list as of 2026-10 (brokerage, STT, exchange, SEBI, stamp duty, GST): "
                 "check your broker's.")
    lines.append(doc["note"] + " Repeat this run with --seed %d." % m["seed"])
    return lines


def render_chart(doc, height=17):
    """ASCII fan chart of the bands: one column per 5-min bar."""
    p, m = doc["plan"], doc["mc"]
    bands, times = m["bands"], m["band_times"]
    lo = min([b[0] for b in bands] + [p["stop"], p["target"]])
    hi = max([b[4] for b in bands] + [p["stop"], p["target"]])
    pad = (hi - lo) * 0.04 or 0.01
    lo, hi = lo - pad, hi + pad
    step = (hi - lo) / (height - 1)

    def row(price):
        return max(0, min(height - 1, int(round((hi - price) / step))))
    grid = [[" "] * len(bands) for _ in range(height)]
    for j, b in enumerate(bands):
        r5, r25, r50, r75, r95 = [row(x) for x in b]
        for r in range(r95, r5 + 1):
            grid[r][j] = "."
        for r in range(r75, r25 + 1):
            grid[r][j] = ":"
        grid[r50][j] = "*"
    marks = {row(p["target"]): "target", row(p["stop"]): "stop"}
    if row(p["entry"]) not in marks:
        marks[row(p["entry"])] = "entry"
    for r in (row(p["target"]), row(p["stop"])):
        grid[r] = ["-" if ch == " " else ch for ch in grid[r]]
    out = ["%s %s, %s replay paths from the %s entry (seed %d)" % (
        p["symbol"], p["date"], "{:,}".format(m.get("band_paths", BAND_PATHS)), p["entry_time"], m["seed"])]
    for r in range(height):
        label = marks.get(r)
        price = {"target": p["target"], "stop": p["stop"], "entry": p["entry"]}.get(label, hi - r * step)
        left = ("%9.2f |" % price) if (label or r in (0, height - 1)) else "          |"
        out.append(left + "".join(grid[r]) + ("  " + label if label else ""))
    axis = [" "] * len(bands)
    for j in sorted({0, len(bands) // 2, len(bands) - 1}):
        lab = times[j]
        s = max(0, min(j, len(bands) - len(lab)))
        axis[s:s + len(lab)] = list(lab)
    out.append("          +" + "-" * len(bands))
    out.append("           " + "".join(axis))
    out.append("* median   : middle half of the paths   . 90 % of the paths   - stop and target")
    return out


# ---------------------------------------------------------------- commands

def _write(path, doc):
    d = os.path.dirname(path)
    if d:
        os.makedirs(d, exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(doc, fh, indent=1, ensure_ascii=False)
        fh.write("\n")


def compute(a):
    now = parse_now(a.now)
    plan = load_plan(a, now)
    if a.offline:
        text, source = read_offline(a.offline, plan["symbol"]), "offline"
    else:
        text, source = fetch_chart(yahoo_symbol(plan["symbol"], plan["exchange"])), "yahoo"
    cas, how = cas_status(plan["symbol"], a.cas, read_config(a.config), today=to_ist(now).date(),
                          online=not a.offline)
    kite = read_kite_bars(a.kite_bars, now) if a.kite_bars else None
    return time_machine(text, plan, now, a.replays, a.seed, a.window, a.until, source, a.qty, a.risk_inr,
                        cas, how, kite)


def cmd_run(a):
    doc = compute(a)
    if a.write:
        _write(a.write, doc)
    if a.json:
        slim = dict(doc, mc={k: v for k, v in doc["mc"].items() if k != "sample_paths"})
        print(json.dumps(slim, indent=2, ensure_ascii=False))
    else:
        print("\n".join(report(doc)))
        if a.write:
            print("Wrote %s." % a.write)
        print("Next: /india-budget sizes it to your budget and your max loss per trade, charges included.")
    return 0


def cmd_chart(a):
    if a.from_file:
        if not os.path.exists(a.from_file):
            raise TMError("no %s: run timemachine.py run --write first" % a.from_file)
        try:
            with open(a.from_file, encoding="utf-8") as fh:
                doc = json.load(fh)
            doc["mc"]["bands"]
        except (ValueError, KeyError, TypeError):
            raise TMError("%s is not a time machine result" % a.from_file)
    else:
        doc = compute(a)
    print("\n".join(render_chart(doc)))
    print("\n".join([doc.get("verdict", "")] + list(doc.get("verdict_notes") or [])))
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(description="Time Machine: replay a planned intraday trade (india-timemachine).")
    sub = ap.add_subparsers(dest="cmd")

    def common(p):
        p.add_argument("--plan", default=os.path.join("trader-in", "plan.json"), help="the plan from india-exit")
        p.add_argument("--symbol", help="or give the plan as flags: NSE symbol (RELIANCE, M&M, RELIANCE.BO)")
        p.add_argument("--exchange", choices=["NSE", "BSE"], help="default NSE (or from the symbol)")
        p.add_argument("--date", help="the plan's day, YYYY-MM-DD (default today, IST)")
        p.add_argument("--entry", type=float)
        p.add_argument("--stop", type=float)
        p.add_argument("--target", type=float)
        p.add_argument("--entry-time", help='the entry bar\'s close in IST, e.g. "09:50"')
        p.add_argument("--setup", default="orb")
        p.add_argument("--or-minutes", type=int, default=15)
        p.add_argument("--stop-rule", default="auto", choices=["auto", "or_mid", "or_low", "pct"])
        p.add_argument("--flat-by", help="with --symbol: the plan's flat-by time (default auto: 15:00 CAS, 15:10)")
        p.add_argument("--product", default="MIS", choices=["MIS", "CNC"], help="with --symbol: for the charges")
        p.add_argument("--qty", type=int, help="shares, for the charges (default: Rs 500 of risk at the stop)")
        p.add_argument("--risk-inr", type=float, help="rupees of risk at the stop; qty = risk / risk a share")
        p.add_argument("--cas", choices=["yes", "no"], help="override the NSE F&O list: is this a CAS stock?")
        p.add_argument("--config", default=os.path.join("trader-in", "config.json"),
                       help="for cas_symbols_extra / non_cas_symbols")
        p.add_argument("--kite-bars", action="append", metavar="FILE",
                       help="Kite get_historical_data JSON (5minute) for the plan day only; repeatable")
        p.add_argument("--replays", type=int, default=10000)
        p.add_argument("--seed", type=int, default=19)
        p.add_argument("--window", type=int, default=2, help="draw from the same time of day +- this many bars")
        p.add_argument("--until", help="end each replay at this IST time (default the plan's flat_by, else 15:00 CAS / 15:10)")
        p.add_argument("--offline", metavar="FILE", help="a saved Yahoo chart JSON (.json or .json.gz), no network")
        p.add_argument("--now", help='pretend it is this time ("2026-10-19 09:55" IST, or ISO with Z)')

    p = sub.add_parser("run", help="replay the plan: 10,000 simulated sessions plus the history")
    common(p)
    p.add_argument("--json", action="store_true")
    p.add_argument("--write", nargs="?", const=os.path.join("trader-in", "timemachine.json"), metavar="PATH",
                   help="save the result (default trader-in/timemachine.json)")
    p = sub.add_parser("chart", help="ASCII fan chart of the replays")
    common(p)
    p.add_argument("--from", dest="from_file", nargs="?", const=os.path.join("trader-in", "timemachine.json"),
                   metavar="PATH", help="draw a saved result instead of running again")
    a = ap.parse_args(argv)
    if not a.cmd:
        ap.print_help()
        return 1
    try:
        return {"run": cmd_run, "chart": cmd_chart}[a.cmd](a)
    except TMError as e:
        print("timemachine.py: %s" % e, file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
