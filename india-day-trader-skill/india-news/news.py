#!/usr/bin/env python3
"""News monitor for india-news. Standard library only, Python 3.9+.

Watches NSE corporate announcements and news headlines for the stocks you
watch or hold and prints only what is new since the last check, newest first,
in India time (IST), with how far each stock is from yesterday's close and a
flag on anything that touches your symbols.

    python3 news.py                                  # watchlist + plan + portfolio symbols
    python3 news.py --symbols RELIANCE HDFCBANK TATAMOTORS
    python3 news.py --symbols RELIANCE --all --hours 72   # everything in 3 days, seen or not
    python3 news.py --no-headlines                   # NSE announcements only
    python3 news.py --no-moves                       # skip the price line per symbol

Your symbols, when --symbols is not given: every hit in trader-in/watchlist.json
(india-scanner), the symbol in trader-in/plan.json (india-exit) and the
positions in trader-in/portfolio.json. Symbols are NSE symbols (RELIANCE,
M&M, BAJAJ-AUTO); RELIANCE.BO or BSE:RELIANCE means the BSE listing (the price
line then comes from BSE; announcements are read from NSE either way).

Sources, one request each per symbol, at most one request a second:
  - NSE corporate announcements (India's version of 8-Ks: board meetings,
    financial results, dividends, acquisitions, credit ratings, resignations,
    SEBI orders...), from NSE's JSON API with a cookie session:
    https://www.nseindia.com/api/corporate-announcements?index=equities&symbol=<SYM>&from_date=..&to_date=..
    If NSE blocks the API, the tool falls back to the public RSS of today's
    announcements (no cookie, matched on the company name):
    https://nsearchives.nseindia.com/content/RSS/Online_announcements.xml
  - Google News RSS, India edition:
    https://news.google.com/rss/search?q="<company>"+when:<n>d&hl=en-IN&gl=IN&ceid=IN:en
  - The move: Yahoo Finance's chart endpoint (unofficial, can lag), the last
    price against yesterday's close: query1.finance.yahoo.com/v8/finance/chart/<SYM>.NS

Times print in India time (IST, UTC+05:30, no daylight saving). State:
trader-in/state/news.json remembers what you have seen (30 days), so an item
shows once. Offline: --offline DIR reads nse-ann-<SYM>.json, nse-rss.xml,
gnews-<SYM>.xml and chart-<SYM>.json from DIR. The tests use it.

Headlines are leads, not facts: open the filing or the story before acting,
and treat nothing here as advice.
"""

import argparse
import datetime as _dt
import email.utils
import gzip
import hashlib
import http.cookiejar
import json
import math
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET

IST = _dt.timezone(_dt.timedelta(hours=5, minutes=30))
NSE_HOME = "https://www.nseindia.com/"
# The home page answers 403 to scripts at times; this page hands out the same cookies.
NSE_WARM = ["https://www.nseindia.com/companies-listing/corporate-filings-insider-trading", NSE_HOME]
NSE_ANN = ("https://www.nseindia.com/api/corporate-announcements?index=equities&symbol=%s"
           "&from_date=%s&to_date=%s")
NSE_RSS = "https://nsearchives.nseindia.com/content/RSS/Online_announcements.xml"
NSE_ANN_PAGE = "https://www.nseindia.com/companies-listing/corporate-filings-announcements"
GNEWS = "https://news.google.com/rss/search?q=%s&hl=en-IN&gl=IN&ceid=IN:en"
CHART = "https://%s.finance.yahoo.com/v8/finance/chart/%s?range=1d&interval=5m"
# NSE wants a browser; Yahoo answers 429 to a fake browser, so it gets the tool's own name.
NSE_UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
          "(KHTML, like Gecko) Chrome/130 Safari/537.36")
TOOL_UA = "Mozilla/5.0 (compatible; india-day-trader-skill/1.0; +https://github.com/)"
KEEP_DAYS = 30
MOVE_PCT = 3.0  # a stock this far from yesterday's close is flagged as moving
BLOCKED_HELP = ("NSE refused the request (it blocks scripts at times). Wait a few minutes and "
                "run again; the public announcements RSS is used meanwhile.")

# NSE announcement subjects (the "desc" field) -> tags. Checked against subject + text.
ANN_TAGS = [
    ("results", r"financial result|integrated filing.{0,3}financ|results? update|limited review"),
    ("board meeting", r"board meeting|outcome of (?:the )?board"),
    ("dividend/bonus/split", r"\bdividend\b|\bbonus\b|sub-?division|stock split|split of|record date|book closure"),
    ("buyback", r"\bbuy-?back\b"),
    ("deal", r"acquisition|amalgamation|merger|demerger|scheme of arrangement|sale or disposal|"
             r"divest|disinvest|joint venture|restructuring|\bstake\b|binding agreement"),
    ("rating", r"credit rating|\bCRISIL\b|\bICRA\b|\bCARE Ratings\b|India Ratings"),
    ("management", r"change in director|appointment|resignation|cessation|\bKMP\b|key managerial|"
                   r"change in management|managing director|\bCEO\b|\bCFO\b|company secretary|auditor"),
    ("regulatory", r"action\(s\) taken or orders passed|\bSEBI\b.{0,40}(?:order|notice|penalt)|"
                   r"show cause|penalt|\border(?:s)? passed\b|\bNCLT\b|\bCCI\b|regulator|GST demand|tax demand"),
    ("capital", r"allotment|preferential|\bQIP\b|rights issue|debenture|\bNCD\b|fund rais|"
                r"initial public offer|\bIPO\b|\bESOP|\bESOS|warrants"),
    ("insider/SAST", r"insider trading|takeover|\bSAST\b|regulation 31|\bpledge|encumbrance"),
    ("clarification", r"news verification|clarification"),
    ("meeting/call", r"analysts?/institutional|con\.? call|earnings call|investor meet|investor presentation"),
    ("risk", r"\bdefault\b|insolvency|\bIBC\b|\bfraud\b|forensic|\braid\b|search and seizure|"
             r"winding up|delay in (?:payment|servicing)|suspension"),
]
# A filing with one of these tags is marked "!": it is the kind that gaps a stock.
IMPORTANT_TAGS = {"results", "dividend/bonus/split", "buyback", "deal", "rating", "regulatory", "risk"}
ROUTINE = re.compile(r"loss of share certificate|duplicate share|copy of newspaper|newspaper publication|"
                     r"trading window|certificate under sebi|declaration of nav|"
                     r"compliance certificate|reg\.? ?74", re.I)

KEYWORDS = [
    ("results", r"\bQ[1-4]\b|\bresults?\b|\bearnings\b|\bnet profit\b|\bprofit (?:rises|falls|jumps|drops|up|down)|"
                r"\brevenue\b|\bPAT\b|\bEBITDA\b|\bguidance\b"),
    ("analyst", r"\bupgrade[sd]?\b|\bdowngrade[sd]?\b|target price|price target|\bbuy rating\b|\bsell rating\b|"
                r"\boverweight\b|\bunderweight\b|\boutperform\b|\bbrokerage[s]?\b|\bJefferies\b|\bCLSA\b|"
                r"\bMorgan Stanley\b|\bGoldman\b|\bBernstein\b|\bMacquarie\b|\bNomura\b"),
    ("deal", r"\bacqui(?:re|res|red|sition)\b|\bmerger\b|\bdemerger\b|\btakeover\b|\bdeal\b|\bstake\b|\bbuyout\b"),
    ("dividend/bonus/split", r"\bdividend\b|\bbonus\b|\bstock split\b|\bsplit\b|\brecord date\b|\bbuy-?back\b"),
    ("rating", r"\bcredit rating\b|\bCRISIL\b|\bICRA\b|\bMoody['’]?s\b|\bFitch\b|\bS&P\b"),
    ("management", r"\bCEO\b|\bCFO\b|\bMD\b|\bchairman\b|\bresign|\bsteps down\b|\bappoint|\bsuccession\b"),
    ("regulatory", r"\bSEBI\b|\bNCLT\b|\bCCI\b|\bpenalty\b|\bshow[- ]cause\b|\bprobe\b|\binvestigation\b|"
                   r"\b[Cc]ourt\b|\b[Tt]ribunal\b|\bED\b|\b[Ii]ncome [Tt]ax\b|\bGST\b|\bban\b"),
    ("block/bulk deal", r"\bblock deal\b|\bbulk deal\b|\bOFS\b|\boffer for sale\b|\bstake sale\b|\bpromoters? (?:sell|buy)"),
    ("capital", r"\bIPO\b|\bQIP\b|\brights issue\b|\bfund ?rais|\bNCD\b|\bpreferential\b"),
    ("risk", r"\bdefault\b|\binsolvency\b|\bfraud\b|\braid\b|\bpledge\b|\bleak(?:ed|s)?\b|\bdowngrade\b"),
    ("circuit", r"\bupper circuit\b|\blower circuit\b|\bcircuit\b|\btrading halt\b|\bsuspen"),
    ("macro", r"\bRBI\b|\brepo rate\b|\bMPC\b|\binflation\b|\bCPI\b|\bGDP\b|\bFIIs?\b|\bFPIs?\b|\bDIIs?\b|"
              r"\brupee\b|\bcrude\b|\bSensex\b|\bNifty\b|\btariffs?\b|\bbudget\b"),
]


class NewsError(Exception):
    pass


# ---------------------------------------------------------------- time

def to_ist(utc):
    """Aware (or naive UTC) datetime -> naive India wall-clock time (UTC+05:30, no DST)."""
    if utc.tzinfo is None:
        utc = utc.replace(tzinfo=_dt.timezone.utc)
    return utc.astimezone(IST).replace(tzinfo=None)


def parse_time(value):
    """RFC 822 (RSS), ISO 8601 or NSE's "21-Oct-2026 22:44:39" (IST) -> aware UTC datetime, or None."""
    value = (value or "").strip()
    if not value:
        return None
    for fmt in ("%d-%b-%Y %H:%M:%S", "%d-%b-%Y %H:%M", "%Y-%m-%d %H:%M:%S"):
        try:
            d = _dt.datetime.strptime(value, fmt).replace(tzinfo=IST)
            return d.astimezone(_dt.timezone.utc)
        except ValueError:
            pass
    d = None
    if re.match(r"^[A-Z][a-z]{2}, ", value) or re.search(r"\b(GMT|UT|[+-]\d{4})$", value):
        try:
            d = email.utils.parsedate_to_datetime(value)
        except (TypeError, ValueError, IndexError):
            d = None
    if d is None:
        try:
            d = _dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    if d.tzinfo is None:
        d = d.replace(tzinfo=_dt.timezone.utc)
    return d.astimezone(_dt.timezone.utc)


# ---------------------------------------------------------------- symbols and inputs

def parse_symbol(text):
    """'RELIANCE' / 'RELIANCE.NS' / 'NSE:RELIANCE' -> ('RELIANCE', 'NSE');
    'RELIANCE.BO' / 'BSE:RELIANCE' / '500325' -> (.., 'BSE')."""
    s = (text or "").strip().upper()
    exch = "NSE"
    if ":" in s:
        pre, s = s.split(":", 1)
        exch = "BSE" if pre == "BSE" else "NSE"
    elif s.endswith(".NS"):
        s = s[:-3]
    elif s.endswith(".BO"):
        s, exch = s[:-3], "BSE"
    if re.match(r"^\d{6}$", s):
        exch = "BSE"
    return s, exch


def yahoo_symbol(symbol, exchange="NSE"):
    return urllib.parse.quote(symbol.upper(), safe="-") + (".BO" if exchange == "BSE" else ".NS")


def load_json(path):
    if path and os.path.isfile(path):
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    return None


def watch_from_files(portfolio="trader-in/portfolio.json", watchlist="trader-in/watchlist.json",
                     plan="trader-in/plan.json"):
    """{symbol: {"name", "exchange"}}: the scanner's hits, the planned trade and your holdings."""
    out = {}

    def add(rec, keep_name=None):
        raw = rec.get("symbol") or rec.get("ticker") or rec.get("tradingsymbol")
        if not raw:
            return
        sym, exch = parse_symbol(raw)
        exch = (rec.get("exchange") or exch).upper()
        prev = out.get(sym, {})
        out[sym] = {"name": rec.get("name") or prev.get("name") or keep_name, "exchange": exch}

    wl = load_json(watchlist) or {}
    for h in wl.get("hits", []):
        add(h)
    pl = load_json(plan) or {}
    if pl.get("symbol"):
        add(pl)
    pf = load_json(portfolio) or {}
    for p in pf.get("positions", []):
        add(p)
    return out


# ---------------------------------------------------------------- fetching

class Fetcher(object):
    """Polite HTTP: one request a second, NSE through a cookie session, retry once."""

    def __init__(self, offline=None, pause=1.0):
        self.offline = offline
        self.pause = pause
        self._last = 0.0
        self._warm = False
        self.jar = http.cookiejar.CookieJar()
        self.opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(self.jar))

    def _wait(self):
        gap = time.time() - self._last
        if gap < self.pause:
            time.sleep(self.pause - gap)
        self._last = time.time()

    def _open(self, url, headers):
        self._wait()
        req = urllib.request.Request(url, headers=headers)
        with self.opener.open(req, timeout=30) as resp:
            data = resp.read()
            if resp.headers.get("Content-Encoding") == "gzip":
                data = gzip.decompress(data)
            return data

    def warm_nse(self):
        """Visit the home page once for NSE's cookies. A refusal here is not fatal:
        the API sometimes answers without them."""
        if self._warm:
            return
        self._warm = True
        for page in NSE_WARM:
            try:
                self._open(page, {"User-Agent": NSE_UA, "Accept": "text/html,*/*",
                                  "Accept-Language": "en-US,en;q=0.9", "Accept-Encoding": "gzip"})
                return
            except (urllib.error.URLError, OSError):
                continue

    def get(self, url, name, kind="plain"):
        """kind: 'nse' (JSON API, cookie session), 'archive' (nsearchives), 'plain' (Yahoo, Google)."""
        if self.offline:
            path = os.path.join(self.offline, name)
            if not os.path.isfile(path):
                raise NewsError("offline: no file %s" % path)
            with open(path, "rb") as fh:
                return fh.read()
        if kind == "nse":
            self.warm_nse()
            headers = {"User-Agent": NSE_UA, "Accept": "application/json, text/plain, */*",
                       "Referer": NSE_HOME, "Accept-Language": "en-US,en;q=0.9",
                       "Accept-Encoding": "gzip"}
        elif kind == "archive":
            headers = {"User-Agent": NSE_UA, "Accept": "*/*", "Accept-Encoding": "gzip"}
        else:
            headers = {"User-Agent": TOOL_UA, "Accept-Encoding": "gzip"}
        last = None
        for attempt in (1, 2):
            try:
                data = self._open(url, headers)
                if kind == "nse" and data[:1] not in (b"[", b"{"):
                    raise NewsError("NSE sent a web page, not data. " + BLOCKED_HELP)
                return data
            except urllib.error.HTTPError as exc:
                last = exc
                if kind == "archive" and attempt == 1:  # browser UA first, then the tool's own
                    headers = dict(headers, **{"User-Agent": TOOL_UA})
                    continue
                if exc.code in (401, 403) and kind == "nse":
                    self._warm = False
                    time.sleep(2)
                    self.warm_nse()
                    continue
                if exc.code == 429 or exc.code >= 500:
                    time.sleep(2)
                    continue
                break
            except (urllib.error.URLError, OSError) as exc:
                last = exc
                time.sleep(1)
        if isinstance(last, urllib.error.HTTPError):
            if kind == "nse" and last.code in (401, 403):
                raise NewsError("HTTP %s from NSE. %s" % (last.code, BLOCKED_HELP))
            raise NewsError("HTTP %s for %s" % (last.code, url))
        raise NewsError("could not reach %s: %s" % (url, getattr(last, "reason", last)))


# ---------------------------------------------------------------- parsing

def _local(tag):
    return tag.rsplit("}", 1)[-1]


def _text(el, name):
    for c in el.iter():
        if _local(c.tag) == name:
            return (c.text or "").strip()
    return ""


def clean(text):
    text = re.sub(r"<[^>]+>", " ", text or "")
    return re.sub(r"\s+", " ", text.replace("''", "'")).strip()


def short_name(name):
    """'Reliance Industries Limited' -> 'Reliance Industries' (for searches and mentions)."""
    n = re.sub(r"\s+", " ", (name or "")).strip()
    n = re.sub(r"(?i)[\s,.]+(?:limited|ltd\.?)$", "", n)
    return n.strip()


def norm_name(name):
    n = re.sub(r"(?i)\b(?:limited|ltd)\b\.?", "", name or "")
    return re.sub(r"[^a-z0-9]+", " ", n.lower()).strip()


def ann_tags(subject, text):
    hay = "%s %s" % (subject, text)
    return [label for label, rx in ANN_TAGS if re.search(rx, hay, re.I)]


def _ann_item(symbol, seq, subject, text, link, when, company, source):
    tags = ann_tags(subject, text)
    title = "%s: %s" % (company or symbol, subject or "announcement")
    if text and text.lower() not in subject.lower():
        snippet = text if len(text) <= 160 else text[:157].rstrip() + "..."
        title += " - " + snippet
    routine = bool(ROUTINE.search(subject + " " + text))
    return {"id": "nse:%s" % seq, "source": source, "kind": "filing", "ticker": symbol,
            "subject": subject, "title": title, "summary": text, "link": link or NSE_ANN_PAGE,
            "time": when, "company": company, "ann_tags": tags, "routine": routine,
            "important": bool(set(tags) & IMPORTANT_TAGS) and not routine}


def parse_nse_announcements(data, symbol):
    """NSE corporate-announcements JSON -> items (newest first, as NSE sends them)."""
    try:
        rows = json.loads(data.decode("utf-8") if isinstance(data, bytes) else data)
    except ValueError:
        raise NewsError("NSE announcements for %s are not JSON. %s" % (symbol, BLOCKED_HELP))
    if isinstance(rows, dict):
        rows = rows.get("data") or []
    out = []
    for r in rows:
        subject = clean(r.get("desc"))
        text = clean(r.get("attchmntText"))
        link = (r.get("attchmntFile") or "").strip()
        if link in ("-", "null"):
            link = ""
        seq = r.get("seq_id") or hashlib.sha1(("%s|%s|%s" % (symbol, r.get("an_dt"), subject))
                                              .encode("utf-8")).hexdigest()[:12]
        out.append(_ann_item((r.get("symbol") or symbol).upper(), seq, subject, text, link,
                             parse_time(r.get("an_dt") or r.get("sort_date")),
                             short_name(r.get("sm_name")) or None, "NSE"))
    return out


def parse_nse_rss(data, names):
    """Public RSS of today's NSE announcements -> items for the companies in names
    ({symbol: company name}); matched on the exact company name, since the feed has no symbol."""
    try:
        root = ET.fromstring(data)
    except ET.ParseError as exc:
        raise NewsError("NSE announcements RSS is not valid XML: %s" % exc)
    by_name = {norm_name(n): s for s, n in names.items() if n}
    out = []
    for it in root.iter("item"):
        company = clean(_text(it, "title"))
        sym = by_name.get(norm_name(company))
        if not sym:
            continue
        desc = clean(_text(it, "description"))
        subject = ""
        m = re.search(r"\|\s*SUBJECT:\s*(.*)$", desc)
        if m:
            subject = m.group(1).strip()
            desc = desc[:m.start()].strip()
        link = _text(it, "link")
        when = parse_time(_text(it, "pubDate"))
        seq = "rss-" + hashlib.sha1(("%s|%s|%s" % (sym, link, when)).encode("utf-8")).hexdigest()[:12]
        out.append(_ann_item(sym, seq, subject, desc, link, when, short_name(company), "NSE RSS"))
    return out


def parse_gnews(data, symbol):
    """Google News RSS -> items. Titles end in " - Publisher"; that goes to "publisher"."""
    try:
        root = ET.fromstring(data)
    except ET.ParseError as exc:
        raise NewsError("news RSS for %s is not valid XML: %s" % (symbol, exc))
    out = []
    for it in root.iter("item"):
        title = clean(_text(it, "title"))
        publisher = _text(it, "source")
        if publisher and title.endswith(" - " + publisher):
            title = title[:-(len(publisher) + 3)].rstrip()
        link = _text(it, "link")
        guid = _text(it, "guid") or link
        out.append({"id": "gn:" + guid, "source": "Google News", "kind": "headline",
                    "ticker": symbol, "title": title, "summary": "", "publisher": publisher,
                    "link": link, "time": parse_time(_text(it, "pubDate"))})
    return out


def parse_chart(data, symbol):
    """Yahoo chart JSON -> {"ticker", "price", "prev_close", "pct", "time", "name"} or raise."""
    try:
        doc = json.loads(data.decode("utf-8") if isinstance(data, bytes) else data)
        meta = doc["chart"]["result"][0]["meta"]
    except (ValueError, KeyError, IndexError, TypeError):
        raise NewsError("chart for %s is not usable" % symbol)
    price = meta.get("regularMarketPrice")
    prev = meta.get("chartPreviousClose") or meta.get("previousClose")
    if not price or not prev:
        raise NewsError("chart for %s has no price" % symbol)
    when = meta.get("regularMarketTime")
    return {"ticker": symbol, "price": round(float(price), 2), "prev_close": round(float(prev), 2),
            "pct": round((float(price) / float(prev) - 1) * 100, 2),
            "exchange": "BSE" if str(meta.get("symbol", "")).endswith(".BO") else "NSE",
            "name": meta.get("longName") or None,
            "time": _dt.datetime.fromtimestamp(when, _dt.timezone.utc) if when else None}


# ---------------------------------------------------------------- the check

def collect_moves(fetcher, watch, errors=None):
    errors = [] if errors is None else errors
    out = []
    for t in sorted(watch):
        exch = watch[t].get("exchange", "NSE")
        if re.match(r"^\d{6}$", t):
            errors.append("price for %s: a BSE code has no Yahoo symbol; use the trading symbol "
                          "(RELIANCE.BO)" % t)
            continue
        name = "chart-%s.json" % t
        sym = yahoo_symbol(t, exch)
        try:
            try:
                data = fetcher.get(CHART % ("query1", sym), name)
            except NewsError as exc:
                if fetcher.offline or not re.search(r"HTTP (429|5\d\d)|could not reach", str(exc)):
                    raise
                data = fetcher.get(CHART % ("query2", sym), name)
            out.append(parse_chart(data, t))
        except NewsError as exc:
            if not (fetcher.offline and "no file" in str(exc)):
                errors.append("price for %s: %s" % (t, exc))
    return out


def collect(fetcher, watch, state, now, hours=48, nse=True, headlines=True, rss=False,
            errors=None):
    """Every item from every source for the watched symbols (window filtering is later)."""
    errors = [] if errors is None else errors
    names = state.setdefault("names", {})
    for t, w in watch.items():
        if w.get("name"):
            names[t] = w["name"]
    items = []
    start = to_ist(now - _dt.timedelta(hours=max(hours, 24))).strftime("%d-%m-%Y")
    end = to_ist(now).strftime("%d-%m-%Y")
    failed = []
    if nse and not rss:
        for t in sorted(watch):
            if re.match(r"^\d{6}$", t):
                errors.append("NSE announcements for %s: a BSE code; use the NSE symbol" % t)
                continue
            url = NSE_ANN % (urllib.parse.quote(t, safe="-"), start, end)
            try:
                got = parse_nse_announcements(fetcher.get(url, "nse-ann-%s.json" % t, "nse"), t)
                for it in got:
                    if it.get("company"):
                        names.setdefault(t, it["company"])
                items += got
            except NewsError as exc:
                failed.append(t)
                if not (fetcher.offline and "no file" in str(exc)):
                    errors.append("NSE announcements for %s: %s" % (t, exc))
    if nse and (rss or failed):
        wanted = {t: names.get(t) for t in (sorted(watch) if rss else failed)}
        missing = sorted(t for t, n in wanted.items() if not n)
        if missing:
            errors.append("no company name known for %s, so the RSS cannot be matched: add "
                          "\"name\" in the watchlist" % ", ".join(missing))
        try:
            got = parse_nse_rss(fetcher.get(NSE_RSS, "nse-rss.xml", "archive"), wanted)
            items += got
            errors.append("NSE announcements for %s read from the public RSS (it carries today's "
                          "announcements only)" % ", ".join(sorted(wanted)))
        except NewsError as exc:
            errors.append("NSE announcements RSS: %s" % exc)
    if headlines:
        days = max(1, min(30, int(math.ceil(hours / 24.0))))
        for t in sorted(watch):
            company = short_name(names.get(t)) if names.get(t) else None
            query = '"%s"' % company if company else '"%s" NSE share' % t
            url = GNEWS % urllib.parse.quote_plus("%s when:%dd" % (query, days))
            try:
                items += parse_gnews(fetcher.get(url, "gnews-%s.xml" % t), t)
            except NewsError as exc:
                if not (fetcher.offline and "no file" in str(exc)):
                    errors.append("headlines for %s: %s" % (t, exc))
    return items


def mention_patterns(watch, names):
    pats = {}
    for t in watch:
        alts = [r"\b%s\b" % re.escape(t), r"\b(?:NSE|BSE)\s*:\s*%s\b" % re.escape(t)]
        n = short_name(names.get(t) or watch[t].get("name") or "")
        if n and len(n) >= 4:
            alts.append(r"\b%s\b" % re.escape(n).replace(r"\ ", r"[\s-]"))
            first = n.split()[0]
            if len(first) >= 5 and first.lower() not in ("india", "indian", "state", "bank", "tata"):
                alts.append(r"\b%s\b" % re.escape(first))
        pats[t] = re.compile("|".join(alts), re.I if not t.isalpha() or len(t) > 4 else 0)
    return pats


def tag(items, watch, names=None):
    """Which of your symbols each item touches, and why it matters."""
    names = names or {}
    pats = mention_patterns(watch, names)
    for it in items:
        hay = "%s %s" % (it["title"], it.get("summary") or "")
        touches = {it["ticker"]} if it["ticker"] in watch else set()
        if it["kind"] == "headline":
            for t, rx in pats.items():
                if rx.search(hay):
                    touches.add(t)
        it["touches"] = sorted(touches)
        if it["kind"] == "filing":
            it["tags"] = list(it.get("ann_tags") or [])
        else:
            it["tags"] = [label for label, rx in KEYWORDS if re.search(rx, hay)]
    return items


def dedupe(items):
    """One item per story: same id, or same link, or same title the same day."""
    seen, out = {}, []
    for it in items:
        keys = [it["id"], it["link"] if it["link"] != NSE_ANN_PAGE else "",
                "%s|%s|%s" % (it["kind"], re.sub(r"\W+", " ", it["title"].lower()).strip(),
                              it["time"].date() if it["time"] else "")]
        hit = next((seen[k] for k in keys if k and k in seen), None)
        if hit is not None:
            for t in it.get("touches", []):
                if t not in hit["touches"]:
                    hit["touches"].append(t)
            hit["touches"].sort()
            continue
        for k in keys:
            if k:
                seen[k] = it
        out.append(it)
    return out


def fingerprint(it):
    return hashlib.sha1(it["id"].encode("utf-8")).hexdigest()[:16]


def select(items, state, now, hours=48, show_all=False, routine=False):
    """Items within the window; only unseen ones unless show_all; routine filings
    (lost share certificates, newspaper copies, trading window) only with routine=True."""
    seen = state.setdefault("seen", {})
    cutoff = now - _dt.timedelta(hours=hours)
    ahead = now + _dt.timedelta(minutes=10)  # a pinned --now: nothing from after it
    out = []
    for it in items:
        if it["time"] is not None and (it["time"] < cutoff or it["time"] > ahead):
            continue
        if it.get("routine") and not routine:
            continue
        fp = fingerprint(it)
        it["new"] = fp not in seen
        if it["new"] or show_all:
            out.append(it)
    out.sort(key=lambda i: (i["time"] or now), reverse=True)
    return out


def remember(items, state, now):
    seen = state.setdefault("seen", {})
    stamp = now.isoformat()
    for it in items:
        seen.setdefault(fingerprint(it), stamp)
    cutoff = (now - _dt.timedelta(days=KEEP_DAYS)).isoformat()
    state["seen"] = {k: v for k, v in seen.items() if v >= cutoff}
    state["last_check"] = stamp


def inr(v):
    """1234567.5 -> '12,34,567.50' (Indian digit grouping)."""
    neg = v < 0
    whole, frac = ("%.2f" % abs(v)).split(".")
    if len(whole) > 3:
        head, tail = whole[:-3], whole[-3:]
        head = ",".join(re.findall(r"\d{1,2}(?=(?:\d{2})*$)", head))
        whole = head + "," + tail
    return ("-" if neg else "") + whole + "." + frac


def render_moves(moves, move_pct=MOVE_PCT):
    out = []
    for m in moves:
        flag = "MOVING" if abs(m["pct"]) >= move_pct else ""
        when = (" at %s IST" % to_ist(m["time"]).strftime("%H:%M")) if m.get("time") else ""
        out.append("%-10s ₹%s %+.2f%% vs yesterday's close ₹%s%s  %s" % (
            m["ticker"] + (" (BSE)" if m.get("exchange") == "BSE" else ""), inr(m["price"]),
            m["pct"], inr(m["prev_close"]), when, flag))
    return out


def render(items, errors, now, moves=None, move_pct=MOVE_PCT):
    out = render_moves(moves or [], move_pct)
    if out:
        out.append("")
    if not items:
        out.append("Nothing new.")
    for it in items:
        when = to_ist(it["time"]).strftime("%Y-%m-%d %H:%M IST") if it["time"] else "time unknown"
        mark = "!" if it.get("important") else " "
        who = ",".join(it["touches"]) or it["ticker"]
        src = it["source"] if it["kind"] == "filing" else (it.get("publisher") or it["source"])
        out.append("%s %s  %-10s %-14s %s" % (mark, when, who, src[:14], it["title"]))
        extra = []
        if it["tags"]:
            extra.append("tags: " + ", ".join(it["tags"]))
        extra.append(it["link"])
        out.append("      " + "  ".join(extra))
    for e in errors:
        out.append("note: %s" % e)
    out.append("Checked %s. Headlines are leads, not facts; open the filing or the story before "
               "acting. Not financial advice." % to_ist(now).strftime("%Y-%m-%d %H:%M IST"))
    return "\n".join(out) + "\n"


def _json_ready(items):
    out = []
    for it in items:
        d = {k: v for k, v in it.items() if k != "ann_tags"}
        d["time"] = it["time"].isoformat() if it["time"] else None
        d["time_ist"] = to_ist(it["time"]).strftime("%Y-%m-%d %H:%M") if it["time"] else None
        out.append(d)
    return out


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--symbols", "--tickers", nargs="*", dest="symbols",
                    help="NSE symbols; default: watchlist + plan + portfolio")
    ap.add_argument("--portfolio", default="trader-in/portfolio.json")
    ap.add_argument("--watchlist", default="trader-in/watchlist.json")
    ap.add_argument("--plan", default="trader-in/plan.json")
    ap.add_argument("--state", default="trader-in/state/news.json")
    ap.add_argument("--hours", type=float, default=48, help="ignore items older than this")
    ap.add_argument("--all", action="store_true", help="show seen items too")
    ap.add_argument("--routine", action="store_true",
                    help="also show routine filings (lost share certificates, newspaper copies)")
    ap.add_argument("--no-nse", action="store_true", help="headlines only")
    ap.add_argument("--rss", action="store_true",
                    help="read NSE announcements from the public RSS only (no cookie, today only)")
    ap.add_argument("--no-headlines", action="store_true", help="NSE announcements only")
    ap.add_argument("--no-moves", action="store_true", help="skip the price line per symbol")
    ap.add_argument("--move-pct", type=float, default=MOVE_PCT, help="flag a move this big (percent)")
    ap.add_argument("--offline", metavar="DIR")
    ap.add_argument("--now", help="ISO time, for reproducible runs")
    ap.add_argument("--dry-run", action="store_true", help="do not update the state file")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)

    known = watch_from_files(args.portfolio, args.watchlist, args.plan)
    if args.symbols:
        watch = {}
        for raw in args.symbols:
            sym, exch = parse_symbol(raw)
            watch[sym] = {"name": (known.get(sym) or {}).get("name"), "exchange": exch}
    else:
        watch = known
    if not watch:
        print("news: no symbols. Pass --symbols, or run /india-scanner first (it writes "
              "trader-in/watchlist.json).", file=sys.stderr)
        return 2
    now = parse_time(args.now) if args.now else _dt.datetime.now(_dt.timezone.utc)
    state = load_json(args.state) or {}
    errors = []
    fetcher = Fetcher(offline=args.offline)
    moves = [] if args.no_moves else collect_moves(fetcher, watch, errors)
    for m in moves:  # Yahoo knows the company name: it helps the RSS match and the news search
        if m.get("name") and not watch[m["ticker"]].get("name"):
            state.setdefault("names", {}).setdefault(m["ticker"], m["name"])
    items = collect(fetcher, watch, state, now, hours=args.hours, nse=not args.no_nse,
                    headlines=not args.no_headlines, rss=args.rss, errors=errors)
    items = dedupe(tag(items, watch, state.get("names")))
    shown = select(items, state, now, hours=args.hours, show_all=args.all, routine=args.routine)
    if args.json:
        mv = [dict(m, time=m["time"].isoformat() if m.get("time") else None,
                   moving=abs(m["pct"]) >= args.move_pct) for m in moves]
        print(json.dumps({"checked": now.isoformat(), "timezone": "IST (UTC+05:30)", "moves": mv,
                          "items": _json_ready(shown), "notes": errors}, indent=2,
                         ensure_ascii=False))
    else:
        sys.stdout.write(render(shown, errors, now, moves, args.move_pct))
    if not args.dry_run:
        remember(items, state, now)
        folder = os.path.dirname(args.state)
        if folder:
            os.makedirs(folder, exist_ok=True)
        with open(args.state, "w", encoding="utf-8") as fh:
            json.dump(state, fh, indent=2, ensure_ascii=False)
            fh.write("\n")
    return 0 if any(i["new"] for i in shown) else 1


if __name__ == "__main__":
    sys.exit(main())
