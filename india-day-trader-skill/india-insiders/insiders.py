#!/usr/bin/env python3
"""Insider trades and big-investor deals for india-insiders. Standard library only.

Reads two kinds of PUBLIC disclosures from NSE:

  1. SEBI PIT (Prohibition of Insider Trading) disclosures: promoters,
     directors, key managerial personnel (KMP), employees and their immediate
     relatives buying, selling or pledging shares of their own company.
  2. NSE bulk deals (one client trading 0.5% or more of a company's shares in
     a day) and block deals (single trades of Rs 10 crore or more in the block
     window). This is how people follow "big bull" investors.

    python3 insiders.py pit RELIANCE                         # insider trades in one stock
    python3 insiders.py pit RELIANCE --from 2026-01-01 --category promoter
    python3 insiders.py deals                                # the last trading day's bulk + block deals
    python3 insiders.py deals --investor kacholia --investor "vijay kedia"
    python3 insiders.py deals --from 2026-10-01 --to 2026-10-22 --symbol RAVITA
    python3 insiders.py investors add "Ashish Kacholia"      # a saved list of names to follow
    python3 insiders.py new                                  # only what is new since the last check

Where the data comes from:
  - PIT:   https://www.nseindia.com/api/corporates-pit?index=equities&symbol=X
           (optional &from_date=DD-MM-YYYY&to_date=DD-MM-YYYY). Needs the NSE cookie session.
  - Deals, last trading day: https://nsearchives.nseindia.com/content/equities/bulk.csv and block.csv
           (public, no cookie).
  - Deals, history: https://www.nseindia.com/api/historicalOR/bulk-block-short-deals
           ?optionType=bulk_deals|block_deals&from=DD-MM-YYYY&to=DD-MM-YYYY[&symbol=X]
           (cookie session). Seen 2026-10-23: at most 70 rows per call, sorted by
           symbol, so this tool asks one day at a time and warns when a day hits 70.

What these are, and are not:
  - PUBLIC disclosures, read fast. Not inside information. An insider must tell
    the company within 2 trading days of a trade (PIT Reg 7(2)) and the company
    tells the exchange within 2 trading days after that, so a PIT row is days old.
    Bulk and block deals are published after the close of the day they happen.
  - Not a reason to buy on their own. Promoters sell for taxes, gifts and loans;
    a big investor's buy can be one leg of a larger plan you cannot see.

State: `new` remembers what it has shown in trader-in/state/insiders.json, so each
filing or deal alerts once. Offline: --offline reads saved files instead of the
network (a PIT JSON file for `pit`, a folder with bulk.csv, block.csv and
pit-<SYMBOL>.json for `deals` and `new`). The tests use it.
"""

import argparse
import csv
import datetime as _dt
import difflib
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

IST = _dt.timezone(_dt.timedelta(hours=5, minutes=30))
NSE = "https://www.nseindia.com"
PIT_URL = NSE + "/api/corporates-pit"
HIST_URL = NSE + "/api/historicalOR/bulk-block-short-deals"
ARCHIVE = "https://nsearchives.nseindia.com/content/equities/%s.csv"
WARMUP = [NSE + "/", NSE + "/companies-listing/corporate-filings-insider-trading"]
BROWSER_UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/130 Safari/537.36")
ARCHIVE_UA = "Mozilla/5.0 (compatible; india-day-trader-skill/1.0; +https://github.com/)"
HIST_CAP = 70          # rows per historical call seen on 2026-10-23
PAUSE = 1.1            # seconds between NSE calls: at most 1 request a second
FIRST_DAYS = 30        # the first `new` run shows PIT filings broadcast in the last 30 days
LATE_TRADING_DAYS = 4  # 2 days insider -> company, 2 days company -> exchange

# NSE trading holidays 2026 (CM segment, read 2026-10-23), for counting trading days.
HOLIDAYS_2026 = {
    "2026-01-15", "2026-01-26", "2026-02-15", "2026-03-03", "2026-03-21", "2026-03-26",
    "2026-03-31", "2026-04-03", "2026-04-14", "2026-05-01", "2026-05-28", "2026-06-26",
    "2026-08-15", "2026-09-14", "2026-10-02", "2026-10-20", "2026-11-08", "2026-11-10",
    "2026-11-24", "2026-12-25",
}

CATEGORIES = {  # what the user types -> words in NSE's personCategory
    "promoter": "promoter", "promoters": "promoter", "promoter group": "promoter group",
    "director": "director", "directors": "director",
    "kmp": "key managerial", "key managerial personnel": "key managerial",
    "relative": "immediate relative", "relatives": "immediate relative",
    "immediate relative": "immediate relative",
    "employee": "employee", "employees": "employee", "designated": "designated",
    "other": "other",
}

NOTE_PIT = ("Public SEBI PIT disclosures, filed by the insider within 2 trading days and by the "
            "company within 2 more. Not inside information, and not a reason to buy on its own.")
NOTE_DEALS = ("Public NSE bulk/block deal reports, published after the close. Client names are as "
              "the broker reported them. Not inside information, and not a reason to buy on its own.")


class InsidersError(Exception):
    pass


# ---------------------------------------------------------------- dates and money

def today_ist():
    return _dt.datetime.now(IST).date()


def parse_date(text):
    """'13-Feb-2026', '22-OCT-2026', '13-02-2026', '2026-02-13' or '18-Feb-2026 19:06'
    -> '2026-02-13'. None if it is not a date."""
    s = (text or "").strip()
    if not s or s in ("-", "NA", "Nil"):
        return None
    s = s.split()[0]
    for fmt in ("%d-%b-%Y", "%d-%m-%Y", "%Y-%m-%d", "%d/%m/%Y"):
        try:
            return _dt.datetime.strptime(s.title() if "%b" in fmt else s, fmt).date().isoformat()
        except ValueError:
            pass
    return None


def nse_date(iso):
    """'2026-10-22' -> '22-10-2026' (the form the NSE API takes)."""
    return _dt.date.fromisoformat(iso).strftime("%d-%m-%Y")


def trading_days_between(start, end, holidays=HOLIDAYS_2026):
    """Trading days after `start` up to and including `end` (weekends and NSE
    holidays skipped). Same day = 0."""
    a, b = _dt.date.fromisoformat(start), _dt.date.fromisoformat(end)
    if b <= a:
        return 0
    n, d = 0, a
    while d < b:
        d += _dt.timedelta(days=1)
        if d.weekday() < 5 and d.isoformat() not in holidays:
            n += 1
    return n


def _num(text):
    s = str(text if text is not None else "").replace(",", "").strip()
    if s in ("", "-", "NA", "Nil", "None"):
        return None
    try:
        v = float(s)
    except ValueError:
        return None
    return int(v) if v == int(v) else v


def group_in(n):
    """Indian digit grouping: 12345678 -> '1,23,45,678'."""
    s = str(int(n))
    neg = s.startswith("-")
    s = s.lstrip("-")
    if len(s) > 3:
        head, tail = s[:-3], s[-3:]
        parts = []
        while len(head) > 2:
            parts.insert(0, head[-2:])
            head = head[:-2]
        if head:
            parts.insert(0, head)
        s = ",".join(parts) + "," + tail
    return ("-" if neg else "") + s


def rupees(v, decimals=0):
    """1234567.8 -> '₹12,34,568' (decimals=0) and adds '(₹x.xx Cr)' from 1 crore up."""
    if v is None:
        return "?"
    whole = int(round(v)) if decimals == 0 else int(v)
    out = "₹" + group_in(whole)
    if decimals:
        out += ("%.*f" % (decimals, abs(v) - int(abs(v))))[1:]
    if abs(v) >= 1e7:
        out += " (₹%.2f Cr)" % (v / 1e7)
    elif abs(v) >= 1e5:
        out += " (₹%.2f lakh)" % (v / 1e5)
    return out


# ---------------------------------------------------------------- the network

class Session:
    """NSE cookie session: warm up on a page, keep the cookies, call the API with
    a Referer, at most one request a second, retry once."""

    def __init__(self, pause=PAUSE):
        self.jar = http.cookiejar.CookieJar()
        self.opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(self.jar))
        self.pause = pause
        self.last = 0.0
        self.warm = False

    def _wait(self):
        gap = time.time() - self.last
        if gap < self.pause:
            time.sleep(self.pause - gap)
        self.last = time.time()

    def _open(self, url, headers, timeout=30):
        self._wait()
        req = urllib.request.Request(url, headers=headers)
        with self.opener.open(req, timeout=timeout) as resp:
            data = resp.read()
            if resp.headers.get("Content-Encoding") == "gzip":
                data = gzip.decompress(data)
            return data

    def warmup(self):
        # The home page is sometimes refused by NSE's CDN (HTTP 403 seen on
        # 2026-10-23) while sub-pages and the API answer: try the next page.
        for url in WARMUP:
            try:
                self._open(url, {"User-Agent": BROWSER_UA, "Accept": "text/html,*/*",
                                 "Accept-Language": "en-US,en;q=0.9"})
                break
            except (urllib.error.URLError, OSError):
                continue
        self.warm = True

    def api(self, url):
        if not self.warm:
            self.warmup()
        headers = {"User-Agent": BROWSER_UA, "Accept": "application/json",
                   "Accept-Language": "en-US,en;q=0.9", "Referer": NSE + "/"}
        err = None
        for attempt in (1, 2):
            try:
                data = self._open(url, headers)
                return json.loads(data.decode("utf-8"))
            except urllib.error.HTTPError as exc:
                err = "HTTP %s" % exc.code
            except urllib.error.URLError as exc:
                err = "could not reach NSE: %s" % exc.reason
            except OSError as exc:  # socket timeouts
                err = "NSE did not answer (%s)" % exc
            except ValueError:
                err = "NSE did not return JSON (it may be blocking scripts right now)"
            if attempt == 1:
                time.sleep(2)
                self.warmup()
        raise InsidersError("%s for %s. NSE sometimes blocks scripts for a while: wait a few "
                            "minutes and try again, or open the page in your browser." % (err, url))

    def archive(self, name):
        url = ARCHIVE % name
        err = None
        # Seen 2026-10-23: the archive host answered the browser UA and, later the
        # same day, dropped the "compatible" one. Try the browser UA first.
        for attempt, ua in ((1, BROWSER_UA), (2, ARCHIVE_UA)):
            req = urllib.request.Request(url, headers={"User-Agent": ua, "Accept": "text/csv,*/*"})
            try:
                self._wait()
                with urllib.request.urlopen(req, timeout=30) as resp:
                    return resp.read().decode("utf-8-sig", "replace")
            except urllib.error.HTTPError as exc:
                err = "HTTP %s for %s" % (exc.code, url)
            except urllib.error.URLError as exc:
                err = "could not reach %s: %s" % (url, exc.reason)
            except OSError as exc:  # socket timeouts
                err = "%s: %s" % (url, exc)
            if attempt == 1:
                time.sleep(2)
        raise InsidersError(err)


# ---------------------------------------------------------------- PIT rows

def _action(txn, mode):
    t, m = (txn or "").lower(), (mode or "").lower()
    if "pledge" in t or "pledge" in m or "revok" in m:
        if "revok" in t or "revok" in m:
            return "pledge revoked"
        if "creation" in m:
            return "pledge created"
        if "invoc" in m or "invoke" in t:
            return "pledge invoked"
        return "pledge"
    if t.startswith("buy"):
        return "buy"
    if t.startswith("sell"):
        return "sell"
    return t or "?"


def _mode(mode):
    m = (mode or "").lower()
    if "esop" in m or "esos" in m:
        return "ESOP"
    if m.startswith("market"):
        return "market"
    if "off market" in m or "off-market" in m:
        return "off-market"
    if "gift" in m:
        return "gift"
    if "pledge" in m or "revok" in m:
        return "pledge"
    if "inter" in m and "se" in m:
        return "inter-se transfer"
    if m == "others":
        return "other"
    return m or "?"


def clean_pit(raw):
    """One NSE corporates-pit row -> a clean dict."""
    trade_from = parse_date(raw.get("acqfromDt"))
    trade_to = parse_date(raw.get("acqtoDt")) or trade_from
    told_company = parse_date(raw.get("intimDt"))
    broadcast = (raw.get("date") or "").strip()
    disclosed = parse_date(broadcast)
    row = {
        "id": "pit:%s" % (raw.get("did") or "%s-%s" % (raw.get("pid"), raw.get("acqName"))),
        "source": "SEBI PIT",
        "symbol": (raw.get("symbol") or "").upper(),
        "exchange": "NSE",
        "company": raw.get("company"),
        "who": " ".join((raw.get("acqName") or "").split()),
        "category": raw.get("personCategory"),
        "security": raw.get("secType"),
        "action": _action(raw.get("tdpTransactionType"), raw.get("acqMode")),
        "nse_type": raw.get("tdpTransactionType"),
        "mode": _mode(raw.get("acqMode")),
        "nse_mode": raw.get("acqMode"),
        "shares": _num(raw.get("secAcq")),
        "value_inr": _num(raw.get("secVal")),
        "before_shares": _num(raw.get("befAcqSharesNo")),
        "before_pct": _num(raw.get("befAcqSharesPer")),
        "after_shares": _num(raw.get("afterAcqSharesNo")),
        "after_pct": _num(raw.get("afterAcqSharesPer")),
        "trade_from": trade_from,
        "trade_to": trade_to,
        "told_company": told_company,
        "disclosed": disclosed,
        "disclosed_at": broadcast or None,
        "traded_on": raw.get("exchange") if raw.get("exchange") not in ("NA", "-", None) else None,
        "derivative": raw.get("derivativeType") if raw.get("derivativeType") not in ("-", None) else None,
        "regulation": raw.get("anex"),
        "remarks": raw.get("remarks") if raw.get("remarks") not in ("-", None) else None,
        "xbrl": raw.get("xbrl"),
        "lag_days": None, "lag_trading_days": None, "late": False,
    }
    if row["value_inr"] == 0:
        row["value_inr"] = None   # gifts, pledges and off-market moves are filed with value 0
    if row["shares"] and row["value_inr"]:
        row["avg_price"] = round(row["value_inr"] / row["shares"], 2)
    if trade_to and disclosed:
        row["lag_days"] = (_dt.date.fromisoformat(disclosed) - _dt.date.fromisoformat(trade_to)).days
        row["lag_trading_days"] = trading_days_between(trade_to, disclosed)
        row["late"] = row["lag_trading_days"] > LATE_TRADING_DAYS
    return row


def parse_pit(payload):
    """NSE corporates-pit JSON -> clean rows, newest disclosure first."""
    if not isinstance(payload, dict) or "data" not in payload:
        raise InsidersError("this does not look like an NSE corporates-pit response")
    rows = [clean_pit(r) for r in payload.get("data") or []]
    rows.sort(key=lambda r: (r["disclosed_at"] and _sort_stamp(r["disclosed_at"]) or "", r["id"]),
              reverse=True)
    return rows


def _sort_stamp(stamp):
    d = parse_date(stamp)
    m = re.search(r"(\d{1,2}):(\d{2})", stamp or "")
    return "%s %s" % (d or "", "%02d:%s" % (int(m.group(1)), m.group(2)) if m else "")


def pit_url(symbol=None, since=None, until=None):
    q = [("index", "equities")]
    if symbol:
        q.append(("symbol", symbol.upper()))
    if since or until:
        q.append(("from_date", nse_date(since or "2000-01-01")))
        q.append(("to_date", nse_date(until or today_ist().isoformat())))
    return PIT_URL + "?" + urllib.parse.urlencode(q)


def fetch_pit(session, symbol=None, since=None, until=None):
    return parse_pit(session.api(pit_url(symbol, since, until)))


def select_pit(rows, category=None, action=None, since=None, until=None, who=None):
    want = CATEGORIES.get((category or "").lower().strip(), (category or "").lower().strip())
    out = []
    for r in rows:
        if want and want not in (r["category"] or "").lower():
            continue
        if action and not r["action"].startswith(action.lower()):
            continue
        day = r["disclosed"] or r["trade_to"] or ""
        if since and day < since:
            continue
        if until and day > until:
            continue
        if who and not name_match(r["who"], who):
            continue
        out.append(r)
    return out


# ---------------------------------------------------------------- deals

def _deal(kind, date, symbol, name, client, side, qty, price, remarks=None):
    qty, price = _num(qty), _num(price)
    date = parse_date(date)
    client = " ".join((client or "").split())
    symbol = (symbol or "").strip().upper()
    side = (side or "").strip().upper()
    row = {"id": "%s:%s|%s|%s|%s|%s|%s" % (kind, date, symbol, client, side, qty, price),
           "source": "NSE %s deal" % kind, "kind": kind, "date": date, "symbol": symbol,
           "exchange": "NSE", "security": (name or "").strip(), "client": client,
           "side": side, "action": {"BUY": "buy", "SELL": "sell"}.get(side, side.lower()),
           "shares": qty, "price": price,
           "value_inr": round(qty * price, 2) if qty is not None and price is not None else None,
           "remarks": remarks if remarks not in ("-", "", None) else None}
    return row


def parse_deals_csv(text, kind):
    """bulk.csv / block.csv from nsearchives -> rows."""
    text = text.lstrip("﻿")
    reader = csv.DictReader(io.StringIO(text))
    need = {"Date", "Symbol", "Client Name", "Buy/Sell", "Quantity Traded"}
    if not reader.fieldnames or not need <= {f.strip() for f in reader.fieldnames}:
        raise InsidersError("%s.csv does not have the expected columns (got %s)"
                            % (kind, reader.fieldnames))
    out = []
    for raw in reader:
        r = {(k or "").strip(): (v or "").strip() for k, v in raw.items()}
        if not r.get("Symbol"):
            continue
        out.append(_deal(kind, r.get("Date"), r.get("Symbol"), r.get("Security Name"),
                         r.get("Client Name"), r.get("Buy/Sell"), r.get("Quantity Traded"),
                         r.get("Trade Price / Wght. Avg. Price"), r.get("Remarks")))
    return out


def parse_deals_hist(payload, kind):
    """historicalOR/bulk-block-short-deals JSON -> rows."""
    if not isinstance(payload, dict) or "data" not in payload:
        raise InsidersError("this does not look like an NSE historical deals response")
    return [_deal(kind, r.get("BD_DT_DATE"), r.get("BD_SYMBOL"), r.get("BD_SCRIP_NAME"),
                  r.get("BD_CLIENT_NAME"), r.get("BD_BUY_SELL"), r.get("BD_QTY_TRD"),
                  r.get("BD_TP_WATP"), r.get("BD_REMARKS"))
            for r in payload.get("data") or []]


def hist_url(kind, day_from, day_to, symbol=None):
    q = [("optionType", "%s_deals" % kind), ("from", nse_date(day_from)), ("to", nse_date(day_to))]
    if symbol:
        q.append(("symbol", symbol.upper()))
    return HIST_URL + "?" + urllib.parse.urlencode(q)


def weekdays(since, until):
    d, end, out = _dt.date.fromisoformat(since), _dt.date.fromisoformat(until), []
    while d <= end:
        if d.weekday() < 5 and d.isoformat() not in HOLIDAYS_2026:
            out.append(d.isoformat())
        d += _dt.timedelta(days=1)
    return out


def fetch_deals_hist(session, kinds, since, until, symbol=None, warn=None):
    """One call per day per kind (the API returns at most 70 rows a call).
    With one symbol, one call per kind for the whole range."""
    out = []
    for kind in kinds:
        spans = [(since, until)] if symbol else [(d, d) for d in weekdays(since, until)]
        for a, b in spans:
            rows = parse_deals_hist(session.api(hist_url(kind, a, b, symbol)), kind)
            if len(rows) >= HIST_CAP and warn is not None:
                warn.append("%s deals %s%s: NSE returned %d rows, its limit per call, so some "
                            "deals are missing (rows come sorted by symbol). Add --symbol to get "
                            "them all for one stock." % (kind, a, "" if a == b else " to " + b,
                                                         len(rows)))
            out.extend(rows)
    return out


# ---------------------------------------------------------------- names

def _norm(name):
    return re.sub(r"[^A-Z0-9]+", " ", (name or "").upper()).split()


def _token_ok(q, words):
    for w in words:
        if w == q or (len(q) >= 3 and w.startswith(q)):
            return True
        if (len(q) >= 5 and abs(len(q) - len(w)) <= 2 and q[0] == w[0]
                and difflib.SequenceMatcher(None, q, w).ratio() >= 0.85):
            return True  # AGARWAL ~ AGRAWAL, JHUNJUNWALA ~ JHUNJHUNWALA
    return False


def name_match(client, query):
    """Fuzzy client-name match: every word of the query must match a word of the
    name (exact, a prefix of 3+ letters, or a close spelling)."""
    words = _norm(client)
    q = _norm(query)
    return bool(q) and all(_token_ok(t, words) for t in q)


def mark_churn(rows):
    """A client that bought AND sold the same stock on the same day (in the same
    kind of deal) is trading, not building a position: flag both rows."""
    sides = {}
    for r in rows:
        sides.setdefault((r["kind"], r["date"], r["symbol"], r["client"]), set()).add(r["action"])
    for r in rows:
        r["same_day_both_sides"] = sides[(r["kind"], r["date"], r["symbol"], r["client"])] >= {"buy", "sell"}
    return rows


def select_deals(rows, investors=None, symbols=None, side=None, kinds=None):
    mark_churn(rows)
    out = []
    for r in rows:
        if kinds and r["kind"] not in kinds:
            continue
        if side and r["action"] != side.lower():
            continue
        if symbols and r["symbol"] not in symbols:
            continue
        if investors:
            hit = [q for q in investors if name_match(r["client"], q)]
            if not hit:
                continue
            r = dict(r, matched=hit)
        out.append(r)
    out.sort(key=lambda r: (r["date"] or "", r["value_inr"] or 0), reverse=True)
    return out


# ---------------------------------------------------------------- files

def load_json(path, default=None):
    if path and os.path.isfile(path):
        with open(path, encoding="utf-8") as fh:
            try:
                return json.load(fh)
            except ValueError:
                raise InsidersError("%s is not valid JSON" % path)
    return default


def save_json(path, data):
    folder = os.path.dirname(path)
    if folder:
        os.makedirs(folder, exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2, ensure_ascii=False)
        fh.write("\n")


def _sym(s):
    s = (s or "").strip().upper()
    for p in ("NSE:", "BSE:"):
        if s.startswith(p):
            s = s[len(p):]
    for suf in (".NS", ".BO"):
        if s.endswith(suf):
            s = s[:-len(suf)]
    return s


def held_symbols(portfolio):
    pf = load_json(portfolio, {}) or {}
    out = set()
    for p in pf.get("positions", []) if isinstance(pf, dict) else []:
        s = _sym(p.get("symbol") or p.get("ticker"))
        if s:
            out.add(s)
    return out


def watch_symbols(watchlist):
    wl = load_json(watchlist, None)
    out = set()
    items = []
    if isinstance(wl, dict):
        items = wl.get("hits") or wl.get("symbols") or []
    elif isinstance(wl, list):
        items = wl
    for it in items:
        s = _sym(it.get("symbol") or it.get("ticker") if isinstance(it, dict) else it)
        if s:
            out.add(s)
    return out


def load_investors(path):
    data = load_json(path, {}) or {}
    return list(data.get("investors", [])) if isinstance(data, dict) else []


def load_state(path):
    st = load_json(path, None)
    if not isinstance(st, dict):
        st = {}
    st.setdefault("seen", [])
    st.setdefault("last_check", None)
    return st


# ---------------------------------------------------------------- output

def _days(n, word):
    return "%d %s%s" % (n, word, "" if n == 1 else "s")


def _pct(v):
    return "?" if v is None else ("%g%%" % v)


def describe_pit(r):
    bits = ["%s (%s)" % (r["who"], (r["category"] or "?").lower())]
    what = r["action"]
    if r["shares"] is not None:
        what += " %s %s" % (group_in(r["shares"]),
                            "shares" if "equity" in (r["security"] or "").lower()
                            else (r["security"] or "securities").lower())
    what += " of %s" % r["symbol"]
    if r["value_inr"]:
        what += ", %s" % rupees(r["value_inr"])
        if r.get("avg_price"):
            what += " (about ₹%.2f a share)" % r["avg_price"]
    bits.append(what)
    bits.append(r["mode"] + (" on %s" % r["traded_on"] if r["traded_on"] else ""))
    if not r["before_pct"] and not r["after_pct"]:
        bits.append("holding under 0.01% of the company")
    else:
        bits.append("holding %s -> %s" % (_pct(r["before_pct"]), _pct(r["after_pct"])))
    when = "traded %s" % (r["trade_to"] if r["trade_from"] in (None, r["trade_to"])
                          else "%s to %s" % (r["trade_from"], r["trade_to"]))
    if r["disclosed"]:
        when += ", on NSE %s" % r["disclosed"]
        if r["lag_days"] is not None:
            when += " (%s, %s later%s)" % (
                _days(r["lag_days"], "day"), _days(r["lag_trading_days"], "trading day"),
                "; LATE: over %d trading days" % LATE_TRADING_DAYS if r["late"] else "")
    bits.append(when)
    return ", ".join(bits)


def describe_deal(r):
    out = "%s %s deal %s: %s %s %s %s at ₹%s = %s" % (
        r["date"], r["kind"], r["symbol"], r["client"], r["action"],
        group_in(r["shares"]) if r["shares"] is not None else "?",
        "shares", ("%.2f" % r["price"]) if r["price"] is not None else "?",
        rupees(r["value_inr"]))
    if r.get("same_day_both_sides"):
        out += " (bought and sold the same day: trading, not a position)"
    if r.get("matched"):
        out += "  [matches %s]" % ", ".join(r["matched"])
    return out


def render_pit(rows, watch=(), title=None):
    out = [title] if title else []
    for r in rows:
        flag = "  <- you hold %s" % r["symbol"] if r["symbol"] in watch else ""
        out.append("  - %s%s" % (describe_pit(r), flag))
    if not rows:
        out.append("  no insider disclosures matched")
    out.append(NOTE_PIT)
    return "\n".join(out) + "\n"


def render_deals(rows, watch=(), title=None, limit=None):
    out = [title] if title else []
    shown = rows if not limit else rows[:limit]
    for r in shown:
        flag = "  <- you hold %s" % r["symbol"] if r["symbol"] in watch else ""
        out.append("  - %s%s" % (describe_deal(r), flag))
    if limit and len(rows) > limit:
        out.append("  ... and %d more (use --limit 0 or --json for all)" % (len(rows) - limit))
    if not rows:
        out.append("  no deals matched")
    out.append(NOTE_DEALS)
    return "\n".join(out) + "\n"


# ---------------------------------------------------------------- CLI

def _iso_arg(s):
    d = parse_date(s)
    if not d:
        raise argparse.ArgumentTypeError("not a date: %s (use YYYY-MM-DD)" % s)
    return d


def _parser():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd")

    def files(p):
        p.add_argument("--portfolio", default="trader-in/portfolio.json")
        p.add_argument("--tickers", nargs="*", help="also flag these symbols")
        p.add_argument("--json", action="store_true")

    p = sub.add_parser("pit", help="SEBI PIT insider disclosures for one stock")
    p.add_argument("symbol", nargs="?", help="NSE symbol (leave out for the whole market, "
                                             "which NSE often answers with nothing)")
    p.add_argument("--from", dest="since", type=_iso_arg)
    p.add_argument("--to", dest="until", type=_iso_arg)
    p.add_argument("--category", help="promoter, director, kmp, relative, employee, other")
    p.add_argument("--action", help="buy, sell or pledge")
    p.add_argument("--who", help="fuzzy match on the insider's name")
    p.add_argument("--limit", type=int, default=25)
    p.add_argument("--offline", metavar="FILE", help="a saved corporates-pit JSON")
    files(p)

    p = sub.add_parser("deals", help="NSE bulk and block deals")
    p.add_argument("--kind", choices=["bulk", "block", "all"], default="all")
    p.add_argument("--investor", action="append", help="fuzzy client-name match; repeat")
    p.add_argument("--saved", action="store_true", help="match the saved investor list")
    p.add_argument("--investors-file", default="trader-in/investors.json")
    p.add_argument("--symbol", action="append", help="only these NSE symbols; repeat")
    p.add_argument("--side", choices=["buy", "sell"])
    p.add_argument("--date", type=_iso_arg, help="one past day (history API)")
    p.add_argument("--from", dest="since", type=_iso_arg, help="history from YYYY-MM-DD")
    p.add_argument("--to", dest="until", type=_iso_arg, help="history to YYYY-MM-DD")
    p.add_argument("--limit", type=int, default=40)
    p.add_argument("--offline", metavar="DIR", help="folder with bulk.csv and block.csv")
    files(p)

    p = sub.add_parser("investors", help="the saved list of investor names to follow")
    p.add_argument("op", choices=["list", "add", "remove"])
    p.add_argument("names", nargs="*")
    p.add_argument("--investors-file", default="trader-in/investors.json")

    p = sub.add_parser("new", help="PIT filings and deals not seen at the last check")
    p.add_argument("--symbols", nargs="*", help="stocks to check for PIT filings "
                                                "(default: what you hold + the watchlist)")
    p.add_argument("--investor", action="append")
    p.add_argument("--investors-file", default="trader-in/investors.json")
    p.add_argument("--watchlist", default="trader-in/watchlist.json")
    p.add_argument("--all-deals", action="store_true",
                   help="alert on every bulk/block deal, not only your names and stocks")
    p.add_argument("--no-pit", action="store_true")
    p.add_argument("--no-deals", action="store_true")
    p.add_argument("--state", default="trader-in/state/insiders.json")
    p.add_argument("--today", type=_iso_arg, help="YYYY-MM-DD, for reproducible runs")
    p.add_argument("--dry-run", action="store_true", help="do not update the state file")
    p.add_argument("--offline", metavar="DIR",
                   help="folder with bulk.csv, block.csv and pit-<SYMBOL>.json")
    files(p)
    return ap


def _watch(args):
    w = held_symbols(getattr(args, "portfolio", None))
    w.update(_sym(t) for t in (getattr(args, "tickers", None) or []))
    return w


def _read(path):
    with open(path, encoding="utf-8-sig") as fh:
        return fh.read()


def latest_deals(session, kinds, offline=None):
    rows = []
    for kind in kinds:
        if offline:
            path = os.path.join(offline, "%s.csv" % kind)
            text = _read(path) if os.path.isfile(path) else None
            if text is None:
                continue
        else:
            text = session.archive(kind)
        rows.extend(parse_deals_csv(text, kind))
    return rows


def main(argv=None):
    args = _parser().parse_args(argv)
    if not args.cmd:
        _parser().print_help()
        return 2
    try:
        return _run(args)
    except (InsidersError, OSError) as exc:
        print("insiders: %s" % exc, file=sys.stderr)
        return 2


def _run(args):
    session = Session()

    if args.cmd == "investors":
        data = load_json(args.investors_file, {}) or {}
        names = list(data.get("investors", []))
        if args.op == "add":
            for n in args.names:
                if n.lower() not in [x.lower() for x in names]:
                    names.append(n)
        elif args.op == "remove":
            drop = {n.lower() for n in args.names}
            names = [x for x in names if x.lower() not in drop]
        if args.op != "list":
            data["investors"] = names
            save_json(args.investors_file, data)
        print("\n".join(names) if names else "No saved investors. Add one: "
              "insiders.py investors add \"Ashish Kacholia\"")
        return 0

    if args.cmd == "pit":
        if args.offline:
            rows = parse_pit(json.loads(_read(args.offline)))
        else:
            rows = fetch_pit(session, args.symbol, args.since, args.until)
            if not rows and not args.symbol:
                raise InsidersError(
                    "NSE returned no rows for the whole-market query (on 2026-10-23 it answered "
                    "scripts with an empty list). Give a symbol: insiders.py pit RELIANCE")
        if args.symbol:
            rows = [r for r in rows if r["symbol"] == _sym(args.symbol)] or rows
        rows = select_pit(rows, args.category, args.action, args.since, args.until, args.who)
        if args.limit:
            rows = rows[:args.limit]
        if args.json:
            print(json.dumps(rows, indent=2, ensure_ascii=False))
        else:
            sys.stdout.write(render_pit(rows, _watch(args), "SEBI PIT disclosures%s, newest first:"
                                        % (" for " + _sym(args.symbol) if args.symbol else "")))
        return 0 if rows else 1

    if args.cmd == "deals":
        kinds = ["bulk", "block"] if args.kind == "all" else [args.kind]
        symbols = {_sym(s) for s in args.symbol or []} or None
        investors = list(args.investor or [])
        if args.saved:
            investors += load_investors(args.investors_file)
        warn = []
        since = args.date or args.since
        until = args.date or args.until or (since and today_ist().isoformat())
        if since:
            if args.offline:
                rows = []
                for kind in kinds:
                    for name in sorted(os.listdir(args.offline)):
                        if name.startswith("hist-%s" % kind) and name.endswith(".json"):
                            rows += parse_deals_hist(json.loads(_read(
                                os.path.join(args.offline, name))), kind)
                rows = [r for r in rows if since <= (r["date"] or "") <= until]
            else:
                one = sorted(symbols)[0] if symbols and len(symbols) == 1 else None
                rows = fetch_deals_hist(session, kinds, since, until, one, warn)
        else:
            rows = latest_deals(session, kinds, args.offline)
        picked = select_deals(rows, investors or None, symbols, args.side)
        if args.json:
            print(json.dumps({"deals": picked, "warnings": warn}, indent=2, ensure_ascii=False))
        else:
            days = sorted({r["date"] for r in rows if r["date"]})
            span = (days[0] if len(days) == 1 else "%s to %s" % (days[0], days[-1])) if days else "?"
            title = "NSE %s deals, %s%s:" % ("/".join(kinds), span,
                                             (", matching " + ", ".join(investors)) if investors else "")
            sys.stdout.write(render_deals(picked, _watch(args), title, args.limit))
            for w in warn:
                print("warning: " + w)
        return 0 if picked else 1

    # new
    today = args.today or today_ist().isoformat()
    state = load_state(args.state)
    seen = set(state["seen"])
    first = state["last_check"] is None
    held = _watch(args)
    symbols = {_sym(s) for s in args.symbols} if args.symbols else held | watch_symbols(args.watchlist)
    investors = list(args.investor or []) + load_investors(args.investors_file)
    fresh_pit, fresh_deals, all_ids, notes = [], [], [], []

    if not args.no_pit:
        if not symbols:
            notes.append("No stocks to check for insider filings: hold something in "
                         "trader-in/portfolio.json, run the scanner, or pass --symbols.")
        cutoff = (_dt.date.fromisoformat(today) - _dt.timedelta(days=FIRST_DAYS)).isoformat()
        for sym in sorted(symbols):
            if args.offline:
                path = os.path.join(args.offline, "pit-%s.json" % sym)
                if not os.path.isfile(path):
                    continue
                rows = parse_pit(json.loads(_read(path)))
            else:
                try:
                    rows = fetch_pit(session, sym)
                except InsidersError as exc:
                    notes.append("PIT %s: %s" % (sym, exc))
                    continue
            for r in rows:
                all_ids.append(r["id"])
                if r["id"] in seen:
                    continue
                if first and (r["disclosed"] or "") < cutoff:
                    continue
                fresh_pit.append(r)

    if not args.no_deals:
        try:
            deals = latest_deals(session, ["bulk", "block"], args.offline)
        except InsidersError as exc:
            deals = []
            notes.append("deals: %s" % exc)
        if args.all_deals:
            mine = deals
        else:
            by_name = select_deals(deals, investors) if investors else []
            by_stock = select_deals(deals, symbols=symbols) if symbols else []
            ids, mine = set(), []
            for r in by_name + by_stock:
                if r["id"] not in ids:
                    ids.add(r["id"])
                    mine.append(r)
            if not investors and not symbols:
                notes.append("No investor names saved and no stocks to watch, so no deal alerts. "
                             "Try: insiders.py investors add kacholia (or --all-deals).")
        for r in deals:
            all_ids.append(r["id"])
        fresh_deals = [r for r in mine if r["id"] not in seen]

    if args.json:
        print(json.dumps({"pit": fresh_pit, "deals": fresh_deals, "notes": notes},
                         indent=2, ensure_ascii=False))
    else:
        if not fresh_pit and not fresh_deals:
            print("Nothing new since %s." % (state["last_check"] or "the last check"))
        if fresh_pit:
            sys.stdout.write(render_pit(fresh_pit, held, "NEW insider (SEBI PIT) disclosures:"))
        if fresh_deals:
            sys.stdout.write(render_deals(sorted(fresh_deals, key=lambda r: (r["date"] or "",
                                          r["value_inr"] or 0), reverse=True),
                                          held, "NEW bulk/block deals:"))
        for n in notes:
            print("note: " + n)
    if not args.dry_run:
        seen.update(all_ids)
        state["seen"] = sorted(seen)
        state["last_check"] = _dt.datetime.now(IST).replace(microsecond=0).isoformat()
        save_json(args.state, state)
    return 0 if (fresh_pit or fresh_deals) else 1


if __name__ == "__main__":
    sys.exit(main())
