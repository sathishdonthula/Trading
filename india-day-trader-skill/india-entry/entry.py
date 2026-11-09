#!/usr/bin/env python3
"""Entry Timer for india-entry: watches a setup bar by bar on NSE / BSE and
says the moment to get in. Standard library only (Python 3.9+). Long side
only. It never places an order.

    python3 entry.py check RELIANCE                          # opening-range breakout, 5-minute bars
    python3 entry.py check RELIANCE --now "2026-10-19 09:50" --offline RELIANCE-60d5m.json.gz
    python3 entry.py check RELIANCE --setup level --level 1230    # first close above a price
    python3 entry.py check RELIANCE --interval 1m --min-volume-x 1.5 --write
    python3 entry.py check BSE:RELIANCE                      # the BSE line (also RELIANCE.BO or 500325)
    python3 entry.py watch RELIANCE --every 30 --until 14:45  # one line per closed bar
    python3 entry.py watchlist                                # every hit in trader-in/watchlist.json

Symbols: an NSE symbol (RELIANCE, M&M, BAJAJ-AUTO) is NSE. RELIANCE.NS or
NSE:RELIANCE is NSE too. RELIANCE.BO, BSE:RELIANCE or a 6-digit BSE code
(500325) is BSE. Yahoo's symbol is SYMBOL.NS or SYMBOL.BO.

Setups (longs only):
  orb    opening-range breakout. The range is the high and low of the first
         --or-minutes (default 15: the 09:15, 09:20 and 09:25 five-minute bars).
         It triggers on the first COMPLETED bar after the range that CLOSES
         above the range high. A wick above the high is not a trigger.
  level  the first completed bar that closes above --level (for example a
         20-day high from india-scanner).
  --min-volume-x X  the trigger bar also needs at least X times the average
         volume of the opening-range bars. Yahoo books no volume on the 09:15
         bar of most NSE stocks, so bars with zero volume are left out of that
         average.

A trigger's time is the trigger bar's CLOSE time (the 09:45 bar closes at
9:50 AM) and its price is that close, to the paisa. Only bars on the interval
grid count (Yahoo's live tick, e.g. 11:24:35, is dropped) and a bar counts
only once it has closed: now >= its start + the interval.

Statuses: triggered; waiting (not open yet, range still forming, or no close
above the level yet); expired (no trigger by no_entries_after, default 14:45);
no-data (the market is closed today or Yahoo sent no bars).

Market (IST, UTC+05:30, no daylight saving): pre-open 09:00-09:08, normal
session 09:15-15:30, no early closes. Holidays: the NSE trading-holiday list,
fetched from nseindia.com and cached in trader-in/holidays.json (refreshed
weekly), with the 2026 list built in as a fallback. --offline never touches
the network. Diwali Muhurat trading is a special evening session: not covered.

trader-in/config.json (optional): "no_entries_before" (09:30) and
"no_entries_after" (14:45), IST.

watch wakes just after each bar closes (bars only change then), re-polls every
--every seconds (default 30) while Yahoo has not posted the new bar, prints one
line per closed bar and stops on a trigger (exit 0, writes trader-in/entry.json),
on expiry, at the close or at --until (exit 1). With --now or --offline it runs
on a simulated clock and replays the day without sleeping.

Data: Yahoo's free chart JSON (unofficial, can lag, bars not single trades)
  https://query1.finance.yahoo.com/v8/finance/chart/RELIANCE.NS?range=1d&interval=5m
  falls back to query2 on HTTP 429 or 5xx; a 429 gets one retry after a pause.
  --offline PATH reads a saved chart JSON (.json or .json.gz) instead, or
  PATH/<SYMBOL>.json when PATH is a folder. Without --now, an offline file is
  read as of its own time (meta.regularMarketTime).
  If the Zerodha Kite MCP is connected in Claude, Claude may read a live last
  price there; this script never calls it and runs on free Yahoo data alone.

Exit codes: check 0 = triggered, 1 = anything else or an error. watch 0 on a
trigger, 1 otherwise. watchlist 0 when at least one symbol triggered.
"""

import argparse
import csv
import datetime
import gzip
import http.cookiejar
import io
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

UA = "Mozilla/5.0 (compatible; india-day-trader-skill/1.0; +https://github.com/)"
BROWSER_UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/130 Safari/537.36")
PAUSE = 0.5      # seconds between requests to the free endpoint
BACKOFF = 5.0    # seconds before the one retry after an HTTP 429
GRACE = 2        # seconds after a bar closes before watch asks for it
SETTLE = 60      # seconds a closed bar may wait for Yahoo's data to catch up
HOSTS = ("query1", "query2")
INTERVALS = {"1m": 60, "5m": 300}
DEFAULTS = {"no_entries_before": "09:30", "no_entries_after": "14:45"}
EPOCH = datetime.datetime(1970, 1, 1)
IST = datetime.timezone(datetime.timedelta(hours=5, minutes=30))
IST_OFFSET = datetime.timedelta(hours=5, minutes=30)
HOLIDAY_URL = "https://www.nseindia.com/api/holiday-master?type=trading"
HOLIDAY_CACHE = "trader-in/holidays.json"
NSE_COOKIE_PAGE = "https://www.nseindia.com/companies-listing/corporate-filings-insider-trading"
HOLIDAY_MAX_AGE = 7 * 86400

# NSE capital-market trading holidays 2026 (NSE CM list, read 2026-10-23). The live list
# from nseindia.com, cached in trader-in/holidays.json, is merged on top of this.
NSE_HOLIDAYS = {
    "2026-01-15": "NSE trading holiday",
    "2026-01-26": "Republic Day",
    "2026-02-15": "Mahashivratri",
    "2026-03-03": "Holi",
    "2026-03-21": "Id-ul-Fitr (Ramzan Id)",
    "2026-03-26": "Shri Ram Navami",
    "2026-03-31": "Shri Mahavir Jayanti",
    "2026-04-03": "Good Friday",
    "2026-04-14": "Dr. Baba Saheb Ambedkar Jayanti",
    "2026-05-01": "Maharashtra Day",
    "2026-05-28": "Bakri Id",
    "2026-06-26": "Muharram",
    "2026-08-15": "Independence Day",
    "2026-09-14": "Ganesh Chaturthi",
    "2026-10-02": "Mahatma Gandhi Jayanti",
    "2026-10-20": "Dussehra",
    "2026-11-08": "Diwali Laxmi Pujan (Muhurat trading session only)",
    "2026-11-10": "Diwali Balipratipada",
    "2026-11-24": "Guru Nanak Jayanti",
    "2026-12-25": "Christmas",
}
CALENDAR_YEARS = {2026}
OPEN = datetime.time(9, 15)
CLOSE = datetime.time(15, 30)
PRE_OPEN = datetime.time(9, 0)


class EntryError(Exception):
    pass


_Err = EntryError


# ---------------------------------------------------------------- time (IST is a fixed UTC+05:30)

def to_ist(utc):
    return utc.replace(tzinfo=None) + IST_OFFSET


def ist_to_utc(local):
    return local.replace(tzinfo=None) - IST_OFFSET


def utc_now():
    return datetime.datetime.now(datetime.timezone.utc).replace(microsecond=0, tzinfo=None)


def utc_of(ts):
    """Unix seconds -> naive UTC, without the machine's time zone."""
    return EPOCH + datetime.timedelta(seconds=int(ts))


def ts_of(utc):
    return int((utc.replace(tzinfo=None) - EPOCH).total_seconds())


def iso(utc):
    return utc.strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_now(text):
    """--now -> naive UTC. "2026-10-19 09:50" (or ...T09:50, or just 09:50 for
    today) is India time; an offset or Z makes it absolute."""
    if not text:
        return utc_now()
    t = text.strip()
    if t.upper().endswith(" IST"):
        t = t[:-4].strip()
    if re.match(r"^\d{1,2}:\d{2}$", t):
        t = "%s %s" % (to_ist(utc_now()).date().isoformat(), t.zfill(5))
    t = t.replace("Z", "+00:00")
    try:
        dt = datetime.datetime.fromisoformat(t)
    except ValueError:
        raise EntryError('--now wants an India time like "2026-10-19 09:50" (or ...Z for UTC)')
    if dt.tzinfo is None:
        return ist_to_utc(dt)
    return (dt - dt.utcoffset()).replace(tzinfo=None)


def parse_hhmm(text, what):
    m = re.match(r"^\s*(\d{1,2}):(\d{2})\s*$", str(text))
    if not m or int(m.group(1)) > 23 or int(m.group(2)) > 59:
        raise EntryError("%s wants an India time like 14:45, not %r" % (what, text))
    return datetime.time(int(m.group(1)), int(m.group(2)))


def hm(t):
    """09:45, 10:10: the contract's wall-clock form."""
    return "%02d:%02d" % (t.hour, t.minute)


def short(t):
    """9:45, 10:10: for sentences."""
    return "%d:%02d" % (t.hour, t.minute)


def ampm(t):
    h = t.hour % 12 or 12
    return "%d:%02d %s" % (h, t.minute, "AM" if t.hour < 12 else "PM")


# ---------------------------------------------------------------- the NSE calendar

def _parse_day(text):
    s = str(text).strip()
    for fmt in ("%Y-%m-%d", "%d-%b-%Y", "%d-%B-%Y", "%d/%m/%Y"):
        try:
            return datetime.datetime.strptime(s, fmt).date().isoformat()
        except ValueError:
            pass
    return None


def holidays_from_doc(doc):
    """{iso date: name} from the NSE answer ({"CM": [{"tradingDate": "26-Jan-2026", ...}]})
    or a cache: {"holidays": {date: name}} or {"holidays": [{"date"|"tradingDate": .., "name"|"description": ..}]}
    or a plain list of dates."""
    out = {}
    if isinstance(doc, dict):
        items = doc.get("holidays")
        if items is None:
            items = doc.get("CM")
        if items is None:
            items = []
    else:
        items = doc
    if isinstance(items, dict):
        items = [{"date": k, "name": v} for k, v in items.items()]
    for it in items or []:
        if isinstance(it, str):
            d, name = _parse_day(it), "NSE trading holiday"
        elif isinstance(it, dict):
            d = _parse_day(it.get("date") or it.get("tradingDate") or "")
            name = it.get("name") or it.get("description") or "NSE trading holiday"
        else:
            continue
        if d:
            out[d] = str(name).strip()
    return out


def _nse_session_get(url, timeout=15):
    """GET an NSE JSON API with a cookie session: a listing page first (for the cookies), then the API."""
    jar = http.cookiejar.CookieJar()
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
    opener.open(urllib.request.Request(NSE_COOKIE_PAGE, headers={      # the home page answers 403 to scripts
        "User-Agent": BROWSER_UA, "Accept": "text/html"}), timeout=timeout).read()
    time.sleep(1.0)
    req = urllib.request.Request(url, headers={"User-Agent": BROWSER_UA, "Accept": "application/json",
                                               "Referer": "https://www.nseindia.com/"})
    with opener.open(req, timeout=timeout) as r:
        return r.read().decode("utf-8", "replace")


def fetch_nse_holidays():
    """The live NSE trading-holiday list ({date: name}). Raises EntryError."""
    try:
        doc = json.loads(_nse_session_get(HOLIDAY_URL))
    except (urllib.error.URLError, OSError, ValueError) as e:
        raise EntryError("NSE's holiday list did not load (%s)" % (e,))
    days = holidays_from_doc(doc)
    if not days:
        raise EntryError("NSE's holiday list came back empty")
    return days


def load_holidays(path=HOLIDAY_CACHE, fetch=True, now_utc=None):
    """Merge the cached (and, when it is missing or a week old, the live) NSE list
    into NSE_HOLIDAYS. Returns a note for the user or ''. Never raises."""
    cached, age = {}, None
    if path and os.path.exists(path):
        try:
            with open(path, encoding="utf-8") as fh:
                cached = holidays_from_doc(json.load(fh))
            age = time.time() - os.path.getmtime(path)
        except (OSError, ValueError):
            cached = {}
    note = ""
    if fetch and (not cached or age is None or age > HOLIDAY_MAX_AGE):
        try:
            live = fetch_nse_holidays()
            cached = live
            if path:
                write_json(path, {"created_by": "india-entry", "source": HOLIDAY_URL,
                                  "fetched_at": iso(now_utc or utc_now()), "holidays": live})
        except EntryError as e:
            note = "%s: using the built-in 2026 list" % e
        except OSError:
            pass
    NSE_HOLIDAYS.update(cached)
    for d in cached:
        CALENDAR_YEARS.add(int(d[:4]))
    return note


def trading_day(d):
    """(is_trading_day, reason). Weekends and the NSE holiday list."""
    if d.weekday() >= 5:
        return False, "weekend"
    name = NSE_HOLIDAYS.get(d.isoformat())
    if name:
        return False, "NSE holiday: " + name
    return True, ""


def session(d, cas=None):
    """(open_time, close_time) in India time, or None if closed all day. The close is the end of
    continuous trading: 15:15 for a CAS (F&O) stock from 2026-08-03 (or when unknown), else 15:30."""
    ok, _ = trading_day(d)
    if not ok:
        return None
    return OPEN, parse_hhmm(session_rules(cas, d)["close"], "close")


def next_open(d):
    nd = d + datetime.timedelta(days=1)
    for _ in range(15):
        if session(nd):
            return "%s 09:15" % nd.isoformat()
        nd += datetime.timedelta(days=1)
    return None


# ---------------------------------------------------------------- symbols

def parse_symbol(text):
    """(symbol, exchange). RELIANCE, RELIANCE.NS, NSE:RELIANCE -> NSE;
    RELIANCE.BO, BSE:RELIANCE, 500325 -> BSE."""
    t = str(text).strip().upper()
    if not t:
        raise EntryError("give a symbol, like RELIANCE")
    exch = "NSE"
    m = re.match(r"^(NSE|BSE)\s*:\s*(.+)$", t)
    if m:
        exch, t = m.group(1), m.group(2).strip()
    elif t.endswith(".NS"):
        t = t[:-3]
    elif t.endswith(".BO"):
        exch, t = "BSE", t[:-3]
    elif re.match(r"^\d{6}$", t):
        exch = "BSE"
    if not re.match(r"^[A-Z0-9&\-_]+$", t):
        raise EntryError("%r does not look like an NSE or BSE symbol" % text)
    return t, exch


def yahoo_symbol(sym, exch="NSE"):
    return "%s.%s" % (sym.upper(), "BO" if exch == "BSE" else "NS")


def chart_path(sym, rng, interval, exch="NSE"):
    return "/v8/finance/chart/%s?range=%s&interval=%s" % (
        urllib.parse.quote(yahoo_symbol(sym, exch), safe=""), rng, interval)


def chart_range(day, today, interval):
    """The smallest Yahoo range that still holds the session of `day`."""
    back = (today - day).days
    if back <= 0:
        return "1d"
    if back <= 6:
        return "5d"
    if interval == "1m":
        raise EntryError("Yahoo keeps 1-minute bars for about a week: use --interval 5m for %s" % day)
    if back <= 28:
        return "1mo"
    if back <= 58:
        return "60d"
    raise EntryError("Yahoo keeps 5-minute bars for about 60 days: %s is too far back" % day)


# ---------------------------------------------------------------- session rules (closing auction) and Kite files
# The same block is copied into india-entry/entry.py and india-exit/exit.py.

CAS_START = datetime.date(2026, 8, 3)       # NSE closing auction session (CAS) 15:15-15:35 for F&O stocks
PREOPEN_NEW = datetime.date(2026, 9, 7)     # pre-open order entry 09:00-09:10 from this day (09:00-09:08 before)
FO_LIST_URL = "https://nsearchives.nseindia.com/content/fo/fo_mktlots.csv"
FO_CACHE = "trader-in/fo_list.txt"
FO_MAX_AGE = 7 * 86400
FO_INDEXES = {"NIFTY", "BANKNIFTY", "FINNIFTY", "MIDCPNIFTY", "NIFTYNXT50", "NIFTYFPI"}
SOURCE_LABEL = {"yahoo": "Yahoo, free, can lag", "kite": "Kite candles", "kite-ltp": "Kite, live",
                "flags": "your price"}


def session_rules(cas, day):
    """The day's session for one stock. cas: True / False / None (unknown -> the stricter CAS rules).
    Zerodha MIS auto square-off: CAS stocks 15:12, others 15:25 (support.zerodha.com, read 2026-10-23)."""
    strict = cas is not False and day >= CAS_START
    return {
        "open": "09:15",
        "close": "15:15" if strict else "15:30",        # end of continuous trading
        "square_off": "15:12" if strict else "15:25",   # Zerodha MIS auto square-off
        "flat_by_default": "15:00" if strict else "15:10",
        "no_entries_after_default": "14:45",
        "bars_5m": 72 if strict else 75,
        "pre_open": "09:00-09:10" if day >= PREOPEN_NEW else "09:00-09:08",
        "cas": cas, "strict": strict,
        "label": ("CAS stock (F&O): continuous trading ends 15:15, Zerodha squares off MIS at 15:12" if strict and cas
                  else "F&O list unknown: using the stricter CAS rules (continuous trading ends 15:15, "
                       "Zerodha squares off MIS at 15:12)" if strict
                  else "non-CAS stock: trades to 15:30, Zerodha squares off MIS at 15:25"),
    }


def _fetch_text(url, timeout=20):
    """NSE archives: the browser user agent first, the pack's own as a fallback."""
    err = None
    for ua in (BROWSER_UA, UA):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": ua, "Accept": "*/*"})
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.read().decode("utf-8", "replace")
        except (urllib.error.URLError, OSError) as e:
            err = e
    raise OSError(str(err))


def parse_fo_list(text):
    """NSE fo_mktlots.csv (UNDERLYING, SYMBOL, lots...) or our cache (one symbol a line) -> set of stock symbols."""
    lines = [x for x in text.splitlines() if x.strip()]
    out = set()
    if lines and lines[0].strip().upper().startswith("UNDERLYING"):
        for row in csv.reader(io.StringIO("\n".join(lines[1:]))):
            if len(row) < 2:
                continue
            und, sym = row[0].strip().upper(), row[1].strip().upper()
            if not sym or und.startswith("NIFTY") or sym in FO_INDEXES or sym == "SYMBOL":
                continue
            out.add(sym)
        return out
    for x in lines:
        x = x.strip()
        if not x.startswith("#"):
            out.add(x.upper())
    return out


def load_fo_list(path=FO_CACHE, fetch=True):
    """(set of F&O stock symbols or None, note). Cache refreshed when older than 7 days."""
    have, age = None, None
    if path and os.path.exists(path):
        try:
            with open(path, encoding="utf-8", errors="replace") as fh:
                text = fh.read()
            have = parse_fo_list(text)
            m = re.match(r"#\s*fetched\s+(\d{4}-\d{2}-\d{2})", text)
            if m:
                age = (datetime.date.today() - datetime.date.fromisoformat(m.group(1))).days * 86400
            else:
                age = time.time() - os.path.getmtime(path)
        except (OSError, ValueError):
            have = None
    if have and (age is not None and age <= FO_MAX_AGE or not fetch):
        return have, ""
    if not fetch:
        return None, "no F&O list (%s) offline: using the stricter CAS rules" % path
    try:
        fresh = parse_fo_list(_fetch_text(FO_LIST_URL))
        if not fresh:
            raise OSError("empty list")
    except OSError as e:
        if have:
            return have, "NSE's F&O list did not refresh (%s): using the cached one" % e
        return None, "NSE's F&O list did not load (%s): using the stricter CAS rules" % e
    try:
        d = os.path.dirname(path)
        if d:
            os.makedirs(d, exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write("# fetched %s from %s\n" % (datetime.date.today().isoformat(), FO_LIST_URL))
            fh.write("\n".join(sorted(fresh)) + "\n")
    except OSError:
        pass
    return fresh, ""


def cas_status(sym, flag=None, cfg=None, fo=None):
    """True (CAS stock), False (not), None (unknown). --cas yes|no wins, then the config's
    cas_symbols_extra / non_cas_symbols, then NSE's F&O list. BSE lines follow the NSE status."""
    if flag:
        f = str(flag).strip().lower()
        if f in ("yes", "y", "true", "1"):
            return True
        if f in ("no", "n", "false", "0"):
            return False
        if f not in ("auto", ""):
            raise _Err("--cas is yes, no or auto, not %r" % flag)
    cfg = cfg or {}
    s = sym.upper()
    if s in [str(x).upper() for x in (cfg.get("non_cas_symbols") or [])]:
        return False
    if s in [str(x).upper() for x in (cfg.get("cas_symbols_extra") or [])]:
        return True
    if fo is None:
        return None
    return s in fo


def _kite_ts(text):
    dt = datetime.datetime.fromisoformat(str(text).replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=datetime.timezone(IST_OFFSET))
    return int((dt - datetime.datetime(1970, 1, 1, tzinfo=datetime.timezone.utc)).total_seconds())


def kite_candles(doc):
    """Kite get_historical_data output -> list of candle dicts, or None when it is not that shape.
    Accepts the MCP list [{"date","open","high","low","close","volume","oi"}], {"data": [...]}
    and the raw API {"data": {"candles": [[date, o, h, l, c, v, oi], ...]}}."""
    if isinstance(doc, dict):
        doc = doc.get("data")
        if isinstance(doc, dict):
            doc = doc.get("candles")
    if not isinstance(doc, list) or (doc and not isinstance(doc[0], (dict, list))):
        return None
    out = []
    for c in doc:
        if isinstance(c, list) and len(c) >= 5:
            c = {"date": c[0], "open": c[1], "high": c[2], "low": c[3], "close": c[4],
                 "volume": c[5] if len(c) > 5 else 0}
        if not isinstance(c, dict) or "date" not in c:
            return None
        out.append(c)
    return out


def kite_chart(paths, sym, exch="NSE"):
    """Merge one or more saved Kite candle files (de-duplicated by date) into the chart shape.
    data_time is when the newest file was saved (its mtime): a candle closing after that was still forming."""
    by_t, saved = {}, None
    for p in paths:
        try:
            with open(p, encoding="utf-8") as fh:
                candles = kite_candles(json.load(fh))
        except (OSError, ValueError) as e:
            raise _Err("could not read the Kite file %s: %s" % (p, e))
        if candles is None:
            raise _Err("%s is not Kite get_historical_data output (a list of candles with a \"date\")" % p)
        m = int(os.path.getmtime(p))
        saved = m if saved is None else max(saved, m)
        for c in candles:
            try:
                t = _kite_ts(c["date"])
                bar = {"t": t, "o": float(c["open"] if c.get("open") is not None else c["close"]),
                       "h": float(c["high"] if c.get("high") is not None else c["close"]),
                       "l": float(c["low"] if c.get("low") is not None else c["close"]),
                       "c": float(c["close"]), "v": int(c.get("volume") or 0)}
            except (KeyError, TypeError, ValueError):
                continue                    # a candle without a price is skipped, never fatal
            by_t[t] = bar
    bars = [by_t[t] for t in sorted(by_t)]
    gaps = [b["t"] - a["t"] for a, b in zip(bars, bars[1:]) if b["t"] > a["t"]]
    step = min(gaps) if gaps else 300
    gran = {60: "1m", 300: "5m", 900: "15m", 1800: "30m", 3600: "60m"}.get(step, "%ds" % step)
    ysym = yahoo_symbol(sym, exch)
    return {"symbol": sym.upper(), "exchange": exch, "yahoo": ysym, "source": "kite", "kite_saved": saved,
            "meta": {"symbol": ysym, "dataGranularity": gran}, "bars": bars, "prev_close": None,
            "data_time": saved, "no_settle": True}


def kite_ltp(path, sym, exch="NSE"):
    """Last price from saved Kite get_ltp / get_quotes output ({"NSE:RELIANCE": {"last_price": ...}})."""
    try:
        with open(path, encoding="utf-8") as fh:
            doc = json.load(fh)
    except (OSError, ValueError) as e:
        raise _Err("could not read the Kite file %s: %s" % (path, e))
    if isinstance(doc, dict) and isinstance(doc.get("data"), dict):
        doc = doc["data"]
    rec = doc.get("%s:%s" % (exch, sym.upper())) if isinstance(doc, dict) else None
    if not isinstance(rec, dict) or rec.get("last_price") is None:
        raise _Err("%s has no last price for %s:%s" % (path, exch, sym.upper()))
    return float(rec["last_price"])


def parse_price_flag(items, sym):
    """--price SYM=PRICE (repeatable) -> the price for sym, or None."""
    for it in items or []:
        k, _, v = str(it).partition("=")
        k = k.strip().upper().split(":")[-1].replace(".NS", "").replace(".BO", "")
        if k == sym.upper():
            try:
                return float(v)
            except ValueError:
                raise _Err("--price wants SYMBOL=PRICE, not %r" % it)
    return None


# ---------------------------------------------------------------- Yahoo

def _open_url(url, timeout=20):
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode("utf-8", "replace")


def _sleep(seconds):
    time.sleep(seconds)


def yahoo_get(path):
    """GET a Yahoo path: query1 first, query2 when query1 answers 429 or 5xx.
    An HTTP 429 gets one retry on the same host after BACKOFF seconds."""
    last = "no answer"
    for host in HOSTS:
        url = "https://%s.finance.yahoo.com%s" % (host, path)
        for attempt in (0, 1):
            try:
                return _open_url(url)
            except urllib.error.HTTPError as e:
                code, body = e.code, None
                try:
                    if code == 404:     # Yahoo says why in the body: an unknown symbol
                        body = e.read().decode("utf-8", "replace")
                except Exception:
                    body = None
                finally:
                    e.close()
                if code == 404:
                    if body:
                        return body
                    raise EntryError("Yahoo has no chart at %s (HTTP 404)" % url.split("?")[0])
                last = "HTTP %d" % code
                if code == 429 and attempt == 0:
                    _sleep(BACKOFF)
                    continue
                if code == 429 or code >= 500:
                    break
                raise EntryError("Yahoo answered HTTP %d for %s" % (code, url.split("?")[0]))
            except urllib.error.URLError as e:
                last = "could not connect: %s" % (e.reason,)
                break
            except OSError as e:
                last = "network error: %s" % (e,)
                break
    raise EntryError("Yahoo did not answer (%s on query1 and query2): wait a minute and try again" % last)


def parse_chart(text, sym, exch="NSE"):
    """Yahoo chart JSON v8 -> {symbol, exchange, yahoo, meta, bars, prev_close, data_time}.
    Bars with a null close are dropped here; off-grid ticks later."""
    try:
        data = json.loads(text)
        res = data["chart"]["result"]
    except (ValueError, KeyError, TypeError):
        raise EntryError("Yahoo returned something unexpected for %s" % sym)
    if not res:
        err = (data.get("chart") or {}).get("error") or {}
        raise EntryError("Yahoo has no data for %s: %s" % (sym, err.get("description") or "unknown symbol"))
    r = res[0] or {}
    meta = r.get("meta") or {}
    q = ((r.get("indicators") or {}).get("quote") or [{}])[0] or {}

    def at(key, i):
        arr = q.get(key) or []
        return arr[i] if i < len(arr) else None

    bars = []
    for i, t in enumerate(r.get("timestamp") or []):
        c = at("close", i)
        if t is None or c is None:
            continue
        o, h, lo = at("open", i), at("high", i), at("low", i)
        bars.append({"t": int(t), "o": float(c if o is None else o), "h": float(c if h is None else h),
                     "l": float(c if lo is None else lo), "c": float(c), "v": int(at("volume", i) or 0)})
    ysym = str(meta.get("symbol") or yahoo_symbol(sym, exch)).upper()
    return {"symbol": sym.upper(), "exchange": exch, "yahoo": ysym, "meta": meta, "bars": bars,
            "prev_close": meta.get("previousClose") or meta.get("chartPreviousClose"),
            "data_time": meta.get("regularMarketTime")}


def read_text(path):
    if path.endswith(".gz"):
        with gzip.open(path, "rt", encoding="utf-8") as fh:
            return fh.read()
    with open(path, encoding="utf-8") as fh:
        return fh.read()


def offline_file(path, sym, exch="NSE"):
    if os.path.isdir(path):
        y = yahoo_symbol(sym, exch)
        for name in (sym, y, sym + "-60d5m"):
            for ext in (".json", ".json.gz"):
                p = os.path.join(path, name + ext)
                if os.path.exists(p):
                    return p
        raise EntryError("no offline file for %s in %s (want %s.json)" % (sym, path, sym))
    if not os.path.exists(path):
        raise EntryError("no offline file %s" % path)
    return path


def get_chart(sym, interval, day=None, offline=None, today=None, exch="NSE", kite=None):
    """Bars for one symbol: saved Kite candles (kite=[files]), a saved file (Yahoo chart JSON or
    Kite candles, detected), or Yahoo (range=1d for today)."""
    sym = sym.upper()
    if not kite and offline:
        f = offline_file(offline, sym, exch)
        if not f.endswith(".gz"):
            try:
                with open(f, encoding="utf-8") as fh:
                    if kite_candles(json.load(fh)) is not None:
                        kite = [f]
            except (OSError, ValueError):
                pass
    if kite:
        ch = kite_chart(kite, sym, exch)
        gran = ch["meta"]["dataGranularity"]
        if ch["bars"] and gran != interval:
            raise EntryError("%s has %s bars, not %s: use --interval %s" % (sym, gran, interval, gran))
        return ch
    if offline:
        f = offline_file(offline, sym, exch)
        try:
            text = read_text(f)
        except (OSError, ValueError) as e:
            raise EntryError("could not read %s: %s" % (f, e))
    else:
        today = today or to_ist(utc_now()).date()
        text = yahoo_get(chart_path(sym, chart_range(day or today, today, interval), interval, exch))
    ch = parse_chart(text, sym, exch)
    if ch["yahoo"] != yahoo_symbol(sym, exch):
        raise EntryError("the chart file holds %s, not %s" % (ch["yahoo"], yahoo_symbol(sym, exch)))
    gran = ch["meta"].get("dataGranularity")
    if gran and gran != interval:
        raise EntryError("%s has %s bars, not %s: use --interval %s" % (sym, gran, interval, gran))
    return ch


# ---------------------------------------------------------------- money

def inr(x):
    """1227.8 -> ₹1,227.80; 123456.78 -> ₹1,23,456.78 (Indian digit grouping)."""
    v = round(float(x), 2) + 0.0
    sign = "-" if v < 0 else ""
    whole, frac = ("%.2f" % abs(v)).split(".")
    if len(whole) > 3:
        head, tail = whole[:-3], whole[-3:]
        parts = []
        while len(head) > 2:
            parts.insert(0, head[-2:])
            head = head[:-2]
        if head:
            parts.insert(0, head)
        whole = ",".join(parts + [tail])
    return "%s₹%s.%s" % (sign, whole, frac)


# ---------------------------------------------------------------- the rule

def read_config(path):
    cfg = dict(DEFAULTS)
    if path and os.path.exists(path):
        try:
            with open(path, encoding="utf-8") as fh:
                doc = json.load(fh)
        except ValueError as e:
            raise EntryError("%s is not valid JSON: %s" % (path, e))
        for k in DEFAULTS:
            if doc.get(k):
                cfg[k] = doc[k]
    for k in DEFAULTS:
        parse_hhmm(cfg[k], k)
    return cfg


def r2(x):
    return None if x is None else round(float(x), 2) + 0.0   # + 0.0 turns -0.0 into 0.0


def prev_session_close(chart, day):
    """Yesterday's close: Yahoo's previousClose when the chart is today's, else the
    last bar of the session before `day` in the file (a 5-minute close, near the official one)."""
    dt = chart.get("data_time")
    if dt and to_ist(utc_of(dt)).date() == day and chart.get("prev_close"):
        return r2(chart["prev_close"]), "yahoo"
    best = None
    for b in chart["bars"]:
        d = to_ist(utc_of(b["t"])).date()
        if d < day and (best is None or b["t"] > best["t"]):
            best = b
    if best:
        return r2(best["c"]), "last bar %s" % to_ist(utc_of(best["t"])).date().isoformat()
    return None, None


def evaluate(chart, now_utc, setup="orb", or_minutes=15, interval="5m", level=None,
             min_volume_x=None, cfg=None, cas=None):
    """Pure function: the setup's status at now_utc from one session's bars."""
    cfg = cfg or dict(DEFAULTS)
    if interval not in INTERVALS:
        raise EntryError("--interval is 5m or 1m")
    step = INTERVALS[interval]
    if or_minutes <= 0 or (or_minutes * 60) % step:
        raise EntryError("--or-minutes must be a whole number of %s bars" % interval)
    if setup not in ("orb", "level"):
        raise EntryError("--setup is orb or level")
    if setup == "level" and level is None:
        raise EntryError("--setup level needs --level PRICE (for example a 20-day high)")
    n_or = or_minutes * 60 // step
    local = to_ist(now_utc)
    day = local.date()
    pc, pc_src = prev_session_close(chart, day)
    rules = session_rules(cas, day)
    src = chart.get("source") or "yahoo"
    res = {"symbol": chart["symbol"], "exchange": chart.get("exchange") or "NSE", "source": src,
           "cas": cas, "session_close": rules["close"], "square_off": rules["square_off"],
           "session_label": rules["label"],
           "yahoo_symbol": chart.get("yahoo"), "currency": "INR", "setup": setup, "side": "buy",
           "date": day.isoformat(), "interval": interval, "or_minutes": or_minutes,
           "now": local.strftime("%Y-%m-%d %H:%M:%S"), "status": "waiting",
           "phase": "", "reason": "", "opening_range": None, "level": None, "trigger": None,
           "last": None, "forming": None, "min_volume_x": min_volume_x,
           "prev_close": pc, "prev_close_source": pc_src, "calendar_known": day.year in CALENDAR_YEARS, "_bars": []}
    dt = chart.get("data_time")
    res["data_time"] = to_ist(utc_of(dt)).strftime("%Y-%m-%d %H:%M:%S") if dt else None
    ok, why = trading_day(day)
    if not ok:
        res.update(status="no-data", phase="closed", reason="the NSE is closed today (%s)" % why,
                   next_open=next_open(day))
        return res
    o, c = session(day, cas)
    open_dt = datetime.datetime.combine(day, o)
    close_dt = datetime.datetime.combine(day, c)
    before = datetime.datetime.combine(day, parse_hhmm(cfg["no_entries_before"], "no_entries_before"))
    cutoff = min(datetime.datetime.combine(day, parse_hhmm(cfg["no_entries_after"], "no_entries_after")), close_dt)
    or_end = open_dt + datetime.timedelta(minutes=or_minutes)
    res.update(opens=hm(o), closes=hm(c), no_entries_before=hm(before), no_entries_after=hm(cutoff))

    by_slot = {}
    for b in chart["bars"]:
        if b["t"] % step:
            continue                       # Yahoo's live tick (11:24:35) is not a bar
        et = to_ist(utc_of(b["t"]))
        if et.date() != day or not (open_dt <= et < close_dt):
            continue
        slot = int((et - open_dt).total_seconds()) // step
        by_slot[slot] = dict(b, et=et, end=et + datetime.timedelta(seconds=step), slot=slot)
    bars, pending = [], None
    now_ts = ts_of(now_utc)
    top = max(by_slot) if by_slot else -1
    for slot in sorted(by_slot):
        b = by_slot[slot]
        end_ts = b["t"] + step
        if end_ts <= now_ts:
            # Closed by the clock. On live data it counts once Yahoo has caught up: its last
            # trade is at or after the close, a later bar exists, or a minute has passed.
            settled = now_ts >= end_ts + SETTLE and not chart.get("no_settle")
            if dt is None or dt >= end_ts or slot < top or settled:
                bars.append(b)
            else:
                pending = b
                res["forming"] = {"bar_start": hm(b["et"]), "closes": ampm(b["end"]), "price": r2(b["c"])}
                if src == "kite":
                    res["lag_note"] = ("the Kite candles were saved at %s, before the %s bar's close: fetch "
                                       "get_historical_data again and save the file" % (
                                           to_ist(utc_of(dt)).strftime("%H:%M:%S"), short(b["et"])))
                else:
                    res["lag_note"] = ("Yahoo's last trade is %s, before the %s bar's close: it counts once Yahoo "
                                       "catches up (a minute at most)" % (to_ist(utc_of(dt)).strftime("%H:%M:%S"),
                                                                           short(b["et"])))
        elif b["et"] <= local:
            res["forming"] = {"bar_start": hm(b["et"]), "closes": ampm(b["end"]), "price": r2(b["c"])}
    res["_bars"] = bars
    res["bars_closed"] = len(bars)

    if local < open_dt:
        pre = (" (pre-open order entry %s)" % rules["pre_open"]) if local.time() >= PRE_OPEN else ""
        res.update(phase="pre-open", reason="the NSE opens at 9:15%s%s; continuous trading ends %s today (%s)" % (
            " and the %d-minute range is set at %s" % (or_minutes, short(or_end)) if setup == "orb" else "", pre,
            rules["close"], "CAS stock" if rules["strict"] else "non-CAS stock"))
        return res

    or_bars = [b for b in bars if b["slot"] < n_or]
    vol_bars = [b for b in or_bars if b["v"] > 0]       # Yahoo books 0 on most NSE 09:15 bars
    avg_vol = (sum(b["v"] for b in vol_bars) / float(len(vol_bars))) if vol_bars else None
    if or_bars:
        res["opening_range"] = {"high": r2(max(b["h"] for b in or_bars)), "low": r2(min(b["l"] for b in or_bars)),
                                "minutes": or_minutes, "bars": len(or_bars), "of": n_or,
                                "complete": local >= or_end,
                                "avg_volume": int(round(avg_vol)) if avg_vol is not None else None}
    if setup == "orb":
        if local < or_end:
            res.update(phase="forming", reason="%d of %d range bars closed; the range is set at %s" % (
                len(or_bars), n_or, short(or_end)))
            return res
        if not or_bars:
            res.update(status="no-data", phase="open", reason="%s sent no bars for the opening range" % (
                "the Kite file has" if src == "kite" else "Yahoo"))
            return res
        lvl = max(b["h"] for b in or_bars)
    else:
        lvl = float(level)
    res["level"] = r2(lvl)

    first_slot = n_or if setup == "orb" else 0
    trig, thin = None, []
    for b in bars:
        if b["slot"] < first_slot or b["end"] < before or b["end"] > cutoff:
            continue
        if b["c"] > lvl:
            vx = (b["v"] / avg_vol) if avg_vol else None
            if min_volume_x and (vx is None or vx < min_volume_x):
                thin.append(b)
                continue
            trig = b
            break
    last = bars[-1] if bars else None
    if last:
        res["last"] = {"bar_start": hm(last["et"]), "close": r2(last["c"]),
                       "vs_level_pct": r2(100.0 * (last["c"] / lvl - 1))}

    if trig:
        vx = (trig["v"] / avg_vol) if avg_vol else None
        res.update(status="triggered", phase="open" if local < close_dt else "after-close",
                   reason="the %s bar closed above %s" % (short(trig["et"]), "the range high" if setup == "orb" else "the level"))
        res["trigger"] = {"time": ampm(trig["end"]), "close_time": hm(trig["end"]), "bar_start": hm(trig["et"]),
                          "bar_index": trig["slot"], "price": r2(trig["c"]), "close": trig["c"],
                          "volume": trig["v"], "volume_x": r2(vx), "pct_over": r2(100.0 * (trig["c"] / lvl - 1))}
        res["minutes_since"] = int((local - trig["end"]).total_seconds() // 60)
        if dt and utc_of(dt) < ist_to_utc(trig["end"]):
            res["data_note"] = ("Yahoo's last trade is %s, before this bar closed: its close can still move a tick or two"
                                % to_ist(utc_of(dt)).strftime("%H:%M:%S"))
        return res
    if not bars:
        res.update(status="no-data", phase="open", reason="%s no bars for %s yet" % (
            "the Kite file has" if src == "kite" else "Yahoo sent", day.isoformat()))
        return res
    what = "close above the range high %s" % inr(lvl) if setup == "orb" else "close above %s" % inr(lvl)
    if local >= cutoff and not (pending and pending["end"] <= cutoff):
        res.update(status="expired", phase="after-close" if local >= close_dt else "after-cutoff",
                   reason="no %s by %s, the last time for a new entry%s" % (
                       what, short(cutoff), "; continuous trading ended at %s (%s)" % (
                           rules["close"], "CAS stock" if rules["strict"] else "non-CAS stock")
                       if local >= close_dt else ""))
        return res
    res["phase"] = "open"
    if last["c"] > lvl and last in thin:
        res["reason"] = "the %s bar closed above at %s but on %.2fx the range's volume, under your %.2fx" % (
            short(last["et"]), inr(last["c"]), last["v"] / avg_vol if avg_vol else 0, min_volume_x)
    elif last["c"] > lvl and last["end"] < before:
        res["reason"] = "the %s bar closed above at %s, but no entries before %s (config)" % (
            short(last["et"]), inr(last["c"]), short(before))
    else:
        gap = lvl - last["c"]
        res["reason"] = "last close %s (%s bar), %s %s %s (%+.2f%%)" % (
            inr(last["c"]), short(last["et"]), inr(abs(gap)), "under" if gap >= 0 else "over",
            "the range high %s" % inr(lvl) if setup == "orb" else "the level %s" % inr(lvl),
            100.0 * (last["c"] / lvl - 1))
    return res


def public(res):
    """The result without the internal bar list, ready for JSON."""
    return {k: v for k, v in res.items() if not k.startswith("_")}


def entry_doc(res, now_utc):
    """trader-in/entry.json: the original pack's entry.json shape plus exchange,
    yahoo_symbol, currency and prev_close."""
    t = res.get("trigger") or {}
    orr = res.get("opening_range")
    return {"created_by": "india-entry", "symbol": res["symbol"], "exchange": res.get("exchange") or "NSE",
            "yahoo_symbol": res.get("yahoo_symbol"), "currency": "INR",
            "setup": res["setup"], "side": "buy",
            "date": res["date"], "status": res["status"], "time": t.get("time"), "bar_start": t.get("bar_start"),
            "bar_index": t.get("bar_index"), "price": t.get("price"), "level": res.get("level"),
            "interval": res["interval"],
            "opening_range": {"high": orr["high"], "low": orr["low"], "minutes": orr["minutes"]} if orr else None,
            "prev_close": res.get("prev_close"), "cas": res.get("cas"), "session_close": res.get("session_close"),
            "square_off": res.get("square_off"),
            "source": res.get("source") or "yahoo", "checked_at": iso(now_utc), "reason": res.get("reason")}


def write_json(path, doc):
    d = os.path.dirname(path)
    if d:
        os.makedirs(d, exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(doc, fh, indent=2, ensure_ascii=False)
        fh.write("\n")


# ---------------------------------------------------------------- words

LABEL = {"triggered": "TRIGGERED", "waiting": "WAITING", "expired": "EXPIRED", "no-data": "NO DATA"}
PHASE_LABEL = {"pre-open": "NOT OPEN YET", "forming": "RANGE FORMING", "closed": "MARKET CLOSED"}
SHORT = {"pre-open": "not open", "forming": "forming", "closed": "closed"}


def setup_name(res):
    if res["setup"] == "orb":
        return "opening-range breakout (%d-minute range)" % res["or_minutes"]
    return "close above %s" % inr(res["level"]) if res["level"] is not None else "close above a level"


def describe(res):
    """Plain lines for a person."""
    when = datetime.datetime.strptime(res["now"], "%Y-%m-%d %H:%M:%S")
    bars = "5-minute" if res["interval"] == "5m" else "1-minute"
    src = SOURCE_LABEL.get(res.get("source") or "yahoo")
    tag = "; %s" % src
    head = "%s (%s)  %s %s IST  %s on %s bars (%s)" % (
        res["symbol"], res.get("exchange") or "NSE", when.strftime("%a %Y-%m-%d"), when.strftime("%H:%M"),
        setup_name(res), bars, src)
    if res.get("data_time") and res["data_time"] <= res["now"] and res["data_time"][:10] == res["date"]:
        head += ", data to %s" % res["data_time"][11:]
    out = [head]
    orr = res.get("opening_range")
    if orr:
        start = datetime.datetime.strptime(res["opens"], "%H:%M")
        end = start + datetime.timedelta(minutes=orr["minutes"])
        out.append("Opening range %s-%s: high %s, low %s (%d of %d bars%s%s)." % (
            short(start), short(end), inr(orr["high"]), inr(orr["low"]), orr["bars"], orr["of"],
            "" if orr["complete"] else ", still forming", tag))
    label = PHASE_LABEL.get(res["phase"]) or LABEL.get(res["status"], res["status"].upper())
    t = res.get("trigger")
    if t:
        out.append("TRIGGERED %s at %s: the %s bar (bar %d) closed above %s %s (%+.2f%%%s%s)." % (
            t["time"], inr(t["price"]), short(datetime.datetime.strptime(t["bar_start"], "%H:%M")), t["bar_index"],
            "the range high" if res["setup"] == "orb" else "the level", inr(res["level"]), t["pct_over"],
            (", %.2fx the range's volume" % t["volume_x"]) if res.get("min_volume_x") and t.get("volume_x") else "",
            tag))
        step = INTERVALS[res["interval"]] // 60
        if res.get("minutes_since", 0) > step and res.get("last"):
            out.append("That was %d minutes ago; the last close is %s (%s bar%s). A late entry is a different "
                       "trade: the price, the stop distance and the odds have moved." % (
                           res["minutes_since"], inr(res["last"]["close"]), res["last"]["bar_start"], tag))
        if res.get("data_note"):
            out.append(res["data_note"] + ".")
        if res.get("minutes_since", 0) > step:
            out.append("Nothing was placed.")
        else:
            out.append("This is the moment to get in, if you take the trade. Nothing was placed.")
        out.append("Next: set the stop and target BEFORE you buy: /india-exit "
                   "(exit.py plan --from trader-in/entry.json).")
    else:
        out.append("%s  %s%s." % (label, res["reason"], (" (%s)" % src) if res.get("last") or res.get("level") else ""))
        f = res.get("forming")
        if res.get("lag_note") and res["status"] == "waiting":
            out.append(res["lag_note"] + ".")
        elif f and res["status"] == "waiting" and res["phase"] in ("open", "forming"):
            out.append("The %s bar is still forming (%s so far, closes %s%s): it does not count until it closes." % (
                f["bar_start"].lstrip("0"), inr(f["price"]), f["closes"], tag))
        if res["status"] == "expired":
            out.append("No entry on this setup today. Nothing was placed.")
        if res.get("next_open"):
            out.append("Next open: %s IST." % res["next_open"])
    if res.get("ltp") is not None:
        lvl = res.get("level")
        out.append("Last price %s (%s)%s: information only, the signal comes from closed bars." % (
            inr(res["ltp"]), SOURCE_LABEL["kite-ltp"] if res.get("ltp_source") == "kite" else "your price",
            (", %+.2f%% vs the level %s" % (100.0 * (res["ltp"] / lvl - 1), inr(lvl))) if lvl else ""))
    if res.get("cas") is None and res.get("session_close") == "15:15" and res.get("phase") != "closed":
        out.append(res["session_label"] + ".")
    if not res.get("calendar_known"):
        out.append("No NSE holiday list for %s here (NSE has not published it, or it did not load): "
                   "check nseindia.com for this date." % res["date"][:4])
    return out


def bar_line(b, res):
    """One watch line for a bar that just closed."""
    start = short(b["et"])
    orr = res.get("opening_range") or {}
    lvl = res.get("level")
    t = res.get("trigger")
    n_or = orr.get("of") or 0
    if res["setup"] == "orb" and b["slot"] < n_or:
        seen = [x for x in res["_bars"] if x["slot"] <= b["slot"]]
        note = "range so far %s to %s" % (inr(min(x["l"] for x in seen)), inr(max(x["h"] for x in seen)))
        if b["slot"] == n_or - 1:
            note = "opening range set: high %s, low %s" % (inr(orr["high"]), inr(orr["low"]))
    elif t and t["bar_index"] == b["slot"]:
        note = "TRIGGERED: closed above %s (%+.2f%%). Get in now if you take it: %s, %s" % (
            inr(lvl), t["pct_over"], t["time"], inr(t["price"]))
    elif lvl is None:
        note = ""
    elif b["c"] > lvl:
        note = "above %s" % inr(lvl)
        if res["setup"] == "level" or b["slot"] >= n_or:
            avg = orr.get("avg_volume")
            if res.get("min_volume_x") and avg:
                note += ", but volume %.2fx is under %.2fx" % (b["v"] / float(avg), res["min_volume_x"])
            elif b["end"] < datetime.datetime.combine(b["et"].date(), parse_hhmm(res["no_entries_before"], "x")):
                note += ", but no entries before %s" % res["no_entries_before"]
    else:
        note = "%s under %s" % (inr(lvl - b["c"]), inr(lvl))
    return "%5s bar  closed %s  %s  (%s)" % (start, inr(b["c"]), note,
                                             SOURCE_LABEL.get(res.get("source") or "yahoo"))


# ---------------------------------------------------------------- watch

class SimClock(object):
    """A clock that moves only when it sleeps: replays a day without waiting."""

    def __init__(self, start):
        self.t = start

    def now(self):
        return self.t

    def sleep(self, seconds):
        self.t += datetime.timedelta(seconds=max(1, int(round(seconds))))


def watch_loop(provider, clock, sleep, emit, opts, every=30, until=None, max_errors=5, on_trigger=None):
    """Poll bar by bar until a trigger (0), expiry, the close or --until (1).
    provider(now_utc) -> chart, clock() -> naive UTC, sleep(seconds), emit(line).
    It sleeps in steps of at most `every` seconds and re-reads the clock each time,
    so a computer that dozed off catches up as soon as it wakes."""
    step = INTERVALS[opts.get("interval", "5m")]
    printed, errors, said_preopen, first = -1, 0, False, True
    next_fetch = None
    while True:
        now = clock()
        if next_fetch is not None and now < next_fetch:
            sleep(max(1, min(every, (next_fetch - now).total_seconds())))
            continue
        local = to_ist(now)
        day = local.date()
        s = session(day, opts.get("cas"))
        if not s:
            emit("%s: market closed today (%s). Nothing to watch." % (day.isoformat(), trading_day(day)[1]))
            return 1, None
        open_dt = datetime.datetime.combine(day, s[0])
        close_dt = datetime.datetime.combine(day, s[1])
        if local < open_dt + datetime.timedelta(seconds=step):
            if not said_preopen:
                emit("%s IST  %s: watching from the first bar's close at %s." % (
                    local.strftime("%H:%M"), "not open yet" if local < open_dt else "open, no bar closed yet",
                    short(open_dt + datetime.timedelta(seconds=step))))
                said_preopen = True
            next_fetch = ist_to_utc(open_dt) + datetime.timedelta(seconds=step + GRACE)
            continue
        try:
            chart = provider(now)
            errors = 0
        except EntryError as e:
            errors += 1
            emit("%s IST  could not read bars: %s" % (local.strftime("%H:%M:%S"), e))
            if errors >= max_errors:
                emit("Stopping after %d failed reads in a row." % errors)
                return 1, None
            next_fetch = now + datetime.timedelta(seconds=every)
            continue
        res = evaluate(chart, now, **opts)
        bars = res["_bars"]
        lines = describe(res)
        fresh = first
        if first:
            emit(lines[0])
            if len(lines) > 1 and lines[1].startswith("Opening range"):
                emit(lines[1])
            if bars and not res.get("trigger"):
                emit(bar_line(bars[-1], res))
            printed = bars[-1]["slot"] if bars else -1
            first = False
        else:
            for b in bars:
                if b["slot"] > printed:
                    emit(bar_line(b, res))
                    printed = b["slot"]
        if res["status"] == "triggered":
            for line in lines[1:]:
                if line.startswith("Opening range") or (line.startswith("TRIGGERED") and not fresh):
                    continue
                emit(line)
            if on_trigger:
                on_trigger(res, now)
            return 0, res
        if res["status"] == "expired" or local >= close_dt:
            emit("EXPIRED  %s." % (res["reason"] or "the market closed without a trigger"))
            return 1, res
        if until and local >= until:
            emit("Stopped at --until %s without a trigger. %s." % (short(until), res["reason"]))
            return 1, res
        # sleep until just after the next bar closes; poll every `every` seconds if Yahoo is behind
        cur_slot = int((local - open_dt).total_seconds()) // step
        expected_last = cur_slot - 1
        behind = (not bars) or bars[-1]["slot"] < expected_last
        if behind:
            wait = every
        else:
            nxt = open_dt + datetime.timedelta(seconds=(cur_slot + 1) * step + GRACE)
            wait = (nxt - local).total_seconds()
        cutoff = datetime.datetime.combine(day, parse_hhmm(res.get("no_entries_after") or DEFAULTS["no_entries_after"], "cutoff"))
        for stop_at in (until, cutoff, close_dt):
            if stop_at and stop_at > local:
                wait = min(wait, (stop_at - local).total_seconds())
        next_fetch = now + datetime.timedelta(seconds=max(1, wait))


# ---------------------------------------------------------------- commands

def _opts(a, cfg):
    if a.side in ("sell", "short"):
        raise EntryError("this pack plans longs only: no short entries. Watch for a buy (--side buy) instead.")
    if a.setup == "orb" and a.level is not None:
        raise EntryError("--level is for --setup level (an orb's level is its range high)")
    if a.min_volume_x is not None and a.min_volume_x <= 0:
        raise EntryError("--min-volume-x must be above 0")
    if a.level is not None and a.level <= 0:
        raise EntryError("--level must be a price above 0")
    return {"setup": a.setup, "or_minutes": a.or_minutes, "interval": a.interval, "level": a.level,
            "min_volume_x": a.min_volume_x, "cfg": cfg}


def _cas(a, sym):
    """CAS status for one symbol: --cas, the config's lists, then NSE's F&O list (cached;
    --offline and --no-fetch never fetch)."""
    cfg = {}
    if a.config and os.path.exists(a.config):
        try:
            with open(a.config, encoding="utf-8") as fh:
                cfg = json.load(fh)
        except ValueError:
            cfg = {}
    if a.cas and a.cas.lower() != "auto":
        return cas_status(sym, a.cas, cfg)
    if not hasattr(a, "_fo"):
        a._fo, note = load_fo_list(a.fo_list, fetch=not a.offline and not a.no_fetch)
        if note:
            print("Note: " + note + ".", file=sys.stderr)
    return cas_status(sym, None, cfg, a._fo)


def _ltp(a, sym, exch, res):
    """Add a live last price (Kite get_ltp file or --price) to the result, for information."""
    p = parse_price_flag(a.price, sym)
    if p is not None:
        res["ltp"], res["ltp_source"] = p, "flags"
    elif a.kite_ltp:
        res["ltp"], res["ltp_source"] = kite_ltp(a.kite_ltp, sym, exch), "kite"


def _calendar(a):
    """Merge the NSE holiday list (cache, then live). --offline never fetches."""
    note = load_holidays(a.holidays, fetch=not a.offline and not a.no_fetch)
    if note:
        print("Note: " + note + ".", file=sys.stderr)


def _load(a, sym, exch, now):
    """(chart, now). An offline file without --now is read as of its own time."""
    real_today = to_ist(utc_now()).date()
    if (a.offline or a.kite_bars) and now is None:
        ch = get_chart(sym, a.interval, offline=a.offline, exch=exch, kite=a.kite_bars)
        return ch, (utc_of(ch["data_time"]) if ch.get("data_time") else utc_now())
    now = now or utc_now()
    return get_chart(sym, a.interval, to_ist(now).date(), a.offline, real_today, exch, a.kite_bars), now


def cmd_check(a):
    cfg = read_config(a.config)
    opts = _opts(a, cfg)
    sym, exch = parse_symbol(a.symbol)
    _calendar(a)
    opts["cas"] = _cas(a, sym)
    chart, now = _load(a, sym, exch, parse_now(a.now) if a.now else None)
    res = evaluate(chart, now, **opts)
    _ltp(a, sym, exch, res)
    if a.write:
        write_json(a.write, entry_doc(res, now))
    if a.json:
        print(json.dumps(public(res), indent=2, ensure_ascii=False))
    else:
        for line in describe(res):
            print(line)
        if a.write:
            print("Wrote %s (status %s)." % (a.write, res["status"]))
    return 0 if res["status"] == "triggered" else 1


def cmd_watch(a):
    cfg = read_config(a.config)
    opts = _opts(a, cfg)
    sym, exch = parse_symbol(a.symbol)
    if a.every <= 0:
        raise EntryError("--every must be above 0 seconds")
    _calendar(a)
    opts["cas"] = _cas(a, sym)
    start = parse_now(a.now) if a.now else None
    if a.offline or a.kite_bars or start is not None:
        if start is None:
            start = _load(a, sym, exch, None)[1]
        clock = SimClock(start)
        now_fn, sleep_fn = clock.now, clock.sleep
    else:
        now_fn, sleep_fn = utc_now, _sleep
    real_today = to_ist(utc_now()).date()

    def provider(now):
        return get_chart(sym, a.interval, to_ist(now).date(), a.offline, real_today, exch, a.kite_bars)

    until = None
    if a.until:
        t = parse_hhmm(a.until, "--until")
        until = datetime.datetime.combine(to_ist(now_fn()).date(), t)

    def on_trigger(res, now):
        write_json(a.out, entry_doc(res, now))
        print("Wrote %s." % a.out, flush=True)

    def emit(line):
        print(line, flush=True)

    code, _ = watch_loop(provider, now_fn, sleep_fn, emit, opts, every=a.every, until=until,
                         on_trigger=on_trigger)
    return code


def cmd_watchlist(a):
    cfg = read_config(a.config)
    opts = _opts(a, cfg)
    if not os.path.exists(a.watchlist):
        raise EntryError("no watchlist at %s: run /india-scanner first (it writes the hits there)" % a.watchlist)
    try:
        with open(a.watchlist, encoding="utf-8") as fh:
            doc = json.load(fh)
    except ValueError as e:
        raise EntryError("%s is not valid JSON: %s" % (a.watchlist, e))
    hits = [h for h in (doc.get("hits") or []) if isinstance(h, dict) and h.get("symbol")]
    if not hits:
        raise EntryError("no hits in %s: the scanner found nothing to watch" % a.watchlist)
    if a.offline and not os.path.isdir(a.offline):
        raise EntryError("watchlist --offline wants a folder of <SYMBOL>.json files")
    _calendar(a)
    now = parse_now(a.now) if a.now else None
    rows = []
    for i, h in enumerate(hits):
        label = str(h["symbol"]).upper()
        o = dict(opts)
        if a.setup == "level" and o["level"] is None:
            o["level"] = h.get("level")
        try:
            sym, exch = parse_symbol(label)
            if h.get("exchange") in ("NSE", "BSE") and ":" not in label and "." not in label:
                exch = h["exchange"]
            label = sym if exch == "NSE" else "%s:BSE" % sym
            if a.setup == "level" and o["level"] is None:
                raise EntryError("needs a level: --level PRICE or a \"level\" in the watchlist")
            if i and not a.offline:
                _sleep(PAUSE)
            o["cas"] = _cas(a, sym)
            chart, when = _load(a, sym, exch, now)
            rows.append((label, evaluate(chart, when, **o), None))
        except EntryError as e:
            rows.append((label, None, str(e)))
    if a.json:
        print(json.dumps([{"symbol": s, "result": public(r) if r else None, "error": e} for s, r, e in rows],
                         indent=2, ensure_ascii=False))
    else:
        print("%-12s  %-9s  %-8s  %-10s  %-10s  %-10s  %-8s  %s" % (
            "SYMBOL", "STATUS", "TIME", "PRICE", "LEVEL", "LAST", "VS LEVEL", "SOURCE"))
        for s, r, e in rows:
            if e:
                print("%-12s  error: %s" % (s, e))
                continue
            t = r.get("trigger") or {}
            last = r.get("last") or {}
            status = SHORT.get(r["phase"], r["status"]) if r["status"] != "triggered" else "triggered"
            print("%-12s  %-9s  %-8s  %-10s  %-10s  %-10s  %-8s  %s" % (
                s, status[:9], t.get("time") or "-", inr(t["price"]) if t else "-",
                inr(r["level"]) if r.get("level") is not None else "-",
                inr(last["close"]) if last else "-",
                ("%+.2f%%" % last["vs_level_pct"]) if last.get("vs_level_pct") is not None else "-",
                SOURCE_LABEL.get(r.get("source") or "yahoo")))
        n = sum(1 for _, r, _ in rows if r and r["status"] == "triggered")
        print("%d of %d triggered. Pick one, then: entry.py check <SYMBOL> --write, and /india-exit." % (n, len(rows)))
        print("Yahoo bars are free, unofficial and can lag. Nothing was placed.")
    return 0 if any(r and r["status"] == "triggered" for _, r, _ in rows) else 1


def main(argv=None):
    ap = argparse.ArgumentParser(description="Entry Timer (india-entry): watches an NSE/BSE setup bar by bar.")
    sub = ap.add_subparsers(dest="cmd")

    def common(p):
        p.add_argument("--setup", choices=["orb", "level"], default="orb")
        p.add_argument("--or-minutes", type=int, default=15, help="opening range length (default 15)")
        p.add_argument("--interval", choices=["5m", "1m"], default="5m")
        p.add_argument("--level", type=float, help="the price to close above (--setup level)")
        p.add_argument("--min-volume-x", type=float, help="trigger bar volume vs the range bars' average")
        p.add_argument("--side", default="buy", choices=["buy", "long", "sell", "short"], help="buy only")
        p.add_argument("--now", help='pretend it is this India time, e.g. "2026-10-19 09:50"')
        p.add_argument("--offline", metavar="PATH", help="saved Yahoo chart JSON (.json/.json.gz) or a folder")
        p.add_argument("--config", default="trader-in/config.json")
        p.add_argument("--holidays", default=HOLIDAY_CACHE, help="NSE holiday cache (default trader-in/holidays.json)")
        p.add_argument("--no-fetch", action="store_true", help="do not fetch the NSE holiday or F&O list")
        p.add_argument("--kite-bars", action="append", metavar="FILE",
                       help="saved Kite get_historical_data output (repeatable: merged by date)")
        p.add_argument("--kite-ltp", metavar="FILE", help="saved Kite get_ltp / get_quotes output (information only)")
        p.add_argument("--price", action="append", metavar="SYM=PRICE", help="a last price you give (information only)")
        p.add_argument("--cas", default="auto", help="yes / no / auto: is it a CAS (F&O) stock (default: NSE's F&O list)")
        p.add_argument("--fo-list", default=FO_CACHE, help="NSE F&O list cache (default trader-in/fo_list.txt)")

    p = sub.add_parser("check", help="the setup's status right now")
    p.add_argument("symbol")
    common(p)
    p.add_argument("--write", nargs="?", const="trader-in/entry.json", metavar="PATH",
                   help="save the result (default trader-in/entry.json)")
    p.add_argument("--json", action="store_true")
    p = sub.add_parser("watch", help="poll bar by bar until it triggers or expires")
    p.add_argument("symbol")
    common(p)
    p.add_argument("--every", type=float, default=30, help="seconds between polls while Yahoo is behind")
    p.add_argument("--until", help="stop at this India time (HH:MM)")
    p.add_argument("--out", default="trader-in/entry.json", help="where a trigger is written")
    p = sub.add_parser("watchlist", help="check every hit in trader-in/watchlist.json")
    common(p)
    p.add_argument("--watchlist", default="trader-in/watchlist.json")
    p.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)
    if not a.cmd:
        ap.print_help()
        return 1
    try:
        return {"check": cmd_check, "watch": cmd_watch, "watchlist": cmd_watchlist}[a.cmd](a)
    except EntryError as e:
        print("entry.py: %s" % e, file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("entry.py: stopped.", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
