#!/usr/bin/env python3
"""Exit Planner for india-exit: sets the stop and the target BEFORE you
enter, so you know when to get out. NSE / BSE, prices in rupees on the NSE
tick grid. Standard library only (Python 3.9+). Long side only. It never
places an order.

    python3 exit.py plan                                    # from trader-in/entry.json: or-mid stop, 2R target
    python3 exit.py plan --stop or-mid --target 1R --write  # the example: stop 1215.30, target 1240.30
    python3 exit.py plan --symbol RELIANCE --entry 1227.80 --or-high 1223.40 --or-low 1207.30
    python3 exit.py plan --stop atr:1.5 --offline RELIANCE-60d5m.json.gz   # ATR needs the session's bars
    python3 exit.py plan --symbol SBIN --entry 880.50 --setup level --stop pct:1
    python3 exit.py show                                    # the current trader-in/plan.json

Stops (a long's stop rounds DOWN to the tick, away from the entry):
  or-mid     the opening-range midpoint (the default for an orb entry)
  or-low     the opening-range low
  price:X    your price
  pct:X      X percent under the entry
  atr:K      K times the 14-bar ATR under the entry, on the session's bars up
             to the entry bar (--offline FILE or Yahoo). With fewer than 14
             bars by then it averages all of them and says so.
  A level setup has no opening-range default: give a stop.

Targets (round DOWN to the tick for a long, so the order can fill):
  2R (default), 1R, 1.5R ...   entry + R x risk, where risk = entry - stop
  price:X                      your price

Tick size (NSE, from Apr 15 2025, NSE reviews it monthly): under 250 -> 0.01;
250-1000 -> 0.05; 1000-5000 -> 0.10; 5000-10000 -> 0.50; 10000-20000 -> 1.00;
above 20000 -> 5.00.

Refused, with the reason: a stop at or above the entry, a target at or below
it, a risk under 0.05% of the price or under one tick (too tight to survive
the spread), a stop below the lower circuit (prev close x (1 - band), band
from NSE's sec_list.csv or --band), an entry.json that has not triggered
(unless you give --entry), a short (this pack plans longs only).

Price bands: https://nsearchives.nseindia.com/content/equities/sec_list.csv
(public), cached in trader-in/sec_list.csv once a day. "No Band" means an F&O
stock: NSE sets a dynamic band (usually 10%, flexed in steps), so there is no
fixed circuit to check. BSE symbols need --band. Series BE (trade-to-trade)
is flagged: no intraday (MIS) trading on it.

flat_by (default 15:10 IST, or "flat_by" in trader-in/config.json): out by
then, win or lose. Zerodha squares off MIS equity at 15:25 (Rs 50 + GST per
squared-off order), so a later flat-by gets a warning and one at or after the
15:30 close is refused. "min_reward_risk" (default 1.0) only warns here;
india-risk enforces it.

A stop is a plan, not a guarantee: a gap or a fast market can fill past it.
Exit codes: plan 0 = a plan, 1 = refused or an error. show 0 = a plan exists.
"""

import argparse
import csv
import datetime
import gzip
import io
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from decimal import Decimal, InvalidOperation, ROUND_FLOOR, ROUND_CEILING, ROUND_HALF_UP

UA = "Mozilla/5.0 (compatible; india-day-trader-skill/1.0; +https://github.com/)"
BROWSER_UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/130 Safari/537.36")
BACKOFF = 5.0
HOSTS = ("query1", "query2")
INTERVALS = {"1m": 60, "5m": 300}
DEFAULTS = {"flat_by": "auto", "min_reward_risk": 1.0}    # auto: 15:00 for a CAS stock, 15:10 otherwise
ATR_BARS = 14
MIN_RISK_PCT = Decimal("0.05")     # risk under 0.05% of the price is inside the spread's noise
PAISA = Decimal("0.01")
EPOCH = datetime.datetime(1970, 1, 1)
IST_OFFSET = datetime.timedelta(hours=5, minutes=30)
SEC_LIST_URL = "https://nsearchives.nseindia.com/content/equities/sec_list.csv"
SEC_LIST_CACHE = "trader-in/sec_list.csv"
NOTE = "A stop is a plan, not a guarantee: a gap or a fast market can fill past it."

# NSE capital-market trading holidays 2026 (NSE CM list, read 2026-10-23). A cached live
# list in trader-in/holidays.json (written by india-entry and others) is merged on top.
NSE_HOLIDAYS = {
    "2026-01-15": "NSE trading holiday", "2026-01-26": "Republic Day", "2026-02-15": "Mahashivratri",
    "2026-03-03": "Holi", "2026-03-21": "Id-ul-Fitr (Ramzan Id)", "2026-03-26": "Shri Ram Navami",
    "2026-03-31": "Shri Mahavir Jayanti", "2026-04-03": "Good Friday",
    "2026-04-14": "Dr. Baba Saheb Ambedkar Jayanti", "2026-05-01": "Maharashtra Day", "2026-05-28": "Bakri Id",
    "2026-06-26": "Muharram", "2026-08-15": "Independence Day", "2026-09-14": "Ganesh Chaturthi",
    "2026-10-02": "Mahatma Gandhi Jayanti", "2026-10-20": "Dussehra",
    "2026-11-08": "Diwali Laxmi Pujan (Muhurat trading session only)", "2026-11-10": "Diwali Balipratipada",
    "2026-11-24": "Guru Nanak Jayanti", "2026-12-25": "Christmas",
}
OPEN = datetime.time(9, 15)
CLOSE = datetime.time(15, 30)


class PlanError(Exception):
    pass


_Err = PlanError


# ---------------------------------------------------------------- time (IST is a fixed UTC+05:30)

def to_ist(utc):
    return utc.replace(tzinfo=None) + IST_OFFSET


def ist_to_utc(local):
    return local.replace(tzinfo=None) - IST_OFFSET


def utc_now():
    return datetime.datetime.now(datetime.timezone.utc).replace(microsecond=0, tzinfo=None)


def utc_of(ts):
    return EPOCH + datetime.timedelta(seconds=int(ts))


def parse_now(text):
    """--now -> naive UTC. A time without an offset is India time."""
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
        raise PlanError('--now wants an India time like "2026-10-19 09:50" (or ...Z for UTC)')
    if dt.tzinfo is None:
        return ist_to_utc(dt)
    return (dt - dt.utcoffset()).replace(tzinfo=None)


def parse_hhmm(text, what):
    m = re.match(r"^\s*(\d{1,2}):(\d{2})\s*$", str(text))
    if not m or int(m.group(1)) > 23 or int(m.group(2)) > 59:
        raise PlanError("%s wants an India time like 15:10, not %r" % (what, text))
    return datetime.time(int(m.group(1)), int(m.group(2)))


def parse_clock(text, what):
    """9:50 AM, 09:50, 3:05 PM -> time."""
    m = re.match(r"^\s*(\d{1,2}):(\d{2})\s*([AaPp][Mm])?\s*(?:IST)?\s*$", str(text))
    if not m:
        raise PlanError("%s wants a time like 9:50 AM, not %r" % (what, text))
    h, mi = int(m.group(1)), int(m.group(2))
    if m.group(3):
        if not 1 <= h <= 12:
            raise PlanError("%s wants a time like 9:50 AM, not %r" % (what, text))
        h = h % 12 + (12 if m.group(3).upper() == "PM" else 0)
    if h > 23 or mi > 59:
        raise PlanError("%s wants a time like 9:50 AM, not %r" % (what, text))
    return datetime.time(h, mi)


def hm(t):
    return "%02d:%02d" % (t.hour, t.minute)


def ampm(t):
    h = t.hour % 12 or 12
    return "%d:%02d %s" % (h, t.minute, "AM" if t.hour < 12 else "PM")


def load_holiday_cache(path="trader-in/holidays.json"):
    """Merge a cached NSE holiday list ({"holidays": {date: name}} or NSE's {"CM": [...]}) if present."""
    if not path or not os.path.exists(path):
        return
    try:
        with open(path, encoding="utf-8") as fh:
            doc = json.load(fh)
    except (OSError, ValueError):
        return
    items = doc.get("holidays") if isinstance(doc, dict) else doc
    if items is None and isinstance(doc, dict):
        items = doc.get("CM") or []
    if isinstance(items, dict):
        items = [{"date": k, "name": v} for k, v in items.items()]
    for it in items or []:
        raw = it if isinstance(it, str) else (it.get("date") or it.get("tradingDate") or "") if isinstance(it, dict) else ""
        for fmt in ("%Y-%m-%d", "%d-%b-%Y"):
            try:
                d = datetime.datetime.strptime(str(raw).strip(), fmt).date().isoformat()
            except ValueError:
                continue
            NSE_HOLIDAYS[d] = (it.get("name") or it.get("description") or "NSE trading holiday") \
                if isinstance(it, dict) else "NSE trading holiday"
            break


def session(d, cas=None):
    """(open_time, close_time) in India time, or None if closed all day. The close is the end of
    continuous trading: 15:15 for a CAS (F&O) stock from 2026-08-03 (or when unknown), else 15:30."""
    if d.weekday() >= 5 or d.isoformat() in NSE_HOLIDAYS:
        return None
    return OPEN, parse_hhmm(session_rules(cas, d)["close"], "close")


# ---------------------------------------------------------------- symbols

def parse_symbol(text, exchange=None):
    """(symbol, exchange). RELIANCE, RELIANCE.NS, NSE:RELIANCE -> NSE;
    RELIANCE.BO, BSE:RELIANCE, 500325 -> BSE."""
    t = str(text or "").strip().upper()
    if not t:
        raise PlanError("give a symbol, like RELIANCE")
    exch = str(exchange or "NSE").upper()
    m = re.match(r"^(NSE|BSE)\s*:\s*(.+)$", t)
    if m:
        exch, t = m.group(1), m.group(2).strip()
    elif t.endswith(".NS"):
        exch, t = "NSE", t[:-3]
    elif t.endswith(".BO"):
        exch, t = "BSE", t[:-3]
    elif re.match(r"^\d{6}$", t):
        exch = "BSE"
    if exch not in ("NSE", "BSE") or not re.match(r"^[A-Z0-9&\-_]+$", t):
        raise PlanError("%r does not look like an NSE or BSE symbol" % text)
    return t, exch


def yahoo_symbol(sym, exch="NSE"):
    return "%s.%s" % (sym.upper(), "BO" if exch == "BSE" else "NS")


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


# ---------------------------------------------------------------- money math (Decimal, on the tick grid)

def D(x, what="a number"):
    try:
        if isinstance(x, Decimal):
            return x
        if isinstance(x, float):
            return Decimal(repr(x))
        return Decimal(str(x).strip().lstrip("₹").replace("Rs", "").replace(",", "").strip())
    except (InvalidOperation, ValueError):
        raise PlanError("%s is not a number: %r" % (what, x))


def tick_size(price):
    """NSE tick size by price band (from Apr 15 2025; NSE reviews it monthly)."""
    p = D(price)
    if p < 250:
        return Decimal("0.01")
    if p < 1000:
        return Decimal("0.05")
    if p < 5000:
        return Decimal("0.10")
    if p < 10000:
        return Decimal("0.50")
    if p < 20000:
        return Decimal("1.00")
    return Decimal("5.00")


def round_to_tick(price, mode="nearest"):
    """Round to the stock's tick: 'down', 'up' or 'nearest' (half up). Exact, in Decimal."""
    p = D(price)
    t = tick_size(p)
    rounding = {"down": ROUND_FLOOR, "up": ROUND_CEILING, "nearest": ROUND_HALF_UP}[mode]
    out = ((p / t).quantize(Decimal("1"), rounding=rounding) * t).quantize(PAISA)
    if mode == "down" and out > 0 and tick_size(out) != t:     # crossed into a finer band: still on its grid
        out = ((p / tick_size(out)).quantize(Decimal("1"), rounding=ROUND_FLOOR) * tick_size(out)).quantize(PAISA)
    return out


def paise(x):
    return D(x).quantize(PAISA, rounding=ROUND_HALF_UP)


def inr(x):
    """1227.8 -> ₹1,227.80; 123456.78 -> ₹1,23,456.78 (Indian digit grouping)."""
    v = D(x).quantize(PAISA, rounding=ROUND_HALF_UP)
    sign = "-" if v < 0 else ""
    whole, frac = ("%s" % abs(v)).split(".")
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


def raw_inr(x):
    """A worked-out price before rounding: rupees with grouping, all its decimals."""
    v = D(x)
    if v == v.quantize(PAISA):
        return inr(v)
    whole = inr(int(v)).split(".")[0]
    return "%s.%s" % (whole, format(v.normalize(), "f").split(".")[1])


def num(x):
    """1, 1.5, 2.25: a Decimal without trailing zeros."""
    s = format(D(x).normalize(), "f")
    return s


def ratio(x):
    """reward:risk for people: 1.0, 1.5, 1.37."""
    s = "%.2f" % float(x)
    return s[:-1] if s.endswith("0") else s


WORDS = {1: "one", 2: "two", 3: "three", 4: "four", 5: "five"}


def parse_stop(text):
    t = str(text).strip().lower()
    if t in ("or-mid", "or_mid", "mid", "or-midpoint"):
        return "or_mid", None
    if t in ("or-low", "or_low", "low"):
        return "or_low", None
    m = re.match(r"^(price|pct|atr):\s*(?:₹|rs\.?)?\s*([0-9][0-9,]*\.?[0-9]*)$", t)
    if not m:
        raise PlanError("--stop is or-mid, or-low, price:X, pct:X or atr:K (got %r)" % text)
    kind, val = m.group(1), D(m.group(2), "--stop")
    if val <= 0:
        raise PlanError("--stop %s needs a number above 0" % kind)
    if kind == "pct" and val >= 100:
        raise PlanError("--stop pct:X needs X under 100")
    return kind, val


def parse_target(text):
    t = str(text).strip()
    m = re.match(r"^([0-9]*\.?[0-9]+)\s*[Rr]$", t)
    if m:
        r = D(m.group(1), "--target")
        if r <= 0:
            raise PlanError("--target needs an R multiple above 0, like 1R or 2R")
        return "r", r
    m = re.match(r"^price:\s*(?:₹|rs\.?)?\s*([0-9][0-9,]*\.?[0-9]*)$", t.lower())
    if m:
        return "price", D(m.group(1), "--target")
    raise PlanError("--target is 2R, 1R, 1.5R ... or price:X (got %r)" % text)


def r_label(r):
    n = int(r) if r == r.to_integral_value() else None
    return "%sR (%s times the risk)" % (num(r), WORDS.get(n, num(r)))


def compute(entry, stop_rule, stop_param, target_rule, target_param, or_high=None, or_low=None,
            atr=None, atr_bars=None, lower_circuit=None, tick=None):
    """The stop, the target and the risk, exact on the tick grid. Raises PlanError with the reason.
    tick: the instrument's own tick (Kite search_instruments) instead of the NSE table."""
    rt = round_to_tick
    if tick is not None:
        tk = D(tick)

        def rt(price, mode="nearest"):
            r = {"down": ROUND_FLOOR, "up": ROUND_CEILING, "nearest": ROUND_HALF_UP}[mode]
            return ((D(price) / tk).quantize(Decimal("1"), rounding=r) * tk).quantize(PAISA)
    e = rt(entry, "nearest")
    if e <= 0:
        raise PlanError("the entry price must be above ₹0")
    detail = ""
    if stop_rule in ("or_mid", "or_low"):
        if or_high is None or or_low is None:
            raise PlanError("an %s stop needs the opening range (--or-high and --or-low)" % stop_rule.replace("_", "-"))
        oh, ol = paise(or_high), paise(or_low)
        if oh <= ol:
            raise PlanError("the opening-range high %s must be above its low %s" % (inr(oh), inr(ol)))
        if stop_rule == "or_mid":
            raw = (oh + ol) / 2
            label = "opening-range midpoint"
            detail = "(%s + %s) / 2 = %s" % (inr(oh), inr(ol), raw_inr(raw))
        else:
            raw, label = ol, "opening-range low"
    elif stop_rule == "price":
        raw, label = stop_param, "your price"
    elif stop_rule == "pct":
        raw = e * (1 - stop_param / 100)
        label = "%s%% under the entry" % num(stop_param)
        detail = "%s x (1 - %s%%) = %s" % (inr(e), num(stop_param), raw_inr(raw))
    elif stop_rule == "atr":
        if atr is None:
            raise PlanError("an atr stop needs the session's bars")
        a = D(round(atr, 6))
        raw = e - stop_param * a
        label = "%s x ATR(%d) under the entry" % (num(stop_param), atr_bars)
        detail = "ATR ₹%s over the %d bars to the entry%s" % (
            num(a.quantize(Decimal("0.0001"))), atr_bars, " (fewer than 14 so far)" if atr_bars < ATR_BARS else "")
    else:
        raise PlanError("unknown stop rule %r" % stop_rule)
    raw = D(raw)
    stop = rt(raw, "down")
    if stop != raw:
        tick_txt = "rounded down to the %s tick" % inr(D(tick) if tick is not None else tick_size(stop))
        detail = (detail + ", " + tick_txt) if detail else "%s %s" % (raw_inr(raw), tick_txt)
    if stop >= e:
        raise PlanError("the stop %s (%s) is at or above the entry %s: a long's stop goes under the entry"
                        % (inr(stop), label, inr(e)))
    if stop <= 0:
        raise PlanError("the stop works out to %s: pick a stop above ₹0" % inr(stop))
    if lower_circuit is not None and stop < D(lower_circuit):
        raise PlanError("the stop %s is below today's lower circuit %s: the price cannot trade there, so the "
                        "stop would never fill at that price. Tighten it to %s or above, or skip the trade."
                        % (inr(stop), inr(lower_circuit), inr(lower_circuit)))
    risk = e - stop
    floor_risk = e * MIN_RISK_PCT / 100
    if risk < floor_risk or risk < (D(tick) if tick is not None else tick_size(e)):
        raise PlanError("the stop is %s under the entry, less than 0.05%% of the price (%s on %s) or one tick: "
                        "too tight to survive the spread, normal wiggle would stop you out. Widen it."
                        % (inr(risk), inr(floor_risk.quantize(PAISA, rounding=ROUND_HALF_UP)), inr(e)))
    if target_rule == "r":
        raw_t = e + target_param * risk
        target = rt(raw_t, "down")
        t_label = r_label(target_param)
        if target != raw_t:
            t_label += ", rounded down to the tick"
    else:
        target = rt(target_param, "down")
        t_label = None
    if target <= e:
        raise PlanError("the target %s is at or below the entry %s: a long's target goes above it"
                        % (inr(target), inr(e)))
    rr = ((target - e) / risk).quantize(PAISA, rounding=ROUND_HALF_UP)
    if t_label is None:
        t_label = "your price (%sR)" % ratio(rr)
    return {"entry": e, "stop": stop, "stop_label": label, "stop_detail": detail, "target": target,
            "target_r": target_param if target_rule == "r" else rr, "target_label": t_label,
            "risk": risk, "reward_risk": rr, "tick": D(tick) if tick is not None else tick_size(e)}


# ---------------------------------------------------------------- price bands (circuit limits)

def _open_url(url, timeout=20, ua=UA):
    req = urllib.request.Request(url, headers={"User-Agent": ua, "Accept": "*/*"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode("utf-8", "replace")


def parse_sec_list(text):
    """sec_list.csv -> {SYMBOL: {"series": "EQ", "band": 20.0 or None ("No Band")}}."""
    out = {}
    rows = csv.reader(io.StringIO(text))
    head = None
    for row in rows:
        if head is None:
            head = [h.strip().lower() for h in row]
            continue
        if not row:
            continue
        rec = dict(zip(head, [c.strip() for c in row]))
        sym = (rec.get("symbol") or "").upper()
        if not sym:
            continue
        band_txt = rec.get("band") or ""
        try:
            band = float(band_txt)
        except ValueError:
            band = None
        series = (rec.get("series") or "").upper()
        if sym in out and out[sym]["series"] == "EQ":
            continue                       # keep the EQ line when a symbol has two series
        out[sym] = {"series": series, "band": band, "band_text": band_txt}
    return out


def sec_list(path=SEC_LIST_CACHE, fetch=True):
    """The NSE price-band list: the cached file if it is from today (IST), else a fresh copy
    (saved back to the cache). Returns ({symbol: rec}, note)."""
    today = to_ist(utc_now()).date()
    fresh = (path and os.path.exists(path) and
             to_ist(utc_of(os.path.getmtime(path))).date() == today)
    if fresh or (path and os.path.exists(path) and not fetch):
        with open(path, encoding="utf-8", errors="replace") as fh:
            note = "" if fresh else "the price-band list %s is not today's" % path
            return parse_sec_list(fh.read()), note
    if not fetch:
        return {}, "no price-band list (%s)" % path
    text, err = None, None
    for ua in (BROWSER_UA, UA):        # NSE archives answer a browser user agent; the pack's own as a fallback
        try:
            text = _open_url(SEC_LIST_URL, ua=ua)
            break
        except (urllib.error.URLError, OSError) as e:
            err = e
    if text is None:
        return {}, "NSE's price-band list did not load (%s)" % (err,)
    try:
        if path:
            d = os.path.dirname(path)
            if d:
                os.makedirs(d, exist_ok=True)
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(text)
    except OSError:
        pass
    return parse_sec_list(text), ""


def circuits(prev_close, band):
    """(lower, upper) circuit for a band in percent: prev close x (1 -/+ band), on the tick grid
    (lower rounds up, upper rounds down: the prices that can actually trade)."""
    pc, b = D(prev_close), D(band)
    return round_to_tick(pc * (1 - b / 100), "up"), round_to_tick(pc * (1 + b / 100), "down")


# ---------------------------------------------------------------- bars for ATR and the previous close

def _sleep(seconds):
    time.sleep(seconds)


def yahoo_get(path):
    """query1 first, query2 on 429 or 5xx; one retry after a pause on 429."""
    last = "no answer"
    for host in HOSTS:
        url = "https://%s.finance.yahoo.com%s" % (host, path)
        for attempt in (0, 1):
            try:
                return _open_url(url)
            except urllib.error.HTTPError as e:
                code, body = e.code, None
                try:
                    if code == 404:
                        body = e.read().decode("utf-8", "replace")
                except Exception:
                    body = None
                finally:
                    e.close()
                if code == 404:
                    if body:
                        return body
                    raise PlanError("Yahoo has no chart at %s (HTTP 404)" % url.split("?")[0])
                last = "HTTP %d" % code
                if code == 429 and attempt == 0:
                    _sleep(BACKOFF)
                    continue
                if code == 429 or code >= 500:
                    break
                raise PlanError("Yahoo answered HTTP %d for %s" % (code, url.split("?")[0]))
            except urllib.error.URLError as e:
                last = "could not connect: %s" % (e.reason,)
                break
            except OSError as e:
                last = "network error: %s" % (e,)
                break
    raise PlanError("Yahoo did not answer (%s on query1 and query2): wait a minute and try again" % last)


def read_text(path):
    if path.endswith(".gz"):
        with gzip.open(path, "rt", encoding="utf-8") as fh:
            return fh.read()
    with open(path, encoding="utf-8") as fh:
        return fh.read()


def load_chart(sym, interval, day, offline=None, exch="NSE", kite=None):
    ysym = yahoo_symbol(sym, exch)
    if not kite and offline and os.path.isfile(offline) and not offline.endswith(".gz"):
        try:
            with open(offline, encoding="utf-8") as fh:
                if kite_candles(json.load(fh)) is not None:
                    kite = [offline]
        except (OSError, ValueError):
            pass
    if kite:
        return kite_chart(kite, sym, exch)
    if offline:
        path = offline
        if os.path.isdir(offline):
            path = None
            for name in (sym.upper(), ysym, sym.upper() + "-60d5m"):
                for ext in (".json", ".json.gz"):
                    p = os.path.join(offline, name + ext)
                    if path is None and os.path.exists(p):
                        path = p
            path = path or os.path.join(offline, sym.upper() + ".json")
        if not os.path.exists(path):
            raise PlanError("no offline file %s" % path)
        try:
            text = read_text(path)
        except (OSError, ValueError) as e:
            raise PlanError("could not read %s: %s" % (path, e))
    else:
        back = (to_ist(utc_now()).date() - day).days
        rng = "5d" if back <= 6 else "1mo" if back <= 28 else "60d"
        if interval == "1m" and back > 6:
            raise PlanError("Yahoo keeps 1-minute bars for about a week")
        text = yahoo_get("/v8/finance/chart/%s?range=%s&interval=%s" % (
            urllib.parse.quote(ysym, safe=""), rng, interval))
    try:
        data = json.loads(text)
        res = data["chart"]["result"]
    except (ValueError, KeyError, TypeError):
        raise PlanError("Yahoo returned something unexpected for %s" % sym)
    if not res:
        err = (data.get("chart") or {}).get("error") or {}
        raise PlanError("Yahoo has no data for %s: %s" % (sym, err.get("description") or "unknown symbol"))
    r = res[0] or {}
    meta = r.get("meta") or {}
    if str(meta.get("symbol") or ysym).upper() != ysym:
        raise PlanError("the chart file holds %s, not %s" % (meta.get("symbol"), ysym))
    q = ((r.get("indicators") or {}).get("quote") or [{}])[0] or {}

    def at(key, i):
        arr = q.get(key) or []
        return arr[i] if i < len(arr) else None

    bars = []
    for i, t in enumerate(r.get("timestamp") or []):
        c = at("close", i)
        if t is None or c is None:
            continue
        h, lo = at("high", i), at("low", i)
        bars.append({"t": int(t), "h": float(c if h is None else h), "l": float(c if lo is None else lo),
                     "c": float(c)})
    return {"bars": bars, "data_time": meta.get("regularMarketTime"), "source": "yahoo",
            "prev_close": meta.get("previousClose") or meta.get("chartPreviousClose")}


def prev_session_close(chart, day):
    """Yesterday's close: Yahoo's previousClose when the chart's last trade is on `day`, else the
    last bar of the session before `day` (a 5-minute close, near NSE's official close)."""
    dt = chart.get("data_time")
    if dt and to_ist(utc_of(dt)).date() == day and chart.get("prev_close"):
        return round(float(chart["prev_close"]), 2)
    best = None
    for b in chart["bars"]:
        if to_ist(utc_of(b["t"])).date() < day and (best is None or b["t"] > best["t"]):
            best = b
    return round(best["c"], 2) if best else None


def session_bars(chart, day, interval, upto_slot=None, now_utc=None, cas=None):
    """Closed, on-grid bars of one IST session, oldest first (the live tick is dropped)."""
    step = INTERVALS[interval]
    s = session(day, cas)
    if not s:
        return []
    open_dt = datetime.datetime.combine(day, s[0])
    close_dt = datetime.datetime.combine(day, s[1])
    if now_utc is None and chart.get("data_time"):
        now_utc = utc_of(chart["data_time"])
    local = to_ist(now_utc) if now_utc else None
    out = {}
    for b in chart["bars"]:
        if b["t"] % step:
            continue
        et = to_ist(utc_of(b["t"]))
        if et.date() != day or not (open_dt <= et < close_dt):
            continue
        if local and et + datetime.timedelta(seconds=step) > local:
            continue
        slot = int((et - open_dt).total_seconds()) // step
        if upto_slot is not None and slot > upto_slot:
            continue
        out[slot] = b
    return [out[k] for k in sorted(out)]


def atr_of(bars, n=ATR_BARS):
    """Average true range of the last n bars (all of them if fewer). The first
    bar of the session uses its own high - low (no overnight gap)."""
    trs, prev = [], None
    for b in bars:
        tr = b["h"] - b["l"]
        if prev is not None:
            tr = max(tr, abs(b["h"] - prev), abs(b["l"] - prev))
        trs.append(tr)
        prev = b["c"]
    use = trs[-n:]
    return sum(use) / len(use), len(use)


# ---------------------------------------------------------------- plan

def read_json(path, what):
    if not path or not os.path.exists(path):
        raise PlanError("no %s at %s" % (what, path))
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except ValueError as e:
        raise PlanError("%s is not valid JSON: %s" % (path, e))


def read_config(path):
    cfg = dict(DEFAULTS)
    if path and os.path.exists(path):
        doc = read_json(path, "config")
        for k in DEFAULTS:
            if doc.get(k) is not None:
                cfg[k] = doc[k]
    return cfg


def resolve_flat_by(day, explicit, cfg, entry_time, rules=None):
    """(HH:MM, warnings). "auto" (the default) is the stock's flat_by_default: 15:00 for a CAS stock,
    15:10 otherwise. One at or after Zerodha's square-off (15:12 CAS, 15:25 non-CAS) is clamped to
    that default, with a warning."""
    rules = rules or session_rules(None, day or datetime.date.today())
    text = explicit or cfg.get("flat_by") or DEFAULTS["flat_by"]
    warns = []
    if str(text).strip().lower() == "auto":
        text = rules["flat_by_default"]
    t = parse_hhmm(text, "--flat-by" if explicit else "flat_by")
    if t <= OPEN:
        raise PlanError("flat-by %s is before the 9:15 AM open" % hm(t))
    sq = parse_hhmm(rules["square_off"], "square_off")
    if t >= sq:
        warns.append("Flat-by %s is at or after Zerodha's %s MIS auto square-off for this stock (%s; ₹50 + GST "
                     "per order it squares off): moved to %s." % (hm(t), rules["square_off"],
                                                                  "CAS stock" if rules["strict"] else "non-CAS stock",
                                                                  rules["flat_by_default"]))
        t = parse_hhmm(rules["flat_by_default"], "flat_by")
    if entry_time and entry_time >= t:
        raise PlanError("the entry at %s is at or after your flat-by %s: there is no time left to hold it"
                        % (ampm(entry_time), hm(t)))
    return hm(t), warns


def gather(a):
    """The entry facts, from trader-in/entry.json or the flags."""
    if a.symbol and a.from_path:
        raise PlanError("use --from or --symbol, not both")
    if a.side in ("sell", "short"):
        raise PlanError("this pack plans longs only: no stops above a short entry. Plan a buy instead.")
    if a.symbol:
        entry_px, entry_src = a.entry, "flags"
        if entry_px is None:
            s0, e0 = parse_symbol(a.symbol, a.exchange)
            entry_px = parse_price_flag(a.price, s0)
            if entry_px is None and a.kite_ltp:
                entry_px, entry_src = kite_ltp(a.kite_ltp, s0, e0), "kite-ltp"
        if entry_px is None:
            raise PlanError("--symbol needs --entry PRICE (or --kite-ltp FILE / --price SYM=PRICE)")
        if (a.or_high is None) != (a.or_low is None):
            raise PlanError("give both --or-high and --or-low, or neither")
        sym, exch = parse_symbol(a.symbol, a.exchange)
        info = {"symbol": sym, "exchange": exch, "entry": entry_px, "entry_source": entry_src,
                "setup": a.setup or ("orb" if a.or_high is not None else "level"),
                "date": a.date, "entry_time": a.time, "entry_bar": a.entry_bar, "interval": a.interval or "5m",
                "or_high": a.or_high, "or_low": a.or_low, "or_minutes": a.or_minutes,
                "prev_close": a.prev_close, "source": "flags"}
    else:
        path = a.from_path or "trader-in/entry.json"
        doc = read_json(path, "entry (run /india-entry first, or give --symbol and --entry)")
        if str(doc.get("side") or "buy").lower() not in ("buy", "long"):
            raise PlanError("%s is a %s: this pack plans longs only" % (path, doc.get("side")))
        if doc.get("status") != "triggered" and a.entry is None:
            raise PlanError("%s says %s is %s, not triggered: plan once it triggers, or give --entry PRICE to plan ahead"
                            % (path, doc.get("symbol"), doc.get("status") or "unknown"))
        orr = doc.get("opening_range") or {}
        if not doc.get("symbol"):
            raise PlanError("%s has no symbol or price" % path)
        sym, exch = parse_symbol(doc.get("symbol"), a.exchange or doc.get("exchange"))
        info = {"symbol": sym, "exchange": exch,
                "entry": a.entry if a.entry is not None else doc.get("price"),
                "setup": a.setup or doc.get("setup") or "orb", "date": a.date or doc.get("date"),
                "entry_time": a.time or (doc.get("time") if a.entry is None else None),
                "entry_bar": a.entry_bar if a.entry_bar is not None else (doc.get("bar_index") if a.entry is None else None),
                "interval": a.interval or doc.get("interval") or "5m",
                "or_high": a.or_high if a.or_high is not None else orr.get("high"),
                "or_low": a.or_low if a.or_low is not None else orr.get("low"),
                "or_minutes": orr.get("minutes") or a.or_minutes,
                "prev_close": a.prev_close if a.prev_close is not None else doc.get("prev_close"),
                "entry_source": "flags" if a.entry is not None else str(doc.get("source") or "yahoo").lower(),
                "source": path}
        if info["entry"] is None:
            raise PlanError("%s has no symbol or price" % path)
    if info["interval"] not in INTERVALS:
        raise PlanError("interval is 5m or 1m")
    return info


def parse_band(text):
    t = str(text).strip().lower().rstrip("%")
    if t in ("none", "no", "no band", "noband", "0"):
        return None
    try:
        b = float(t)
    except ValueError:
        raise PlanError("--band is a percent (2, 5, 10, 20) or none, not %r" % text)
    if not 0 < b < 100:
        raise PlanError("--band must be between 0 and 100 percent")
    return b


def kite_instrument(path, sym, exch="NSE"):
    """Saved Kite search_instruments output -> {tick, lower, upper, series, date} for sym.
    The date is a "fetched" field when present, else the file's save date (IST)."""
    try:
        with open(path, encoding="utf-8") as fh:
            doc = json.load(fh)
    except (OSError, ValueError) as e:
        raise PlanError("could not read the Kite file %s: %s" % (path, e))
    items = doc.get("data") if isinstance(doc, dict) and "data" in doc else doc
    if isinstance(items, dict):
        items = [items]
    rec = None
    for it in items or []:
        if isinstance(it, dict) and str(it.get("tradingsymbol") or "").upper() == sym.upper() and \
                str(it.get("exchange") or exch).upper() == exch:
            rec = it
            break
    if rec is None:
        raise PlanError("%s has no %s:%s instrument (Kite search_instruments output)" % (path, exch, sym.upper()))
    fetched = (doc.get("fetched") if isinstance(doc, dict) else None) or rec.get("fetched")
    day = str(fetched)[:10] if fetched else to_ist(utc_of(os.path.getmtime(path))).date().isoformat()

    def pos(x):
        try:
            return float(x) if x is not None and float(x) > 0 else None
        except (TypeError, ValueError):
            return None
    return {"tick": pos(rec.get("tick_size")), "lower": pos(rec.get("lower_circuit_limit")),
            "upper": pos(rec.get("upper_circuit_limit")), "series": rec.get("series"), "date": day,
            "token": rec.get("instrument_token")}


def band_info(a, info, day, chart, warnings):
    """(band, series, prev_close, lower, upper, circuit_source). Never raises for missing data: it warns.
    A same-day Kite instrument file wins over sec_list.csv."""
    kinst = info.get("kite_instrument")
    if kinst and kinst["lower"] is not None and kinst["upper"] is not None:
        series = kinst.get("series")
        if series and series not in ("EQ",):
            if series in ("BE", "BZ"):
                warnings.append("Series %s is trade-to-trade: no intraday (MIS) trading allowed, every buy is "
                                "delivery (CNC)." % series)
        return None, series, info.get("prev_close"), D(kinst["lower"]), D(kinst["upper"]), "kite"
    band, series, known = None, None, False
    if a.band is not None:
        band, known = parse_band(a.band), True
    elif info["exchange"] == "BSE":
        warnings.append("BSE price bands are not in NSE's list: give --band to check the circuit.")
    else:
        table, note = sec_list(a.sec_list, fetch=not a.offline and not a.no_fetch)
        rec = table.get(info["symbol"])
        if rec:
            band, series, known = rec["band"], rec["series"], True
            if band is None:
                warnings.append("%s has No Band in NSE's list (an F&O stock): NSE sets a dynamic price band, "
                                "so there is no fixed circuit to check the stop against." % info["symbol"])
        elif table:
            warnings.append("%s is not in NSE's price-band list (sec_list.csv): circuit not checked."
                            % info["symbol"])
        if note:
            warnings.append(note[0].upper() + note[1:] + ": circuit not checked." if not rec else note + ".")
    if series and series != "EQ":
        if series in ("BE", "BZ"):
            warnings.append("Series %s is trade-to-trade: no intraday (MIS) trading allowed, every buy is "
                            "delivery (CNC)." % series)
        else:
            warnings.append("Series %s, not EQ: check it is an ordinary equity line." % series)
    pc = info.get("prev_close")
    if band is not None and pc is None:
        if chart is None and not a.no_fetch:
            try:
                chart = load_chart(info["symbol"], info["interval"], day, a.offline, info["exchange"])
            except PlanError as e:
                warnings.append("No previous close (%s): circuit not checked." % e)
        if chart is not None:
            pc = prev_session_close(chart, day)
        if pc is None:
            warnings.append("No previous close: give --prev-close to check the %s%% circuit." % num(D(band)))
    lower = upper = None
    if band is not None and pc is not None:
        lower, upper = circuits(pc, band)
    return band if known else None, series, pc, lower, upper, ("flags" if a.band is not None else "sec_list") \
        if lower is not None else None


def _cas(a, sym, warnings):
    """CAS status: --cas, the config's cas_symbols_extra / non_cas_symbols, then NSE's F&O list."""
    cfg = {}
    if a.config and os.path.exists(a.config):
        cfg = read_json(a.config, "config")
    if a.cas and str(a.cas).lower() != "auto":
        return cas_status(sym, a.cas, cfg)
    fo, note = load_fo_list(a.fo_list, fetch=not a.offline and not a.no_fetch)
    if note:
        warnings.append(note[0].upper() + note[1:] + ".")
    return cas_status(sym, None, cfg, fo)


def build_plan(a, now_utc):
    info = gather(a)
    cfg = read_config(a.config)
    load_holiday_cache(os.path.join(os.path.dirname(a.config) or ".", "holidays.json"))
    stop_text = a.stop
    if not stop_text:
        if info["setup"] != "orb":
            raise PlanError("a %s setup has no opening-range stop to default to: give --stop price:X, pct:X or atr:K"
                            % info["setup"])
        stop_text = "or-mid"
    stop_rule, stop_param = parse_stop(stop_text)
    target_rule, target_param = parse_target(a.target or "2R")
    if info["date"]:
        try:
            day = datetime.date.fromisoformat(str(info["date"]))
        except ValueError:
            raise PlanError("date wants YYYY-MM-DD, not %r" % info["date"])
    else:
        day = to_ist(now_utc).date()
    entry_time = parse_clock(info["entry_time"], "the entry time") if info["entry_time"] else None
    warnings = []
    cas = _cas(a, info["symbol"], warnings)
    rules = session_rules(cas, day)
    tick, tick_src = None, "nse-table"
    if a.kite_instrument:
        kinst = kite_instrument(a.kite_instrument, info["symbol"], info["exchange"])
        if kinst["date"] == day.isoformat():
            info["kite_instrument"] = kinst
            if kinst["tick"]:
                tick, tick_src = kinst["tick"], "kite"
        else:
            warnings.append("The Kite instrument file is from %s, not the plan's day %s: its tick and circuit limits "
                            "are for that day only, so the NSE tick table and sec_list.csv are used instead."
                            % (kinst["date"], day.isoformat()))
    atr = atr_n = chart = None
    if stop_rule == "atr":
        chart = load_chart(info["symbol"], info["interval"], day, a.offline, info["exchange"], a.kite_bars)
        bars = session_bars(chart, day, info["interval"], info["entry_bar"], parse_now(a.now) if a.now else None,
                            cas)
        if len(bars) < 3:
            raise PlanError("an atr stop needs at least 3 closed %s bars of %s on %s by the entry; found %d"
                            % (info["interval"], info["symbol"], day, len(bars)))
        atr, atr_n = atr_of(bars)
    elif a.offline and info.get("prev_close") is None:
        try:
            chart = load_chart(info["symbol"], info["interval"], day, a.offline, info["exchange"])
        except PlanError:
            chart = None
    band, series, pc, lower, upper, circuit_src = band_info(a, info, day, chart, warnings)
    c = compute(info["entry"], stop_rule, stop_param, target_rule, target_param, info["or_high"], info["or_low"],
                atr, atr_n, lower, tick)
    flat_by, flat_warns = resolve_flat_by(day, a.flat_by, cfg, entry_time, rules)
    warnings = flat_warns + warnings
    if upper is not None and c["entry"] >= upper * Decimal("0.99"):
        warnings.append("The entry %s is within 1%% of the upper circuit %s: it can lock there with no sellers, "
                        "and india-risk blocks entries at or near it." % (inr(c["entry"]), inr(upper)))
    if upper is not None and c["target"] > upper:
        warnings.append("The target %s is above today's upper circuit %s: it cannot fill today." % (
            inr(c["target"]), inr(upper)))
    orr = None
    if info["or_high"] is not None and info["or_low"] is not None:
        orr = {"high": float(paise(info["or_high"])), "low": float(paise(info["or_low"])),
               "minutes": int(info["or_minutes"] or 15)}
    plan = {"created_by": "india-exit", "symbol": info["symbol"], "exchange": info["exchange"], "currency": "INR",
            "side": "buy", "date": day.isoformat(),
            "setup": info["setup"], "entry": float(c["entry"]),
            "entry_time": ampm(entry_time) if entry_time else None,
            "entry_bar": info["entry_bar"], "interval": info["interval"], "opening_range": orr,
            "stop": float(c["stop"]), "stop_rule": stop_rule, "stop_label": c["stop_label"],
            "target": float(c["target"]), "target_r": float(c["target_r"]), "target_label": c["target_label"],
            "risk_per_share": float(c["risk"]), "reward_risk": float(c["reward_risk"]), "flat_by": flat_by,
            "tick": float(c["tick"]), "tick_source": tick_src, "product": "MIS",
            "cas": cas, "square_off": rules["square_off"], "session_close": rules["close"],
            "entry_source": info.get("entry_source") or "flags", "circuit_source": circuit_src,
            "prev_close": float(pc) if pc is not None else None, "band_pct": band, "series": series,
            "lower_circuit": float(lower) if lower is not None else None,
            "upper_circuit": float(upper) if upper is not None else None,
            "stop_detail": c["stop_detail"], "created_at": now_utc.strftime("%Y-%m-%dT%H:%M:%SZ"), "note": NOTE}
    if series in ("BE", "BZ"):
        plan["product"] = "CNC"
    if atr is not None:
        plan["atr"] = round(atr, 4)
        plan["atr_bars"] = atr_n
    mrr = cfg.get("min_reward_risk")
    try:
        if mrr is not None and c["reward_risk"] < D(mrr):
            warnings.append("Reward:risk %s is under your min_reward_risk %s in the config: india-risk checks that."
                            % (ratio(c["reward_risk"]), num(D(mrr))))
    except PlanError:
        pass
    if cas is None and rules["strict"]:
        warnings.append(rules["label"] + ".")
    if session(day) is None:
        warnings.append("%s is not an NSE trading day (weekend or holiday)." % day.isoformat())
    today = to_ist(now_utc).date()
    if day != today:
        warnings.append("This plan is for %s, not today (%s)." % (day.isoformat(), today.isoformat()))
    return plan, warnings


def describe(plan):
    day = datetime.date.fromisoformat(plan["date"])
    setup = {"orb": "opening-range breakout", "level": "close above a level"}.get(plan["setup"], plan["setup"])
    e = D(plan["entry"])
    risk = D(plan["risk_per_share"])
    pct = (risk / e * 100).quantize(PAISA, rounding=ROUND_HALF_UP)
    when = ""
    if plan.get("entry_time"):
        when = " at %s" % plan["entry_time"]
        if plan.get("entry_bar") is not None:
            when += " (bar %d)" % plan["entry_bar"]
    out = ["Exit plan: %s (%s) long, %s, %s on %s bars" % (
        plan["symbol"], plan.get("exchange") or "NSE", day.strftime("%a %Y-%m-%d"), setup,
        "5-minute" if plan["interval"] == "5m" else "1-minute")]
    out.append("  Entry         %s%s (%s)" % (inr(plan["entry"]), when,
                                         SOURCE_LABEL.get(plan.get("entry_source") or "flags", "your price")))
    out.append("  Stop          %s  %s%s" % (inr(plan["stop"]), plan["stop_label"],
                                             (": " + plan["stop_detail"]) if plan.get("stop_detail") else ""))
    out.append("  Target        %s  %s" % (inr(plan["target"]), plan["target_label"]))
    out.append("  Risk a share  %s (%s%% of the entry)" % (inr(plan["risk_per_share"]), pct))
    out.append("  Reward:risk   %s" % ratio(plan["reward_risk"]))
    out.append("  Flat by       %s IST, win or lose: no overnight hold (Zerodha squares off MIS at %s)"
               % (plan["flat_by"], plan.get("square_off") or "15:12"))
    if plan.get("tick") is not None:
        out.append("  Tick          %s (%s)" % (inr(plan["tick"]), "Kite instrument" if plan.get("tick_source") == "kite"
                                              else "NSE tick-size table"))
    if plan.get("lower_circuit") is not None:
        if plan.get("circuit_source") == "kite":
            how = "Kite instrument, today's limits"
        else:
            how = "%s%% band on the previous close %s" % (num(D(plan["band_pct"])), inr(plan["prev_close"]))
        out.append("  Circuit       %s to %s (%s)" % (inr(plan["lower_circuit"]), inr(plan["upper_circuit"]), how))
    if plan.get("session_close"):
        out.append("  Session       continuous trading to %s IST (%s)" % (
            plan["session_close"], "CAS stock" if plan.get("cas") else "non-CAS stock" if plan.get("cas") is False
            else "F&O list unknown, CAS rules"))
    out.append("Set before the entry. If price hits the stop, you are out; never move it further away.")
    out.append(NOTE)
    return out


def cmd_plan(a):
    now = parse_now(a.now)
    plan, warnings = build_plan(a, now)
    if a.write:
        d = os.path.dirname(a.write)
        if d:
            os.makedirs(d, exist_ok=True)
        with open(a.write, "w", encoding="utf-8") as fh:
            json.dump(plan, fh, indent=2, ensure_ascii=False)
            fh.write("\n")
    if a.json:
        print(json.dumps(dict(plan, warnings=warnings), indent=2, ensure_ascii=False))
        return 0
    for line in describe(plan):
        print(line)
    for w in warnings:
        print("Note: " + w)
    if a.write:
        print("Wrote %s." % a.write)
    print("Next: /india-timemachine replays it, then /india-budget sizes it to your max loss "
          "(charges included). Nothing was placed.")
    return 0


def cmd_show(a):
    if not os.path.exists(a.plan):
        print("No plan at %s yet. Make one: exit.py plan --write" % a.plan)
        return 1
    plan = read_json(a.plan, "plan")
    for k in ("symbol", "date", "entry", "stop", "target", "risk_per_share", "reward_risk", "flat_by"):
        if plan.get(k) is None:
            raise PlanError("%s is missing %r: make a new plan with exit.py plan --write" % (a.plan, k))
    if a.json:
        print(json.dumps(plan, indent=2, ensure_ascii=False))
        return 0
    for line in describe(dict({"setup": "orb", "interval": "5m", "stop_label": "", "target_label": ""}, **plan)):
        print(line)
    today = to_ist(parse_now(a.now)).date().isoformat()
    if plan["date"] != today:
        print("Note: this plan is for %s, not today (%s)." % (plan["date"], today))
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(description="Exit Planner (india-exit): the stop and target before the entry.")
    sub = ap.add_subparsers(dest="cmd")
    p = sub.add_parser("plan", help="set the stop and target")
    p.add_argument("--from", dest="from_path", metavar="PATH", help="entry.json to plan from (default trader-in/entry.json)")
    p.add_argument("--symbol", help="NSE symbol (RELIANCE), RELIANCE.BO / BSE:RELIANCE / 500325 for BSE")
    p.add_argument("--exchange", choices=["NSE", "BSE"])
    p.add_argument("--entry", type=float, help="entry price (or a plan-ahead price with --from)")
    p.add_argument("--or-high", type=float)
    p.add_argument("--or-low", type=float)
    p.add_argument("--or-minutes", type=int, default=15)
    p.add_argument("--setup", choices=["orb", "level"])
    p.add_argument("--date", help="session date YYYY-MM-DD (default: the entry's, or today)")
    p.add_argument("--time", help="entry time, e.g. 9:50 AM")
    p.add_argument("--entry-bar", type=int)
    p.add_argument("--interval", choices=["5m", "1m"])
    p.add_argument("--side", default="buy", choices=["buy", "long", "sell", "short"], help="buy only")
    p.add_argument("--stop", help="or-mid (orb default), or-low, price:X, pct:X, atr:K")
    p.add_argument("--target", help="2R (default), 1R, 1.5R, price:X")
    p.add_argument("--flat-by", help="HH:MM IST or auto (default: config flat_by, auto = 15:00 CAS / 15:10 non-CAS)")
    p.add_argument("--band", help="price band in percent (2, 5, 10, 20) or none; default: NSE's sec_list.csv")
    p.add_argument("--prev-close", type=float, help="previous close for the circuit (default: entry.json or Yahoo)")
    p.add_argument("--sec-list", default=SEC_LIST_CACHE, help="NSE price-band list cache (default trader-in/sec_list.csv)")
    p.add_argument("--no-fetch", action="store_true", help="no network: skip the band list, F&O list and Yahoo")
    p.add_argument("--cas", default="auto", help="yes / no / auto: is it a CAS (F&O) stock (default: NSE's F&O list)")
    p.add_argument("--fo-list", default=FO_CACHE, help="NSE F&O list cache (default trader-in/fo_list.txt)")
    p.add_argument("--kite-bars", action="append", metavar="FILE",
                   help="saved Kite get_historical_data output, for an atr stop (repeatable)")
    p.add_argument("--kite-instrument", metavar="FILE",
                   help="saved Kite search_instruments output: its tick and circuit limits win for that day")
    p.add_argument("--kite-ltp", metavar="FILE", help="saved Kite get_ltp / get_quotes output: the entry with --symbol")
    p.add_argument("--price", action="append", metavar="SYM=PRICE", help="a last price you give: the entry with --symbol")
    p.add_argument("--config", default="trader-in/config.json")
    p.add_argument("--offline", metavar="PATH", help="saved Yahoo chart JSON for an atr stop or the previous close")
    p.add_argument("--now", help='pretend it is this India time, e.g. "2026-10-19 09:50"')
    p.add_argument("--write", nargs="?", const="trader-in/plan.json", metavar="PATH",
                   help="save the plan (default trader-in/plan.json)")
    p.add_argument("--json", action="store_true")
    p = sub.add_parser("show", help="print the current plan")
    p.add_argument("--plan", default="trader-in/plan.json")
    p.add_argument("--now")
    p.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)
    if not a.cmd:
        ap.print_help()
        return 1
    try:
        return {"plan": cmd_plan, "show": cmd_show}[a.cmd](a)
    except PlanError as e:
        print("exit.py: %s" % e, file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
