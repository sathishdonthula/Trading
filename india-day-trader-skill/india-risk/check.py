#!/usr/bin/env python3
"""Pre-trade risk check for india-risk. Standard library only, no network. Python 3.9+.

Reads the proposed orders (trader-in/orders.json, Kite place_order shape plus
an exit plan) and checks every one against your rules (trader-in/config.json),
your account (trader-in/portfolio.json), prices (trader-in/prices.json), the
NSE security list (trader-in/sec_list.csv: series and price band) and what was
placed today (trader-in/state/broker.json). Each order gets PASS or BLOCK with
the reasons. Orders are checked in file order as if each PASS had filled.

    python3 check.py                                 # the trader-in/ files, writes trader-in/state/risk.json
    python3 check.py --now "2026-10-19 09:50"        # pin the clock (IST)
    python3 check.py --orders trader-in/orders.json --json
    python3 check.py --no-write                      # check without recording the verdicts

Entries and exits. An ENTRY opens or adds to a position (a BUY, or a SELL
that would go short). An EXIT only reduces what you hold. Getting out must
always be possible: an exit is checked for format, asset, short sales and
Zerodha's 15:25 MIS square-off only; every day limit turns into a warning.

What blocks an order (every order):

  format        Kite place_order fields: variety, exchange NSE/BSE, tradingsymbol,
                transaction_type BUY/SELL, a whole-share quantity, product MIS/CNC,
                order_type MARKET/LIMIT/SL/SL-M with the prices each one needs,
                validity DAY/IOC/TTL, prices on the NSE tick, a tag of at most 20
                letters and digits; an entry's exit plan (stop leg SL-M SELL with a
                trigger_price, target leg LIMIT SELL, or a CNC two-leg GTT) with the
                same symbol, quantity and product
  asset         derivatives (NFO/BFO/MCX, ...FUT, ...CE, ...PE) unless "allow_options": true;
                margin products (NRML, MTF)
  short         selling more than you hold, unless "allow_short": true
  square-off    any MIS order at or after Zerodha's MIS square-off for that stock that day:
                15:12 for closing-auction (CAS, F&O) stocks since 2026-08-03, 15:25 for the rest;
                unknown CAS status uses 15:12. An MIS entry whose flat_by is at or after it

What blocks an entry (on top):

  blocked       a symbol on your "blocked" list
  series        BE / BZ (trade-to-trade) with product MIS: no intraday allowed
  circuit       the entry at or within 1% of the upper circuit: Kite's limits for today
                (--kite-instrument) win, else NSE's band on the previous close
  size          under min_order_inr, over max_order_inr
  position      the stock would be over max_position_pct of the account
  cash          the buy would dip into your cash buffer, or need margin (MIS leverage)
  duplicate     the same symbol and side twice in this batch, or already placed today
  daily limit   max_orders_per_day orders placed today
  stop          no stop while "require_stop": true; a stop on the wrong side of the entry
  loss          qty x (entry - stop) + round-trip charges over max_loss_per_trade_inr.
                The entry is the limit price for a LIMIT order, else the HIGHER of the
                plan's entry and a fresh price (under 15 minutes old)
  reward:risk   (target - entry) / (entry - stop) under min_reward_risk
  daily loss    the max losses at the stops of today's entries plus this one over
                max_daily_loss_inr (win or lose, each counts in full)
  trades        max_trades_per_day entries already placed today
  window        before no_entries_before or after no_entries_after (IST), or the market
                is closed (weekend, NSE holiday, outside 09:15 to 15:30)

Exit code 0 when every order passes, 1 when any order is blocked.
The verdicts go to trader-in/state/risk.json with a fingerprint of each order,
so india-broker only shows an order as ready if THIS exact order passed today.
"""

import argparse
import csv
import datetime
import hashlib
import json
import math
import os
import re
import sys

DEFAULTS = {
    "budget": None, "cash_buffer_pct": 10, "max_position_pct": 75, "max_order_inr": 40000,
    "min_order_inr": 100, "max_loss_per_trade_inr": 500, "max_daily_loss_inr": 1500, "max_trades_per_day": 3,
    "max_orders_per_day": 10, "min_reward_risk": 1.0, "require_stop": True,
    "no_entries_before": "09:30", "no_entries_after": "14:45", "flat_by": "auto",
    "product": "MIS", "exchange": "NSE", "broker": "zerodha",
    "paper": True, "blocked": [], "allow_options": False, "allow_margin": False, "allow_short": False,
    "cas_symbols_extra": [], "non_cas_symbols": [],
}
# Kite place_order parameters (github.com/zerodha/kite-mcp-server, mcp/post_tools.go, checked 2026-10-23)
KITE_FIELDS = ("variety", "exchange", "tradingsymbol", "transaction_type", "quantity", "product",
               "order_type", "price", "validity", "validity_ttl", "disclosed_quantity", "trigger_price",
               "iceberg_legs", "iceberg_quantity", "tag")
GTT_FIELDS = ("exchange", "tradingsymbol", "transaction_type", "product", "trigger_type",
              "trigger_value", "quantity", "limit_price", "upper_trigger_value", "upper_quantity",
              "upper_limit_price", "lower_trigger_value", "lower_quantity", "lower_limit_price")
VARIETIES = {"regular", "amo"}
ORDER_TYPES = {"MARKET", "LIMIT", "SL", "SL-M"}
VALIDITIES = {"DAY", "IOC", "TTL"}
PRODUCTS = {"MIS", "CNC"}
SYMBOL = re.compile(r"^[A-Z0-9][A-Z0-9&\-]{0,19}$")
DERIV = re.compile(r"(\d{2}[A-Z]{3}FUT|\d+(CE|PE))$")
TAG = re.compile(r"^[A-Za-z0-9]{1,20}$")
HHMM = re.compile(r"^([01]?\d|2[0-3]):([0-5]\d)$")
PAISA = 0.005
FRESH_MINUTES = 15
NEAR_CIRCUIT_PCT = 1.0
REF_WORDS = {"plan": "the plan's entry", "price": "the latest price", "limit": "the limit price",
             "trigger": "the trigger price", "stale price": "an old price"}

# NSE capital-market trading holidays 2026 (NSE holiday-master, read 2026-10-23). Weekend dates are harmless.
# 2027 is not published yet: cache the live list to trader-in/holidays.json (india-alerts does it).
NSE_HOLIDAYS = {
    "2026-01-15": "Municipal Corporation Election - Maharashtra",
    "2026-01-26": "Republic Day",
    "2026-02-15": "Mahashivratri",
    "2026-03-03": "Holi",
    "2026-03-21": "Id-Ul-Fitr",
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
    "2026-11-08": "Diwali Laxmi Pujan (Muhurat trading: timing announced by NSE)",
    "2026-11-10": "Diwali-Balipratipada",
    "2026-11-24": "Prakash Gurpurb Sri Guru Nanak Dev",
    "2026-12-25": "Christmas",
}
CALENDAR_YEARS = (2026,)
OPEN = datetime.time(9, 15)
CLOSE = datetime.time(15, 30)


class RiskError(Exception):
    pass


# ---------------------------------------------------------------- time (IST is UTC+05:30, no DST)

def to_ist(utc):
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
        raise RiskError("--now wants a time like \"2026-10-19 09:50\" (IST) or ...Z (UTC)")
    if dt.tzinfo is None:
        return ist_to_utc(dt)
    return (dt - dt.utcoffset()).replace(tzinfo=None)


def parse_utc(text):
    if not text:
        return None
    try:
        dt = datetime.datetime.fromisoformat(str(text).strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is not None:
        dt = (dt - dt.utcoffset()).replace(tzinfo=None)
    return dt


def load_holidays(doc):
    """trader-in/holidays.json -> {YYYY-MM-DD: name}. Takes NSE's holiday-master JSON ({"CM": [{tradingDate:
    "26-Jan-2026", description}]}), {"holidays": {...}} or a plain list of dates."""
    out = {}
    if isinstance(doc, dict) and isinstance(doc.get("holidays"), (dict, list)):
        doc = doc["holidays"]
    if isinstance(doc, dict) and isinstance(doc.get("CM"), list):
        for r in doc["CM"]:
            try:
                d = datetime.datetime.strptime(str(r.get("tradingDate")), "%d-%b-%Y").date()
            except ValueError:
                continue
            out[d.isoformat()] = r.get("description") or "NSE holiday"
    elif isinstance(doc, dict):
        out.update({str(k): str(v) for k, v in doc.items() if re.match(r"^\d{4}-\d{2}-\d{2}$", str(k))})
    elif isinstance(doc, list):
        for x in doc:
            if re.match(r"^\d{4}-\d{2}-\d{2}$", str(x)):
                out[str(x)] = "NSE holiday"
    return out


def trading_day(d, holidays=None):
    """(is_trading_day, reason). Weekends and the NSE holiday list."""
    if d.weekday() >= 5:
        return False, "weekend"
    name = (holidays or NSE_HOLIDAYS).get(d.isoformat())
    if name:
        return False, "NSE holiday: " + name
    return True, ""


def hhmm(value, key):
    if value in (None, "", False):
        return None
    m = HHMM.match(str(value).strip())
    if not m:
        raise RiskError("%s must be an IST time like \"09:30\", got %r" % (key, value))
    return datetime.time(int(m.group(1)), int(m.group(2)))


def window_check(now_ist, c, holidays=None):
    """Reasons an entry cannot be placed now (IST). Empty when the window is off."""
    before = hhmm(c.get("no_entries_before"), "no_entries_before")
    after = hhmm(c.get("no_entries_after"), "no_entries_after")
    if before is None and after is None:
        return [], []
    warns = []
    d, t = now_ist.date(), now_ist.time().replace(second=0, microsecond=0)
    years = set(CALENDAR_YEARS)
    if holidays:
        years |= {int(k[:4]) for k in holidays}
    if d.year not in years:
        warns.append("the built-in holiday list covers %s only and %d is not published in it: fetch NSE's "
                     "list (india-alerts caches trader-in/holidays.json)" % (", ".join(map(str, sorted(years))),
                                                                              d.year))
    hol = dict(NSE_HOLIDAYS)
    hol.update(holidays or {})
    ok, why = trading_day(d, hol)
    stamp = now_ist.strftime("%a %Y-%m-%d %H:%M")
    if not ok:
        return ["window: the market is closed today (%s, %s IST)" % (why, stamp)], warns
    if t < OPEN or now_ist.time() >= CLOSE:
        return ["window: the market is closed (%s IST; the normal session is %s to %s)"
                % (stamp, OPEN.strftime("%H:%M"), CLOSE.strftime("%H:%M"))], warns
    if before is not None and t < before:
        return ["window: no new entries before %s IST (it is %s)" % (before.strftime("%H:%M"),
                                                                    t.strftime("%H:%M"))], warns
    if after is not None and t > after:
        return ["window: no new entries after %s IST (it is %s)" % (after.strftime("%H:%M"),
                                                                   t.strftime("%H:%M"))], warns
    return [], warns


# ---------------------------------------------------------------- session rules (CAS) and Kite files (shared block)
# Since 2026-08-03 NSE runs a closing auction (CAS, 15:15-15:35) for stocks with F&O contracts: their continuous
# trading ends at 15:15 and Zerodha squares off MIS at 15:12 (non-CAS: 15:30 and 15:25; ₹50 + GST an order).
# Unknown CAS status uses the stricter CAS rules. Same block in india-budget, india-risk and india-broker.

CAS_START = datetime.date(2026, 8, 3)
SOURCE_WORDS = {"kite": "Kite, live", "kite-candles": "Kite candles", "yahoo": "Yahoo, free, can lag",
                "plan": "the plan", "manual": "typed in", "limit": "the limit price"}


def session_rules(cas, day):
    """Session times for one symbol on one day. cas: True / False / None (unknown -> strict)."""
    if isinstance(day, str):
        day = datetime.date.fromisoformat(day[:10])
    strict = cas is not False and day >= CAS_START
    return {
        "open": "09:15",
        "close": "15:15" if strict else "15:30",        # end of continuous trading
        "square_off": "15:12" if strict else "15:25",   # Zerodha MIS auto square-off
        "flat_by_default": "15:00" if strict else "15:10",
        "no_entries_after_default": "14:45",
        "bars_5m": 72 if strict else 75,
        "cas": cas, "label": "CAS stock (F&O): continuous trading ends 15:15, Zerodha squares off MIS at 15:12"
                             if strict else "non-CAS stock: trades to 15:30, Zerodha squares off MIS at 15:25",
    }


def load_fo_list(path):
    """trader-in/fo_list.txt (one symbol a line, first line '# fetched YYYY-MM-DD') or NSE's fo_mktlots.csv
    -> (set of CAS symbols, fetched date or None). Index rows (UNDERLYING starting with NIFTY) are skipped."""
    if not path or not os.path.exists(path):
        return None, None
    syms, fetched = set(), None
    with open(path, encoding="utf-8", errors="replace") as fh:
        lines = fh.read().splitlines()
    if path.lower().endswith(".csv") or (lines and "," in lines[0]):
        for line in lines:
            cells = [c.strip() for c in line.split(",")]
            if len(cells) < 2 or not cells[1] or cells[1].upper() == "SYMBOL":
                continue
            if cells[0].upper().startswith("NIFTY") or cells[0].upper() == "UNDERLYING":
                continue
            syms.add(cells[1].upper())
        m = re.search(r"(\d{4}-\d{2}-\d{2})", os.path.basename(path))
        fetched = m.group(1) if m else None
    else:
        for line in lines:
            line = line.strip()
            if line.startswith("#"):
                m = re.search(r"(\d{4}-\d{2}-\d{2})", line)
                if m and not fetched:
                    fetched = m.group(1)
            elif line:
                syms.add(line.upper())
    return syms, fetched


def cas_status(sym, plan=None, cfg=None, fo=None, flag=None):
    """(True / False / None, where it came from) for a symbol. Order: --cas, the plan, the config lists,
    the F&O list; else None (unknown -> the strict rules)."""
    sym = str(sym or "").upper()
    if flag in ("yes", "no"):
        return flag == "yes", "--cas %s" % flag
    if isinstance(plan, dict) and isinstance(plan.get("cas"), bool):
        return plan["cas"], "the plan"
    cfg = cfg or {}
    if sym in {str(s).upper() for s in cfg.get("non_cas_symbols") or []}:
        return False, "non_cas_symbols in the config"
    if sym in {str(s).upper() for s in cfg.get("cas_symbols_extra") or []}:
        return True, "cas_symbols_extra in the config"
    if fo is not None:
        return sym in fo, "the NSE F&O list"
    return None, "unknown (no F&O list): the stricter CAS rules apply"


def resolve_flat_by(value, rules):
    """(HH:MM, warning or None). "auto" or empty -> the symbol's flat_by_default; a time at or after the
    square-off is clamped to the default."""
    v = str(value or "auto").strip()
    if v.lower() == "auto":
        return rules["flat_by_default"], None
    if not re.match(r"^([01]\d|2[0-3]):[0-5]\d$", v):
        return rules["flat_by_default"], "flat_by %r is not HH:MM: using %s" % (value, rules["flat_by_default"])
    if v >= rules["square_off"]:
        return rules["flat_by_default"], ("flat_by %s is at or after Zerodha's %s MIS square-off for this stock: "
                                          "using %s" % (v, rules["square_off"], rules["flat_by_default"]))
    return v, None


def _kite_key(key):
    k = str(key).upper()
    exch, sym = (k.split(":", 1) if ":" in k else ("NSE", k))
    return exch, sym


def load_kite_ltp(doc):
    """A Kite get_ltp / get_quotes answer ({"NSE:RELIANCE": {"last_price": 1190, "ohlc": {"close": ...}}},
    maybe wrapped in "data") -> {"NSE:RELIANCE": {"price", "prev_close", "time"}}."""
    if isinstance(doc, dict) and isinstance(doc.get("data"), dict):
        doc = doc["data"]
    out = {}
    for key, v in (doc or {}).items() if isinstance(doc, dict) else []:
        if not isinstance(v, dict) or _f(v.get("last_price")) is None:
            continue
        exch, sym = _kite_key(key)
        out["%s:%s" % (exch, sym)] = {"price": float(v["last_price"]),
                                      "prev_close": _f((v.get("ohlc") or {}).get("close")),
                                      "time": v.get("last_trade_time") or v.get("timestamp")}
    return out


def kite_lookup(table, sym, exch="NSE"):
    """The Kite row for a symbol: its own exchange first, then NSE (BSE follows NSE)."""
    if not table:
        return None
    sym = str(sym).upper()
    return table.get("%s:%s" % (str(exch or "NSE").upper(), sym)) or table.get("NSE:" + sym)


def parse_price_args(values):
    """['RELIANCE=1227.8', 'BSE:RELIANCE=1228'] -> {"NSE:RELIANCE": {"price": 1227.8, ...}} (typed in by Claude
    from a Kite get_ltp it just made)."""
    out = {}
    for v in values or []:
        if "=" not in str(v):
            raise ValueError("--price wants SYMBOL=PRICE, like RELIANCE=1227.8")
        k, p = str(v).rsplit("=", 1)
        exch, sym = _kite_key(k)
        price = _f(p.replace(",", "").replace("₹", ""))
        if price is None or price <= 0:
            raise ValueError("--price %s: %r is not a price" % (k, p))
        out["%s:%s" % (exch, sym)] = {"price": price, "prev_close": None, "time": None}
    return out


def load_kite_instrument(path):
    """A Kite search_instruments answer ({"data": [{tradingsymbol, exchange, tick_size, lower_circuit_limit,
    upper_circuit_limit, series}]}) -> ({"NSE:RELIANCE": {...}}, date YYYY-MM-DD). The circuit limits are for
    ONE day: the file's "fetched" date, else a date in its name, else its modified date (IST)."""
    with open(path, encoding="utf-8") as fh:
        doc = json.load(fh)
    rows = doc.get("data") if isinstance(doc, dict) else doc
    if isinstance(rows, dict):
        rows = [rows]
    out = {}
    for r in rows or []:
        if not isinstance(r, dict) or not r.get("tradingsymbol"):
            continue
        out["%s:%s" % (str(r.get("exchange") or "NSE").upper(), str(r["tradingsymbol"]).upper())] = {
            "tick": _f(r.get("tick_size")), "lower": _f(r.get("lower_circuit_limit")) or None,
            "upper": _f(r.get("upper_circuit_limit")) or None, "series": str(r.get("series") or "").upper(),
            "token": r.get("instrument_token")}
    day = str(doc.get("fetched"))[:10] if isinstance(doc, dict) and doc.get("fetched") else None
    if not day:
        m = re.search(r"(\d{4}-\d{2}-\d{2})", os.path.basename(path))
        day = m.group(1) if m else None
    if not day:
        mt = datetime.datetime.utcfromtimestamp(os.path.getmtime(path)) + datetime.timedelta(hours=5, minutes=30)
        day = mt.date().isoformat()
    return out, day


# ---------------------------------------------------------------- ticks, charges, money (shared table)

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


def round_to_tick(price, mode="nearest", tick=None):
    """Round to the stock's tick: 'down', 'up' or 'nearest'. Works in paise so 1215.35 stays exact.
    tick: Kite's tick_size for the stock (search_instruments) wins over the table."""
    t = int(round((tick or tick_size(price)) * 100))
    p = round(price * 100, 4)
    n = p / t
    k = math.floor(n + 1e-9) if mode == "down" else math.ceil(n - 1e-9) if mode == "up" else math.floor(n + 0.5)
    return round(k * t / 100.0, 2)


def on_tick(price, tick=None):
    return abs(round_to_tick(price, "nearest", tick) - float(price)) < 0.001


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
    """Rupees with Indian digit grouping: 123456.78 -> '₹1,23,456.78'."""
    x = float(x)
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


def money(x):
    s = inr(x)
    return s[:-3] if s.endswith(".00") else s


def _p(x):
    return "%.2f" % float(x)


def _f(x):
    if isinstance(x, bool):
        return None
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def _pct(x):
    x = float(x)
    return str(int(x)) if x == int(x) else ("%.2f" % x)


def _num(x):
    x = float(x)
    return str(int(x)) if x == int(x) else ("%.4f" % x).rstrip("0")


# ---------------------------------------------------------------- files and helpers

def read_json(path, what, required=True):
    if not path or not os.path.exists(path):
        if required:
            raise RiskError("no %s at %s" % (what, path))
        return None
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except json.JSONDecodeError as e:
        raise RiskError("%s is not valid JSON: %s" % (path, e))


def load_sec_list(path):
    """NSE sec_list.csv (Symbol, Series, Security Name, Band, Remarks) -> {SYMBOL: {series, band}}.
    band is a percent (2, 5, 10, 20) or None for "No Band". An EQ row wins over others."""
    out = {}
    if not path or not os.path.exists(path):
        return out
    with open(path, encoding="utf-8", errors="replace", newline="") as fh:
        for row in csv.DictReader(fh):
            row = {str(k).strip(): (v or "").strip() for k, v in row.items() if k}
            sym = row.get("Symbol", "").upper()
            if not sym:
                continue
            band = _f(row.get("Band"))
            rec = {"series": row.get("Series", "").upper(), "band": band, "band_text": row.get("Band", "")}
            if sym not in out or rec["series"] == "EQ":
                out[sym] = rec
    return out


def exit_core(order):
    """The parts of the exit plan that reach Kite (last_price is filled in at placement, so it is left out)."""
    ep = order.get("exit_plan") if isinstance(order.get("exit_plan"), dict) else None
    if not ep:
        return None
    core = {}
    for leg in ("stop_leg", "target_leg"):
        if isinstance(ep.get(leg), dict):
            core[leg] = {k: ep[leg][k] for k in KITE_FIELDS if ep[leg].get(k) is not None}
    if isinstance(ep.get("gtt"), dict):
        core["gtt"] = {k: ep["gtt"][k] for k in GTT_FIELDS if ep["gtt"].get(k) is not None}
    return core or None


def fingerprint(order):
    """Same function in india-broker/ticket.py: the exact order (entry + exit legs) that was checked."""
    core = {k: order[k] for k in KITE_FIELDS if k in order and order[k] is not None}
    ex = exit_core(order)
    if ex:
        core["exit_plan"] = ex
    blob = json.dumps(core, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]


def account(cfg, portfolio, prices):
    """Starting account: cash, shares and position values. No portfolio = a fresh account funded
    with the budget."""
    warnings = []
    if portfolio is None:
        budget = _f(cfg.get("budget"))
        if not budget:
            raise RiskError("no trader-in/portfolio.json and no budget in the config: "
                            "nothing to check the orders against")
        return {"cash": budget, "shares": {}, "values": {}}, warnings
    cash = _f(portfolio.get("cash"))
    if cash is None:
        raise RiskError("portfolio.json needs a \"cash\" number")
    shares, values = {}, {}
    for p in portfolio.get("positions", []):
        t = str(p.get("ticker") or p.get("symbol") or p.get("tradingsymbol") or "").upper()
        if not t:
            continue
        n = _f(p.get("shares", p.get("qty", p.get("quantity"))))
        if n is None:
            n = sum(_f(l.get("shares")) or 0 for l in p.get("lots", []))
        shares[t] = shares.get(t, 0) + n
        if prices.get(t):
            values[t] = shares[t] * prices[t]
        else:
            basis = sum(_f(l.get("cost")) or 0 for l in p.get("lots", []))
            values[t] = basis
            warnings.append("no price for %s: valued at its cost basis %s" % (t, money(basis)))
    return {"cash": cash, "shares": shares, "values": values}, warnings


def rec_day(rec):
    return str(rec.get("date") or str(rec.get("at", ""))[:10])


def rec_is_entry(rec):
    """india-broker logs the kind india-risk gave; older records: a buy is an entry."""
    k = rec.get("kind")
    if k:
        return k == "entry"
    return str(rec.get("side", "")).lower() == "buy"


def placed_today(state, today):
    return [r for r in (state or {}).get("orders", []) if r.get("status") == "placed" and rec_day(r) == today]


# ---------------------------------------------------------------- the check

def kind_guess(order, acct, sym, q):
    """entry or exit before the full check: a sell of what you hold is an exit."""
    side = str(order.get("transaction_type", "")).lower()
    held = acct["shares"].get(sym, 0.0)
    if side == "sell" and held > 1e-9 and q is not None and q <= held + 1e-9:
        return "exit"
    return "entry"


def check(orders, cfg, portfolio=None, prices=None, state=None, today=None, now=None, price_times=None,
          sec_list=None, prev_close=None, holidays=None, kite_ltp=None, kite_inst=None, kite_day=None,
          fo=None, cas_flag=None, price_sources=None):
    """Pure function. Returns {"results": [...], "pass": n, "block": n, "warnings": [...]}.

    now          naive UTC (default: the real clock); its IST date is "today"
    price_times  {SYM: naive UTC time of that price}; None = every price counts as fresh
    sec_list     {SYM: {series, band}} from NSE's sec_list.csv; None = not checked (a warning)
    prev_close   {SYM: previous close}, for the price band
    holidays     {YYYY-MM-DD: name} on top of the built-in NSE list
    kite_ltp     {"NSE:SYM": {"price"}} from a Kite get_ltp / get_quotes Claude just made: fresh, wins over prices
    kite_inst    {"NSE:SYM": {tick, lower, upper}} from Kite search_instruments for kite_day (used only when
                 kite_day is today: the circuit limits are for one day)
    fo, cas_flag the NSE F&O list (set) and --cas yes|no: closing-auction (CAS) session rules per symbol
    price_sources {SYM: "kite" | "yahoo"} for the prices in prices.json (default yahoo)"""
    c = dict(DEFAULTS)
    c.update(cfg or {})
    prices = {str(k).upper(): float(v) for k, v in (prices or {}).items() if _f(v)}
    prev_close = {str(k).upper(): float(v) for k, v in (prev_close or {}).items() if _f(v)}
    now_utc = now or parse_now(None)
    now_ist = to_ist(now_utc)
    today = today or now_ist.date().isoformat()
    acct, top_warn = account(c, portfolio, prices)
    blocked = {str(b).upper() for b in c.get("blocked") or []}
    done_today = placed_today(state, today)
    count_today = len(done_today)
    entries_today = [r for r in done_today if rec_is_entry(r)]
    loss_today = 0.0
    for r in entries_today:
        ml = _f(r.get("max_loss"))
        if ml is None:
            top_warn.append("an entry placed today (%s %s) has no max loss in the log; not counted in "
                            "the daily loss" % (r.get("side"), r.get("symbol")))
        else:
            loss_today += ml
    placed_keys = {(str(r.get("symbol", "")).upper(), str(r.get("side", "")).lower()) for r in done_today}
    win_reasons, win_warns = window_check(now_ist, c, holidays)
    open_day = trading_day(now_ist.date(), dict(NSE_HOLIDAYS, **(holidays or {})))[0]
    hm_now = now_ist.strftime("%H:%M")
    price_sources = {str(k).upper(): v for k, v in (price_sources or {}).items()}
    kite_ltp = kite_ltp or {}
    use_kite_inst = bool(kite_inst) and kite_day == today
    if kite_inst and not use_kite_inst:
        top_warn.append("the Kite instrument file is from %s, not today %s: its circuit limits are for one day "
                        "only, so they are not used" % (kite_day, today))
    seen = set()
    results = []
    batch_entries, batch_loss = 0, 0.0
    if not c.get("paper", True):
        top_warn.append("LIVE account (\"paper\": false): these orders use real money")
    if sec_list is None:
        top_warn.append("no NSE security list (trader-in/sec_list.csv): series (BE/BZ) and price bands "
                        "were not checked")
    top_warn.extend(win_warns)

    def fresh(sym, exch="NSE"):
        k = kite_lookup(kite_ltp, sym, exch)
        if k:
            return k["price"], "kite"
        return fresh_free(sym), price_sources.get(sym, "yahoo")

    def fresh_free(sym):
        if sym not in prices:
            return None
        if price_times is None:
            return prices[sym]
        t = price_times.get(sym)
        if t is None:
            return None
        age = (now_utc - t).total_seconds()
        return prices[sym] if -60 <= age <= FRESH_MINUTES * 60 else None

    for i, o in enumerate(orders, 1):
        reasons, warns, info = [], [], []
        later = []                       # entry-only reasons, turned into warnings for an exit
        sym = str(o.get("tradingsymbol", "")).strip().upper()
        exch = str(o.get("exchange", "")).strip().upper()
        side = str(o.get("transaction_type", "")).strip().lower()
        typ = str(o.get("order_type", "")).strip().upper()
        product = str(o.get("product", "")).strip().upper()
        variety = str(o.get("variety", "regular")).strip().lower()
        validity = str(o.get("validity", "DAY")).strip().upper()
        qty_raw = o.get("quantity")
        plan = o.get("plan") if isinstance(o.get("plan"), dict) else {}
        ep = o.get("exit_plan") if isinstance(o.get("exit_plan"), dict) else None
        value = None
        otick = _f(plan.get("tick")) or None      # india-budget records the tick it used (Kite's, or the table's)

        # format (Kite place_order)
        if not sym:
            reasons.append("format: no tradingsymbol")
        if variety not in VARIETIES:
            reasons.append("format: variety must be regular (or amo), got %r" % o.get("variety"))
        if exch not in ("NSE", "BSE"):
            if exch in ("NFO", "BFO", "MCX", "CDS"):
                reasons.append("asset: %s is a derivatives exchange; this pack trades NSE / BSE equities "
                               "only" % exch)
            else:
                reasons.append("format: exchange must be NSE or BSE, got %r" % o.get("exchange"))
        if side not in ("buy", "sell"):
            reasons.append("format: transaction_type must be BUY or SELL, got %r" % o.get("transaction_type"))
        q = _f(qty_raw)
        if q is None or q <= 0 or abs(q - round(q)) > 1e-9:
            reasons.append("format: quantity must be a whole number of shares, got %r" % qty_raw)
            q = None if q is None or q <= 0 else q
        elif isinstance(qty_raw, str):
            warns.append("send quantity as a number (%s), the way Kite's place_order types it" % qty_raw)
        if product in ("NRML", "MTF"):
            reasons.append("asset: product %s is margin / derivatives; this pack uses MIS (intraday) or CNC "
                           "(delivery) only" % product)
        elif product not in PRODUCTS:
            reasons.append("format: product must be MIS or CNC, got %r" % o.get("product"))
        if typ not in ORDER_TYPES:
            reasons.append("format: order_type must be MARKET, LIMIT, SL or SL-M, got %r" % o.get("order_type"))
        if validity not in VALIDITIES:
            reasons.append("format: validity must be DAY, IOC or TTL, got %r" % o.get("validity"))
        if validity == "TTL" and not _f(o.get("validity_ttl")):
            reasons.append("format: a TTL order needs validity_ttl (minutes)")
        for k in ("price", "trigger_price"):
            if o.get(k) is not None:
                v = _f(o.get(k))
                if v is None or v <= 0:
                    reasons.append("format: %s must be a positive number, got %r" % (k, o.get(k)))
                elif not on_tick(v, otick):
                    reasons.append("format: %s %s is not on the tick (%s); Kite rejects it, use %s"
                                   % (k, o.get(k), _num(otick or tick_size(v)), _p(round_to_tick(v, "nearest", otick))))
        if typ in ("LIMIT", "SL") and _f(o.get("price")) is None:
            reasons.append("format: a %s order needs a price" % typ)
        if typ in ("SL", "SL-M") and _f(o.get("trigger_price")) is None:
            reasons.append("format: a %s order needs a trigger_price" % typ)
        if typ == "MARKET" and o.get("price") is not None:
            warns.append("a MARKET order ignores its price %s" % o.get("price"))
        if o.get("tag") is not None and not TAG.match(str(o.get("tag"))):
            reasons.append("format: tag must be at most 20 letters and digits, got %r" % o.get("tag"))
        if variety == "amo":
            warns.append("after-market order (amo): it goes to the exchange at the next open")

        # asset
        if sym and DERIV.search(sym):
            if not c.get("allow_options"):
                reasons.append("asset: %s looks like a future or an option; derivatives are off "
                               "(\"allow_options\": false)" % sym)
        elif sym and not SYMBOL.match(sym):
            reasons.append("asset: %s does not look like an NSE / BSE equity symbol" % sym)

        # the reference price: what one share costs to get in
        plan_entry = _f(plan.get("entry"))
        live, live_src = fresh(sym, exch)
        krow = kite_lookup(kite_inst, sym, exch) if use_kite_inst else None
        ktick = (krow or {}).get("tick") or None
        cas, cas_from = cas_status(sym, plan, c, fo, cas_flag)
        rules = session_rules(cas, today)
        ref, ref_from = None, None
        if typ in ("LIMIT", "SL"):
            ref, ref_from = _f(o.get("price")), "limit"
        elif typ == "SL-M":
            ref, ref_from = _f(o.get("trigger_price")), "trigger"
        if ref is None:
            cands = [(p, w) for p, w in ((plan_entry, "plan"), (live, "price")) if p]
            if cands:
                ref, ref_from = max(cands, key=lambda x: x[0])
                ref = round_to_tick(ref, "nearest", ktick)
                if live and ref_from == "price" and plan_entry and live > plan_entry + PAISA:
                    info.append("%s last %s (%s) is above the plan's entry %s: the risk is measured from %s"
                                % (sym, _p(live), SOURCE_WORDS.get(live_src, live_src), _p(plan_entry), _p(ref)))
            elif sym in prices:
                ref, ref_from = prices[sym], "stale price"
        if live is not None and ref_from != "price" and live_src == "kite":
            info.append("%s last %s (Kite, live) is not above the entry it measures from" % (sym, _p(live)))
        if plan_entry and live is None and sym in prices and price_times is not None and typ == "MARKET":
            warns.append("the %s price in trader-in/prices.json is older than %d minutes: not used"
                         % (sym, FRESH_MINUTES))
        if q and ref:
            value = q * ref

        # entry or exit
        held = acct["shares"].get(sym, 0.0)
        held_value = acct["values"].get(sym, 0.0)
        exiting = False
        if side == "sell" and held > 1e-9 and q is not None:
            exiting = q <= held + 1e-9
        elif side == "buy" and held < -1e-9 and q is not None:
            exiting = q <= -held + 1e-9
        kind = "exit" if exiting else "entry"
        if kind == "entry" and value is None and q:
            later.append("price: no price for %s to value a %s-share order; add it to trader-in/prices.json "
                         "(india-alerts: alerts.py quote %s --write, or Kite get_ltp)" % (sym, _num(q), sym))

        # session rules for this symbol today: Zerodha's MIS square-off (CAS 15:12, non-CAS 15:25)
        if cas is None:
            warns.append("CAS status of %s unknown (%s): the stricter CAS rules apply (square-off %s)"
                         % (sym, cas_from, rules["square_off"]))
        if product == "MIS" and open_day and hm_now >= rules["square_off"]:
            reasons.append("square-off: it is %s IST, at or after Zerodha's %s MIS auto square-off for %s (%s); "
                           "Zerodha closes open MIS positions itself (₹50 + GST an order). Check get_positions"
                           % (hm_now, rules["square_off"], sym, rules["label"]))
        if product == "MIS" and side == "buy" and kind_guess(o, acct, sym, q) == "entry":
            raw_fb = str(plan.get("flat_by") or c.get("flat_by") or "auto")
            if raw_fb.lower() != "auto" and re.match(r"^\d{2}:\d{2}$", raw_fb) and raw_fb >= rules["square_off"]:
                later.append("flat_by: %s is at or after Zerodha's %s MIS square-off for %s: plan to be out earlier "
                             "(\"auto\" = %s)" % (raw_fb, rules["square_off"], sym, rules["flat_by_default"]))
        if side == "buy" and open_day and rules["close"] <= hm_now < CLOSE.strftime("%H:%M"):
            later.append("window: continuous trading in %s ended at %s IST (closing auction); no new entries"
                         % (sym, rules["close"]))

        # blocked list, series, circuit, size
        if sym in blocked:
            later.append("blocked: %s is on your blocked list" % sym)
        sec = (sec_list or {}).get(sym) if sec_list is not None else None
        series = str(plan.get("series") or (sec or {}).get("series") or "").upper()
        if series in ("BE", "BZ") and product == "MIS" and side == "buy":
            later.append("series: %s is series %s (trade-to-trade): no intraday (MIS) allowed, every trade "
                         "must be delivered (CNC)" % (sym, series))
        elif series and series not in ("EQ", "BE", "BZ"):
            warns.append("series %s for %s: this pack is built for EQ shares" % (series, sym))
        if sec_list is not None and sym and sec is None and exch == "NSE":
            warns.append("%s is not in the NSE security list: price band not checked" % sym)
        band = (sec or {}).get("band") if sec else _f(plan.get("band"))
        pc = prev_close.get(sym) or _f(plan.get("prev_close"))
        upper = lower = None
        circuit_src = None
        if krow and krow.get("upper"):
            upper, lower, circuit_src = krow["upper"], krow.get("lower"), "Kite instrument limits for %s" % kite_day
            if side == "buy" and kind == "entry" and ref:
                if ref >= upper * (1 - NEAR_CIRCUIT_PCT / 100.0) - PAISA:
                    later.append("circuit: the entry %s is %s today's upper circuit %s (%s %s-%s); at the circuit "
                                 "there are no sellers and you may not get out"
                                 % (_p(ref), "at" if ref >= upper - PAISA else "within 1% of", _p(upper),
                                    circuit_src, _p(lower or 0), _p(upper)))
        elif band and pc:
            circuit_src = "NSE sec_list %s%% band on the %s previous close" % (_num(band), _p(pc))
            upper = round_to_tick(pc * (1 + band / 100.0), "down")
            lower = round_to_tick(pc * (1 - band / 100.0), "up")
            if side == "buy" and kind == "entry" and ref:
                near = upper * (1 - NEAR_CIRCUIT_PCT / 100.0)
                if ref >= near - PAISA:
                    later.append("circuit: the entry %s is %s the upper circuit %s (%s%% band on the %s previous "
                                 "close); at the circuit there are no sellers and you may not get out"
                                 % (_p(ref), "at" if ref >= upper - PAISA else "within 1% of", _p(upper),
                                    _num(band), _p(pc)))
        elif band and not pc and side == "buy" and kind == "entry":
            warns.append("%s has a %s%% price band but no previous close to measure it from (prices.json "
                         "detail.prev_close): circuit not checked" % (sym, _num(band)))
        if value is not None:
            if value < float(c.get("min_order_inr") or 0) - PAISA:
                later.append("size: %s is under your %s minimum order" % (money(value), money(c["min_order_inr"])))
            if c.get("max_order_inr") and value > float(c["max_order_inr"]) + PAISA:
                later.append("size: %s is over your %s max order" % (money(value), money(c["max_order_inr"])))

        # account effects, simulated in order
        cash = acct["cash"]
        total = cash + sum(acct["values"].values())
        buffer_inr = total * float(c.get("cash_buffer_pct") or 0) / 100.0
        if value is not None and side == "buy" and kind == "entry":
            after_cash = cash - value
            if after_cash < -PAISA and not c.get("allow_margin"):
                later.append("cash: needs %s but only %s cash; that would be margin (MIS leverage, borrowed "
                             "money)" % (money(value), money(cash)))
            elif after_cash < buffer_inr - PAISA:
                later.append("cash: leaves %s cash, under your %s%% buffer of %s"
                             % (money(max(after_cash, 0)), _pct(c["cash_buffer_pct"]), money(buffer_inr)))
            pos_after = held_value + value
            limit = total * float(c.get("max_position_pct") or 100) / 100.0
            if total > 0 and pos_after > limit + PAISA:
                later.append("position: %s would be %s, %.1f%% of the %s account, over your %s%% cap"
                             % (sym, money(pos_after), 100.0 * pos_after / total, money(total),
                                _pct(c["max_position_pct"])))
        if side == "sell" and sym and kind == "entry":
            over = (q > held + 1e-9) if q is not None else held <= 0
            if over and not c.get("allow_short"):
                reasons.append("short: selling more %s than you hold (%s shares); short selling is off. "
                               "If you bought it, record the fill first (ticket.py fill)" % (sym, _num(held)))

        # the exit plan: stop and target legs
        leg_stop = leg_target = None
        if ep and kind == "entry":
            legs = []
            if ep.get("kind") == "gtt-oco" or isinstance(ep.get("gtt"), dict):
                g = ep.get("gtt") or {}
                leg_stop, leg_target = _f(g.get("lower_trigger_value")), _f(g.get("upper_trigger_value"))
                if str(g.get("trigger_type")) != "two-leg":
                    reasons.append("format: the exit GTT must be trigger_type two-leg (stop and target)")
                if product != "CNC" or str(g.get("product", "")).upper() != "CNC":
                    reasons.append("format: a GTT exit plan is for CNC (delivery) orders; MIS uses SL-M + LIMIT")
                for k in ("upper_quantity", "lower_quantity"):
                    if q is not None and _f(g.get(k)) != q:
                        reasons.append("format: the GTT %s %r does not match the entry quantity %s"
                                       % (k, g.get(k), _num(q)))
                for k in ("upper_trigger_value", "upper_limit_price", "lower_trigger_value", "lower_limit_price"):
                    v = _f(g.get(k))
                    if v is None or v <= 0:
                        reasons.append("format: the GTT needs a positive %s" % k)
                    elif not on_tick(v):
                        reasons.append("format: the GTT %s %s is not on the tick" % (k, _p(v)))
                legs.append(g)
            else:
                sl, tp = ep.get("stop_leg"), ep.get("target_leg")
                if isinstance(sl, dict):
                    leg_stop = _f(sl.get("trigger_price"))
                    if str(sl.get("order_type", "")).upper() not in ("SL-M", "SL") or leg_stop is None:
                        reasons.append("format: the stop leg must be an SL-M (or SL) SELL with a trigger_price")
                    legs.append(sl)
                if isinstance(tp, dict):
                    leg_target = _f(tp.get("price"))
                    if str(tp.get("order_type", "")).upper() != "LIMIT" or leg_target is None:
                        reasons.append("format: the target leg must be a LIMIT SELL with a price")
                    legs.append(tp)
                for lg in legs:
                    if str(lg.get("transaction_type", "")).upper() != "SELL":
                        reasons.append("format: an exit leg of a long entry must be a SELL")
                    if q is not None and _f(lg.get("quantity")) != q:
                        reasons.append("format: an exit leg's quantity %r does not match the entry quantity %s"
                                       % (lg.get("quantity"), _num(q)))
                    if str(lg.get("product", "")).upper() != product:
                        reasons.append("format: an exit leg's product %r does not match the entry's %s"
                                       % (lg.get("product"), product))
                    for k in ("price", "trigger_price"):
                        v = _f(lg.get(k))
                        if v is not None and not on_tick(v):
                            reasons.append("format: an exit leg's %s %s is not on the tick (%s)"
                                           % (k, _p(v), _num(tick_size(v))))
            for lg in legs:
                if str(lg.get("tradingsymbol", "")).upper() != sym or str(lg.get("exchange", "")).upper() != exch:
                    reasons.append("format: an exit leg is for %s:%s, the entry is %s:%s"
                                   % (lg.get("exchange"), lg.get("tradingsymbol"), exch, sym))

        # day-trade checks: stop, loss, reward:risk, daily loss, trades, window
        max_loss = risk_ps = rr = stop = target = ch = None
        if kind == "entry" and side in ("buy", "sell"):
            long = side == "buy"
            plan_stop = _f(plan.get("stop"))
            stop = leg_stop if leg_stop else plan_stop
            target = leg_target or _f(plan.get("target"))
            if leg_stop and plan_stop and abs(leg_stop - plan_stop) > PAISA:
                warns.append("the exit plan's stop %s differs from the plan's stop %s: the exit leg is the one "
                             "Kite gets" % (_p(leg_stop), _p(plan_stop)))
            if stop and not leg_stop:
                warns.append("the stop %s is in the plan only, not in an exit leg: set a stop alert "
                             "(india-alerts: alerts.py add --from-plan)" % _p(stop))
            if not stop:
                if c.get("require_stop", True):
                    later.append("stop: no stop. Every entry needs one (\"require_stop\": true): an exit plan "
                                 "stop leg, from india-exit and india-budget")
                else:
                    warns.append("no stop: the max loss is not capped, so it is not counted in the daily loss")
            elif ref:
                if (long and stop >= ref - PAISA) or (not long and stop <= ref + PAISA):
                    later.append("stop: the stop %s must be %s the entry %s for a %s"
                                 % (_p(stop), "below" if long else "above", _p(ref), side.upper()))
                else:
                    if lower and long and stop < lower - PAISA:
                        warns.append("the stop %s is below the lower circuit %s: if the stock locks at the "
                                     "circuit the stop cannot fill" % (_p(stop), _p(lower)))
                    risk_c = int(round(abs(ref - stop) * 100))
                    risk_ps = risk_c / 100.0
                    shares = q or 0
                    buy_v, sell_v = (shares * ref, shares * stop) if long else (shares * stop, shares * ref)
                    ch = charges(buy_v, sell_v, product or "MIS", exch or "NSE")
                    max_loss = round(shares * risk_c / 100.0 + ch["total"], 2)
                    cap = _f(c.get("max_loss_per_trade_inr"))
                    if cap and max_loss > cap + PAISA:
                        later.append("loss: %s shares x %s risk a share = %s + %s charges = %s at the stop, over "
                                     "your %s max loss per trade"
                                     % (_num(shares), _p(risk_ps), money(shares * risk_c / 100.0),
                                        money(ch["total"]), money(max_loss), money(cap)))
                    min_rr = _f(c.get("min_reward_risk"))
                    if target:
                        if (long and target <= ref + PAISA) or (not long and target >= ref - PAISA):
                            later.append("target: the target %s must be %s the entry %s"
                                         % (_p(target), "above" if long else "below", _p(ref)))
                        else:
                            rr = round(int(round(abs(target - ref) * 100)) / float(risk_c), 2)
                            if min_rr and rr < min_rr - 1e-9:
                                later.append("reward:risk: %.2f (target %s, entry %s, stop %s) is under your "
                                             "minimum %s" % (rr, _p(target), _p(ref), _p(stop), _num(min_rr)))
                    elif min_rr:
                        later.append("reward:risk: no target to measure it against (min_reward_risk %s)"
                                     % _num(min_rr))
            daily = _f(c.get("max_daily_loss_inr"))
            if daily and max_loss is not None:
                used = loss_today + batch_loss
                if used + max_loss > daily + PAISA:
                    later.append("daily loss: %s already at risk today plus %s for this one = %s, over your %s "
                                 "daily loss limit" % (money(used), money(max_loss), money(used + max_loss),
                                                       money(daily)))
            cap_t = int(c.get("max_trades_per_day") or 0)
            if cap_t and len(entries_today) + batch_entries >= cap_t:
                later.append("trades: %d entries placed today plus %d passed earlier in this batch, your limit "
                             "is %d a day" % (len(entries_today), batch_entries, cap_t))
            later.extend(win_reasons)
            if stop and ref and risk_ps:
                info.append("entry %s (%s), stop %s, target %s: risk %s a share"
                            % (_p(ref), REF_WORDS.get(ref_from, ref_from) + (
                                ", %s" % SOURCE_WORDS.get(live_src, live_src) if ref_from == "price" else ""), _p(stop),
                               _p(target) if target else "none", _p(risk_ps)))
                info.append("max loss %s incl. %s charges%s, reward:risk %s%s"
                            % (money(max_loss), money(ch["total"]),
                               (" (limit %s)" % money(c["max_loss_per_trade_inr"]))
                               if _f(c.get("max_loss_per_trade_inr")) else "",
                               ("%.2f" % rr) if rr is not None else "n/a",
                               (" (min %s)" % _num(c["min_reward_risk"])) if _f(c.get("min_reward_risk")) else ""))
            if upper:
                info.append("circuit %s to %s (%s)" % (_p(lower or 0), _p(upper), circuit_src))
            if daily or cap_t:
                info.append("today before this: %d of %s trades, %s of %s daily loss"
                            % (len(entries_today) + batch_entries, cap_t or "unlimited",
                               money(loss_today + batch_loss), money(daily) if daily else "no limit on"))

        # duplicates and the daily order limit
        key = (sym, side)
        if key in seen:
            later.append("duplicate: a second %s %s in this batch" % (side.upper(), sym))
        if key in placed_keys:
            later.append("duplicate: a %s %s was already placed today" % (side.upper(), sym))
        passed_so_far = sum(1 for r in results if r["verdict"] == "PASS")
        cap_n = int(c.get("max_orders_per_day") or 0)
        if cap_n and count_today + passed_so_far >= cap_n:
            later.append("daily limit: %d placed today plus %d passed earlier in this batch, "
                         "your limit is %d a day" % (count_today, passed_so_far, cap_n))
        seen.add(key)

        if kind == "entry":
            reasons.extend(later)
        else:
            warns.extend("exit, not blocked: " + x for x in later)
            if win_reasons and product != "MIS":
                warns.append("the market is not open for new orders now; Kite may reject it or take it as AMO")

        verdict = "BLOCK" if reasons else "PASS"
        if verdict == "PASS":
            if kind == "entry" and side in ("buy", "sell"):
                batch_entries += 1
                batch_loss += max_loss or 0.0
            if value is not None and q is not None:
                if side == "buy":
                    acct["cash"] -= value
                    acct["values"][sym] = acct["values"].get(sym, 0.0) + value
                    acct["shares"][sym] = held + q
                elif side == "sell":
                    acct["cash"] += value
                    acct["values"][sym] = max(acct["values"].get(sym, 0.0) - value, 0.0)
                    acct["shares"][sym] = held - q
            elif q is not None and side == "sell":
                acct["shares"][sym] = held - q
        results.append({
            "index": i, "symbol": sym, "exchange": exch, "side": side, "product": product, "kind": kind,
            "amount": "%s shares" % (_num(q) if q is not None else qty_raw),
            "value_inr": round(value, 2) if value is not None else None,
            "entry_ref": round(ref, 2) if ref else None, "entry_ref_from": ref_from,
            "stop": stop, "target": target, "risk_per_share": risk_ps,
            "charges": ch["total"] if ch else None, "max_loss": max_loss,
            "reward_risk": rr, "upper_circuit": upper, "lower_circuit": lower, "series": series or None,
            "cas": cas, "square_off": rules["square_off"], "session_close": rules["close"],
            "price_source": (live_src if ref_from == "price" else ref_from),
            "circuit_source": circuit_src,
            "fingerprint": fingerprint(o), "verdict": verdict,
            "reasons": reasons, "warnings": warns, "info": info,
        })

    return {
        "date": today,
        "now_ist": now_ist.strftime("%Y-%m-%d %H:%M"),
        "paper": bool(c.get("paper", True)),
        "currency": "INR",
        "results": results,
        "pass": sum(1 for r in results if r["verdict"] == "PASS"),
        "block": sum(1 for r in results if r["verdict"] == "BLOCK"),
        "warnings": top_warn,
        "cash_after": round(acct["cash"], 2),
        "today": {"orders_placed": count_today, "entries_placed": len(entries_today),
                  "loss_at_risk": round(loss_today, 2)},
    }


def report(res):
    lines = []
    for w in res["warnings"]:
        lines.append("WARNING: " + w)
    if res["warnings"]:
        lines.append("")
    for r in res["results"]:
        lines.append("%-5s #%d %s %s:%s %s %s, %s" % (r["verdict"], r["index"], r["side"].upper(), r["exchange"],
                                                     r["symbol"], r["amount"], r["product"], r["kind"]))
        for x in r.get("info", []):
            lines.append("          " + x)
        for x in r["reasons"]:
            lines.append("        - " + x)
        for x in r["warnings"]:
            lines.append("        ~ " + x)
    lines.append("")
    lines.append("%d passed, %d blocked at %s IST. Cash after the passed orders: %s (%s account)."
                 % (res["pass"], res["block"], res["now_ist"], money(res["cash_after"]),
                    "paper" if res["paper"] else "LIVE"))
    if res["block"]:
        lines.append("Blocked orders do not go to the broker. Fix the order or your rules, then check again.")
    else:
        lines.append("Every order passed. india-broker still asks you yes or no for each one.")
    lines.append("Education and tooling, not financial advice.")
    return "\n".join(lines)


def load_prices(pdoc):
    """prices.json -> ({SYM: price}, {SYM: time as naive UTC or None}, {SYM: prev_close}, {SYM: source}).
    Keys like "RELIANCE", "NSE:RELIANCE" or "RELIANCE.NS" all map to RELIANCE."""
    if not isinstance(pdoc, dict):
        return {}, {}, {}, {}
    prices = pdoc.get("prices", pdoc)
    if not isinstance(prices, dict):
        return {}, {}, {}, {}
    out, times, prev, src = {}, {}, {}, {}
    as_of = parse_utc(pdoc.get("as_of"))
    detail = pdoc.get("detail") or {}
    for s, p in prices.items():
        if _f(p) is None:
            continue
        key = str(s).upper()
        sym = key.split(":", 1)[1] if ":" in key else key
        for suf in (".NS", ".BO"):
            if sym.endswith(suf):
                sym = sym[:-3]
        out[sym] = float(p)
        d = detail.get(s) or detail.get(sym) or {}
        times[sym] = parse_utc(d.get("time")) or parse_utc(d.get("fetched")) or as_of
        if _f(d.get("prev_close")):
            prev[sym] = float(d["prev_close"])
        src[sym] = "kite" if str(d.get("source") or pdoc.get("source") or "").lower().startswith("kite") else "yahoo"
    return out, times, prev, src


def main(argv=None):
    ap = argparse.ArgumentParser(description="Check proposed orders against your limits (india-risk).")
    ap.add_argument("--orders", default="trader-in/orders.json")
    ap.add_argument("--config", default="trader-in/config.json")
    ap.add_argument("--portfolio", default="trader-in/portfolio.json",
                    help="your account; if missing, a fresh account funded with the budget")
    ap.add_argument("--prices", default="trader-in/prices.json")
    ap.add_argument("--sec-list", default="trader-in/sec_list.csv",
                    help="NSE sec_list.csv (series and price band), from nsearchives.nseindia.com")
    ap.add_argument("--holidays", default="trader-in/holidays.json", help="cached NSE holiday list")
    ap.add_argument("--state", default="trader-in/state/broker.json", help="what india-broker placed today")
    ap.add_argument("--kite-ltp", metavar="FILE", help="a saved Kite get_ltp / get_quotes answer: the fresh price")
    ap.add_argument("--price", action="append", metavar="SYM=PRICE", help="a price Claude just got from Kite get_ltp")
    ap.add_argument("--kite-instrument", metavar="FILE",
                    help="a saved Kite search_instruments answer: today's circuit limits and tick")
    ap.add_argument("--fo-list", default="trader-in/fo_list.txt",
                    help="NSE F&O symbols (CAS stocks): trader-in/fo_list.txt or fo_mktlots.csv")
    ap.add_argument("--cas", choices=["yes", "no"], help="override: is this a closing-auction (F&O) stock?")
    ap.add_argument("--now", help="pretend it is this IST time, e.g. \"2026-10-19 09:50\"")
    ap.add_argument("--today", help="YYYY-MM-DD (default: today in IST)")
    ap.add_argument("--out", default="trader-in/state/risk.json", help="where the verdicts are recorded")
    ap.add_argument("--no-write", action="store_true", help="do not record the verdicts")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)
    try:
        now = parse_now(a.now)
        if a.today and not re.match(r"^\d{4}-\d{2}-\d{2}$", a.today):
            raise RiskError("--today wants a date like 2026-10-19")
        doc = read_json(a.orders, "orders file (run india-budget first)")
        orders = doc.get("orders", doc) if isinstance(doc, dict) else doc
        if not isinstance(orders, list) or not orders:
            raise RiskError("no orders in %s" % a.orders)
        cfg = read_json(a.config, "config", required=False) or {}
        portfolio = read_json(a.portfolio, "portfolio", required=False)
        pdoc = read_json(a.prices, "prices", required=False) or {}
        prices, times, prev, srcs = load_prices(pdoc)
        kite = {}
        try:
            if a.kite_ltp:
                kite.update(load_kite_ltp(read_json(a.kite_ltp, "Kite get_ltp file")))
            kite.update(parse_price_args(a.price))
        except ValueError as e:
            raise RiskError(str(e))
        for k, v in kite.items():
            if v.get("prev_close"):
                prev.setdefault(k.split(":", 1)[1], v["prev_close"])
        kinst, kday = None, None
        if a.kite_instrument:
            try:
                kinst, kday = load_kite_instrument(a.kite_instrument)
            except (OSError, ValueError) as e:
                raise RiskError("cannot read the Kite instrument file %s: %s" % (a.kite_instrument, e))
        fo = load_fo_list(a.fo_list)[0]
        state = read_json(a.state, "broker state", required=False)
        sec = load_sec_list(a.sec_list) if a.sec_list and os.path.exists(a.sec_list) else None
        hol = load_holidays(read_json(a.holidays, "holidays", required=False))
        res = check(orders, cfg, portfolio, prices, state, a.today, now, times, sec, prev, hol,
                    kite_ltp=kite, kite_inst=kinst, kite_day=kday, fo=fo, cas_flag=a.cas, price_sources=srcs)
        if pdoc.get("as_of"):
            res["prices_as_of"] = pdoc["as_of"]
        if not a.no_write:
            d = os.path.dirname(a.out)
            if d:
                os.makedirs(d, exist_ok=True)
            rec = dict(res)
            rec["checked_at"] = datetime.datetime.now(datetime.timezone.utc).replace(
                microsecond=0, tzinfo=None).strftime("%Y-%m-%dT%H:%M:%SZ")
            rec["orders_file"] = a.orders
            tmp = a.out + ".tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(rec, fh, indent=2, ensure_ascii=False)
                fh.write("\n")
            os.replace(tmp, a.out)
    except RiskError as e:
        print("check.py: %s" % e, file=sys.stderr)
        return 1
    print(json.dumps(res, indent=2, ensure_ascii=False) if a.json else report(res))
    return 1 if res["block"] else 0


if __name__ == "__main__":
    sys.exit(main())
