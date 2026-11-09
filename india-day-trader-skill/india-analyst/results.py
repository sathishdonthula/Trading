#!/usr/bin/env python3
"""Results reader for india-analyst. Standard library only, Python 3.9+.

Reads the quarterly financial results a company filed with NSE (the XBRL
file behind each filing) and lays out what changed, quarter by quarter:
revenue, EBITDA, operating and net margins, net profit, diluted EPS, growth
against the same quarter a year earlier and the last four quarters. Banks get
their own lines (total income, net interest income, operating profit before
provisions). Money prints in rupee crore (1 crore = 1,00,00,000).

    python3 results.py RELIANCE
    python3 results.py RELIANCE TCS --quarters 8
    python3 results.py HDFCBANK --json
    python3 results.py RELIANCE --standalone          # the parent company alone
    python3 results.py RELIANCE INFY HDFCBANK --metrics trader-in/metrics.json
    python3 results.py RELIANCE --transcripts         # links to the concall transcripts

Sources (NSE; the JSON API needs NSE's cookies, the tool visits the home page
first like a browser; the XBRL files on nsearchives need no cookie):
  https://www.nseindia.com/api/integrated-filing-results?index=equities&symbol=<SYM>
      &period_ended=all&type=Integrated%20Filing-%20Financials   (filings from 2025 on)
  https://www.nseindia.com/api/corporates-financial-results?index=equities&symbol=<SYM>
      &period=Quarterly                                          (filings up to Dec 2024)
  https://www.nseindia.com/api/event-calendar?index=equities&symbol=<SYM>  (next board meeting)
  https://nsearchives.nseindia.com/corporate/xbrl/...xml        (one file per quarter)

How the numbers are built:
  - Each filing's XBRL holds the quarter itself (a context of about three
    months with no dimensions). Values are in rupees; the tool shows crore.
  - Consolidated by default (the group, what the market trades on); a company
    without subsidiaries files standalone only, and the tool says so.
    --standalone reads the parent company alone.
  - Revenue = RevenueFromOperations. EBITDA = profit before exceptional items
    and tax + finance costs + depreciation - other income; operating profit
    (EBIT) is the same without adding back depreciation. Both leave other
    income out, so a treasury gain does not pass as operations. Net profit is
    the profit attributable to owners of the parent (consolidated) or the
    profit for the period (standalone). EPS is diluted EPS as filed.
  - Banks file a different format (Banking Regulation Act schedule): total
    income, interest earned and expended, operating profit before provisions.
    For them "revenue" is total income, NII = interest earned - interest
    expended, and there is no EBITDA.
  - A bonus issue or a split changes the share count, so EPS before it is
    not comparable. When the share count (paid-up capital / face value) moved
    by 1.45x or more, the older EPS is restated to today's share count and
    marked "adj".
  - Results are due within 45 days of a quarter's end (60 days for the March
    quarter, which carries the audited year): SEBI LODR Regulation 33.

Parsed filings are cached in trader-in/state/results-cache.json (a filing
does not change; a revised one has a new file). --metrics FILE merges one row
per symbol into a JSON file. Offline: --offline DIR reads integrated-<SYM>.json,
results-<SYM>.json, events-<SYM>.json, ann-<SYM>.json and the XBRL files by
their file names from DIR.

Numbers only: this tool never says whether a stock is a buy.
"""

import argparse
import datetime as _dt
import gzip
import http.cookiejar
import json
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
INTEGRATED_URL = ("https://www.nseindia.com/api/integrated-filing-results?index=equities&symbol=%s"
                  "&period_ended=all&type=Integrated%%20Filing-%%20Financials")
LEGACY_URL = ("https://www.nseindia.com/api/corporates-financial-results?index=equities&symbol=%s"
              "&period=Quarterly")
EVENTS_URL = "https://www.nseindia.com/api/event-calendar?index=equities&symbol=%s"
ANN_URL = ("https://www.nseindia.com/api/corporate-announcements?index=equities&symbol=%s"
           "&from_date=%s&to_date=%s")
TOOL_UA = "Mozilla/5.0 (compatible; india-day-trader-skill/1.0; +https://github.com/)"
NSE_UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
          "(KHTML, like Gecko) Chrome/130 Safari/537.36")
BLOCKED_HELP = ("NSE refused the request (it blocks scripts at times). Wait a few minutes and run "
                "again, or open the company's page on nseindia.com in a browser.")
CRORE = 1e7
XI = "{http://www.xbrl.org/2003/instance}"
SPLIT_RATIO = 1.45  # a share count this many times larger (or smaller) = a bonus or a split

CONCEPTS = {
    "revenue": ["RevenueFromOperations"],
    "other_income": ["OtherIncome"],
    "total_income": ["Income"],
    "finance_costs": ["FinanceCosts"],
    "depreciation": ["DepreciationDepletionAndAmortisationExpense"],
    "pbt_before_exceptional": ["ProfitBeforeExceptionalItemsAndTax"],
    "exceptional": ["ExceptionalItemsBeforeTax", "ExceptionalItems"],
    "pbt": ["ProfitBeforeTax", "ProfitLossFromOrdinaryActivitiesBeforeTax"],
    "profit_owners": ["ProfitOrLossAttributableToOwnersOfParent",
                      "ProfitLossAfterTaxesMinorityInterestAndShareOfProfitLossOfAssociates"],
    "profit": ["ProfitLossForPeriod", "ProfitLossForThePeriod"],
    "eps_diluted": ["DilutedEarningsLossPerShareFromContinuingAndDiscontinuedOperations",
                    "DilutedEarningsLossPerShareFromContinuingOperations",
                    "DilutedEarningsPerShareAfterExtraordinaryItems",
                    "DilutedEarningsPerShareBeforeExtraordinaryItems",
                    "BasicEarningsLossPerShareFromContinuingAndDiscontinuedOperations",
                    "BasicEarningsPerShareAfterExtraordinaryItems"],
    "interest_earned": ["InterestEarned"],
    "interest_expended": ["InterestExpended"],
    "ppop": ["OperatingProfitBeforeProvisionAndContingencies"],
    "provisions": ["ProvisionsOtherThanTaxAndContingencies"],
    "paid_up": ["PaidUpValueOfEquityShareCapital"],
    "face_value": ["FaceValueOfEquityShareCapital"],
}


class ResultsError(Exception):
    pass


# ---------------------------------------------------------------- fetching

class Fetcher(object):
    """Polite HTTP: one request a second, NSE through a cookie session, retry once."""

    def __init__(self, offline=None, pause=1.0):
        self.offline, self.pause, self._last, self._warm = offline, pause, 0.0, False
        self.jar = http.cookiejar.CookieJar()
        self.opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(self.jar))

    def _open(self, url, headers):
        gap = time.time() - self._last
        if gap < self.pause:
            time.sleep(self.pause - gap)
        self._last = time.time()
        with self.opener.open(urllib.request.Request(url, headers=headers), timeout=60) as resp:
            data = resp.read()
            if resp.headers.get("Content-Encoding") == "gzip":
                data = gzip.decompress(data)
            return data

    def warm_nse(self):
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

    def get(self, url, name, nse=False):
        if self.offline:
            path = os.path.join(self.offline, name)
            if not os.path.isfile(path):
                raise ResultsError("offline: no file %s" % path)
            with open(path, "rb") as fh:
                return fh.read()
        if nse:
            self.warm_nse()
            headers = {"User-Agent": NSE_UA, "Accept": "application/json, text/plain, */*",
                       "Referer": NSE_HOME, "Accept-Language": "en-US,en;q=0.9",
                       "Accept-Encoding": "gzip"}
        else:
            headers = {"User-Agent": NSE_UA, "Accept": "*/*", "Accept-Encoding": "gzip"}
        last = None
        for attempt in (1, 2):
            try:
                data = self._open(url, headers)
                if nse and data[:1] not in (b"[", b"{"):
                    raise ResultsError("NSE sent a web page, not data. " + BLOCKED_HELP)
                return data
            except urllib.error.HTTPError as exc:
                last = exc
                if not nse and attempt == 1 and exc.code != 404:  # archives: browser UA, then ours
                    headers = dict(headers, **{"User-Agent": TOOL_UA})
                    continue
                if exc.code == 404:
                    break
                if nse and exc.code in (401, 403):
                    self._warm = False
                    time.sleep(2)
                    self.warm_nse()
                    continue
                time.sleep(2)
            except (urllib.error.URLError, OSError) as exc:
                last = exc
                time.sleep(1)
        if isinstance(last, urllib.error.HTTPError):
            if nse and last.code in (401, 403):
                raise ResultsError("HTTP %s from NSE. %s" % (last.code, BLOCKED_HELP))
            raise ResultsError("HTTP %s for %s" % (last.code, url))
        raise ResultsError("could not reach %s: %s" % (url, getattr(last, "reason", last)))

    def json(self, url, name):
        data = self.get(url, name, nse=True)
        try:
            return json.loads(data.decode("utf-8"))
        except ValueError:
            raise ResultsError("NSE sent something that is not JSON. " + BLOCKED_HELP)


def parse_symbol(text):
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


# ---------------------------------------------------------------- the filings list

def _d(text, fmts=("%d-%b-%Y", "%d-%b-%Y %H:%M:%S", "%d-%b-%Y %H:%M")):
    text = (text or "").strip().title()
    for f in fmts:
        try:
            return _dt.datetime.strptime(text, f)
        except ValueError:
            pass
    return None


def filings(integrated, legacy):
    """Both NSE lists -> [{"end", "basis", "xbrl", "ixbrl", "filed", "audited", "bank", "list"}],
    one per (quarter end, basis), the latest filing winning (a revision replaces the original)."""
    rows = []
    for x in (integrated or {}).get("data", []) if isinstance(integrated, dict) else (integrated or []):
        if "Financ" not in (x.get("type") or "Financials"):
            continue
        end, filed = _d(x.get("qe_Date")), _d(x.get("broadcast_Date"))
        if not end or not x.get("xbrl"):
            continue
        basis = "standalone" if (x.get("consolidated") or "").lower().startswith(("stand", "non")) \
            else "consolidated"
        rows.append({"end": end.date().isoformat(), "basis": basis, "xbrl": x["xbrl"],
                     "ixbrl": x.get("ixbrl"), "filed": filed.isoformat(" ") if filed else None,
                     "audited": x.get("audited"), "bank": "BANKING" in x["xbrl"].upper(),
                     "list": "integrated filing", "revised": x.get("type_Sub") == "Revised",
                     "company": x.get("cmName") or x.get("smName")})
    for x in legacy or []:
        if (x.get("cumulative") or "").lower().startswith("cumul") or not x.get("xbrl"):
            continue
        if (x.get("period") or "Quarterly") != "Quarterly":
            continue
        end, filed = _d(x.get("toDate")), _d(x.get("broadCastDate") or x.get("filingDate"))
        if not end:
            continue
        basis = "standalone" if (x.get("consolidated") or "").lower().startswith(("non", "stand")) \
            else "consolidated"
        rows.append({"end": end.date().isoformat(), "basis": basis, "xbrl": x["xbrl"],
                     "ixbrl": None, "filed": filed.isoformat(" ") if filed else None,
                     "audited": x.get("audited"), "bank": x.get("bank") == "B",
                     "list": "financial results", "revised": False,
                     "company": x.get("companyName")})
    best = {}
    for r in rows:
        k = (r["end"], r["basis"])
        if k not in best or (r["filed"] or "") > (best[k]["filed"] or ""):
            best[k] = r
    return sorted(best.values(), key=lambda r: r["end"], reverse=True)


def pick_basis(rows, want="consolidated"):
    """The rows for one basis, and a note when the wanted one does not exist."""
    mine = [r for r in rows if r["basis"] == want]
    if mine:
        return mine, want, None
    other = "standalone" if want == "consolidated" else "consolidated"
    alt = [r for r in rows if r["basis"] == other]
    if alt:
        return alt, other, ("no %s results filed (%s): showing %s." % (
            want, "the company has no subsidiaries" if want == "consolidated" else "a holding company",
            other))
    return [], want, None


# ---------------------------------------------------------------- one XBRL file

def _num(text):
    try:
        return float(text)
    except (TypeError, ValueError):
        return None


def parse_xbrl(data):
    """An NSE results XBRL -> {"start", "end", "values": {key: number}, "name", "nature",
    "format": "ind-as" | "bank", "concepts": {key: tag}} for the quarter in it."""
    try:
        root = ET.fromstring(data)
    except ET.ParseError as exc:
        raise ResultsError("the XBRL file is not valid XML: %s" % exc)
    ctx = {}
    for c in root.iter(XI + "context"):
        if c.find(XI + "scenario") is not None:
            continue
        p = c.find(XI + "period")
        s, e = p.find(XI + "startDate"), p.find(XI + "endDate")
        if s is None or e is None:
            continue
        start, end = _dt.date.fromisoformat(s.text.strip()), _dt.date.fromisoformat(e.text.strip())
        ctx[c.get("id")] = (start, end)
    quarter = [cid for cid, (s, e) in ctx.items() if 80 <= (e - s).days <= 100]
    if not quarter:
        raise ResultsError("no three-month period in the XBRL file")
    quarter.sort(key=lambda cid: (cid != "OneD", cid))
    qid = quarter[0]
    facts, text = {}, {}
    for el in root:
        if el.get("contextRef") != qid:
            continue
        tag = el.tag.rsplit("}", 1)[-1]
        if el.get("unitRef"):
            v = _num(el.text)
            if v is not None:
                facts.setdefault(tag, v)
        else:
            text.setdefault(tag, (el.text or "").strip())
    values, used = {}, {}
    for key, tags in CONCEPTS.items():
        for t in tags:
            if t in facts:
                values[key], used[key] = facts[t], t
                break
    bank = "interest_earned" in values and "revenue" not in values
    if bank:
        values["revenue"] = values.get("total_income")
        used["revenue"] = "Income"
    start, end = ctx[qid]
    return {"start": start.isoformat(), "end": end.isoformat(), "values": values, "concepts": used,
            "name": text.get("NameOfTheCompany"), "nature": text.get("NatureOfReportStandaloneConsolidated"),
            "audited": text.get("WhetherResultsAreAuditedOrUnaudited"),
            "format": "bank" if bank else "ind-as"}


# ---------------------------------------------------------------- the analysis

def fiscal_label(end):
    """Indian financial year April to March: quarter ending 2026-06-30 -> 'Q1 FY27'."""
    d = _dt.date.fromisoformat(end)
    q = {6: 1, 9: 2, 12: 3, 3: 4}.get(d.month)
    fy = d.year + (1 if d.month > 3 else 0)
    return ("Q%d FY%02d" % (q, fy % 100)) if q else d.strftime("%b %Y")


def pct(a, b):
    if a is None or b in (None, 0):
        return None
    return round(100.0 * (a - b) / abs(b), 1)


def margin(part, whole):
    if part is None or not whole:
        return None
    return round(100.0 * part / whole, 1)


def derive(q):
    """Derived lines for one parsed quarter (rupees)."""
    v = q["values"]
    out = {"revenue": v.get("revenue"), "other_income": v.get("other_income"),
           "exceptional": v.get("exceptional") or 0.0, "pbt": v.get("pbt"),
           "net_profit": v.get("profit_owners", v.get("profit")),
           "eps_diluted": v.get("eps_diluted"), "ebitda": None, "operating_profit": None,
           "nii": None, "provisions": v.get("provisions"), "shares": None}
    if v.get("paid_up") and v.get("face_value"):
        out["shares"] = v["paid_up"] / v["face_value"]
    if q["format"] == "bank":
        if v.get("interest_earned") is not None and v.get("interest_expended") is not None:
            out["nii"] = v["interest_earned"] - v["interest_expended"]
        out["operating_profit"] = v.get("ppop")
    else:
        base = v.get("pbt_before_exceptional")
        if base is None and v.get("pbt") is not None:
            base = v["pbt"] - (v.get("exceptional") or 0.0)
        if base is not None and v.get("finance_costs") is not None:
            ebit = base + v["finance_costs"] - (v.get("other_income") or 0.0)
            out["operating_profit"] = ebit
            if v.get("depreciation") is not None:
                out["ebitda"] = ebit + v["depreciation"]
    return out


def _year_before(end):
    d = _dt.date.fromisoformat(end)
    try:
        prior = d.replace(year=d.year - 1)
    except ValueError:  # 29 Feb
        prior = d.replace(year=d.year - 1, day=28)
    return prior.isoformat()


def eps_factor(cur, prior):
    """Share-count ratio when a bonus or split sits between two quarters, else 1."""
    if not cur.get("shares") or not prior.get("shares"):
        return 1.0
    r = cur["shares"] / prior["shares"]
    return r if (r >= SPLIT_RATIO or r <= 1 / SPLIT_RATIO) else 1.0


def analyse(quarters, n=6, name=None, basis="consolidated"):
    """quarters: {end: parsed xbrl (with "filing")}. Every number in the report, as a dict."""
    if not quarters:
        raise ResultsError("no quarterly results could be read")
    d = {end: dict(derive(q), filing=q.get("filing") or {}, format=q["format"])
         for end, q in quarters.items()}
    ends = sorted(d, reverse=True)
    latest = d[ends[0]]
    fmt = quarters[ends[0]]["format"]
    rows = []
    for i, end in enumerate(ends[:n]):
        c = d[end]
        p = d.get(_year_before(end))
        f = eps_factor(c, p) if p else 1.0
        prior_eps = (p["eps_diluted"] / f) if (p and p.get("eps_diluted") is not None) else None
        lf = eps_factor(latest, c)
        r = c["revenue"]
        rows.append({
            "end": end, "fiscal": fiscal_label(end), "basis": basis, "format": c["format"],
            "revenue": r, "net_profit": c["net_profit"], "ebitda": c["ebitda"],
            "operating_profit": c["operating_profit"], "nii": c["nii"],
            "other_income": c["other_income"], "exceptional": c["exceptional"], "pbt": c["pbt"],
            "provisions": c["provisions"],
            "eps_diluted": round(c["eps_diluted"], 2) if c["eps_diluted"] is not None else None,
            "eps_today_shares": round(c["eps_diluted"] / lf, 2) if c["eps_diluted"] is not None else None,
            "ebitda_margin": margin(c["ebitda"], r), "operating_margin": margin(c["operating_profit"], r),
            "net_margin": margin(c["net_profit"], r),
            "revenue_yoy": pct(r, p["revenue"]) if p else None,
            "net_profit_yoy": pct(c["net_profit"], p["net_profit"]) if p else None,
            "nii_yoy": pct(c["nii"], p["nii"]) if p else None,
            "eps_yoy": pct(c["eps_diluted"], prior_eps) if p else None,
            "eps_adjusted": f != 1.0,
            "prior": {"end": _year_before(end), "revenue": p["revenue"], "net_profit": p["net_profit"],
                      "other_income": p["other_income"], "pbt": p["pbt"],
                      "exceptional": p["exceptional"]} if p else None,
            "filed": c["filing"].get("filed"), "audited": c["filing"].get("audited"),
            "xbrl": c["filing"].get("xbrl"), "ixbrl": c["filing"].get("ixbrl"),
        })
    ttm = {}
    last4 = ends[:4]
    if len(last4) == 4 and (_dt.date.fromisoformat(last4[0]) - _dt.date.fromisoformat(last4[-1])).days < 300:
        for key in ("revenue", "net_profit", "nii"):
            vals = [d[e][key] for e in last4]
            if all(v is not None for v in vals):
                ttm[key] = sum(vals)
        eps = [d[e]["eps_diluted"] for e in last4]
        if all(v is not None for v in eps):
            ttm["eps_diluted"] = round(sum(v / eps_factor(latest, d[e]) for v, e in zip(eps, last4)), 2)
    return {"entity": name, "basis": basis, "format": fmt, "rows": rows, "ttm": ttm,
            "concepts": quarters[ends[0]]["concepts"]}


def cr(v, signed=False):
    """Rupees -> '3,11,850' crore, Indian grouping."""
    if v is None:
        return "-"
    x = v / CRORE
    neg = x < 0
    s = "%.0f" % abs(x)
    if len(s) > 3:
        head, tail = s[:-3], s[-3:]
        s = ",".join(re.findall(r"\d{1,2}(?=(?:\d{2})*$)", head)) + "," + tail
    return ("-" if neg else ("+" if signed and x > 0 else "")) + s


def money(v):
    if v is None:
        return "n/a"
    return ("-₹%s cr" % cr(-v)) if v < 0 else "₹%s cr" % cr(v)


def changes(result):
    """Plain sentences: what changed in the latest quarter."""
    rows = result["rows"]
    if not rows:
        return []
    cur = rows[0]
    prev = rows[1] if len(rows) > 1 else None
    bank = cur["format"] == "bank"
    out = []
    label = "Quarter ending %s (%s, %s%s)" % (
        cur["end"], cur["fiscal"], cur["basis"],
        ", %s" % cur["audited"].lower().replace("-", "") if cur.get("audited") else "")
    line = "%s: %s %s" % (label, "total income" if bank else "revenue", money(cur["revenue"]))
    if cur["revenue_yoy"] is not None:
        line += " (%+.1f%% vs a year earlier)" % cur["revenue_yoy"]
    line += ", net profit %s" % money(cur["net_profit"])
    if cur["net_profit_yoy"] is not None:
        line += " (%+.1f%%)" % cur["net_profit_yoy"]
    if cur["eps_diluted"] is not None:
        line += ", diluted EPS ₹%.2f" % cur["eps_diluted"]
        if cur["eps_yoy"] is not None:
            line += " (%+.1f%%%s)" % (cur["eps_yoy"], ", a year earlier restated for a bonus/split"
                                     if cur["eps_adjusted"] else "")
    out.append(line + ".")
    if bank:
        s = "Net interest income %s" % money(cur["nii"])
        if cur["nii_yoy"] is not None:
            s += " (%+.1f%% vs a year earlier)" % cur["nii_yoy"]
        s += ", operating profit before provisions %s" % money(cur["operating_profit"])
        if cur["provisions"] is not None:
            s += ", provisions %s" % money(cur["provisions"])
        out.append(s + ". A bank has no EBITDA; its revenue line is total income.")
    keys = (("operating_margin", "Operating profit before provisions / total income"),
            ("net_margin", "Net margin")) if bank else \
        (("ebitda_margin", "EBITDA margin"), ("operating_margin", "Operating margin (EBIT)"),
         ("net_margin", "Net margin"))
    for key, name in keys:
        if cur[key] is None:
            continue
        s = "%s %.1f%%" % (name, cur[key])
        if prev and prev[key] is not None:
            s += " (%+.1f points vs the quarter before)" % (cur[key] - prev[key])
        out.append(s + ".")
    if cur["exceptional"]:
        out.append("Exceptional items of %s are inside this quarter's profit: read the notes to "
                   "the results before reading it as growth." % money(cur["exceptional"]))
    if not bank and cur["other_income"] and cur["pbt"] and cur["pbt"] > 0 \
            and cur["other_income"] / cur["pbt"] >= 0.25:
        out.append("Other income (%s) is %.0f%% of profit before tax: part of the profit is "
                   "not from operations." % (money(cur["other_income"]),
                                             100 * cur["other_income"] / cur["pbt"]))
    p = cur.get("prior")
    if not bank and p and p.get("other_income") and p.get("pbt") and p["pbt"] > 0 \
            and p["other_income"] / p["pbt"] >= 0.25:
        out.append("A year earlier other income was %s, %.0f%% of profit before tax: the profit "
                   "comparison is against a base swollen by non-operating income." % (
                       money(p["other_income"]), 100 * p["other_income"] / p["pbt"]))
    if p and p.get("exceptional"):
        out.append("A year earlier profit held exceptional items of %s." % money(p["exceptional"]))
    if prev and cur["revenue"] is not None and prev["revenue"]:
        out.append("%s %+.1f%% vs the quarter before (many businesses are seasonal: compare with "
                   "a year earlier first)." % ("Total income" if bank else "Revenue",
                                              pct(cur["revenue"], prev["revenue"])))
    if any(r["eps_adjusted"] for r in rows):
        out.append("A bonus issue or split changed the share count inside this window: EPS "
                   "growth uses the earlier EPS restated to the new share count.")
    return out


# ---------------------------------------------------------------- the calendar

def add_months(d, n):
    """Last day of the month n months after d's month."""
    m = d.month - 1 + n + 1
    y, m = d.year + m // 12, m % 12 + 1
    return _dt.date(y, m, 1) - _dt.timedelta(days=1)


def next_results(result, events, today):
    """Plain note on the next results: the board meeting date from NSE's calendar, or the
    SEBI deadline when nothing is announced."""
    if not result["rows"]:
        return None
    last = _dt.date.fromisoformat(result["rows"][0]["end"])
    nxt = add_months(last, 3)
    meeting = None
    for e in events or []:
        d = _d(e.get("date"))
        if d and d.date() >= today and re.search(r"result", e.get("purpose") or "", re.I):
            if meeting is None or d.date() < meeting[0]:
                meeting = (d.date(), e.get("purpose"))
    due = nxt + _dt.timedelta(days=60 if nxt.month == 3 else 45)
    if meeting:
        return ("Next results: board meeting on %s (%s, NSE event calendar), for the quarter "
                "ending %s." % (meeting[0].isoformat(), meeting[1], nxt.isoformat()))
    if today <= nxt:
        return ("Next results: the quarter ending %s; due by %s (SEBI: 45 days after the quarter, "
                "60 for March)." % (nxt.isoformat(), due.isoformat()))
    past = [(_d(e.get("date")), e) for e in events or []]
    held = [d.date() for d, e in past if d and nxt < d.date() <= today
            and re.search(r"result", e.get("purpose") or "", re.I)]
    if held:
        return ("The board met on %s to approve results for the quarter ending %s, but NSE's "
                "results list does not carry them yet: read the 'Outcome of Board Meeting' "
                "announcement (/india-news) for the numbers." % (held[-1].isoformat(), nxt.isoformat()))
    return ("Results for the quarter ending %s are not filed yet. SEBI's deadline is %s; no "
            "board meeting date is on NSE's calendar yet: watch /india-news for the intimation "
            "(companies give at least 2 working days' notice)." % (nxt.isoformat(), due.isoformat()))


def transcripts(rows, limit=4):
    """Concall transcript / recording / presentation announcements, newest first."""
    out = []
    for r in rows or []:
        text = re.sub(r"\s+", " ", r.get("attchmntText") or "").strip()
        subj = r.get("desc") or ""
        if re.search(r"transcript|audio recording|link of recording|investor presentation",
                     text + " " + subj, re.I):
            kind = "transcript" if re.search(r"transcript", text, re.I) else \
                "presentation" if re.search(r"presentation", text + subj, re.I) else "recording"
            out.append({"time": r.get("an_dt"), "kind": kind, "text": text[:140],
                        "link": r.get("attchmntFile")})
    return out[:limit]


# ---------------------------------------------------------------- output

def _p(v):
    return "-" if v is None else "%+.1f%%" % v


def _pm(v):
    return "-" if v is None else "%.1f%%" % v


def render(symbol, result, notes=None, extra=None):
    bank = result["format"] == "bank"
    out = ["%s (%s), %s results%s. ₹ crore except EPS. Source: NSE results XBRL." % (
        result["entity"] or symbol, symbol, result["basis"], ", bank format" if bank else ""), ""]
    if bank:
        out.append("%-11s %-8s %10s %8s %9s %8s %7s %7s %10s %8s %8s" % (
            "quarter end", "fiscal", "tot income", "yoy", "NII", "NII yoy", "PPOP", "net",
            "net profit", "EPS dil", "EPS yoy"))
    else:
        out.append("%-11s %-8s %10s %8s %7s %7s %7s %10s %8s %8s" % (
            "quarter end", "fiscal", "revenue", "rev yoy", "EBITDA", "oper", "net", "net profit",
            "EPS dil", "EPS yoy"))
    for r in result["rows"]:
        eps = "-" if r["eps_diluted"] is None else "%.2f" % r["eps_diluted"]
        tail = "  adj" if r["eps_adjusted"] else ""
        if bank:
            out.append("%-11s %-8s %10s %8s %9s %8s %7s %7s %10s %8s %8s%s" % (
                r["end"], r["fiscal"], cr(r["revenue"]), _p(r["revenue_yoy"]), cr(r["nii"]),
                _p(r["nii_yoy"]), _pm(r["operating_margin"]), _pm(r["net_margin"]),
                cr(r["net_profit"]), eps, _p(r["eps_yoy"]), tail))
        else:
            out.append("%-11s %-8s %10s %8s %7s %7s %7s %10s %8s %8s%s" % (
                r["end"], r["fiscal"], cr(r["revenue"]), _p(r["revenue_yoy"]), _pm(r["ebitda_margin"]),
                _pm(r["operating_margin"]), _pm(r["net_margin"]), cr(r["net_profit"]), eps,
                _p(r["eps_yoy"]), tail))
    t = result["ttm"]
    if t:
        out.append("")
        out.append("Last 4 quarters: %s ₹%s cr, net profit ₹%s cr, diluted EPS %s%s." % (
            "total income" if bank else "revenue", cr(t.get("revenue")), cr(t.get("net_profit")),
            "₹%.2f" % t["eps_diluted"] if t.get("eps_diluted") is not None else "-",
            " (on today's share count)" if any(r["eps_adjusted"] for r in result["rows"][:4]) else ""))
    out.append("")
    out.append("What changed:")
    for line in changes(result):
        out.append("  - " + line)
    cur = result["rows"][0] if result["rows"] else None
    if cur:
        out.append("")
        out.append("Latest filing: %s, filed %s IST  %s" % (
            cur["fiscal"], (cur["filed"] or "?")[:16], cur["ixbrl"] or cur["xbrl"]))
    for e in extra or []:
        out.append("%s %s (%s)  %s" % (e["kind"].capitalize() + ":", e["time"], e["text"][:80],
                                      e["link"]))
    for n in notes or []:
        out.append("NOTE: " + n)
    if not bank:
        out.append("EBITDA and operating profit leave other income out: profit before exceptional "
                   "items and tax + finance costs (+ depreciation for EBITDA) - other income.")
    if any(r["eps_adjusted"] for r in result["rows"]):
        out.append("adj: EPS growth against an EPS restated for a bonus issue or split.")
    return "\n".join(out) + "\n"


def metrics_row(symbol, exchange, result):
    rows = result["rows"]
    cur = rows[0] if rows else {}
    t = result["ttm"]
    return {"ticker": symbol, "symbol": symbol, "exchange": exchange, "name": result["entity"],
            "basis": result["basis"], "format": result["format"], "period_end": cur.get("end"),
            "fiscal": cur.get("fiscal"), "currency": "INR",
            "revenue_ttm": t.get("revenue"), "net_income_ttm": t.get("net_profit"),
            "eps_ttm": t.get("eps_diluted"),
            "revenue_ttm_cr": round(t["revenue"] / CRORE) if t.get("revenue") is not None else None,
            "net_profit_ttm_cr": round(t["net_profit"] / CRORE) if t.get("net_profit") is not None else None,
            "revenue_growth_yoy": cur.get("revenue_yoy"), "eps_growth_yoy": cur.get("eps_yoy"),
            "net_income_growth_yoy": cur.get("net_profit_yoy"), "nii_growth_yoy": cur.get("nii_yoy"),
            "gross_margin": None, "ebitda_margin": cur.get("ebitda_margin"),
            "operating_margin": cur.get("operating_margin"), "net_margin": cur.get("net_margin"),
            "source": "NSE results XBRL"}


# ---------------------------------------------------------------- one company

def load_cache(path):
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return {}


def report(fetcher, symbol, quarters=6, basis="consolidated", cache=None, events=True,
           want_transcripts=False, today=None):
    """Everything for one symbol: {"result", "notes", "next", "transcripts", "filings"}."""
    today = today or _dt.datetime.now(IST).date()
    sym = urllib.parse.quote(symbol, safe="-")
    notes = []
    integrated = legacy = None
    errs = []
    try:
        integrated = fetcher.json(INTEGRATED_URL % sym, "integrated-%s.json" % symbol)
    except ResultsError as exc:
        errs.append(str(exc))
    try:
        legacy = fetcher.json(LEGACY_URL % sym, "results-%s.json" % symbol)
    except ResultsError as exc:
        errs.append(str(exc))
    if integrated is None and legacy is None:
        raise ResultsError(errs[0] if errs else "no results list")
    rows = filings(integrated, legacy)
    if not rows:
        raise ResultsError("NSE lists no quarterly results for %s (is it an NSE equity symbol? "
                           "SME and newly listed companies may have none)" % symbol)
    mine, used, note = pick_basis(rows, basis)
    if note:
        notes.append(note)
    parsed = {}
    for f in mine[:quarters + 4]:  # four more for the year-earlier comparison
        q = (cache or {}).get(f["xbrl"])
        if q is None:
            try:
                q = parse_xbrl(fetcher.get(f["xbrl"], f["xbrl"].rsplit("/", 1)[-1]))
            except ResultsError as exc:
                if not (fetcher.offline and "no file" in str(exc)):
                    notes.append("could not read the %s filing (%s): %s" % (f["end"], f["xbrl"], exc))
                continue
            if cache is not None:
                cache[f["xbrl"]] = q
        q = dict(q, filing=f)
        if q["end"] != f["end"]:
            notes.append("the %s filing's XBRL covers %s; used as filed" % (f["end"], q["end"]))
        parsed[q["end"]] = q
    if not parsed:
        raise ResultsError("none of the XBRL files for %s could be read" % symbol)
    name = next((q["name"] for q in parsed.values() if q.get("name")), None) or \
        next((f.get("company") for f in mine if f.get("company")), None)
    result = analyse(parsed, quarters, name=name.title() if name and name.isupper() else name,
                     basis=used)
    if result["format"] == "bank":
        notes.append("bank: total income, NII and operating profit before provisions replace "
                     "revenue and EBITDA.")
    nxt = None
    if events:
        try:
            ev = fetcher.json(EVENTS_URL % sym, "events-%s.json" % symbol)
            nxt = next_results(result, ev if isinstance(ev, list) else ev.get("data", []), today)
        except ResultsError as exc:
            if not (fetcher.offline and "no file" in str(exc)):
                notes.append("event calendar: %s" % exc)
            nxt = next_results(result, [], today)
    if nxt:
        notes.insert(0, nxt)
    tr = []
    if want_transcripts:
        start = (today - _dt.timedelta(days=120)).strftime("%d-%m-%Y")
        try:
            ann = fetcher.json(ANN_URL % (sym, start, today.strftime("%d-%m-%Y")), "ann-%s.json" % symbol)
            tr = transcripts(ann if isinstance(ann, list) else ann.get("data", []))
            if not tr:
                notes.append("no concall transcript filed on NSE in the last 120 days.")
        except ResultsError as exc:
            notes.append("announcements: %s" % exc)
    return {"result": result, "notes": notes, "transcripts": tr}


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("symbols", nargs="*", help="NSE symbols, e.g. RELIANCE HDFCBANK")
    ap.add_argument("--quarters", type=int, default=6)
    ap.add_argument("--standalone", action="store_true", help="the parent company alone")
    ap.add_argument("--no-events", action="store_true", help="skip NSE's event calendar")
    ap.add_argument("--transcripts", action="store_true",
                    help="list the latest concall transcript / recording / presentation filings")
    ap.add_argument("--metrics", metavar="FILE", help="merge a row per symbol into this JSON")
    ap.add_argument("--cache", default="trader-in/state/results-cache.json")
    ap.add_argument("--no-cache", action="store_true")
    ap.add_argument("--offline", metavar="DIR")
    ap.add_argument("--today", help="YYYY-MM-DD, for reproducible runs")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)
    if not args.symbols:
        ap.print_help()
        return 2
    fetcher = Fetcher(offline=args.offline)
    use_cache = not args.no_cache and not args.offline
    cache = load_cache(args.cache) if use_cache else None
    today = _dt.date.fromisoformat(args.today) if args.today else None
    results, rows, failed = [], [], 0
    for raw in args.symbols:
        symbol, exch = parse_symbol(raw)
        if re.match(r"^\d{6}$", symbol):
            print("results: %s is a BSE code; pass the NSE symbol (BSE's API refuses scripts)"
                  % symbol, file=sys.stderr)
            failed += 1
            continue
        try:
            rep = report(fetcher, symbol, args.quarters, "standalone" if args.standalone else
                         "consolidated", cache, not args.no_events, args.transcripts, today)
        except ResultsError as exc:
            print("results: %s: %s" % (symbol, exc), file=sys.stderr)
            failed += 1
            continue
        res = rep["result"]
        results.append({"symbol": symbol, "exchange": exch, "result": res,
                        "changes": changes(res), "notes": rep["notes"],
                        "transcripts": rep["transcripts"]})
        rows.append(metrics_row(symbol, exch, res))
        if not args.json:
            sys.stdout.write(render(symbol, res, rep["notes"], rep["transcripts"]))
            sys.stdout.write("\n")
    if use_cache and cache:
        folder = os.path.dirname(args.cache)
        if folder:
            os.makedirs(folder, exist_ok=True)
        with open(args.cache, "w", encoding="utf-8") as fh:
            json.dump(cache, fh)
    if args.json:
        print(json.dumps(results, indent=2, ensure_ascii=False))
    if args.metrics and rows:
        existing = {}
        if os.path.isfile(args.metrics):
            with open(args.metrics, encoding="utf-8") as fh:
                existing = {r["ticker"]: r for r in json.load(fh).get("companies", [])}
        for r in rows:
            existing[r["ticker"]] = r
        folder = os.path.dirname(args.metrics)
        if folder:
            os.makedirs(folder, exist_ok=True)
        with open(args.metrics, "w", encoding="utf-8") as fh:
            json.dump({"as_of": (today or _dt.datetime.now(IST).date()).isoformat(),
                       "companies": sorted(existing.values(), key=lambda r: r["ticker"])},
                      fh, indent=2, ensure_ascii=False)
            fh.write("\n")
        if not args.json:
            print("wrote %s (%d companies)" % (args.metrics, len(existing)))
    return 0 if results and not failed else (1 if results else 2)


if __name__ == "__main__":
    sys.exit(main())
