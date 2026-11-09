#!/usr/bin/env python3
"""Price alerts and the NSE clock for india-alerts. Standard library only, Python 3.8+.

    python3 alerts.py clock                          # is NSE open? pre-open, next open, holidays
    python3 alerts.py holidays                       # this year's NSE trading holidays
    python3 alerts.py quote RELIANCE --write         # quotes -> trader-in/prices.json
    python3 alerts.py quote --holdings --write       # every symbol in your portfolio and plan
    python3 alerts.py add --from-plan                # stop, target and flat-by from trader-in/plan.json
    python3 alerts.py add --symbol RELIANCE --stop 1215.30
    python3 alerts.py add --symbol RELIANCE --target 1240.30
    python3 alerts.py add --at 15:10 --label "flat by" --symbol RELIANCE
    python3 alerts.py add --symbol TCS --pct 3
    python3 alerts.py add --symbol RPOWER --circuit  # near the upper / lower circuit (price band)
    python3 alerts.py add --opening-bell             # 09:15
    python3 alerts.py add --pre-open                 # 09:00
    python3 alerts.py add --mis-reminder --symbol RELIANCE   # 3 min before Zerodha's square-off (15:09 CAS)
    python3 alerts.py clock --symbol RELIANCE        # that stock's session: CAS (to 15:15) or not (to 15:30)
    python3 alerts.py check                          # rings what hit, once; exit 1 if nothing rang
    python3 alerts.py check --price RELIANCE=1227.8  # a price pushed in (e.g. from Kite get_ltp)
    python3 alerts.py check --kite-ltp ltp.json --kite-instrument RELIANCE-instrument-2026-10-23.json
    python3 alerts.py check --now "2026-10-19 15:10" --offline tests/fixtures/india-alerts
    python3 alerts.py list
    python3 alerts.py reset reliance-plan-stop

Symbols: RELIANCE / NSE:RELIANCE / RELIANCE.NS = NSE (the default); RELIANCE.BO /
BSE:RELIANCE = BSE. A 6-digit BSE scrip code (500325) is BSE, but Yahoo does not
know scrip codes, so use the symbol with .BO.

Rule types:
  above / target  the price is at or above "price"
  below / stop    the price is at or below "price"
  pct_move        moved "pct" percent or more from the previous close (or "ref");
                  "direction": "up" or "down"
  circuit         within "within" percent (default 1) of the upper or lower circuit,
                  from the NSE price band in sec_list.csv and the previous close
  opening_bell    NSE's normal session opened (09:15 IST), once per trading day
  pre_open        the pre-open session is running (09:00-09:15 IST), once per trading day
  time            the IST clock passed "at" (HH:MM) on a trading day, once per trading day;
                  "mis": true adds the Zerodha MIS auto square-off wording; "close" and
                  "square_off" carry the symbol's session (CAS stocks 15:15 / 15:12)

A rule with "date" is only watched on that IST date (rules from a plan are for
the plan's day only). An above / below alert rings ONCE and then stays quiet until
you reset it, unless the rule has "rearm": true. pct_move and circuit ring at most
once per day.

Prices, in this order:
  1. --price SYMBOL=PRICE (repeatable) and --kite-json FILE (a Kite MCP get_ltp,
     get_quotes or get_ohlc answer saved to a file): pushed in by Claude from the
     Zerodha Kite MCP when it is connected. This script never calls Kite itself.
  2. pushed prices already in trader-in/prices.json (any source other than yahoo)
     that are at most --max-age minutes old.
  3. Yahoo chart JSON, free, delayed and unofficial:
     https://query1.finance.yahoo.com/v8/finance/chart/RELIANCE.NS (query2 on a 429
     or a 5xx; one retry with a pause on a 429; one request per symbol, half a
     second apart). --offline DIR reads DIR/yahoo-<SYMBOL>.NS.json (or .json.gz).

NSE calendar: live from https://www.nseindia.com/api/holiday-master?type=trading
(segment CM), cached in trader-in/holidays.json for 7 days; if NSE cannot be
reached, the built-in 2026 list (NSE CM list, read 2026-10-23). The 2027 list is
not published yet: the clock says so for 2027 dates.
"""

import argparse
import csv
import datetime
import gzip
import http.cookiejar
import io
import json
import math
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
PAUSE = 0.5    # seconds between requests: be polite to free endpoints
BACKOFF = 2.0  # seconds to wait before the one retry after a 429
YAHOO_HOSTS = ("query1.finance.yahoo.com", "query2.finance.yahoo.com")
NSE_HOME = "https://www.nseindia.com/"
NSE_COOKIE_PAGES = ("https://www.nseindia.com/companies-listing/corporate-filings-insider-trading", NSE_HOME)
NSE_HOLIDAY_API = "https://www.nseindia.com/api/holiday-master?type=trading"
SEC_LIST_URL = "https://nsearchives.nseindia.com/content/equities/sec_list.csv"
HHMM = re.compile(r"^([01]?\d|2[0-3]):([0-5]\d)$")
IST = datetime.timezone(datetime.timedelta(hours=5, minutes=30))   # fixed, no daylight saving

# NSE trading holidays, Capital Market segment, from the NSE holiday API (read 2026-10-23).
# Weekend dates in the list are harmless.
NSE_HOLIDAYS_2026 = {
    "2026-01-15": "Municipal Corporation Election - Maharashtra",
    "2026-01-26": "Republic Day",
    "2026-02-15": "Mahashivratri",
    "2026-03-03": "Holi",
    "2026-03-21": "Id-Ul-Fitr (Ramadan Eid)",
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
    "2026-11-08": "Diwali Laxmi Pujan",
    "2026-11-10": "Diwali-Balipratipada",
    "2026-11-24": "Prakash Gurpurb Sri Guru Nanak Dev",
    "2026-12-25": "Christmas",
}
MUHURAT_2026 = {"2026-11-08": "Diwali Laxmi Pujan"}
PRE_OPEN = datetime.time(9, 0)
PRE_OPEN_END = datetime.time(9, 8)      # order entry 09:00-09:07, matching to 09:08
OPEN = datetime.time(9, 15)
CLOSE = datetime.time(15, 30)
CLOSING_SESSION = (datetime.time(15, 40), datetime.time(16, 0))
MIS_SQUAREOFF = "15:25"   # Zerodha MIS auto square-off for non-CAS stocks (CAS stocks: 15:12, see session_rules)
CACHE_DAYS = 7


class AlertError(Exception):
    pass


# ---------------------------------------------------------------- money and ticks

def inr(x):
    """1234567.8 -> '₹12,34,567.80' (Indian digit grouping)."""
    x = float(x)
    neg, s = x < 0, "%.2f" % abs(x)
    whole, frac = s.split(".")
    if len(whole) > 3:
        head, tail, groups = whole[:-3], whole[-3:], []
        while len(head) > 2:
            groups.insert(0, head[-2:])
            head = head[:-2]
        if head:
            groups.insert(0, head)
        whole = ",".join(groups) + "," + tail
    return ("-" if neg else "") + "₹" + whole + "." + frac


def tick_size(price):
    """NSE tick size by price (from Apr 15 2025, reviewed monthly by NSE)."""
    p = float(price)
    if p < 250:
        return 0.01
    if p < 1000:
        return 0.05
    if p < 5000:
        return 0.10
    if p < 10000:
        return 0.50
    if p < 20000:
        return 1.00
    return 5.00


def round_to_tick(price, mode="nearest", tick=None):
    t = tick or tick_size(price)
    n = float(price) / t
    if mode == "down":
        n = math.floor(n + 1e-6)
    elif mode == "up":
        n = math.ceil(n - 1e-6)
    else:
        n = round(n)
    return round(n * t, 2)


# ---------------------------------------------------------------- time

def to_ist(utc):
    """Naive UTC -> naive IST."""
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
        raise AlertError("--now wants a time like \"2026-10-19 10:15\" (IST) or ...Z (UTC)")
    if dt.tzinfo is None:
        return ist_to_utc(dt)
    return (dt - dt.utcoffset()).replace(tzinfo=None)


class Calendar(object):
    """NSE trading holidays: {iso date: name}, the years the list covers, Muhurat days."""

    def __init__(self, holidays, years, source, muhurat=None):
        self.holidays, self.years, self.source = dict(holidays), set(years), source
        self.muhurat = dict(muhurat or {})


FALLBACK = Calendar(NSE_HOLIDAYS_2026, {2026}, "built-in 2026 list (NSE, read 2026-10-23)", MUHURAT_2026)


def parse_nse_holidays(doc, source="NSE holiday API"):
    """The NSE holiday-master answer ({"CM": [{"tradingDate": "26-Jan-2026", ...}]}) -> Calendar,
    merged with the built-in 2026 list."""
    rows = (doc or {}).get("CM") if isinstance(doc, dict) else None
    if not isinstance(rows, list) or not rows:
        raise AlertError("the NSE holiday list has no CM (equity) segment")
    hol, years, muhurat = dict(FALLBACK.holidays), set(FALLBACK.years), dict(FALLBACK.muhurat)
    for r in rows:
        try:
            d = datetime.datetime.strptime(str(r.get("tradingDate")), "%d-%b-%Y").date()
        except (ValueError, TypeError):
            continue
        name = str(r.get("description") or "holiday").strip()
        if "laxmi pujan" in name.lower() or "muhurat" in name.lower():
            muhurat[d.isoformat()] = name.rstrip("*").strip()
        hol[d.isoformat()] = name.rstrip("*").strip()
        years.add(d.year)
    return Calendar(hol, years, source, muhurat)


_build_opener = urllib.request.build_opener   # tests swap these
_sleep = time.sleep


def fetch_nse_holidays(timeout=20):
    """NSE wants a cookie session: GET a page for cookies first (the insider-trading page; the
    home page answers 403 to scripts and is only the fallback; the API often works anyway),
    wait a second, then the API with a Referer."""
    op = _build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
    for page in NSE_COOKIE_PAGES:
        try:
            op.open(urllib.request.Request(page, headers={"User-Agent": BROWSER_UA}), timeout=timeout).read()
            break
        except Exception:
            continue
    _sleep(1.0)
    hdr = {"User-Agent": BROWSER_UA, "Accept": "application/json", "Referer": NSE_HOME}
    last = None
    for attempt in (1, 2):
        try:
            with op.open(urllib.request.Request(NSE_HOLIDAY_API, headers=hdr), timeout=timeout) as r:
                return json.loads(r.read().decode("utf-8", "replace"))
        except (urllib.error.URLError, ValueError, OSError) as e:
            last = e
            if attempt == 1:
                _sleep(1.0)
    raise AlertError("NSE blocked or did not answer the holiday API (%s)" % last)


def load_calendar(cache=None, offline=None, refresh=False, now_utc=None):
    """(Calendar, note). Offline: DIR/nse-holidays.json or the built-in list, no network.
    Online: the cache if under CACHE_DAYS old, else the NSE API (cached), else the old cache,
    else the built-in list."""
    if offline:
        p = os.path.join(offline, "nse-holidays.json")
        if os.path.exists(p):
            return parse_nse_holidays(_read(p, {}), "NSE holiday list (offline file)"), ""
        return FALLBACK, ""
    now_utc = now_utc or parse_now(None)
    cached = None
    if cache and os.path.exists(cache):
        try:
            cached = _read(cache, None)
            fetched = datetime.datetime.strptime(str(cached.get("fetched", ""))[:19], "%Y-%m-%dT%H:%M:%S")
            cal = parse_nse_holidays(cached, "NSE holiday API (cached %s)" % fetched.date())
            if not refresh and now_utc - fetched < datetime.timedelta(days=CACHE_DAYS):
                return cal, ""
        except (AlertError, ValueError, TypeError, AttributeError):
            cached = None
    try:
        doc = fetch_nse_holidays()
        cal = parse_nse_holidays(doc, "NSE holiday API (live)")
        if cache:
            _write(cache, {"fetched": now_utc.strftime("%Y-%m-%dT%H:%M:%SZ"), "source": NSE_HOLIDAY_API,
                           "CM": doc.get("CM")})
        return cal, ""
    except AlertError as e:
        if cached:
            try:
                return parse_nse_holidays(cached, "NSE holiday API (old cache)"), "%s: using the cached list." % e
            except AlertError:
                pass
        return FALLBACK, "%s: using the built-in 2026 list." % e


def trading_day(d, cal=FALLBACK):
    """(is_trading_day, reason). Weekends and the NSE holiday list."""
    if d.weekday() >= 5:
        return False, "weekend"
    name = cal.holidays.get(d.isoformat())
    if name:
        return False, "NSE holiday: " + name
    return True, ""


CAS_START = datetime.date(2026, 8, 3)       # NSE closing auction session for F&O stocks
NEW_PRE_OPEN = datetime.date(2026, 9, 7)    # the 09:00-09:10 / matching 09:10-09:12 pre-open
FO_LIST_URL = "https://nsearchives.nseindia.com/content/fo/fo_mktlots.csv"


def session_rules(cas, day):
    """cas: True / False / None (unknown -> the stricter CAS rules). Same function in every script."""
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


def parse_fo_mktlots(text):
    """NSE fo_mktlots.csv -> the set of F&O stock symbols (index rows, UNDERLYING "NIFTY...", skipped)."""
    out = set()
    for i, row in enumerate(csv.reader(io.StringIO(text.lstrip("\ufeff")))):
        if len(row) < 2 or i == 0 and row[0].strip().upper() == "UNDERLYING":
            continue
        und, sym = row[0].strip().upper(), row[1].strip().upper()
        if not sym or sym == "SYMBOL" or und.startswith("NIFTY"):
            continue
        out.add(sym)
    return out


def load_cas_list(offline=None, cache=None, now_utc=None, refresh=False):
    """(set of CAS symbols or None if unknown, source). Offline: DIR/fo_mktlots.csv or DIR/fo_list.txt.
    Online: trader-in/fo_list.txt if under 7 days old, else nsearchives (browser UA, then ours)."""
    def read_list(path):
        with open(path, encoding="utf-8") as fh:
            lines = [x.strip() for x in fh if x.strip()]
        fetched = lines[0][len("# fetched"):].strip() if lines and lines[0].startswith("# fetched") else ""
        return {x.upper() for x in lines if not x.startswith("#")}, fetched
    if offline:
        p = os.path.join(offline, "fo_mktlots.csv")
        if os.path.exists(p):
            with open(p, encoding="utf-8") as fh:
                return parse_fo_mktlots(fh.read()), "NSE fo_mktlots.csv (offline file)"
        p = os.path.join(offline, "fo_list.txt")
        if os.path.exists(p):
            return read_list(p)[0], "fo_list.txt (offline file)"
        return None, ""
    now_utc = now_utc or parse_now(None)
    old = None
    if cache and os.path.exists(cache):
        try:
            syms, fetched = read_list(cache)
            old = (syms, "NSE F&O list (cached %s)" % fetched)
            age = to_ist(now_utc).date() - datetime.date.fromisoformat(fetched)
            if not refresh and age.days < CACHE_DAYS:
                return old
        except (ValueError, OSError):
            pass
    try:
        code, text = _get(FO_LIST_URL, ua=BROWSER_UA)
        if code != 200:
            code, text = _get(FO_LIST_URL, ua=UA)
        if code != 200:
            raise AlertError("nsearchives fo_mktlots.csv answered HTTP %d" % code)
        syms = parse_fo_mktlots(text)
        if not syms:
            raise AlertError("fo_mktlots.csv had no stock rows")
    except AlertError:
        return old if old else (None, "")
    if cache:
        d = os.path.dirname(cache)
        if d:
            os.makedirs(d, exist_ok=True)
        with open(cache, "w", encoding="utf-8") as fh:
            fh.write("# fetched %s\n" % to_ist(now_utc).date().isoformat())
            fh.write("\n".join(sorted(syms)) + "\n")
    return syms, "NSE F&O list (live)"


def cas_status(sym, cas_list, cfg=None, override=None):
    """True / False / None (unknown) for one symbol. --cas yes|no wins, then the config lists."""
    if override in ("yes", "no"):
        return override == "yes"
    cfg = cfg if isinstance(cfg, dict) else {}
    s = str(sym or "").upper()
    if s in {str(x).upper() for x in cfg.get("non_cas_symbols") or []}:
        return False
    if s in {str(x).upper() for x in cfg.get("cas_symbols_extra") or []}:
        return True
    if cas_list is None:
        return None
    return s in cas_list


def pre_open_phase(d, t):
    """(phase text) during 09:00-09:15, None outside."""
    if not (PRE_OPEN <= t < OPEN):
        return None
    if d >= NEW_PRE_OPEN:
        if t < datetime.time(9, 5):
            return "pre-open order entry: market and limit orders (09:00-09:05)"
        if t < datetime.time(9, 10):
            return "pre-open order entry: limit orders only (09:05-09:10)"
        if t < datetime.time(9, 12):
            return "pre-open price discovery and matching (09:10-09:12)"
        return "pre-open buffer, the normal session opens at 09:15 (09:12-09:15)"
    if t < PRE_OPEN_END:
        return "pre-open session (orders 09:00-09:07, matching to 09:08)"
    return "pre-open matching done, the normal session opens at 09:15"


def market_status(now_utc, cal=FALLBACK, cas="all"):
    """The NSE status. cas="all": the market as a whole (non-CAS stocks trade to 15:30).
    cas True / False / None: one symbol's session (None = unknown, the stricter CAS rules)."""
    local = to_ist(now_utc)
    d, t = local.date(), local.time()
    ok, reason = trading_day(d, cal)
    st = {"now_ist": local.strftime("%Y-%m-%d %H:%M"), "date": d.isoformat(), "weekday": local.strftime("%A"),
          "calendar_known": d.year in cal.years, "calendar_source": cal.source,
          "open": False, "phase": "closed", "reason": reason}
    if d.isoformat() in cal.muhurat:
        st["muhurat"] = ("Muhurat trading today (%s): a special evening session; NSE announces its timing "
                         "in a circular. This tool does not know those hours: check nseindia.com."
                         % cal.muhurat[d.isoformat()])
    if ok:
        auction = d >= CAS_START
        if cas == "all":
            close = CLOSE
            rules = session_rules(False, d)
            st["mis_squareoff"] = "15:12 (CAS) / 15:25 (non-CAS)" if auction else rules["square_off"]
        else:
            rules = session_rules(cas, d)
            close = _hhmm(rules["close"])
            st["mis_squareoff"] = rules["square_off"]
            st["cas"], st["session_label"] = cas, rules["label"]
        st.update({"pre_open": "09:00-09:10, matching 09:10-09:12" if d >= NEW_PRE_OPEN else "09:00-09:08",
                   "opens": OPEN.strftime("%H:%M"), "closes": close.strftime("%H:%M")})
        if auction:
            st["cas_close"], st["non_cas_close"], st["closing_auction"] = "15:15", "15:30", "15:15-15:35"
        phase = pre_open_phase(d, t)
        if t < PRE_OPEN:
            st["reason"] = "before the pre-open (pre-open 09:00, normal session 09:15)"
        elif phase:
            st["phase"], st["reason"] = "pre_open", phase
        elif t < close:
            st["open"], st["phase"], st["reason"] = True, "open", "normal session"
            if cas == "all" and auction and t >= datetime.time(15, 15):
                st["reason"] = ("normal session for non-CAS stocks; CAS (F&O) stocks are in the closing "
                                "auction 15:15-15:35")
        elif auction and t < datetime.time(15, 35) and cas != False:
            st["phase"], st["reason"] = "closing_auction", ("continuous trading over: closing auction session "
                                                            "15:15-15:35 (CAS stocks)")
        elif CLOSING_SESSION[0] <= t < CLOSING_SESSION[1]:
            st["phase"], st["reason"] = "closing", "after the close (closing session 15:40-16:00, at the closing price)"
        else:
            st["reason"] = "after the close"
    nd = d if (ok and t < OPEN) else d + datetime.timedelta(days=1)
    for _ in range(15):
        if trading_day(nd, cal)[0]:
            st["next_open"] = "%s 09:15" % nd.isoformat()
            break
        nd += datetime.timedelta(days=1)
    return st


def calendar_warning(st):
    if st["calendar_known"]:
        return ""
    y = st["date"][:4]
    return ("NSE has not published a %s holiday list that this tool could read (2027 was not out on "
            "2026-10-23): weekends are known, holidays are not. Check nseindia.com before trading." % y)


# ---------------------------------------------------------------- symbols and quotes

def parse_symbol(text, exchange=None):
    """'RELIANCE' / 'NSE:RELIANCE' / 'RELIANCE.NS' -> ('RELIANCE', 'NSE');
    'RELIANCE.BO' / 'BSE:RELIANCE' / '500325' -> BSE."""
    s = str(text or "").strip().upper()
    ex = None
    if ":" in s:
        pre, s = s.split(":", 1)
        if pre not in ("NSE", "BSE"):
            raise AlertError("unknown exchange %r in %r (NSE or BSE)" % (pre, text))
        ex = pre
    elif s.endswith(".NS"):
        s, ex = s[:-3], "NSE"
    elif s.endswith(".BO"):
        s, ex = s[:-3], "BSE"
    if not s:
        raise AlertError("empty symbol")
    if re.match(r"^\d{6}$", s):
        ex = "BSE"
    return s, (ex or str(exchange or "NSE").upper())


def yahoo_symbol(sym, exchange="NSE"):
    if re.match(r"^\d{6}$", sym):
        raise AlertError("Yahoo does not know BSE scrip codes like %s: use the symbol with .BO "
                         "(RELIANCE.BO)" % sym)
    return sym.upper() + (".BO" if exchange == "BSE" else ".NS")


def yahoo_url(ysym, host=YAHOO_HOSTS[0]):
    return "https://%s/v8/finance/chart/%s?range=1d&interval=1d" % (host, urllib.parse.quote(ysym, safe=""))


def parse_yahoo(text, sym, exchange="NSE"):
    try:
        data = json.loads(text)
        res = data["chart"]["result"]
    except (ValueError, KeyError, TypeError):
        raise AlertError("Yahoo returned something unexpected for %s" % sym)
    if not res:
        err = (data.get("chart") or {}).get("error") or {}
        raise AlertError("Yahoo has no data for %s: %s" % (sym, err.get("description", "unknown symbol")))
    m = res[0].get("meta", {})
    price = m.get("regularMarketPrice")
    if price is None:
        raise AlertError("Yahoo has no price for %s" % sym)
    t = m.get("regularMarketTime")
    when = (datetime.datetime.fromtimestamp(t, datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            if t else "")
    q = (res[0].get("indicators", {}).get("quote") or [{}])[0]
    opens = [x for x in (q.get("open") or []) if x is not None]
    return {"symbol": sym.upper(), "exchange": exchange, "price": float(price),
            "open": opens[0] if opens else None,
            "high": m.get("regularMarketDayHigh"), "low": m.get("regularMarketDayLow"),
            "prev_close": m.get("previousClose") or m.get("chartPreviousClose"), "time": when,
            "currency": m.get("currency") or "INR", "source": "yahoo"}


_urlopen = urllib.request.urlopen   # tests swap this


def _get(url, timeout=20, ua=UA):
    """(HTTP status, body). A network failure is an AlertError."""
    req = urllib.request.Request(url, headers={"User-Agent": ua, "Accept": "*/*"})
    try:
        with _urlopen(req, timeout=timeout) as r:
            return 200, r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        try:
            body = e.read().decode("utf-8", "replace")
        except Exception:
            body = ""
        finally:
            try:
                e.close()
            except Exception:
                pass
        return e.code, body
    except urllib.error.URLError as e:
        raise AlertError("could not reach %s: %s" % (url.split("/")[2], e.reason))


def fetch_yahoo(ysym, timeout=20):
    """query1 first. A 429 waits BACKOFF seconds and tries once more; a 429 or a 5xx
    after that moves on to query2. A 404 comes back as Yahoo's own error JSON."""
    last = None
    for host in YAHOO_HOSTS:
        url = yahoo_url(ysym, host)
        code, text = _get(url, timeout)
        if code == 429:
            _sleep(BACKOFF)
            code, text = _get(url, timeout)
        if code == 200:
            return text
        if code == 404 and text.strip().startswith("{"):
            return text
        last = (url, code)
        if code != 429 and code < 500:
            break
    raise AlertError("%s answered HTTP %d" % (last[0].split("?")[0], last[1]))


def _offline_text(folder, name):
    base = os.path.join(folder, name)
    if os.path.exists(base):
        with open(base, encoding="utf-8") as fh:
            return fh.read()
    if os.path.exists(base + ".gz"):
        with gzip.open(base + ".gz", "rt", encoding="utf-8") as fh:
            return fh.read()
    raise AlertError("no offline quote file %s" % base)


def get_quotes(items, offline=None, pause=PAUSE, pushed=None, need_prev=()):
    """items: [(SYMBOL, EXCHANGE)] -> ({SYMBOL: quote}, [errors]). A pushed price (Kite, --price)
    is used as it is; the rest come from Yahoo one request per symbol, with a pause between.
    A pushed price without a previous close, for a symbol in need_prev (pct_move, circuit),
    borrows Yahoo's previous close."""
    quotes, errors, n = {}, [], 0
    pushed = pushed or {}
    for sym, exch in items:
        sym = sym.upper()
        if sym in quotes:
            continue   # one price per symbol: the first exchange asked for wins
        if sym in pushed:
            quotes[sym] = dict(pushed[sym], exchange=pushed[sym].get("exchange") or exch)
            if quotes[sym].get("prev_close") or sym not in need_prev:
                continue
        try:
            ysym = yahoo_symbol(sym, exch)
            if offline:
                text = _offline_text(offline, "yahoo-%s.json" % ysym)
            else:
                if n and pause:
                    _sleep(pause)
                n += 1
                text = fetch_yahoo(ysym)
            yq = parse_yahoo(text, sym, exch)
        except AlertError as e:
            if sym not in quotes:
                errors.append(str(e))
            continue
        if sym in quotes:
            quotes[sym]["prev_close"] = yq.get("prev_close")
        else:
            quotes[sym] = yq
    return quotes, errors


def _stamp(utc):
    return utc.strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_price_args(values, now_utc, source="kite"):
    """['RELIANCE=1227.8', 'NSE:TCS=2559.7'] -> {SYMBOL: quote}."""
    out = {}
    for v in values or []:
        if "=" not in v:
            raise AlertError("--price wants SYMBOL=PRICE, like RELIANCE=1227.8")
        k, p = v.split("=", 1)
        sym, exch = parse_symbol(k)
        try:
            price = float(p.replace(",", "").replace("₹", ""))
        except ValueError:
            raise AlertError("--price %s: %r is not a number" % (k, p))
        if price <= 0:
            raise AlertError("--price %s must be above zero" % k)
        out[sym] = {"symbol": sym, "exchange": exch, "price": price, "prev_close": None,
                    "time": _stamp(now_utc), "currency": "INR", "source": source}
    return out


def parse_kite(doc, now_utc):
    """A Kite get_ltp / get_quotes / get_ohlc answer ({"NSE:RELIANCE": {"last_price": ...,
    "ohlc": {"close": prev close}}}, maybe wrapped in "data") -> {SYMBOL: quote}."""
    if isinstance(doc, dict) and isinstance(doc.get("data"), dict):
        doc = doc["data"]
    if not isinstance(doc, dict):
        raise AlertError("the Kite file is not a JSON object of NSE:SYMBOL keys")
    out = {}
    for key, v in doc.items():
        if not isinstance(v, dict) or v.get("last_price") is None:
            continue
        sym, exch = parse_symbol(key)
        if sym in out and out[sym]["exchange"] == "NSE" and exch != "NSE":
            continue   # NSE:RELIANCE and BSE:RELIANCE in one answer: keep NSE
        ohlc = v.get("ohlc") or {}
        t = v.get("last_trade_time") or v.get("timestamp")
        out[sym] = {"symbol": sym, "exchange": exch, "price": float(v["last_price"]),
                    "prev_close": ohlc.get("close"), "open": ohlc.get("open"), "high": ohlc.get("high"),
                    "low": ohlc.get("low"), "time": str(t) if t else _stamp(now_utc),
                    "currency": "INR", "source": "kite"}
    if not out:
        raise AlertError("no last_price in the Kite file")
    return out


def src_label(q):
    src = (q or {}).get("source")
    if src == "kite":
        return "Kite, live"
    if src == "yahoo":
        return "Yahoo, free, can lag"
    return "%s, pushed in" % (src or "unknown")


def _file_date(path, doc=None):
    """The IST date a saved Kite file is for: its "fetched" field, a YYYY-MM-DD in its name, or its mtime."""
    if isinstance(doc, dict) and doc.get("fetched"):
        return str(doc["fetched"])[:10]
    m = re.search(r"(\d{4}-\d{2}-\d{2})", os.path.basename(path or ""))
    if m:
        return m.group(1)
    try:
        return to_ist(datetime.datetime.utcfromtimestamp(os.path.getmtime(path))).date().isoformat()
    except OSError:
        return ""


def parse_kite_instrument(doc, path=""):
    """Kite search_instruments answer ({"data": [{tradingsymbol, exchange, tick_size, lower_circuit_limit,
    upper_circuit_limit, series}]}) -> {SYMBOL: {...}}. The limits are for the day the file was saved."""
    rows = doc.get("data") if isinstance(doc, dict) else doc
    if not isinstance(rows, list):
        raise AlertError("%s is not a Kite search_instruments answer" % (path or "the instrument file"))
    day, out = _file_date(path, doc), {}
    for r in rows:
        if not isinstance(r, dict) or not r.get("tradingsymbol"):
            continue
        if str(r.get("instrument_type") or "EQ").upper() != "EQ":
            continue
        sym = str(r["tradingsymbol"]).upper()
        exch = str(r.get("exchange") or "NSE").upper()
        if sym in out and out[sym]["exchange"] == "NSE" and exch != "NSE":
            continue
        lo, up = r.get("lower_circuit_limit"), r.get("upper_circuit_limit")
        out[sym] = {"exchange": exch, "tick": r.get("tick_size"), "series": r.get("series"),
                    "lower": float(lo) if lo else None, "upper": float(up) if up else None, "date": day}
    return out


def pushed_from_prices(path, now_utc, max_age_min):
    """Fresh prices someone pushed into prices.json (source not yahoo)."""
    doc = _read(path, {}) if path else {}
    if not isinstance(doc, dict):
        return {}
    out = {}
    detail = doc.get("detail") or {}
    for key, price in (doc.get("prices") or {}).items():
        try:
            sym, exch = parse_symbol(key)
            price = float(price)
        except (AlertError, TypeError, ValueError):
            continue
        d = detail.get(key) or detail.get(sym) or {}
        src = d.get("source") or doc.get("source") or ""
        if not src or src == "yahoo":
            continue
        when = d.get("fetched") or doc.get("as_of") or ""
        try:
            t = datetime.datetime.strptime(str(when)[:19], "%Y-%m-%dT%H:%M:%S")
        except ValueError:
            continue
        if not (datetime.timedelta(minutes=-1) <= now_utc - t <= datetime.timedelta(minutes=max_age_min)):
            continue
        out[sym] = {"symbol": sym, "exchange": d.get("exchange") or exch, "price": price,
                    "prev_close": d.get("prev_close"), "time": d.get("time") or when,
                    "currency": "INR", "source": src}
    return out


def collect_pushed(a, now_utc):
    pushed = {}
    if getattr(a, "prices_in", None):
        pushed.update(pushed_from_prices(a.prices_in, now_utc, a.max_age))
    if getattr(a, "kite_json", None):
        pushed.update(parse_kite(_read(a.kite_json, None), now_utc))
    pushed.update(parse_price_args(getattr(a, "price", None), now_utc, getattr(a, "price_source", "kite")))
    return pushed


def write_prices(path, quotes, now_utc):
    old = {}
    if os.path.exists(path):
        try:
            with open(path, encoding="utf-8") as fh:
                old = json.load(fh)
        except (ValueError, OSError):
            old = {}
    prices = dict(old.get("prices", {}))
    detail = dict(old.get("detail", {}))
    stamp = _stamp(now_utc)
    for s, q in quotes.items():
        prices[s] = q["price"]
        detail[s] = {"exchange": q.get("exchange", "NSE"), "prev_close": q.get("prev_close"),
                     "time": q.get("time"), "source": q.get("source"), "fetched": stamp}
    sources = sorted({q.get("source") for q in quotes.values() if q.get("source")})
    doc = {"as_of": stamp, "currency": "INR", "source": ",".join(sources) or old.get("source", ""),
           "note": ("Yahoo quotes are free and delayed; kite prices came from the Zerodha Kite MCP. For "
                    "alerts and sizing checks: the exchange price is the one that fills. "
                    "detail.fetched is when each price was read."),
           "prices": prices, "detail": detail}
    _write(path, doc)
    return doc


def _write(path, doc):
    d = os.path.dirname(path)
    if d:
        os.makedirs(d, exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(doc, fh, indent=2, ensure_ascii=False)
        fh.write("\n")
    os.replace(tmp, path)


def _read(path, default):
    if not path or not os.path.exists(path):
        return default
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except ValueError as e:
        raise AlertError("%s is not valid JSON: %s" % (path, e))


# ---------------------------------------------------------------- price bands

def parse_sec_list(text):
    """NSE sec_list.csv (Symbol,Series,Security Name,Band,Remarks) -> {SYMBOL: {band, series}}.
    band is a percent (2, 5, 10, 20) or None for "No Band" (F&O stocks: dynamic bands)."""
    out = {}
    for r in csv.DictReader(io.StringIO(text.lstrip("﻿"))):
        r = {str(k).strip().lower(): str(v or "").strip() for k, v in r.items() if k}
        sym = r.get("symbol", "").upper()
        if not sym:
            continue
        try:
            band = float(r.get("band", ""))
        except ValueError:
            band = None
        out[sym] = {"band": band, "series": r.get("series", "").upper()}
    return out


def load_bands(offline=None, cache=None, now_utc=None):
    """sec_list.csv from DIR (offline), else a cache from today (IST), else nsearchives (cached)."""
    if offline:
        p = os.path.join(offline, "sec_list.csv")
        if not os.path.exists(p):
            raise AlertError("no offline sec_list.csv in %s" % offline)
        with open(p, encoding="utf-8") as fh:
            return parse_sec_list(fh.read())
    now_utc = now_utc or parse_now(None)
    if cache and os.path.exists(cache):
        mtime = datetime.datetime.utcfromtimestamp(os.path.getmtime(cache))
        if to_ist(mtime).date() == to_ist(now_utc).date():
            with open(cache, encoding="utf-8") as fh:
                return parse_sec_list(fh.read())
    code, text = _get(SEC_LIST_URL, ua=BROWSER_UA)    # browser UA first, the compatible UA as fallback
    if code != 200:
        code, text = _get(SEC_LIST_URL, ua=UA)
    if code != 200:
        raise AlertError("nsearchives sec_list.csv answered HTTP %d" % code)
    if cache:
        d = os.path.dirname(cache)
        if d:
            os.makedirs(d, exist_ok=True)
        with open(cache, "w", encoding="utf-8") as fh:
            fh.write(text)
    return parse_sec_list(text)


def circuits(prev_close, band):
    """(lower, upper) circuit from the previous close and the band percent, on the tick."""
    up = float(prev_close) * (1 + band / 100.0)
    lo = float(prev_close) * (1 - band / 100.0)
    return round_to_tick(lo, "up"), round_to_tick(up, "down")


# ---------------------------------------------------------------- rules

RULE_TYPES = {"above", "below", "stop", "target", "pct_move", "circuit", "opening_bell", "pre_open", "time"}
NO_QUOTE = {"opening_bell", "pre_open", "time"}


def _hhmm(text):
    m = HHMM.match(str(text or "").strip())
    if not m:
        return None
    return datetime.time(int(m.group(1)), int(m.group(2)))


def validate(rule):
    t = rule.get("type")
    if t not in RULE_TYPES:
        raise AlertError("rule %s: unknown type %r" % (rule.get("id"), t))
    if t not in NO_QUOTE and not rule.get("symbol"):
        raise AlertError("rule %s needs a symbol" % rule.get("id"))
    if rule.get("exchange") and rule["exchange"] not in ("NSE", "BSE"):
        raise AlertError("rule %s: exchange must be NSE or BSE" % rule.get("id"))
    if t in ("above", "below", "stop", "target") and not isinstance(rule.get("price"), (int, float)):
        raise AlertError("rule %s needs a numeric price" % rule.get("id"))
    if t == "pct_move" and not isinstance(rule.get("pct"), (int, float)):
        raise AlertError("rule %s needs a numeric pct" % rule.get("id"))
    if t == "time" and _hhmm(rule.get("at")) is None:
        raise AlertError("rule %s needs \"at\" as an IST time like \"15:10\"" % rule.get("id"))
    if rule.get("date") and not re.match(r"^\d{4}-\d{2}-\d{2}$", str(rule["date"])):
        raise AlertError("rule %s: date must be YYYY-MM-DD" % rule.get("id"))


def active(rule, today):
    """A rule with a "date" is watched on that IST date only."""
    return not rule.get("date") or str(rule["date"]) == today


def _rang_today(stamp, today):
    """Did a rule that rang at this UTC stamp ring on this IST date?"""
    if not stamp:
        return False
    try:
        utc = datetime.datetime.strptime(stamp[:19], "%Y-%m-%dT%H:%M:%S")
    except ValueError:
        return stamp[:10] == today
    return to_ist(utc).date().isoformat() == today


def _rule_id(r):
    return r.get("id") or "%s-%s" % (str(r.get("symbol", "market")).lower(), r["type"])


def evaluate(rules, quotes, status, state, now_utc):
    """Pure function. Returns (rings, new_state). Each ring: {id, symbol, message}."""
    state = json.loads(json.dumps(state or {}))
    rung = state.setdefault("rung", {})
    today = status["date"]
    local = to_ist(now_utc)
    stamp = _stamp(now_utc)
    rings = []
    for r in rules:
        validate(r)
        rid = _rule_id(r)
        if not active(r, today):
            continue
        typ = {"stop": "below", "target": "above"}.get(r["type"], r["type"])
        if typ == "opening_bell":
            if status["open"] and not _rang_today(rung.get(rid), today):
                rings.append({"id": rid, "symbol": "", "message": "Opening bell: NSE is open (%s IST), normal "
                              "session to 15:30." % status["now_ist"]})
                rung[rid] = stamp
            continue
        if typ == "pre_open":
            if status["phase"] == "pre_open" and not _rang_today(rung.get(rid), today):
                rings.append({"id": rid, "symbol": "", "message": "Pre-open: NSE's pre-open session is on (%s IST; "
                              "orders 09:00-09:07, matching to 09:08), the normal session opens at 09:15."
                              % status["now_ist"]})
                rung[rid] = stamp
            continue
        if typ == "time":
            at = _hhmm(r.get("at"))
            if status.get("opens") and local.time() >= at and not _rang_today(rung.get(rid), today):
                label = r.get("label") or "time alert"
                msg = "%s%s: it is %s IST (set for %s)" % (label[:1].upper(), label[1:],
                                                           local.strftime("%H:%M"), at.strftime("%H:%M"))
                sym = str(r.get("symbol") or "").upper()
                close = r.get("close") or status["closes"]
                sq = r.get("square_off") or status.get("mis_squareoff") or MIS_SQUAREOFF
                now_s = local.strftime("%H:%M")
                auction = close == "15:15"
                if local.time() < OPEN:
                    msg += ", the market is not open yet."
                elif now_s < close:
                    msg += ", continuous trading%s ends at %s%s" % (" in " + sym if sym else "", close,
                                                                  " (closing auction 15:15-15:35)" if auction else "")
                    if now_s < sq[:5]:
                        msg += " and Zerodha squares off MIS positions at %s" % sq
                    msg += "."
                elif auction and now_s < "15:35":
                    msg += ", continuous trading%s has ended (closing auction to 15:35)." % (" in " + sym if sym else "")
                else:
                    msg += ", the market has closed."
                if r.get("mis"):
                    msg += (" Zerodha auto squares off open MIS (intraday) positions at %s and charges \u20b950 + GST "
                            "per order it closes: close them yourself first, or convert to CNC only if you mean "
                            "to hold overnight." % sq)
                sym = str(r.get("symbol") or "").upper()
                if sym:
                    msg += (" If %s is still open, close it: india-broker flatten --symbol %s builds the sell "
                            "ticket, then india-risk and your yes." % (sym, sym))
                rings.append({"id": rid, "symbol": sym, "message": msg})
                rung[rid] = stamp
            continue
        sym = parse_symbol(r["symbol"])[0]
        q = quotes.get(sym)
        if not q:
            continue
        p = q["price"]
        if typ in ("above", "below"):
            hit = p >= r["price"] if typ == "above" else p <= r["price"]
            if hit and rid not in rung:
                word = "at or above" if typ == "above" else "at or below"
                label = {"stop": "stop", "target": "target"}.get(r["type"], "alert level")
                rings.append({"id": rid, "symbol": sym, "message": "%s %s %s (%s): %s your %s %s%s."
                              % (sym, "is" if status["open"] else "last", inr(p), src_label(q), word, label,
                                 inr(r["price"]),
                                 (" (" + r["note"] + ")") if r.get("note") else "")})
                rung[rid] = stamp
            elif not hit and rid in rung and r.get("rearm"):
                del rung[rid]
        elif typ == "pct_move":
            ref = r.get("ref") or q.get("prev_close") or q.get("open")
            if not ref:
                continue
            move = 100.0 * (p / float(ref) - 1)
            want = r.get("direction")
            hit = abs(move) >= r["pct"] and (not want or (want == "up") == (move > 0))
            if hit and not _rang_today(rung.get(rid), today):
                rings.append({"id": rid, "symbol": sym, "message": "%s moved %+.2f%% to %s (from %s; %s)."
                              % (sym, move, inr(p), inr(ref), src_label(q))})
                rung[rid] = stamp
        elif typ == "circuit":
            band, prev = q.get("band"), q.get("prev_close")
            if _rang_today(rung.get(rid), today):
                continue
            if q.get("circuit_limits"):
                lo, up = q["circuit_limits"]
                basis = "Kite's circuit limits for today"
            elif band and prev:
                lo, up = circuits(prev, band)
                basis = "%g%% band from the previous close %s" % (band, inr(prev))
            else:
                continue
            within = float(r.get("within", 1.0))
            side = None
            if p >= up * (1 - within / 100.0):
                side, level = "upper", up
            elif p <= lo * (1 + within / 100.0):
                side, level = "lower", lo
            if side:
                gap = abs(level / p - 1) * 100
                rings.append({"id": rid, "symbol": sym, "message": (
                    "%s %s (%s) is %.2f%% from its %s circuit %s (%s). At the "
                    "circuit trading can freeze with only %s, and you may not get out at your price%s."
                    % (sym, inr(p), src_label(q), gap, side, inr(level), basis,
                       "buyers" if side == "upper" else "sellers",
                       "; india-risk blocks new entries near the upper circuit" if side == "upper" else ""))})
                rung[rid] = stamp
    state["last_check"] = stamp
    return rings, state


def _minus(hhmm, minutes):
    t = datetime.datetime.combine(datetime.date(2000, 1, 1), _hhmm(hhmm)) - datetime.timedelta(minutes=minutes)
    return t.strftime("%H:%M")


def rules_from_plan(plan, cfg_flat_by=None, cas=None, today=None):
    """trader-in/plan.json -> a stop alert, a target alert and a flat-by time alert, for the plan's day.
    flat_by "auto" (or none) = the symbol's default (15:00 CAS, 15:10 non-CAS); a flat_by at or after
    the symbol's Zerodha square-off is clamped to that default (plan_warnings says so)."""
    if not isinstance(plan, dict) or not plan.get("symbol"):
        raise AlertError("the plan has no symbol (run india-exit first)")
    sym, exch = parse_symbol(plan["symbol"], plan.get("exchange"))
    out = []
    for key, typ, word in (("stop", "stop", "plan stop"), ("target", "target", "plan target")):
        v = plan.get(key)
        if not isinstance(v, (int, float)) or v <= 0:
            raise AlertError("the plan has no %s (run india-exit)" % key)
        label = plan.get("%s_label" % key)
        out.append({"id": "%s-plan-%s" % (sym.lower(), key), "symbol": sym, "exchange": exch, "type": typ,
                    "price": float(v), "note": word + (": " + label if label else "")})
    try:
        day = datetime.date.fromisoformat(str(plan.get("date") or today or to_ist(parse_now(None)).date()))
    except ValueError:
        raise AlertError("the plan's date %r is not YYYY-MM-DD" % plan.get("date"))
    sr = session_rules(cas, day)
    want = str(plan.get("flat_by") or cfg_flat_by or "auto").strip()
    flat = sr["flat_by_default"] if want.lower() == "auto" else want
    if _hhmm(flat) is None:
        raise AlertError("the plan's flat_by %r is not a time like 15:10 (or \"auto\")" % want)
    rule = {"id": "%s-flat-by" % sym.lower(), "symbol": sym, "exchange": exch, "type": "time", "at": flat,
            "label": "flat by", "close": sr["close"], "square_off": sr["square_off"]}
    if str(plan.get("product", "MIS")).upper() == "MIS" and _hhmm(flat) >= _hhmm(sr["square_off"]):
        rule["at"], rule["clamped_from"] = sr["flat_by_default"], flat
    out.append(rule)
    if plan.get("date"):
        for r in out:
            r["date"] = str(plan["date"])
    return out


def plan_warnings(plan, rules, cas=None):
    r = rules[-1]
    out = []
    if r.get("clamped_from"):
        out.append("flat by %s is at or after Zerodha's MIS auto square-off for %s (%s): moved to %s. At %s "
                   "Zerodha closes the position for you and charges \u20b950 + GST."
                   % (r["clamped_from"], r["symbol"], r["square_off"], r["at"], r["square_off"]))
    if r["at"] >= r.get("close", "15:30"):
        out.append("flat by %s is after continuous trading ends (%s): it will ring too late." % (r["at"], r["close"]))
    if cas is None:
        out.append("the F&O (CAS) list was not available, so %s gets the stricter CAS times (trading to 15:15, "
                   "square-off 15:12). Pass --cas no if it has no F&O contracts." % r["symbol"])
    return out


# ---------------------------------------------------------------- commands

def holdings(a):
    items = []
    pf = _read(a.portfolio, {})
    for p in pf.get("positions", []) if isinstance(pf, dict) else []:
        s = p.get("symbol") or p.get("ticker") or p.get("tradingsymbol")
        if s:
            items.append(parse_symbol(s, p.get("exchange")))
    plan = _read(a.plan, {})
    if isinstance(plan, dict) and plan.get("symbol"):
        items.append(parse_symbol(plan["symbol"], plan.get("exchange")))
    return items


def _dedupe(items):
    out, seen = [], set()
    for s, e in items:
        if s and s not in seen:
            seen.add(s)
            out.append((s, e))
    return out


def _calendar(a, now):
    return load_calendar(getattr(a, "holidays", None), getattr(a, "offline", None),
                         getattr(a, "refresh", False), now)


def today_lines(st, d):
    """The session times for a trading day, both closes and the pre-open phases."""
    out = []
    if d >= NEW_PRE_OPEN:
        out.append("Pre-open: 09:00-09:05 market and limit orders, 09:05-09:10 limit orders only, matching "
                   "09:10-09:12, buffer to 09:15. Normal session from 09:15.")
    else:
        out.append("Pre-open: 09:00-09:08 (orders to 09:07). Normal session from 09:15.")
    if d >= CAS_START:
        out.append("Close: CAS stocks (with F&O contracts) trade continuously to 15:15, then the closing "
                   "auction 15:15-15:35; non-CAS stocks trade to 15:30.")
        out.append("Zerodha MIS auto square-off: 15:12 for CAS stocks, 15:25 for non-CAS (\u20b950 + GST per order).")
    else:
        out.append("Close: 15:30 for every stock; closing session 15:40-16:00. Zerodha MIS auto square-off 15:25.")
    return out


def _cas_list(a, now):
    if not hasattr(a, "_cas_cache"):
        a._cas_cache = load_cas_list(getattr(a, "offline", None), getattr(a, "fo_list", None), now,
                                     getattr(a, "refresh", False))
    return a._cas_cache


def _cas_for(a, sym, now):
    cfg = _read(getattr(a, "config", None), {}) if getattr(a, "config", None) else {}
    lst, _src = _cas_list(a, now)
    return cas_status(sym, lst, cfg, getattr(a, "cas", None))


def cmd_clock(a):
    now = parse_now(a.now)
    cal, note = _calendar(a, now)
    st = market_status(now, cal)
    sym_st = None
    if a.symbol:
        sym = parse_symbol(a.symbol)[0]
        cas = _cas_for(a, sym, now)
        sym_st = dict(market_status(now, cal, cas), symbol=sym)
        if cas is None:
            sym_st["cas_note"] = ("the F&O (CAS) list was not available: %s gets the stricter CAS times. Pass "
                                  "--cas no if it has no F&O contracts." % sym)
    if a.json:
        print(json.dumps(dict(st, calendar_note=note or calendar_warning(st), symbol_status=sym_st), indent=2,
                         ensure_ascii=False))
    else:
        print("IST %s (%s): NSE %s, %s." % (st["now_ist"], st["weekday"], "OPEN" if st["open"] else "closed",
                                            st["reason"]))
        if st.get("opens"):
            for line in today_lines(st, datetime.date.fromisoformat(st["date"])):
                print(line)
        if sym_st and sym_st.get("opens"):
            print("%s: %s; now %s." % (sym_st["symbol"], sym_st["session_label"], sym_st["reason"]))
            if sym_st.get("cas_note"):
                print(sym_st["cas_note"])
        if not st["open"] and st.get("next_open"):
            print("Next open: %s IST." % st["next_open"])
        if st.get("muhurat"):
            print(st["muhurat"])
        print("Holidays: %s." % cal.source)
        for line in (note, calendar_warning(st)):
            if line:
                print(line)
    return 0 if (sym_st or st)["open"] else 1


def cmd_holidays(a):
    now = parse_now(a.now)
    cal, note = _calendar(a, now)
    year = a.year or to_ist(now).year
    days = sorted(d for d in cal.holidays if d.startswith(str(year)))
    if year not in cal.years:
        print("NSE has not published a %d holiday list that this tool could read (2027 was not out on "
              "2026-10-23). Check nseindia.com." % year)
        return 1
    print("NSE trading holidays %d (%s):" % (year, cal.source))
    for d in days:
        wd = datetime.date.fromisoformat(d).strftime("%a")
        extra = "  (Muhurat trading: timing announced by NSE)" if d in cal.muhurat else ""
        print("  %s %s  %s%s" % (d, wd, cal.holidays[d], extra))
    if note:
        print(note)
    return 0


def cmd_quote(a):
    items = _dedupe([parse_symbol(s) for s in a.symbols] + (holdings(a) if a.holdings else []))
    if not items:
        raise AlertError("which symbols? alerts.py quote RELIANCE TCS, or --holdings")
    now = parse_now(a.now)
    quotes, errors = get_quotes(items, a.offline, pushed=collect_pushed(a, now))
    if a.write and quotes:
        write_prices(a.write, quotes, now)
    if a.json:
        print(json.dumps({"quotes": quotes, "errors": errors}, indent=2, ensure_ascii=False))
    else:
        for s, _ in items:
            q = quotes.get(s)
            if q:
                chg = ""
                if q.get("prev_close"):
                    chg = " (%+.2f%% vs prev close %s)" % (100.0 * (q["price"] / q["prev_close"] - 1),
                                                          inr(q["prev_close"]))
                print("%-10s %s %s%s  %s  (%s)" % (s, q.get("exchange", "NSE"), inr(q["price"]), chg,
                                                   q.get("time", ""), src_label(q)))
        for e in errors:
            print("error: " + e)
        if a.write and quotes:
            print("Wrote %d price(s) to %s." % (len(quotes), a.write))
        print("Yahoo quotes are free, delayed and unofficial: for alerts only. The exchange price is the one "
              "that fills.")
    return 0 if quotes else 1


def cmd_check(a):
    doc = _read(a.rules, None)
    if not doc or not doc.get("rules"):
        raise AlertError("no rules in %s: add some with alerts.py add --from-plan" % a.rules)
    rules = doc["rules"]
    now = parse_now(a.now)
    cal, note = _calendar(a, now)
    status = market_status(now, cal)
    watched = [r for r in rules if active(r, status["date"])]
    items = _dedupe([parse_symbol(r.get("symbol"), r.get("exchange")) for r in watched
                     if r.get("type") not in NO_QUOTE and r.get("symbol")])
    need_prev = {parse_symbol(r["symbol"])[0] for r in watched if r.get("type") in ("pct_move", "circuit")}
    quotes, errors = (get_quotes(items, a.offline, pushed=collect_pushed(a, now), need_prev=need_prev)
                      if items else ({}, []))
    circuit_syms = {parse_symbol(r["symbol"])[0] for r in watched if r.get("type") == "circuit"}
    kite_inst = {}
    for path in a.kite_instrument or []:
        kite_inst.update(parse_kite_instrument(_read(path, None), path))
    for s in sorted(circuit_syms & set(quotes)):
        k = kite_inst.get(s)
        if not k or k.get("lower") is None or k.get("upper") is None:
            continue
        if k["date"] == status["date"]:
            quotes[s]["circuit_limits"] = (k["lower"], k["upper"])
        else:
            errors.append("Kite circuit limits for %s are from %s, not today (%s): using NSE's sec_list.csv band"
                          % (s, k["date"] or "an unknown day", status["date"]))
    if circuit_syms & set(quotes) - {s for s in quotes if quotes[s].get("circuit_limits")}:
        try:
            bands = load_bands(a.offline, a.bands, now)
        except AlertError as e:
            bands = {}
            errors.append(str(e))
        for s in sorted(circuit_syms & set(quotes) - {x for x in quotes if quotes[x].get("circuit_limits")}):
            b = bands.get(s)
            if b and b["band"]:
                quotes[s]["band"] = b["band"]
            elif b:
                errors.append("%s has no fixed price band (No Band: an F&O stock with dynamic bands), so no "
                              "circuit alert" % s)
            elif bands:
                errors.append("%s is not in NSE's sec_list.csv: no price band known" % s)
            if b and b["series"] in ("BE", "BZ"):
                errors.append("%s is in series %s (trade-to-trade): no intraday trading" % (s, b["series"]))
            if quotes[s].get("band") and not quotes[s].get("prev_close"):
                errors.append("%s: no previous close, so no circuit levels" % s)
    state = _read(a.state, {})
    rings, state = evaluate(rules, quotes, status, state, now)
    if not a.no_write:
        _write(a.state, state)
        if quotes:
            write_prices(a.prices_out, quotes, now)
    if a.json:
        print(json.dumps({"market": status, "rings": rings, "errors": errors, "calendar_note": note,
                          "quotes": {s: q["price"] for s, q in quotes.items()},
                          "sources": {s: q["source"] for s, q in quotes.items()}}, indent=2, ensure_ascii=False))
    else:
        print("NSE %s (%s IST, %s)." % ("OPEN" if status["open"] else "closed", status["now_ist"],
                                        status["reason"]))
        for line in (status.get("muhurat"), note, calendar_warning(status)):
            if line:
                print(line)
        for r in rings:
            print("ALERT " + r["message"])
        if not rings:
            print("Nothing hit. %d rule(s) watched today%s." % (
                len(watched), (", " + ", ".join("%s %s (%s)" % (s, inr(quotes[s]["price"]), src_label(quotes[s]))
                                                for s, _ in items if s in quotes)) if quotes else ""))
        for e in errors:
            print("error: " + e)
        if rings:
            print("An alert is information, not an order. Zerodha has no bracket orders: a stop (SL-M) or target "
                  "(LIMIT) works only if you placed it after the entry filled, and when one fills the other must "
                  "be cancelled (for CNC a GTT OCO does that). Check Kite get_positions / get_orders and record "
                  "any fill (ticket.py fill). To close a position yourself: ticket.py flatten, then india-risk "
                  "and your yes.")
    return 0 if rings else 1


def cmd_add(a):
    doc = _read(a.rules, {"rules": []})
    doc.setdefault("rules", [])
    if a.from_plan:
        plan = _read(a.plan, None)
        if plan is None:
            raise AlertError("no plan at %s (run india-exit first)" % a.plan)
        cfg = _read(a.config, {}) if a.config else {}
        now = parse_now(None)
        cas = _cas_for(a, parse_symbol(plan.get("symbol", ""), plan.get("exchange"))[0], now) \
            if plan.get("symbol") else None
        new = rules_from_plan(plan, (cfg or {}).get("flat_by"), cas)
        ids = {r["id"] for r in new}
        doc["rules"] = [r for r in doc["rules"] if r.get("id") not in ids] + new
        state = _read(a.state, {})
        if state.get("rung"):
            for i in ids:
                state["rung"].pop(i, None)
            _write(a.state, state)
        _write(a.rules, doc)
        for r in new:
            print("Added %s: %s" % (r["id"], json.dumps(r)))
        for w in plan_warnings(plan, new, cas):
            print("warning: " + w)
        print("Run alerts.py check every few minutes while the trade is open (a scheduled task or /loop). "
              "Yahoo quotes are delayed; with the Kite MCP connected, pass its get_ltp price with --price. "
              "The SL-M and LIMIT orders at Zerodha are what actually stop you out, not these alerts.")
        return 0
    if a.opening_bell:
        rule = {"id": a.id or "opening-bell", "type": "opening_bell"}
    elif a.pre_open:
        rule = {"id": a.id or "pre-open", "type": "pre_open"}
    elif a.mis_reminder:
        now = parse_now(None)
        day = datetime.date.fromisoformat(a.date) if a.date else to_ist(now).date()
        sym = parse_symbol(a.symbol)[0] if a.symbol else None
        cas = _cas_for(a, sym, now) if sym else None
        sr = session_rules(cas, day)
        at = a.at or _minus(sr["square_off"], 3)          # 3 minutes before the symbol's square-off
        if _hhmm(at) is None:
            raise AlertError("--at wants an IST time like 15:09")
        rule = {"id": a.id or ("%s-mis-squareoff" % sym.lower() if sym else "mis-squareoff"), "type": "time",
                "at": at, "label": "MIS auto square-off at %s" % sr["square_off"], "mis": True,
                "close": sr["close"], "square_off": sr["square_off"]}
        if sym:
            rule["symbol"], rule["exchange"] = sym, parse_symbol(a.symbol)[1]
        if cas is None:
            print("note: %s; the stricter CAS time is used (Zerodha 15:12). For a non-CAS stock pass "
                  "--symbol (or --cas no): 15:22 for its 15:25 square-off." %
                  ("no --symbol given" if not sym else "the F&O (CAS) list was not available"))
    elif a.at:
        if _hhmm(a.at) is None:
            raise AlertError("--at wants an IST time like 15:10")
        rule = {"id": a.id or ("time-%s" % a.at.replace(":", "")), "type": "time", "at": a.at,
                "label": a.label or "time alert"}
        if a.symbol:
            sym, exch = parse_symbol(a.symbol)
            rule["symbol"], rule["exchange"] = sym, exch
    else:
        if not a.symbol:
            raise AlertError("--symbol is required (or --from-plan, --at, --opening-bell, --pre-open, "
                             "--mis-reminder)")
        sym, exch = parse_symbol(a.symbol)
        if a.above is not None:
            rule = {"type": "above", "price": a.above}
        elif a.below is not None:
            rule = {"type": "below", "price": a.below}
        elif a.stop is not None:
            rule = {"type": "stop", "price": a.stop}
        elif a.target is not None:
            rule = {"type": "target", "price": a.target}
        elif a.pct is not None:
            rule = {"type": "pct_move", "pct": a.pct}
            if a.direction:
                rule["direction"] = a.direction
        elif a.circuit:
            rule = {"type": "circuit"}
            if a.within is not None:
                rule["within"] = a.within
        else:
            raise AlertError("give one of --above, --below, --stop, --target, --pct or --circuit")
        val = rule.get("price", rule.get("pct"))
        rid = "%s-%s" % (sym.lower(), rule["type"]) + ("-%s" % _n(val) if val is not None else "")
        rule = dict({"id": a.id or rid, "symbol": sym, "exchange": exch}, **rule)
        if a.rearm:
            rule["rearm"] = True
        if a.note:
            rule["note"] = a.note
    if a.date:
        rule["date"] = a.date
    validate(rule)
    if any(r.get("id") == rule["id"] for r in doc["rules"]):
        raise AlertError("a rule with id %s already exists" % rule["id"])
    doc["rules"].append(rule)
    _write(a.rules, doc)
    print("Added %s: %s" % (rule["id"], json.dumps(rule)))
    return 0


def _n(x):
    x = float(x)
    return str(int(x)) if x == int(x) else ("%.2f" % x).rstrip("0").rstrip(".")


def cmd_list(a):
    doc = _read(a.rules, {"rules": []})
    state = _read(a.state, {})
    if not doc.get("rules"):
        print("No rules yet.")
        return 1
    for r in doc["rules"]:
        rid = r.get("id")
        rung = state.get("rung", {}).get(rid)
        print("%-24s %s%s" % (rid, json.dumps({k: v for k, v in r.items() if k != "id"}),
                              ("  (rang %s)" % rung) if rung else ""))
    return 0


def cmd_reset(a):
    state = _read(a.state, {})
    if a.id not in state.get("rung", {}):
        print("%s has not rung (nothing to reset)." % a.id)
        return 1
    del state["rung"][a.id]
    _write(a.state, state)
    print("Re-armed %s." % a.id)
    return 0


def main(argv=None):
    try:
        sys.stdout.reconfigure(errors="replace")
    except (AttributeError, ValueError):
        pass
    ap = argparse.ArgumentParser(description="Price alerts and the NSE clock (india-alerts).")
    sub = ap.add_subparsers(dest="cmd")

    def cal_args(p):
        p.add_argument("--now", help="pretend it is this time (no offset = IST)")
        p.add_argument("--offline", metavar="DIR", help="read yahoo-<SYM>.NS.json, nse-holidays.json, "
                                                        "sec_list.csv from DIR, no network")
        p.add_argument("--holidays", default="trader-in/holidays.json", help="NSE holiday cache")
        p.add_argument("--refresh", action="store_true", help="fetch the NSE holiday list again")
        p.add_argument("--json", action="store_true")

    def price_args(p):
        p.add_argument("--price", action="append", metavar="SYM=PRICE",
                       help="a price pushed in, e.g. from Kite get_ltp (repeatable)")
        p.add_argument("--price-source", default="kite", help="what --price came from (default kite)")
        p.add_argument("--kite-json", "--kite-ltp", dest="kite_json", metavar="FILE",
                       help="a saved Kite get_ltp / get_quotes / get_ohlc answer, verbatim")
        p.add_argument("--max-age", type=float, default=5.0,
                       help="minutes a pushed price in prices.json stays usable (default 5)")

    def cas_args(p):
        p.add_argument("--cas", choices=["yes", "no"], help="force the symbol's closing-auction (F&O) status")
        p.add_argument("--fo-list", default="trader-in/fo_list.txt", help="cache of NSE's F&O (CAS) symbols")
        p.add_argument("--config", default="trader-in/config.json",
                       help="cas_symbols_extra / non_cas_symbols (and flat_by for add)")

    p = sub.add_parser("clock", help="is NSE open?")
    cal_args(p)
    cas_args(p)
    p.add_argument("--symbol", help="also show this symbol's session (CAS or not)")
    p = sub.add_parser("holidays", help="the NSE trading holidays for a year")
    cal_args(p)
    p.add_argument("--year", type=int)
    p = sub.add_parser("quote", help="latest quotes")
    cal_args(p)
    price_args(p)
    p.add_argument("symbols", nargs="*")
    p.add_argument("--holdings", action="store_true", help="add symbols from your portfolio and plan")
    p.add_argument("--portfolio", default="trader-in/portfolio.json")
    p.add_argument("--plan", default="trader-in/plan.json")
    p.add_argument("--prices-in", default=None, help="use fresh pushed prices from this prices.json")
    p.add_argument("--write", nargs="?", const="trader-in/prices.json", metavar="PATH",
                   help="save to prices.json (default trader-in/prices.json)")
    p = sub.add_parser("check", help="check every rule, ring what hit")
    cal_args(p)
    price_args(p)
    p.add_argument("--rules", default="trader-in/alerts.json")
    p.add_argument("--state", default="trader-in/state/alerts.json")
    p.add_argument("--prices-out", default="trader-in/prices.json")
    p.add_argument("--prices-in", default=None,
                   help="read fresh pushed prices from here (default: the --prices-out file)")
    p.add_argument("--bands", default="trader-in/sec_list.csv", help="cache of NSE's sec_list.csv")
    p.add_argument("--kite-instrument", action="append", metavar="FILE",
                   help="a saved Kite search_instruments answer: today's official circuit limits (repeatable)")
    p.add_argument("--no-write", action="store_true")
    p = sub.add_parser("add", help="add a rule")
    p.add_argument("--rules", default="trader-in/alerts.json")
    p.add_argument("--state", default="trader-in/state/alerts.json")
    p.add_argument("--symbol")
    g = p.add_mutually_exclusive_group()
    g.add_argument("--above", type=float)
    g.add_argument("--below", type=float)
    g.add_argument("--stop", type=float)
    g.add_argument("--target", type=float)
    g.add_argument("--pct", type=float)
    g.add_argument("--circuit", action="store_true", help="near the upper or lower circuit")
    g.add_argument("--opening-bell", action="store_true", help="09:15 IST")
    g.add_argument("--pre-open", action="store_true", help="09:00 IST")
    g.add_argument("--from-plan", action="store_true", help="stop, target and flat-by from the plan")
    p.add_argument("--mis-reminder", action="store_true",
                   help="a reminder 3 minutes before Zerodha's MIS square-off: 15:09 CAS, 15:22 non-CAS (--at to move it)")
    p.add_argument("--at", metavar="HH:MM", help="a time alert, IST")
    p.add_argument("--within", type=float, help="circuit: percent from the circuit (default 1)")
    p.add_argument("--plan", default="trader-in/plan.json")
    cas_args(p)
    p.add_argument("--offline", metavar="DIR", help="read fo_mktlots.csv from DIR, no network")
    p.add_argument("--refresh", action="store_true", help="fetch the F&O list again")
    p.add_argument("--label", help="what a time alert says, e.g. \"flat by\"")
    p.add_argument("--date", help="watch this rule on this IST date only (YYYY-MM-DD)")
    p.add_argument("--direction", choices=["up", "down"])
    p.add_argument("--rearm", action="store_true")
    p.add_argument("--note")
    p.add_argument("--id")
    p = sub.add_parser("list", help="list the rules")
    p.add_argument("--rules", default="trader-in/alerts.json")
    p.add_argument("--state", default="trader-in/state/alerts.json")
    p = sub.add_parser("reset", help="re-arm a rule that rang")
    p.add_argument("id")
    p.add_argument("--state", default="trader-in/state/alerts.json")
    a = ap.parse_args(argv)
    if not a.cmd:
        ap.print_help()
        return 1
    if a.cmd == "check" and not a.prices_in:
        a.prices_in = a.prices_out
    try:
        return {"clock": cmd_clock, "holidays": cmd_holidays, "quote": cmd_quote, "check": cmd_check,
                "add": cmd_add, "list": cmd_list, "reset": cmd_reset}[a.cmd](a)
    except AlertError as e:
        print("alerts.py: %s" % e, file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
