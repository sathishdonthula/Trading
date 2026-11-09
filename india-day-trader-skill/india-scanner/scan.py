#!/usr/bin/env python3
"""Chart Scanner for india-scanner: scan thousands of NSE charts for setups. Standard library only.

    python3 scan.py universe                          # NSE EQUITY_L.csv -> trader-in/universe.txt
    python3 scan.py universe --index nifty50          # only the NIFTY 50 (or nifty500)
    python3 scan.py daily                             # before the open: daily setups -> trader-in/watchlist.json
    python3 scan.py daily --symbols RELIANCE,TCS,M&M  # or --watchlist FILE, or --limit 200
    python3 scan.py orb                               # after 9:35 AM IST: opening-range breakouts so far
    python3 scan.py orb --at 10:15 --or-minutes 15    # cut the clock at 10:15 AM IST
    python3 scan.py orb --offline nifty50-5m-2026-10-19.json.gz --now "2026-10-19 10:15"

universe  Downloads NSE's list of every listed equity (EQUITY_L.csv: SYMBOL,
          NAME OF COMPANY, SERIES, ...) and the price-band list (sec_list.csv:
          Symbol, Series, Security Name, Band). Keeps series EQ. Series BE and BZ
          are trade-to-trade (every trade is for delivery: no intraday) and are
          dropped unless --include-t2t, which keeps them flagged. SME (SM/ST),
          ETFs and bonds are not in EQUITY_L.csv. --index nifty50 | nifty500
          keeps only the members of that index (NSE's index constituent CSV).
          Writes one SYMBOL|Name|Series|Band|CAS a line (CAS from fo_mktlots.csv).

daily     Pre-market scan of daily CLOSES (Yahoo spark: 20 symbols a request,
          3 months of 1-day bars, closes only). Liquidity from the spark meta:
          regularMarketPrice >= --min-price (20) and last session's traded value
          (price x volume) >= --min-turnover (5 crore rupees). Setups, from closes:
            breakout_20d     the last close is above every close of the 20 before it
            bull_flag        up more than 10 % over the 10 sessions before the
                             last 5, then the last 5 closes inside a 5 % range
            reclaim_ma20     closed under the 20-day average, then closed above it
            oversold         RSI 14 (Wilder) under 30
            oversold_bounce  RSI 14 was under 30 yesterday and today closed up
          Today's daily bar is dropped until the 3:30 PM close.

orb       Opening-range breakouts, 5-minute bars. The range is the first
          --or-minutes (15) of the session: 9:15 to 9:30 IST. Stage 1: spark
          closes for every symbol; a chart goes on only if a completed bar after
          the range closed at or above the highest close of the range (a necessary
          condition). Stage 2: each candidate's v8 chart (real highs and lows):
          range high = the highest high of the range bars, a hit = the first
          completed bar to CLOSE above it, at that bar's close time. Stage 2
          fetches at most --max-confirm (200) charts, the most traded first.
          A chart is usable when the range bars are there and at most 3 bars
          from the range to the cutoff are empty (Yahoo leaves some NSE bars,
          most days 15:20 and 15:25, without a price: they are skipped). Trade-to-trade (BE/BZ) charts are skipped: no intraday.

CAS: since 2026-08-03 F&O stocks (NSE fo_mktlots.csv) are in the closing
auction 15:15-15:35: continuous trading ends 15:15 (72 bars), Zerodha squares
off MIS at 15:12. Others trade to 15:30 (75 bars), square-off 15:25. Each
stock uses session_rules(cas, day); unknown CAS status -> the stricter rules.

Kite: --kite-ltp FILE (get_ltp / get_quotes output) adds the live price to each
hit; --kite-bars FILE (get_historical_data output) replaces Yahoo for that
chart. The scanner never calls Kite itself.

Price bands: a stock with a band (2, 5, 10, 20 %) cannot trade beyond the
previous close x (1 +/- band). A hit within 1 % of its upper circuit is marked
"near_upper_circuit" (a long entry there is blocked by india-risk). Bands come
from sec_list.csv through universe.txt (column 4) or --bands FILE. "No Band"
(most F&O stocks) means no fixed circuit.

Bars: null closes are dropped, a bar off the 5-minute grid (the live tick)
is dropped, and a bar counts only once it is complete (start + 5 minutes <=
the clock). Times are India time (IST, UTC+05:30, no daylight saving). --now
pins the clock ("2026-10-19 10:15" IST; ...Z for UTC).

Yahoo's endpoints are free, unofficial and can lag. query1 first, query2 on
a 429 or 5xx, one retry after a pause on a 429. NSE files are fetched with a
browser user agent (NSE does not answer scripts that say they are scripts).

--offline PATH reads files instead of the network: universe wants a folder
with EQUITY_L.csv (and sec_list.csv, ind_nifty50list.csv / ind_nifty500list.csv);
daily and orb take a folder of <SYMBOL>.json v8 chart files or spark answers,
or one file that is a spark answer, a v8 chart, or a bundle {"RELIANCE": <v8>, ...}.

A scan is a list of charts that match a rule, not a list of stocks to buy.
Exit code 0 when the scan found something, 1 when it did not or could not run.
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

UA = "Mozilla/5.0 (compatible; india-day-trader-skill/1.0; +https://github.com/)"   # Yahoo: a fake Chrome UA gets 429
NSE_UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) "
          "Chrome/130 Safari/537.36")                                                  # NSE: hangs on script UAs
YAHOO_HOSTS = ("https://query1.finance.yahoo.com", "https://query2.finance.yahoo.com")
NSE_ARCHIVES = "https://nsearchives.nseindia.com/content/"
EQUITY_FILE = "EQUITY_L.csv"
BANDS_FILE = "sec_list.csv"
FO_FILE = "fo_mktlots.csv"   # NSE F&O underlyings: these stocks are in the closing auction (CAS)
INDEX_FILES = {"nifty50": "ind_nifty50list.csv", "nifty500": "ind_nifty500list.csv"}
NSE_COOKIE_PAGE = "https://www.nseindia.com/companies-listing/corporate-filings-insider-trading"
HOLIDAY_API = "https://www.nseindia.com/api/holiday-master?type=trading"
SPARK_MAX = 20      # Yahoo's spark answers 400 above 20 symbols
PAUSE = 0.3         # seconds between Yahoo requests: be polite to free endpoints
NSE_PAUSE = 1.0     # NSE: at most one request a second
BACKOFF = 5.0       # seconds to wait before the one retry after a 429
SLEEP = time.sleep  # tests swap this out
BAR_MIN = 5
MAX_CONFIRM = 200   # stage-2 v8 charts per orb scan, most traded first
MIN_DAILY_BARS = 30
MAX_EMPTY = 3       # empty (null) 5-minute bars a chart may have after the range
NEAR_UC = 0.01      # within 1 % of the upper circuit
DEFAULT_UNIVERSE = "trader-in/universe.txt"
DEFAULT_HOLIDAYS = "trader-in/holidays.json"
DEFAULT_FO_LIST = "trader-in/fo_list.txt"
FO_MAX_AGE_DAYS = 7
NOTE = "A scan is a list of charts that match a rule, not a list of stocks to buy."
T2T = ("BE", "BZ")  # trade-to-trade series: delivery only, no intraday

IST = datetime.timezone(datetime.timedelta(hours=5, minutes=30))
IST_OFFSET = datetime.timedelta(hours=5, minutes=30)

# NSE capital-market trading holidays 2026 (nseindia.com holiday-master, read 2026-10-23).
# Weekend dates are harmless. 11-08 is Diwali Laxmi Pujan: a short Muhurat session at a time NSE announces.
NSE_HOLIDAYS = {
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
    "2026-11-08": "Diwali Laxmi Pujan (Muhurat trading session only)",
    "2026-11-10": "Diwali-Balipratipada",
    "2026-11-24": "Prakash Gurpurb Sri Guru Nanak Dev",
    "2026-12-25": "Christmas",
}
CALENDAR_YEARS = {2026}  # 2027 is not published yet: the live API (cached in trader-in/holidays.json) covers it
OPEN = datetime.time(9, 15)
CLOSE = datetime.time(15, 30)   # the latest close: non-CAS stocks
CAS_START = datetime.date(2026, 8, 3)  # NSE's closing auction session (15:15-15:35) for F&O stocks starts

SETUPS = [
    ("breakout_20d", "20-day closing-high breakout"),
    ("bull_flag", "bull flag"),
    ("reclaim_ma20", "reclaimed the 20-day average"),
    ("oversold", "oversold (RSI under 30)"),
    ("oversold_bounce", "oversold bounce"),
]
LABEL = dict(SETUPS)


class ScanError(Exception):
    pass


# ---------------------------------------------------------------- symbols

def parse_symbol(text):
    """'RELIANCE', 'reliance.ns', 'NSE:RELIANCE' -> ('RELIANCE', 'NSE'); 'RELIANCE.BO', 'BSE:RELIANCE',
    '500325' (a BSE code) -> (.., 'BSE')."""
    s = (text or "").strip().upper()
    if s.startswith("NSE:"):
        return s[4:], "NSE"
    if s.startswith("BSE:"):
        return s[4:], "BSE"
    if s.endswith(".NS"):
        return s[:-3], "NSE"
    if s.endswith(".BO"):
        return s[:-3], "BSE"
    if re.match(r"^\d{6}$", s):
        return s, "BSE"
    return s, "NSE"


def key(sym, exch="NSE"):
    """The key a chart is stored under: 'RELIANCE' on NSE, 'BSE:RELIANCE' on BSE."""
    return sym if exch == "NSE" else "BSE:" + sym


def norm(text):
    return key(*parse_symbol(text))


def split_key(k):
    return parse_symbol(k)


def to_yahoo(k):
    """Our key -> Yahoo's symbol, URL-encoded: M&M -> M%26M.NS, BSE:500325 -> 500325.BO."""
    sym, exch = split_key(k)
    return urllib.parse.quote(sym, safe="-") + (".NS" if exch == "NSE" else ".BO")


def spark_path(keys, rng, interval):
    return "/v7/finance/spark?symbols=%s&range=%s&interval=%s" % (",".join(to_yahoo(k) for k in keys), rng, interval)


def chart_path(k, rng, interval):
    return "/v8/finance/chart/%s?range=%s&interval=%s" % (to_yahoo(k), rng, interval)


# ---------------------------------------------------------------- ticks and bands

def tick_size(price):
    """NSE equity tick size by price (from Apr 15 2025; NSE reviews it monthly)."""
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


def round_to_tick(price, mode="nearest"):
    """Round to the NSE tick: mode 'down', 'up' or 'nearest'."""
    t = tick_size(price)
    n = float(price) / t
    if mode == "down":
        k = int(n + 1e-9) if n >= 0 else -int(-n + 1 - 1e-9)
    elif mode == "up":
        k = int(n - 1e-9) + (0 if abs(n - round(n)) < 1e-9 else 1)
    else:
        k = int(round(n))
    return round(k * t, 2)


def parse_band(text):
    """'20' -> 20.0, 'No Band' / '' -> None."""
    try:
        return float(str(text).strip())
    except (TypeError, ValueError):
        return None


def circuits(prev_close, band):
    """(upper, lower) circuit from the previous close and the band in %, rounded inside the band to the tick."""
    if prev_close is None or band is None:
        return None, None
    up = round_to_tick(prev_close * (1 + band / 100.0), "down")
    lo = round_to_tick(prev_close * (1 - band / 100.0), "up")
    return up, lo


def band_fields(price, prev_close, band):
    """Watchlist fields for the price band. near_upper_circuit: price within 1 % of the upper circuit."""
    up, lo = circuits(prev_close, band)
    return {"band": band, "prev_close": None if prev_close is None else round(prev_close, 2),
            "upper_circuit": up, "lower_circuit": lo,
            "near_upper_circuit": bool(up is not None and price >= up * (1 - NEAR_UC))}


# ---------------------------------------------------------------- time

def to_ist(utc):
    """Naive UTC -> naive IST (a fixed UTC+05:30)."""
    return utc.replace(tzinfo=None) + IST_OFFSET


def ist_to_utc(local):
    return local.replace(tzinfo=None) - IST_OFFSET


def ts_to_ist(ts):
    """Unix seconds -> naive India time. Never uses the machine's time zone."""
    return datetime.datetime(1970, 1, 1) + datetime.timedelta(seconds=int(ts)) + IST_OFFSET


def parse_now(text):
    """ISO time -> naive UTC. A time without an offset is taken as India time."""
    if not text:
        return datetime.datetime.now(datetime.timezone.utc).replace(microsecond=0, tzinfo=None)
    t = text.strip().replace("Z", "+00:00")
    try:
        dt = datetime.datetime.fromisoformat(t)
    except ValueError:
        raise ScanError('--now wants a time like "2026-10-19 10:15" (IST) or ...Z (UTC)')
    if dt.tzinfo is None:
        return ist_to_utc(dt)
    return (dt - dt.utcoffset()).replace(tzinfo=None)


def parse_hhmm(text, flag="--at"):
    m = re.match(r"^\s*(\d{1,2}):(\d{2})\s*$", text or "")
    if not m or int(m.group(1)) > 23 or int(m.group(2)) > 59:
        raise ScanError("%s wants an India time like 10:15" % flag)
    return datetime.time(int(m.group(1)), int(m.group(2)))


HOLIDAYS = dict(NSE_HOLIDAYS)
HOLIDAY_YEARS = set(CALENDAR_YEARS)


def parse_holidays(doc):
    """NSE's holiday-master JSON ({"CM": [{"tradingDate": "26-Jan-2026", "description": ...}]}), or
    {"holidays": {"2026-01-26": "Republic Day"}}, or a list of ISO dates -> {iso: name}."""
    out = {}
    if isinstance(doc, dict) and isinstance(doc.get("CM"), list):
        for row in doc["CM"]:
            try:
                d = datetime.datetime.strptime(row.get("tradingDate", ""), "%d-%b-%Y").date()
            except ValueError:
                continue
            out[d.isoformat()] = (row.get("description") or "NSE holiday").rstrip("*").strip()
    elif isinstance(doc, dict) and isinstance(doc.get("holidays"), (dict, list)):
        return parse_holidays(doc["holidays"])
    elif isinstance(doc, dict):
        for k, v in doc.items():
            if re.match(r"^\d{4}-\d{2}-\d{2}$", str(k)):
                out[k] = str(v or "NSE holiday")
    elif isinstance(doc, list):
        for x in doc:
            d = x.get("date") if isinstance(x, dict) else x
            if isinstance(d, str) and re.match(r"^\d{4}-\d{2}-\d{2}$", d):
                out[d] = (x.get("name") or x.get("description") if isinstance(x, dict) else None) or "NSE holiday"
    return out


def use_holidays(extra):
    """Add holidays (from the cache or the live API) to the built-in list."""
    for d, name in extra.items():
        HOLIDAYS[d] = name
        HOLIDAY_YEARS.add(int(d[:4]))


def load_holiday_cache(path):
    if not path or not os.path.exists(path):
        return False
    try:
        with open(path, encoding="utf-8") as fh:
            got = parse_holidays(json.load(fh))
    except (ValueError, OSError):
        return False
    use_holidays(got)
    return bool(got)


def trading_day(d):
    """(is_trading_day, reason). Weekends and the NSE holiday list."""
    if d.weekday() >= 5:
        return False, "weekend"
    name = HOLIDAYS.get(d.isoformat())
    if name:
        return False, "NSE holiday: " + name
    return True, ""


def session(d):
    """(open_time, close_time) in India time, or None if closed all day. NSE has no early closes."""
    ok, _ = trading_day(d)
    return (OPEN, CLOSE) if ok else None


def session_rules(cas, day):
    """The day's rules for one stock. cas: True / False / None (unknown -> the stricter CAS rules).
    Same function in every script of the pack. Zerodha square-off times: support.zerodha.com, read 2026-10-23."""
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


def _hm(text):
    return datetime.time(int(text[:2]), int(text[3:5]))


def clock_label(t):
    """datetime or time -> '10:15 AM'."""
    h = t.hour % 12 or 12
    return "%d:%02d %s" % (h, t.minute, "AM" if t.hour < 12 else "PM")


def day_label(d):
    return "%s %s" % (d.strftime("%a"), d.isoformat())


# ---------------------------------------------------------------- network

def http_get(url, timeout=20, ua=UA):
    req = urllib.request.Request(url, headers={"User-Agent": ua, "Accept": "*/*"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode("utf-8", "replace")


def yahoo_get(path):
    """GET a Yahoo Finance path. query1 first, query2 on a 429 or 5xx, and one
    more round after BACKOFF seconds if a 429 was seen. 4xx other than 429 is final."""
    last = "no answer"
    for attempt in (0, 1):
        saw_429 = False
        for host in YAHOO_HOSTS:
            try:
                return http_get(host + path)
            except urllib.error.HTTPError as e:
                last = "HTTP %d" % e.code
                try:
                    body = e.read().decode("utf-8", "replace") if 400 <= e.code < 500 and e.code != 429 else ""
                except Exception:
                    body = ""
                finally:
                    e.close()
                if e.code == 429:
                    saw_429 = True
                    continue
                if e.code >= 500:
                    continue
                if body.strip().startswith("{"):
                    return body  # Yahoo's JSON error ("No data found"), parsed by the caller
                raise ScanError("Yahoo answered %s for %s" % (last, path.split("?")[0]))
            except (urllib.error.URLError, OSError) as e:
                last = str(getattr(e, "reason", e))
                continue
        if attempt == 0 and saw_429:
            SLEEP(BACKOFF)
            continue
        break
    raise ScanError("Yahoo answered %s for %s" % (last, path.split("?")[0]))


def nse_get(url, timeout=60):
    """GET an NSE archive file (no cookie needed): the browser UA first, then the compatible UA
    (the archives host drops one or the other at times)."""
    last = None
    for n, ua in enumerate((NSE_UA, UA)):
        if n:
            SLEEP(NSE_PAUSE)
        try:
            return http_get(url, timeout=timeout, ua=ua)
        except urllib.error.HTTPError as e:
            last = "HTTP %d" % e.code
            e.close()
        except (urllib.error.URLError, OSError) as e:
            last = str(getattr(e, "reason", e))
    raise ScanError("NSE did not answer for %s (%s). NSE blocks some networks; try again in a minute, "
                    "or download the file in a browser and pass --offline FOLDER." % (url.rsplit("/", 1)[-1], last))


def fetch_holidays_live(timeout=30):
    """NSE's holiday API needs a cookie: GET a page of nseindia.com first (the home page answers 403 to
    scripts; this one sets the cookie), then the API with a Referer. -> raw JSON doc."""
    cj = http.cookiejar.CookieJar()
    op = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cj))
    hdr = {"User-Agent": NSE_UA, "Accept": "text/html,*/*", "Accept-Language": "en-US,en;q=0.9"}
    try:
        op.open(urllib.request.Request(NSE_COOKIE_PAGE, headers=hdr), timeout=timeout).read()
    except urllib.error.HTTPError:
        pass
    SLEEP(NSE_PAUSE)
    req = urllib.request.Request(HOLIDAY_API, headers=dict(hdr, Accept="application/json",
                                                             Referer="https://www.nseindia.com/"))
    with op.open(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8", "replace"))


def ensure_holidays(day, cache_path, offline=False):
    """Make sure the holiday list covers `day`'s year: the cache, then (online) the live API.
    Returns a warning line or ''."""
    load_holiday_cache(cache_path)
    if day.year in HOLIDAY_YEARS:
        return ""
    if not offline:
        try:
            doc = fetch_holidays_live()
            got = parse_holidays(doc)
            if got:
                use_holidays(got)
                if cache_path:
                    write_json(cache_path, dict(doc, source=HOLIDAY_API,
                                                fetched_at=datetime.datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")))
        except (urllib.error.URLError, OSError, ValueError):
            pass
    if day.year in HOLIDAY_YEARS:
        return ""
    return ("NSE's %d holiday list is not in this tool yet (NSE has not published it, or nseindia.com did not "
            "answer): only weekends are treated as closed." % day.year)


# ---------------------------------------------------------------- parsing

def series_from_result(res):
    """A Yahoo chart result (v8 or one spark response) -> plain lists."""
    q = ((res.get("indicators") or {}).get("quote") or [{}])[0] or {}
    t = res.get("timestamp") or []
    n = len(t)

    def col(k):
        v = q.get(k)
        return list(v) + [None] * (n - len(v)) if isinstance(v, list) else None
    return {"t": list(t), "c": col("close") or [None] * n, "o": col("open"), "h": col("high"),
            "l": col("low"), "v": col("volume"), "meta": res.get("meta") or {}}


def parse_spark(text_or_doc):
    """Spark JSON -> {key: series}. Symbols Yahoo does not know are simply missing from the answer.
    Yahoo's 'M&M.NS' comes back as our 'M&M', '500325.BO' as 'BSE:500325'."""
    doc = json.loads(text_or_doc) if isinstance(text_or_doc, str) else text_or_doc
    sp = (doc or {}).get("spark") if isinstance(doc, dict) else None
    if not isinstance(sp, dict):
        raise ScanError("Yahoo's spark answer is not the shape this tool knows")
    err = sp.get("error")
    out = {}
    for item in sp.get("result") or []:
        resp = item.get("response") or []
        if not resp:
            continue
        ysym = item.get("symbol") or (resp[0].get("meta") or {}).get("symbol") or ""
        out[norm(urllib.parse.unquote(ysym))] = series_from_result(resp[0])
    if err and not out:
        raise ScanError("Yahoo spark: %s" % (err.get("description") or err.get("code") or err))
    return out


def parse_chart(text_or_doc, sym=""):
    """v8 chart JSON -> series. Raises ScanError for Yahoo's 'no data' answers."""
    try:
        doc = json.loads(text_or_doc) if isinstance(text_or_doc, str) else text_or_doc
        chart = doc["chart"]
    except (ValueError, KeyError, TypeError):
        raise ScanError("Yahoo returned something unexpected for %s" % sym)
    res = chart.get("result")
    if not res:
        err = chart.get("error") or {}
        raise ScanError("no data for %s: %s" % (sym, err.get("description", "unknown symbol")))
    return series_from_result(res[0])


def _read_maybe_gz(path):
    if path.endswith(".gz"):
        with gzip.open(path, "rt", encoding="utf-8") as fh:
            return fh.read()
    with open(path, encoding="utf-8") as fh:
        return fh.read()


def _stem(name):
    for ext in (".json.gz", ".json"):
        if name.endswith(ext):
            return name[:-len(ext)]
    return name


KITE_NAME = re.compile(r"^(.+?)-(?:\d*minute|day|\d+min)(?:-|$)", re.I)


def is_kite_candles(doc):
    return isinstance(doc, list) and bool(doc) and isinstance(doc[0], dict) and "date" in doc[0] and "close" in doc[0]


def kite_symbol(name):
    """'RELIANCE-5minute-2026-10-19-open' -> 'RELIANCE' (BAJAJ-AUTO-5minute-... -> BAJAJ-AUTO)."""
    name = re.sub(r"(?i)^kite-", "", name or "")
    m = KITE_NAME.match(name)
    return (m.group(1) if m else name).upper()


def parse_kite_candles(docs):
    """Kite get_historical_data output (a list of {"date": "2026-10-19T09:15:00+05:30", "open", "high", "low",
    "close", "volume", "oi"}), one or more chunks -> series, merged and de-duplicated by date."""
    rows = {}
    for doc in docs:
        if isinstance(doc, dict) and isinstance(doc.get("data"), (list, dict)):
            doc = doc["data"].get("candles", doc["data"]) if isinstance(doc["data"], dict) else doc["data"]
        for r in doc or []:
            try:
                dt = datetime.datetime.fromisoformat(str(r["date"]).replace("Z", "+00:00"))
            except (KeyError, ValueError, TypeError):
                continue
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=IST)
            utc = (dt - dt.utcoffset()).replace(tzinfo=None)
            rows[int((utc - datetime.datetime(1970, 1, 1)).total_seconds())] = r
    t = sorted(rows)
    col = lambda k: [rows[x].get(k) for x in t]
    return {"t": t, "c": col("close"), "o": col("open"), "h": col("high"), "l": col("low"), "v": col("volume"),
            "meta": {"source": "kite"}}


def load_kite_bars(paths):
    """--kite-bars FILE ... -> {key: series}. The symbol comes from the file name (trader-in/kite/<SYMBOL>-5minute-...)."""
    by = {}
    for path in paths or []:
        if not os.path.exists(path):
            raise ScanError("no file at %s" % path)
        try:
            doc = json.loads(_read_maybe_gz(path))
        except ValueError as e:
            raise ScanError("%s is not valid JSON: %s" % (path, e))
        by.setdefault(norm(kite_symbol(_stem(os.path.basename(path)))), []).append(doc)
    return {k: parse_kite_candles(v) for k, v in by.items()}


def parse_kite_ltp(doc):
    """Kite get_ltp / get_quotes output ({"NSE:RELIANCE": {"last_price": 1190, ...}}, maybe under "data") ->
    {key: price}."""
    if isinstance(doc, dict) and isinstance(doc.get("data"), dict):
        doc = doc["data"]
    out = {}
    for k, v in (doc or {}).items():
        if isinstance(v, dict) and v.get("last_price") is not None:
            out[norm(k)] = float(v["last_price"])
    return out


def _absorb(doc, name_hint, data, errors):
    """One parsed offline JSON doc -> data / errors."""
    if is_kite_candles(doc):
        k = norm(kite_symbol(name_hint))
        data[k] = parse_kite_candles([doc])
        return
    if isinstance(doc, dict) and "spark" in doc:
        data.update(parse_spark(doc))
    elif isinstance(doc, dict) and "chart" in doc:
        sym = name_hint
        if not sym:
            try:
                sym = (doc["chart"].get("result") or [{}])[0]["meta"]["symbol"]
            except (AttributeError, KeyError, TypeError, IndexError):
                raise ScanError("an offline v8 chart without a symbol: name the file <SYMBOL>.json")
        k = norm(sym)
        try:
            data[k] = parse_chart(doc, k)
        except ScanError as e:
            errors[k] = str(e)
    elif isinstance(doc, dict):
        for k, v in doc.items():
            _absorb(v, k, data, errors)
    else:
        raise ScanError("offline file %s is not a spark answer, a v8 chart or a bundle" % name_hint)


def load_offline(path):
    """A folder of <SYMBOL>.json(.gz) files or one file -> ({key: series}, {key: error})."""
    data, errors = {}, {}
    if os.path.isdir(path):
        names = sorted(f for f in os.listdir(path) if f.endswith((".json", ".json.gz")))
        if not names:
            raise ScanError("no .json files in %s" % path)
        for f in names:
            try:
                doc = json.loads(_read_maybe_gz(os.path.join(path, f)))
            except ValueError as e:
                raise ScanError("%s is not valid JSON: %s" % (f, e))
            if is_kite_candles(doc) or (isinstance(doc, dict) and ("chart" in doc or "spark" in doc)):
                _absorb(doc, "" if isinstance(doc, dict) and "spark" in doc else _stem(f), data, errors)
            else:
                _absorb(doc, "", data, errors)
    elif os.path.isfile(path):
        try:
            doc = json.loads(_read_maybe_gz(path))
        except ValueError as e:
            raise ScanError("%s is not valid JSON: %s" % (path, e))
        _absorb(doc, _stem(os.path.basename(path)) if is_kite_candles(doc) else "", data, errors)
    else:
        raise ScanError("no offline file or folder at %s" % path)
    return data, errors


# ---------------------------------------------------------------- universe

def _csv_rows(text):
    """CSV text -> list of dicts with stripped keys and values (NSE's headers carry leading spaces)."""
    rd = csv.reader(io.StringIO(text.lstrip("﻿")))
    try:
        hdr = [h.strip() for h in next(rd)]
    except StopIteration:
        return [], []
    rows = [{h: (v or "").strip() for h, v in zip(hdr, r)} for r in rd if r and any(x.strip() for x in r)]
    return hdr, rows


def parse_equity_list(text):
    """EQUITY_L.csv -> rows. Raises ScanError if it is not that file (NSE sometimes serves an HTML page)."""
    hdr, rows = _csv_rows(text)
    if "SYMBOL" not in hdr or "SERIES" not in hdr:
        raise ScanError("not NSE's EQUITY_L.csv (header %s)" % ",".join(hdr)[:60])
    return rows


def parse_bands(text):
    """sec_list.csv -> {(SYMBOL, SERIES): band or None}."""
    hdr, rows = _csv_rows(text)
    if "Symbol" not in hdr or "Band" not in hdr:
        raise ScanError("not NSE's sec_list.csv (header %s)" % ",".join(hdr)[:60])
    return {(r["Symbol"].upper(), r.get("Series", "").upper()): parse_band(r["Band"]) for r in rows}


def parse_index(text):
    """ind_nifty50list.csv / ind_nifty500list.csv -> set of symbols."""
    hdr, rows = _csv_rows(text)
    if "Symbol" not in hdr:
        raise ScanError("not an NSE index constituent list (header %s)" % ",".join(hdr)[:60])
    return {r["Symbol"].upper() for r in rows if r.get("Symbol")}


def build_universe(equity_text, bands_text=None, index_text=None, include_t2t=False, fo=None):
    """-> (kept [(SYM, name, series, band, cas)], counts). band is None for 'No Band' or unknown;
    cas is True / False from the F&O list `fo` (a set), None when there is no list."""
    rows = parse_equity_list(equity_text)
    bands = parse_bands(bands_text) if bands_text else {}
    members = parse_index(index_text) if index_text else None
    kept, counts = {}, {"rows": len(rows)}

    def drop(why):
        counts[why] = counts.get(why, 0) + 1
    for r in rows:
        sym, series = r["SYMBOL"].upper(), r["SERIES"].upper()
        if members is not None and sym not in members:
            drop("not in the index")
            continue
        if series in T2T:
            counts["t2t_" + series] = counts.get("t2t_" + series, 0) + 1
            if not include_t2t:
                drop("trade-to-trade (BE/BZ, no intraday)")
                continue
        elif series != "EQ":
            drop("series " + series)
            continue
        kept[sym] = (sym, " ".join(r.get("NAME OF COMPANY", "").split()), series, bands.get((sym, series)),
                     None if fo is None else sym in fo)
    counts["keep"] = len(kept)
    if members is not None:
        counts["index_members"] = len(members)
        counts["index_missing"] = len(members - set(r["SYMBOL"].upper() for r in rows))
    return [kept[k] for k in sorted(kept)], counts


def parse_fo_list(text):
    """NSE's fo_mktlots.csv (UNDERLYING, SYMBOL, lots...) or our fo_list.txt (one symbol a line) -> set of
    stock symbols. Index rows (UNDERLYING starting NIFTY) and the repeated header row are skipped."""
    text = text.lstrip("\ufeff")
    first = text.split("\n", 1)[0]
    out = set()
    if "," in first and "UNDERLYING" in first.upper():
        for r in csv.reader(io.StringIO(text)):
            if len(r) < 2:
                continue
            und, sym = r[0].strip().upper(), r[1].strip().upper()
            if not sym or sym == "SYMBOL" or und.startswith("NIFTY") or und == "UNDERLYING":
                continue
            out.add(sym)
        return out
    for ln in text.splitlines():
        ln = ln.strip()
        if ln and not ln.startswith("#"):
            out.add(ln.split(",")[0].strip().upper())
    return out


def read_fo_cache(path):
    """-> (set or None, fetched date or None)."""
    if not path or not os.path.exists(path):
        return None, None
    with open(path, encoding="utf-8", errors="replace") as fh:
        text = fh.read()
    m = re.match(r"#\s*fetched\s+(\d{4}-\d{2}-\d{2})", text)
    when = datetime.date.fromisoformat(m.group(1)) if m else None
    if when is None:
        when = datetime.date.fromtimestamp(os.path.getmtime(path))
    return parse_fo_list(text), when


def write_fo_cache(path, symbols, today):
    d = os.path.dirname(path)
    if d:
        os.makedirs(d, exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("# fetched %s (NSE fo_mktlots.csv: stocks with F&O = closing auction 15:15-15:35)\n" % today.isoformat())
        for x in sorted(symbols):
            fh.write(x + "\n")


def fetch_fo_list():
    return parse_fo_list(nse_get(NSE_ARCHIVES + "fo/" + FO_FILE))


def cas_word(cas):
    return "unknown" if cas is None else ("yes" if cas else "no")


def parse_cas_word(text):
    t = (text or "").strip().lower()
    return True if t in ("yes", "cas", "true", "y") else False if t in ("no", "non-cas", "false", "n") else None


def read_universe_meta(path):
    """universe.txt (SYMBOL|Name|Series|Band|CAS) -> {key: {"name", "series", "band", "cas"}}; {} if no file."""
    out = {}
    if not path or not os.path.exists(path):
        return out
    with open(path, encoding="utf-8") as fh:
        for ln in fh:
            ln = ln.strip()
            if not ln or ln.startswith("#"):
                continue
            p = ln.split("|")
            out[norm(p[0])] = {"name": p[1] if len(p) > 1 else "", "series": (p[2] if len(p) > 2 else "EQ") or "EQ",
                               "band": parse_band(p[3]) if len(p) > 3 else None,
                               "cas": parse_cas_word(p[4]) if len(p) > 4 else None}
    return out


def read_bands_file(path):
    """A sec_list.csv -> {key: {"series", "band"}} (the EQ row wins over BE/BZ for the same symbol)."""
    with open(path, encoding="utf-8", errors="replace") as fh:
        b = parse_bands(fh.read())
    out = {}
    for (sym, series), band in sorted(b.items(), key=lambda kv: kv[0][1] != "EQ", reverse=True):
        if series in ("EQ",) + T2T:
            out[sym] = {"name": "", "series": series, "band": band, "cas": None}
    return out


# ---------------------------------------------------------------- daily setups

def rsi(closes, n=14):
    """Wilder's RSI on closes."""
    if len(closes) < n + 1:
        return None
    g = [max(0.0, closes[i] - closes[i - 1]) for i in range(1, len(closes))]
    lo = [max(0.0, closes[i - 1] - closes[i]) for i in range(1, len(closes))]
    ag, al = sum(g[:n]) / n, sum(lo[:n]) / n
    for i in range(n, len(g)):
        ag = (ag * (n - 1) + g[i]) / n
        al = (al * (n - 1) + lo[i]) / n
    return 100 - 100 / (1 + ag / al) if al else 100.0


def daily_closes(series, now_utc):
    """[(date, close)] of COMPLETE daily bars: null closes dropped, today's bar dropped
    until the 3:30 PM close, anything dated after the clock dropped."""
    now = to_ist(now_utc)
    today = now.date()
    s = session(today)
    today_done = s is None or now.time() >= s[1]
    out = {}
    for ts, c in zip(series["t"], series["c"]):
        if c is None:
            continue
        d = ts_to_ist(ts).date()
        if d > today or (d == today and not today_done):
            continue
        out[d] = float(c)  # one bar a day: a late duplicate (the live tick) wins
    return sorted(out.items())


def find_setups(closes):
    """closes (oldest first, >= 30) -> (setup keys, numbers)."""
    c = closes
    last = c[-1]
    hi20 = max(c[-21:-1])
    ma20 = sum(c[-20:]) / 20.0
    ma20_prev = sum(c[-21:-1]) / 20.0
    run = c[-6] / c[-16] - 1
    rng5 = (max(c[-5:]) - min(c[-5:])) / last
    r = rsi(c)
    r_prev = rsi(c[:-1])
    tags = []
    if last > hi20:
        tags.append("breakout_20d")
    if run > 0.10 and rng5 < 0.05:
        tags.append("bull_flag")
    if c[-2] < ma20_prev and last > ma20:
        tags.append("reclaim_ma20")
    if r is not None and r < 30:
        tags.append("oversold")
    if r_prev is not None and r_prev < 30 and last > c[-2]:
        tags.append("oversold_bounce")
    return tags, {"rsi": r, "rsi_prev": r_prev, "high20": hi20, "ma20": ma20,
                  "run_pct": run * 100, "range5_pct": rng5 * 100}


def _info(meta, k):
    m = (meta or {}).get(k) or {}
    return m.get("series") or "EQ", m.get("band")


def _cas(meta, k):
    """CAS status of a key; a BSE listing follows its NSE symbol."""
    m = (meta or {}).get(k) or (meta or {}).get(split_key(k)[0]) or {}
    return m.get("cas")


def rule_fields(cas, day):
    r = session_rules(cas, day)
    return {"cas": cas, "session_close": r["close"], "square_off": r["square_off"],
            "flat_by_default": r["flat_by_default"], "no_entries_after_default": r["no_entries_after_default"]}


def scan_daily(data, symbols, now_utc, min_price, min_volume, min_turnover=0, meta=None, include_t2t=False):
    """Pure. data = {key: series}; meta = {key: {"series", "band"}}. Returns (hits, counts, last_date)."""
    hits, dates = [], {}
    counts = {"requested": len(symbols), "fetched": 0, "t2t": 0, "illiquid": 0, "short": 0, "scanned": 0,
              "cas_unknown": 0}
    for k in symbols:
        s = data.get(k)
        if not s:
            continue
        counts["fetched"] += 1
        series, band = _info(meta, k)
        if _cas(meta, k) is None:
            counts["cas_unknown"] += 1
        if series in T2T and not include_t2t:
            counts["t2t"] += 1
            continue
        bars = daily_closes(s, now_utc)
        m = s.get("meta") or {}
        price = m.get("regularMarketPrice")
        if price is None and bars:
            price = bars[-1][1]
        vol = m.get("regularMarketVolume")
        if (price is None or price < min_price or (min_volume > 0 and (vol is None or vol < min_volume))
                or (min_turnover > 0 and (vol is None or price * vol < min_turnover))):
            counts["illiquid"] += 1
            continue
        if len(bars) < MIN_DAILY_BARS:
            counts["short"] += 1
            continue
        counts["scanned"] += 1
        d_last = bars[-1][0]
        dates[d_last] = dates.get(d_last, 0) + 1
        closes = [c for _, c in bars]
        tags, nums = find_setups(closes)
        if tags:
            sym, exch = split_key(k)
            h = {"symbol": sym, "exchange": exch, "setups": tags, "price": round(closes[-1], 2),
                 "close_date": d_last.isoformat(), "rsi": round(nums["rsi"], 1),
                 "volume": int(vol) if vol is not None else None, "series": series,
                 "intraday": series not in T2T}
            h.update(band_fields(closes[-1], closes[-2], band))  # did it close near the day's upper circuit?
            h.update(rule_fields(_cas(meta, k), to_ist(now_utc).date()))
            hits.append(h)
    hits.sort(key=lambda h: (-(h["price"] * (h["volume"] or 0)), h["symbol"]))  # most rupees traded first
    last_date = max(dates, key=lambda d: (dates[d], d)) if dates else None
    counts["hits"] = len(hits)
    return hits, counts, last_date


# ---------------------------------------------------------------- opening-range breakout

def slot(day, k):
    return datetime.datetime.combine(day, OPEN) + datetime.timedelta(minutes=BAR_MIN * k)


def session_bars(series, day, cutoff, close=CLOSE):
    """5-minute bars of `day` that are complete at `cutoff` (naive IST), on the grid, with a close,
    before `close` (15:15 for a CAS stock: the 15:15-15:35 closing auction is not continuous trading)."""
    out = {}
    hs, ls, v = series.get("h"), series.get("l"), series.get("v")
    for i, ts in enumerate(series["t"]):
        c = series["c"][i]
        if c is None:
            continue
        t = ts_to_ist(ts)
        if t.date() != day or t.second or t.minute % BAR_MIN:
            continue  # another day, or the live tick off the grid
        if t.time() < OPEN or t.time() >= close:
            continue  # pre-open, the closing auction / closing session, the 3:30 PM print
        if t + datetime.timedelta(minutes=BAR_MIN) > cutoff:
            continue  # still in progress at the cutoff
        out[t] = {"start": t, "c": float(c),
                  "h": None if not hs or hs[i] is None else float(hs[i]),
                  "l": None if not ls or ls[i] is None else float(ls[i]),
                  "v": (v[i] or 0) if v else 0}
    return [out[k] for k in sorted(out)]


def prev_close(series, day):
    """The previous session's close: the last bar dated before `day` in the series (a 5-minute bar's close
    is near, not equal to, NSE's official close, which is the last 30 minutes' weighted average), or, for a
    chart that starts on `day` (range=1d), Yahoo's chartPreviousClose / previousClose."""
    best = None
    for ts, c in zip(series["t"], series["c"]):
        if c is not None and ts_to_ist(ts).date() < day:
            best = float(c)
    if best is not None:
        return best
    m = series.get("meta") or {}
    if series["t"] and ts_to_ist(series["t"][0]).date() == day:
        p = m.get("chartPreviousClose", m.get("previousClose"))
        return float(p) if p is not None else None
    return None


def usable(bars, day, cutoff, n_range, close=CLOSE):
    """Every opening-range slot (9:15 on) is there, and at most MAX_EMPTY slots from the range to the
    cutoff (or the stock's close: 15:15 for CAS stocks, 72 bars; 15:30 otherwise, 75 bars) are empty.
    Yahoo leaves some NSE 5-minute bars without a price; those are skipped, never counted as trades.
    For a CAS stock the 15:15-15:25 slots are the closing auction, not gaps. More gaps: a halt."""
    want = int((cutoff - slot(day, 0)).total_seconds() // (BAR_MIN * 60))
    want = min(want, int((datetime.datetime.combine(day, close) - slot(day, 0)).total_seconds() // (BAR_MIN * 60)))
    if want < n_range or len(bars) < n_range or len(bars) > want:
        return False
    if any(bars[k]["start"] != slot(day, k) for k in range(n_range)):
        return False
    if want - len(bars) > MAX_EMPTY:
        return False
    return len(bars) > n_range or want == n_range


def orb_candidate(bars, n_range):
    """Stage 1, closes only: some completed bar after the range closed at or above
    the highest close of the range. Necessary for a breakout (range high >= any range close)."""
    top = max(b["c"] for b in bars[:n_range])
    return any(b["c"] >= top for b in bars[n_range:])


def orb_hit(bars, n_range):
    """Stage 2, real highs and lows: the first completed bar to CLOSE above the range high."""
    rng = bars[:n_range]
    if any(b["h"] is None or b["l"] is None for b in rng):
        return None
    oh = max(b["h"] for b in rng)
    ol = min(b["l"] for b in rng)
    for k in range(n_range, len(bars)):
        b = bars[k]
        if b["c"] > oh:
            closes_at = b["start"] + datetime.timedelta(minutes=BAR_MIN)
            return {"price": round(b["c"], 2), "at": clock_label(closes_at),
                    "bar_start": b["start"].strftime("%H:%M"),
                    "bar_index": int((b["start"] - slot(b["start"].date(), 0)).total_seconds() // (BAR_MIN * 60)),
                    "or_high": round(oh, 2), "or_low": round(ol, 2),
                    "pct_over": round((b["c"] / oh - 1) * 100, 2), "_t": closes_at}
    return None


def orb_window(now_utc, at, or_minutes, until, explicit_day=None):
    """-> (day, cutoff naive IST) or raise ScanError with the plain message."""
    now = to_ist(now_utc)
    day = explicit_day or now.date()
    if or_minutes < BAR_MIN or or_minutes % BAR_MIN or or_minutes > 120:
        raise ScanError("--or-minutes wants a multiple of 5 from 5 to 120 (15 is the classic)")
    ok, why = trading_day(day)
    if not ok:
        raise ScanError("No session on %s (%s): the market is closed. Run orb on a trading day "
                        "after the opening range, or replay a past day with --offline." % (day_label(day), why))
    range_end = slot(day, or_minutes // BAR_MIN)
    first_close = range_end + datetime.timedelta(minutes=BAR_MIN)
    close_dt = datetime.datetime.combine(day, CLOSE)
    if at is not None:
        cutoff = datetime.datetime.combine(day, at)
        if explicit_day is None and cutoff > now + datetime.timedelta(seconds=59):
            raise ScanError("--at %s is later than now (%s IST)." % (at.strftime("%H:%M"), clock_label(now)))
        if cutoff > close_dt:
            raise ScanError("--at %s is after the %s close on %s." % (at.strftime("%H:%M"), clock_label(close_dt),
                                                                       day_label(day)))
    else:
        cutoff = now.replace(second=0, microsecond=0)
        if cutoff >= close_dt:
            raise ScanError("The market closed at %s IST on %s. Replay the session with --at 10:15."
                            % (clock_label(close_dt), day_label(day)))
    if cutoff < first_close:
        raise ScanError("It is %s IST: the %d-minute opening range runs 9:15 to %s and the first bar after it "
                        "closes at %s. Run orb again after %s."
                        % (clock_label(cutoff), or_minutes, clock_label(range_end), clock_label(first_close),
                           clock_label(first_close)))
    if until is not None and cutoff.time() > until:
        raise ScanError("It is %s IST, after %s, your last time for new entries (no_entries_after): an "
                        "opening-range breakout found now is expired. Run orb tomorrow after %s, or replay "
                        "today with --at 10:15." % (clock_label(cutoff), until.strftime("%H:%M"),
                                                      clock_label(first_close)))
    return day, cutoff


def scan_orb(stage1, symbols, day, cutoff, or_minutes, min_price=0.0, min_volume=0, confirm=None,
             max_confirm=None, meta=None, kite=None):
    """stage1 = {key: series} with closes (spark or v8). confirm(key) -> series with highs/lows
    (None = use the stage-1 series). With confirm and max_confirm, only the max_confirm most
    traded candidates (price x volume so far) are confirmed; the rest are counted as unconfirmed.
    Trade-to-trade (BE/BZ) charts are skipped. Each stock uses session_rules for its CAS status and the day.
    kite = set of keys whose stage-1 series are Kite candles (real highs and lows: no stage 2).
    Returns (hits, counts, failed {key: why})."""
    kite = set(kite or ()) | {k for k in symbols if ((stage1.get(k) or {}).get("meta") or {}).get("source") == "kite"}
    n_range = or_minutes // BAR_MIN
    counts = {"requested": len(symbols), "fetched": 0, "t2t": 0, "unusable": 0, "illiquid": 0, "scanned": 0,
              "candidates": 0, "confirmed": 0, "unconfirmed": 0, "cas_unknown": 0}
    hits, failed, cands = [], {}, []
    for k in symbols:
        s = stage1.get(k)
        if not s:
            continue
        counts["fetched"] += 1
        if _info(meta, k)[0] in T2T:
            counts["t2t"] += 1
            continue
        cas = _cas(meta, k)
        if cas is None:
            counts["cas_unknown"] += 1
        close = _hm(session_rules(cas, day)["close"])
        bars = session_bars(s, day, cutoff, close)
        if not usable(bars, day, cutoff, n_range, close):
            counts["unusable"] += 1
            continue
        vol = sum(b["v"] for b in bars) if s.get("v") else (s.get("meta") or {}).get("regularMarketVolume") or 0
        if bars[-1]["c"] < min_price or vol < min_volume:
            counts["illiquid"] += 1
            continue
        counts["scanned"] += 1
        if orb_candidate(bars, n_range):
            cands.append((k, bars, bars[-1]["c"] * vol, s, close))
    counts["candidates"] = len(cands)
    if confirm is not None and max_confirm and len(cands) > max_confirm:
        cands = [c for c in cands if c[0] in kite] + [c for c in cands if c[0] not in kite]
        cands.sort(key=lambda x: (x[0] not in kite, -x[2]))
        counts["unconfirmed"] = len(cands) - max_confirm
        cands = cands[:max_confirm]
    for k, bars, _, s, close in cands:
        full, s_full = bars, s
        if confirm is not None and k not in kite:
            try:
                s_full = confirm(k)
            except ScanError as e:
                failed[k] = str(e)
                continue
            full = session_bars(s_full, day, cutoff, close)
            if not usable(full, day, cutoff, n_range, close):
                failed[k] = "the v8 chart has a missing bar"
                continue
        counts["confirmed"] += 1
        h = orb_hit(full, n_range)
        if h:
            sym, exch = split_key(k)
            series, band = _info(meta, k)
            rec = dict({"symbol": sym, "exchange": exch, "setups": ["orb"]}, **h)
            pc = prev_close(s_full, day)
            if pc is None and s_full is not s:
                pc = prev_close(s, day)
            rec.update(band_fields(h["price"], pc, band))
            rec["series"] = series
            rec.update(rule_fields(_cas(meta, k), day))
            rec["source"] = "kite" if k in kite else "yahoo"
            hits.append(rec)
    hits.sort(key=lambda x: (x["_t"], x["symbol"]))
    for h in hits:
        del h["_t"]
    counts["hits"] = len(hits)
    return hits, counts, failed


# ---------------------------------------------------------------- fetching

def progress(a, msg):
    if not getattr(a, "json", False) and not getattr(a, "quiet", False):
        print(msg, file=sys.stderr)


def fetch_spark(symbols, rng, interval, pause, a=None):
    """Batches of 20. -> ({key: series}, {key: error})."""
    data, failed = {}, {}
    batches = [symbols[i:i + SPARK_MAX] for i in range(0, len(symbols), SPARK_MAX)]
    for n, batch in enumerate(batches):
        if n and pause:
            SLEEP(pause)
        try:
            got = parse_spark(yahoo_get(spark_path(batch, rng, interval)))
        except (ScanError, ValueError) as e:
            for k in batch:
                failed[k] = str(e)
            continue
        for k in batch:
            if k in got:
                data[k] = got[k]
            else:
                failed[k] = "no data from Yahoo"
        if a is not None and len(batches) > 25 and (n + 1) % 50 == 0:
            progress(a, "  spark %d of %d requests" % (n + 1, len(batches)))
    return data, failed


def read_symbols(a, offline_syms=None):
    if a.symbols:
        syms = [norm(s) for s in re.split(r"[,\s]+", a.symbols) if s.strip()]
    elif a.watchlist:
        syms = symbols_from_file(a.watchlist)
    elif offline_syms is not None and not a.universe:
        syms = sorted(offline_syms)  # offline: every chart in the file, unless a list was given
    else:
        path = a.universe or DEFAULT_UNIVERSE
        if not os.path.exists(path):
            raise ScanError("no %s yet: run `scan.py universe` first, or pass --symbols" % path)
        syms = symbols_from_file(path)
    out = []
    for s in syms:
        if s not in out:
            out.append(s)
    if a.limit:
        out = out[:a.limit]
    if not out:
        raise ScanError("no symbols to scan")
    return out


def symbols_from_file(path):
    """A watchlist.json (hits with symbol + exchange), a JSON list, or a text file (SYMBOL|... a line) -> keys."""
    if not os.path.exists(path):
        raise ScanError("no file at %s" % path)
    with open(path, encoding="utf-8") as fh:
        text = fh.read()
    if text.lstrip().startswith(("{", "[")):
        try:
            doc = json.loads(text)
        except ValueError as e:
            raise ScanError("%s is not valid JSON: %s" % (path, e))
        items = doc.get("hits", []) if isinstance(doc, dict) else doc
        out = []
        for x in items:
            if isinstance(x, dict) and x.get("symbol"):
                sym, exch = parse_symbol(str(x["symbol"]))
                out.append(key(sym, (x.get("exchange") or exch).upper()))
            elif x and not isinstance(x, dict):
                out.append(norm(str(x)))
        return out
    out = []
    for ln in text.splitlines():
        ln = ln.strip()
        if ln and not ln.startswith("#"):
            out.append(norm(ln.split("|")[0]))
    return out


def read_config(path):
    if not path or not os.path.exists(path):
        return {}
    try:
        with open(path, encoding="utf-8") as fh:
            cfg = json.load(fh)
    except ValueError as e:
        raise ScanError("%s is not valid JSON: %s" % (path, e))
    return cfg if isinstance(cfg, dict) else {}


def write_json(path, doc):
    d = os.path.dirname(path)
    if d:
        os.makedirs(d, exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(doc, fh, indent=2, ensure_ascii=False)
        fh.write("\n")


def indian(n, dec=2):
    """1234567.8 -> '12,34,567.80' (Indian grouping: thousands, then lakhs and crores)."""
    neg = n < 0
    s = ("%." + str(dec) + "f") % abs(n)
    whole, _, frac = s.partition(".")
    if len(whole) > 3:
        head, tail = whole[:-3], whole[-3:]
        parts = []
        while len(head) > 2:
            parts.insert(0, head[-2:])
            head = head[:-2]
        if head:
            parts.insert(0, head)
        whole = ",".join(parts + [tail])
    return ("-" if neg else "") + whole + ("." + frac if frac else "")


def inr(x):
    return "₹" + indian(x)


def crore(x):
    if not x:
        return "\u20b90"
    return ("₹%s crore" % _n(x / 1e7)) if x >= 1e7 else inr(x)


def _n(x):
    x = float(x)
    return str(int(x)) if x == int(x) else "%.2f" % x


def _count(n):
    return indian(n, 0)


def load_fo(a, today):
    """The F&O (= CAS) list: --fo-list FILE, else the cache trader-in/fo_list.txt, refreshed from NSE when it is
    missing or older than 7 days (online only). -> (set or None, note)."""
    path = getattr(a, "fo_list", None)
    if path:
        if not os.path.exists(path):
            raise ScanError("no file at %s" % path)
        with open(path, encoding="utf-8", errors="replace") as fh:
            return parse_fo_list(fh.read()), ""
    fo, when = read_fo_cache(DEFAULT_FO_LIST)
    stale = fo is None or when is None or (today - when).days > FO_MAX_AGE_DAYS
    if stale and not getattr(a, "offline", None):
        try:
            fo = fetch_fo_list()
            if fo:
                write_fo_cache(DEFAULT_FO_LIST, fo, today)
                return fo, ""
        except ScanError:
            pass
        if fo is not None:
            return fo, "NSE's F&O list could not be refreshed: using the cached one of %s." % when
    return fo, ""


def load_meta(a, symbols=(), cfg=None, today=None):
    """Series, bands and CAS status per key: --bands FILE (a sec_list.csv), else the universe file; CAS from the
    universe file, else the F&O list; then config cas_symbols_extra / non_cas_symbols, then --cas yes|no.
    -> (meta, note). A stock with no CAS status is None: the stricter CAS rules apply."""
    if getattr(a, "bands", None):
        if not os.path.exists(a.bands):
            raise ScanError("no file at %s" % a.bands)
        meta = read_bands_file(a.bands)
    else:
        meta = read_universe_meta(a.universe or DEFAULT_UNIVERSE)
    today = today or to_ist(parse_now(None)).date()
    for k in symbols:
        meta.setdefault(k, {"name": "", "series": "EQ", "band": None, "cas": None})
    note = ""
    if any(m.get("cas") is None for m in meta.values()):
        fo, note = load_fo(a, today)
        if fo is not None:
            for k, m in meta.items():
                if m.get("cas") is None:
                    m["cas"] = split_key(k)[0] in fo   # a BSE listing follows its NSE symbol
    cfg = cfg or {}
    for key_name, val in (("cas_symbols_extra", True), ("non_cas_symbols", False)):
        for x in cfg.get(key_name) or []:
            meta.setdefault(norm(x), {"name": "", "series": "EQ", "band": None})["cas"] = val
    forced = parse_cas_word(getattr(a, "cas", None)) if getattr(a, "cas", None) else None
    if forced is not None:
        for k in symbols:
            meta[k]["cas"] = forced
    return meta, note


def cas_note(counts):
    n = counts.get("cas_unknown", 0)
    if not n:
        return ""
    return ("CAS status unknown for %s stock%s (no F&O list: run scan.py universe, or pass --fo-list): "
            "using the stricter CAS rules (close 15:15, square-off 15:12)." % (_count(n), "" if n == 1 else "s"))


def annotate_ltp(hits, path):
    """--kite-ltp FILE: Kite get_ltp / get_quotes output saved verbatim -> each matching hit gets ltp, ltp_source."""
    if not path:
        return 0
    if not os.path.exists(path):
        raise ScanError("no file at %s" % path)
    try:
        with open(path, encoding="utf-8") as fh:
            ltp = parse_kite_ltp(json.load(fh))
    except ValueError as e:
        raise ScanError("%s is not valid JSON: %s" % (path, e))
    n = 0
    for h in hits:
        k = key(h["symbol"], h.get("exchange", "NSE"))
        if k in ltp:
            h["ltp"] = ltp[k]
            h["ltp_source"] = "kite"
            n += 1
    return n


def print_ltp(hits):
    rows = [h for h in hits if h.get("ltp_source") == "kite"]
    if rows:
        print("Last price now (Kite, live): " + ", ".join("%s %s" % (h["symbol"], inr(h["ltp"])) for h in rows) + ".")


# ---------------------------------------------------------------- commands

def cmd_universe(a):
    t0 = time.time()
    names = [EQUITY_FILE, BANDS_FILE] + ([INDEX_FILES[a.index]] if a.index else [])
    texts = {}
    for n, fname in enumerate(names):
        if a.offline:
            p = os.path.join(a.offline, fname)
            if not os.path.exists(p):
                if fname == BANDS_FILE:
                    continue  # bands are optional
                raise ScanError("no %s in %s" % (fname, a.offline))
            with open(p, encoding="utf-8", errors="replace") as fh:
                texts[fname] = fh.read()
        else:
            if n:
                SLEEP(max(a.pause, NSE_PAUSE))
            sub = "indices/" if fname in INDEX_FILES.values() else "equities/"
            try:
                texts[fname] = nse_get(NSE_ARCHIVES + sub + fname)
            except ScanError:
                if fname == BANDS_FILE:
                    continue
                raise
    fo = None
    if a.fo_list:
        if not os.path.exists(a.fo_list):
            raise ScanError("no file at %s" % a.fo_list)
        with open(a.fo_list, encoding="utf-8", errors="replace") as fh:
            fo = parse_fo_list(fh.read())
    elif a.offline:
        p = os.path.join(a.offline, FO_FILE)
        if os.path.exists(p):
            with open(p, encoding="utf-8", errors="replace") as fh:
                fo = parse_fo_list(fh.read())
    else:
        SLEEP(max(a.pause, NSE_PAUSE))
        try:
            fo = fetch_fo_list()
            if not a.no_write:
                write_fo_cache(os.path.join(os.path.dirname(a.out), "fo_list.txt"),
                               fo, to_ist(parse_now(None)).date())
        except ScanError:
            fo = None
    kept, counts = build_universe(texts[EQUITY_FILE], texts.get(BANDS_FILE),
                                  texts.get(INDEX_FILES[a.index]) if a.index else None, a.include_t2t, fo)
    if not a.no_write:
        d = os.path.dirname(a.out)
        if d:
            os.makedirs(d, exist_ok=True)
        with open(a.out, "w", encoding="utf-8") as fh:
            for sym, name, series, band, cas in kept:
                fh.write("%s|%s|%s|%s|%s\n" % (sym, name, series, "No Band" if band is None else _n(band),
                                                cas_word(cas)))
    banded = sum(1 for x in kept if x[3] is not None)
    ncas = sum(1 for x in kept if x[4])
    t2t = counts.get("t2t_BE", 0) + counts.get("t2t_BZ", 0)
    dropped = {k: v for k, v in counts.items() if k not in ("keep", "rows", "index_members", "index_missing")
               and not k.startswith("t2t_")}
    if a.json:
        print(json.dumps({"count": len(kept), "rows": counts["rows"], "index": a.index,
                          "trade_to_trade": {"BE": counts.get("t2t_BE", 0), "BZ": counts.get("t2t_BZ", 0),
                                             "kept": a.include_t2t},
                          "with_band": banded, "cas": None if fo is None else ncas, "bands_file": BANDS_FILE in texts, "dropped": dropped,
                          "out": None if a.no_write else a.out, "seconds": round(time.time() - t0, 1)}, indent=2))
    else:
        print("NSE equity list (EQUITY_L.csv): %s rows (series EQ, BE, BZ)." % _count(counts["rows"]))
        if a.index:
            print("Index filter %s: %s members%s." % (a.index, _count(counts["index_members"]),
                                                       (", %d not in EQUITY_L.csv" % counts["index_missing"])
                                                       if counts["index_missing"] else ""))
        print("Kept %s NSE equities (series EQ%s)." % (_count(len(kept)), ", BE, BZ flagged" if a.include_t2t else ""))
        print("Trade-to-trade (no intraday): %d BE, %d BZ%s." % (
            counts.get("t2t_BE", 0), counts.get("t2t_BZ", 0),
            " (kept, flagged)" if a.include_t2t else " (left out; --include-t2t keeps them)" if t2t else ""))
        if dropped:
            print("Dropped: " + ", ".join("%s %s" % (_count(v), k) for k, v in
                                          sorted(dropped.items(), key=lambda kv: -kv[1])) + ".")
        if BANDS_FILE in texts:
            print("Price bands from sec_list.csv: %s with a 2-20 %% band, %s with No Band (mostly F&O stocks)."
                  % (_count(banded), _count(len(kept) - banded)))
        else:
            print("No sec_list.csv: price bands unknown.")
        if fo is not None:
            print("Closing auction (CAS, F&O stocks, from fo_mktlots.csv): %s CAS stocks end continuous trading at "
                  "15:15 (Zerodha MIS square-off 15:12); %s trade to 15:30 (square-off 15:25)."
                  % (_count(ncas), _count(len(kept) - ncas)))
        else:
            print("No F&O list (fo_mktlots.csv): CAS status unknown, so the stricter CAS rules apply to every stock.")
        if not a.no_write:
            print("Wrote %s (one SYMBOL|Name|Series|Band|CAS a line)." % a.out)
    return 0 if kept else 1


def _dropped_line(counts):
    return (", %s trade-to-trade (BE/BZ) skipped" % _count(counts["t2t"])) if counts.get("t2t") else ""


def cmd_daily(a):
    t0 = time.time()
    now = parse_now(a.now)
    load_holiday_cache(a.holidays)
    offline_syms = None
    if a.offline:
        data, failed = load_offline(a.offline)
        offline_syms = sorted(set(data) | set(failed))
    symbols = read_symbols(a, offline_syms)
    meta, note = load_meta(a, symbols, read_config(a.config), to_ist(now).date())
    if note:
        progress(a, note)
    if not a.offline:
        progress(a, "Fetching daily closes for %s symbols (%d spark requests)..."
                 % (_count(len(symbols)), (len(symbols) + SPARK_MAX - 1) // SPARK_MAX))
        data, failed = fetch_spark(symbols, "3mo", "1d", a.pause, a)
    hits, counts, last_date = scan_daily(data, symbols, now, a.min_price, a.min_volume, a.min_turnover, meta,
                                         a.include_t2t)
    nfailed = len(symbols) - counts["fetched"]
    annotate_ltp(hits, a.kite_ltp)
    as_of = ("%s close" % last_date.isoformat()) if last_date else None
    doc = {"created_by": "india-scanner", "created_at": now.strftime("%Y-%m-%dT%H:%M:%SZ"), "mode": "daily",
           "as_of": as_of, "universe": len(symbols), "scanned": counts["scanned"], "failed": nfailed,
           "source": "yahoo", "currency": "INR",
           "filters": {"min_price": a.min_price, "min_volume": a.min_volume, "min_turnover": a.min_turnover},
           "note": NOTE, "hits": hits}
    if a.offline:
        doc["offline"] = os.path.basename(a.offline.rstrip("/"))
    if not a.no_write:
        write_json(a.out, doc)
    if a.json:
        print(json.dumps(dict(doc, counts=counts, seconds=round(time.time() - t0, 1)), indent=2, ensure_ascii=False))
        return 0 if hits else 1
    local = to_ist(now)
    print("Daily scan, %s IST, closes through %s (Yahoo, free, can lag; daily closes only)." % (
        local.strftime("%a %Y-%m-%d %H:%M"), last_date.isoformat() if last_date else "nothing"))
    print("Fetched %s of %s charts%s%s. %s passed the liquidity filter (%s+ and %s+ traded)%s; %s scanned." % (
        _count(counts["fetched"]), _count(len(symbols)),
        (" (%s failed or had no data)" % _count(nfailed)) if nfailed else "", _dropped_line(counts),
        _count(counts["fetched"] - counts["t2t"] - counts["illiquid"]), inr(a.min_price), crore(a.min_turnover),
        (", %d had under %d days" % (counts["short"], MIN_DAILY_BARS)) if counts["short"] else "",
        _count(counts["scanned"])))
    if hits:
        per = [(k, sum(1 for h in hits if k in h["setups"])) for k, _ in SETUPS]
        print("%d chart%s matched a setup: %s." % (len(hits), "" if len(hits) == 1 else "s",
                                                  ", ".join("%d %s" % (n, LABEL[k]) for k, n in per if n)))
        print("Most traded first (price x volume):")
    else:
        print("No setups.")
    for h in hits[:a.show]:
        print("  %-11s %12s  RSI %5.1f  %s%s" % (h["symbol"], inr(h["price"]), h["rsi"],
                                                 ", ".join(LABEL[t] for t in h["setups"]), _flags(h)))
    if len(hits) > a.show:
        print("  ... and %d more in the watchlist (--show N for more)." % (len(hits) - a.show))
    print_ltp(hits)
    if cas_note(counts):
        print(cas_note(counts))
    if not a.no_write:
        print("Wrote %s (mode daily, %d hit%s)." % (a.out, len(hits), "" if len(hits) == 1 else "s"))
    print(NOTE + " Next: pick one chart, then /india-news SYMBOL.")
    return 0 if hits else 1


def _flags(h):
    out = ""
    if h.get("near_upper_circuit"):
        out += "  NEAR UPPER CIRCUIT %s (band %s%%)" % (_p(h["upper_circuit"]), _n(h["band"]))
    if h.get("series") in T2T:
        out += "  trade-to-trade %s: no intraday" % h["series"]
    return out


def cmd_orb(a):
    t0 = time.time()
    now = parse_now(a.now)
    at = parse_hhmm(a.at) if a.at else None
    cfg = read_config(a.config)
    until = parse_hhmm(a.until or cfg.get("no_entries_after") or "14:45", "--until")
    data = failed = None
    explicit_day = None
    offline_syms = None
    if a.offline:
        data, failed = load_offline(a.offline)
        offline_syms = sorted(set(data) | set(failed))
        if not a.now:
            days = [ts_to_ist(s["t"][-1]).date() for s in data.values() if s["t"]]
            if days:
                explicit_day = max(days)
                if at is None:
                    raise ScanError("offline replay: give --at HH:MM or --now \"YYYY-MM-DD HH:MM\"")
    warn = ensure_holidays(explicit_day or to_ist(now).date(), a.holidays, bool(a.offline))
    if warn:
        progress(a, warn)
    day, cutoff = orb_window(now, at, a.or_minutes, until, explicit_day)
    symbols = read_symbols(a, offline_syms)
    kite = load_kite_bars(a.kite_bars)
    for k in kite:
        if k not in symbols:
            symbols.append(k)
    meta, note = load_meta(a, symbols, cfg, day)
    if note:
        progress(a, note)
    confirm = None
    if not a.offline:
        today = to_ist(parse_now(None)).date()
        if (today - day).days > 6:
            raise ScanError("Yahoo's free 5-minute history is short: replay %s with --offline" % day.isoformat())
        rng = "1d" if day == today else "5d"
        progress(a, "Stage 1: 5-minute closes for %s symbols (%d spark requests)..."
                 % (_count(len(symbols)), (len(symbols) + SPARK_MAX - 1) // SPARK_MAX))
        data, failed = fetch_spark([k for k in symbols if k not in kite], rng, "%dm" % BAR_MIN, a.pause, a)
        done = []

        def confirm(k):
            if a.pause:
                SLEEP(a.pause)
            done.append(k)
            if len(done) % 50 == 0:
                progress(a, "  stage 2: %d v8 charts so far" % len(done))
            return parse_chart(yahoo_get(chart_path(k, rng, "%dm" % BAR_MIN)), k)
    data = dict(data or {})
    data.update(kite)
    for k in kite:
        (failed or {}).pop(k, None)
    hits, counts, failed2 = scan_orb(data, symbols, day, cutoff, a.or_minutes, a.min_price, a.min_volume, confirm,
                                     a.max_confirm, meta, set(kite))
    annotate_ltp(hits, a.kite_ltp)
    failed = dict(failed or {})
    failed.update(failed2)
    nfailed = len([k for k in symbols if k in failed or k not in data])
    as_of = "%s %s IST" % (day.isoformat(), cutoff.strftime("%H:%M"))
    doc = {"created_by": "india-scanner", "created_at": now.strftime("%Y-%m-%dT%H:%M:%SZ"), "mode": "orb",
           "as_of": as_of, "universe": len(symbols), "scanned": counts["scanned"], "failed": nfailed,
           "unconfirmed": counts["unconfirmed"], "source": "yahoo", "currency": "INR", "or_minutes": a.or_minutes,
           "interval": "%dm" % BAR_MIN,
           "filters": {"min_price": a.min_price, "min_volume": a.min_volume}, "note": NOTE, "hits": hits}
    if a.offline:
        doc["offline"] = os.path.basename(a.offline.rstrip("/"))
    if not a.no_write:
        write_json(a.out, doc)
    if a.json:
        print(json.dumps(dict(doc, counts=counts, seconds=round(time.time() - t0, 1)), indent=2, ensure_ascii=False))
        return 0 if hits else 1
    print("Opening-range breakout scan, %s %s IST: %d-minute range (9:15 to %s), 5-minute bars "
          "(Yahoo, free, can lag%s)." % (day_label(day), clock_label(cutoff), a.or_minutes,
                                         clock_label(slot(day, a.or_minutes // BAR_MIN)),
                                         "; %d from Kite candles" % len(kite) if kite else ""))
    print("Fetched %s of %s charts%s%s; %s usable%s; %s candidates, %s confirmed with real highs and lows."
          % (_count(counts["fetched"]), _count(len(symbols)),
             (" (%s failed or had no data)" % _count(nfailed)) if nfailed else "", _dropped_line(counts),
             _count(counts["scanned"]),
             (" (%s under the price / volume floor)" % _count(counts["illiquid"])) if counts["illiquid"] else "",
             _count(counts["candidates"]), _count(counts["confirmed"])))
    if counts["unconfirmed"]:
        print("PARTIAL: %s more candidates were not checked (only the %s most traded are; --max-confirm 0 "
              "checks all)." % (_count(counts["unconfirmed"]), _count(a.max_confirm)))
    if hits:
        print("%d of %s charts closed above their opening-range high by %s:"
              % (len(hits), ("the %s checked" % _count(counts["confirmed"])) if counts["unconfirmed"]
                 else _count(counts["scanned"]), clock_label(cutoff)))
        for h in hits[:a.show]:
            print("  %8s  %-11s %12s  range %s to %s  %+.2f%% over%s" % (
                h["at"], h["symbol"], inr(h["price"]), _p(h["or_low"]), _p(h["or_high"]), h["pct_over"],
                _flags(h) + ("  (Kite candles)" if h.get("source") == "kite" else "")))
        if len(hits) > a.show:
            print("  ... and %d more in the watchlist (--show N for more)." % (len(hits) - a.show))
    else:
        print("No chart closed above its opening-range high by %s." % clock_label(cutoff))
    print_ltp(hits)
    if cas_note(counts):
        print(cas_note(counts))
    if not a.no_write:
        print("Wrote %s (mode orb, %d hit%s)." % (a.out, len(hits), "" if len(hits) == 1 else "s"))
    print(NOTE + " Next: pick one chart, then /india-news SYMBOL and /india-entry SYMBOL.")
    return 0 if hits else 1


def _p(x):
    return indian(x)


def main(argv=None):
    ap = argparse.ArgumentParser(description="Chart Scanner (india-scanner): daily setups and opening-range "
                                             "breakouts on NSE stocks.")
    sub = ap.add_subparsers(dest="cmd")

    p = sub.add_parser("universe", help="download NSE's equity list -> trader-in/universe.txt")
    p.add_argument("--out", default=DEFAULT_UNIVERSE)
    p.add_argument("--index", choices=sorted(INDEX_FILES), help="keep only the members of this index")
    p.add_argument("--include-t2t", action="store_true", help="keep trade-to-trade BE/BZ names, flagged")
    p.add_argument("--fo-list", metavar="FILE", help="NSE's fo_mktlots.csv (or fo_list.txt) for CAS status")
    p.add_argument("--offline", metavar="DIR", help="a folder with EQUITY_L.csv (sec_list.csv, index CSVs)")
    p.add_argument("--no-write", action="store_true")
    p.add_argument("--pause", type=float, default=NSE_PAUSE)
    p.add_argument("--json", action="store_true")

    def common(p, min_price, min_volume):
        g = p.add_mutually_exclusive_group()
        g.add_argument("--symbols", help="comma-separated NSE symbols instead of the universe (RELIANCE,M&M)")
        g.add_argument("--watchlist", metavar="FILE", help="a watchlist.json (hits) or a text file of symbols")
        p.add_argument("--universe", help="symbol list, one SYMBOL|Name|Series|Band a line (default %s)"
                       % DEFAULT_UNIVERSE)
        p.add_argument("--bands", metavar="FILE", help="NSE's sec_list.csv for series and price bands")
        p.add_argument("--include-t2t", action="store_true", help="scan trade-to-trade (BE/BZ) names too (daily)")
        p.add_argument("--fo-list", metavar="FILE", help="NSE's fo_mktlots.csv (or fo_list.txt): CAS stocks")
        p.add_argument("--cas", choices=["yes", "no"], help="treat every scanned stock as CAS / non-CAS")
        p.add_argument("--kite-ltp", metavar="FILE", help="Kite get_ltp / get_quotes output, saved verbatim: "
                       "adds the live price to each hit")
        p.add_argument("--config", default="trader-in/config.json")
        p.add_argument("--limit", type=int, help="scan only the first N symbols")
        p.add_argument("--min-price", type=float, default=min_price, help="rupees (%s)" % _n(min_price))
        p.add_argument("--min-volume", type=int, default=min_volume)
        p.add_argument("--offline", metavar="PATH", help="fixture folder or file instead of the network")
        p.add_argument("--now", help='pretend it is this time ("2026-10-19 10:15" IST, or ...Z)')
        p.add_argument("--out", default="trader-in/watchlist.json")
        p.add_argument("--holidays", default=DEFAULT_HOLIDAYS, help="NSE holiday cache (%s)" % DEFAULT_HOLIDAYS)
        p.add_argument("--no-write", action="store_true")
        p.add_argument("--pause", type=float, default=PAUSE, help="seconds between requests (%.1f)" % PAUSE)
        p.add_argument("--show", type=int, default=50, help="hits to print (all are written)")
        p.add_argument("--quiet", action="store_true")
        p.add_argument("--json", action="store_true")

    p = sub.add_parser("daily", help="pre-market setups from daily closes")
    common(p, 20.0, 0)
    p.add_argument("--min-turnover", type=float, default=5e7,
                   help="rupees traded last session, price x volume (50000000 = 5 crore)")
    p = sub.add_parser("orb", help="opening-range breakouts so far today")
    common(p, 20.0, 0)
    p.add_argument("--or-minutes", type=int, default=15)
    p.add_argument("--max-confirm", type=int, default=MAX_CONFIRM,
                   help="v8 charts to fetch in stage 2, most traded first (%d; 0 = all)" % MAX_CONFIRM)
    p.add_argument("--at", help="cut the clock at this India time, HH:MM")
    p.add_argument("--until", help="no new entries after HH:MM (default: config no_entries_after, 14:45)")
    p.add_argument("--kite-bars", metavar="FILE", action="append",
                   help="Kite get_historical_data output, saved verbatim as <SYMBOL>-5minute-<date>.json (repeatable)")
    a = ap.parse_args(argv)
    if not a.cmd:
        ap.print_help()
        return 1
    try:
        return {"universe": cmd_universe, "daily": cmd_daily, "orb": cmd_orb}[a.cmd](a)
    except ScanError as e:
        print("scan.py: %s" % e, file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("scan.py: stopped", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
