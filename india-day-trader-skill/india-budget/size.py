#!/usr/bin/env python3
"""Position sizing for india-budget. Standard library only, no network. Python 3.9+.

Default mode: sizes ONE long day trade from trader-in/plan.json (written by
india-exit) to your budget and your max loss per trade, then writes one
order proposal to trader-in/orders.json: an ENTRY leg in the exact shape of
Zerodha Kite's place_order tool, plus an EXIT PLAN (a stop leg and a target
leg) that india-broker places after the entry fills. It never places
anything: india-risk checks the order and india-broker asks you yes or no.

    python3 size.py                                  # trader-in/plan.json + trader-in/config.json
    python3 size.py --write                          # write trader-in/orders.json
    python3 size.py --limit                          # a LIMIT order at the plan's entry
    python3 size.py --limit 1227.50 --write          # a LIMIT order at 1227.50
    python3 size.py --now "2026-10-19 09:50" --json  # pin the clock (IST)

How many shares. Whole shares only (Indian equities have no fractions).

    shares = the smallest of
      max loss       the most shares where shares x (entry - stop) PLUS the
                     round-trip charges (buy at the entry, sell at the stop)
                     fit inside max_loss_per_trade_inr
      position cap   budget x max_position_pct / entry
      max order      max_order_inr / entry
      cash           (budget minus the cash buffer) / entry
                     (and no more than the cash in trader-in/portfolio.json)

Margin: MIS can be bought on up to 5x margin at Zerodha. This pack sizes on
cash only unless "allow_margin": true (and even then it sizes on cash).

Prices round to the NSE tick (from Apr 15 2025, reviewed monthly by NSE):
stops round DOWN, a long target rounds DOWN, a limit buy rounds to the
nearest tick.

Weights mode (kept from v1): split a budget across a list of stocks by
weight, whole shares, CNC market buys with no stop (india-risk blocks them
while require_stop is true).

    python3 size.py --weights RELIANCE=40,TCS=30,HDFCBANK=30 --prices trader-in/prices.json
"""

import argparse
import datetime
import json
import math
import os
import re
import sys

# The shipped config.example.json. Missing keys fall back to these.
DEFAULTS = {
    "budget": None,
    "cash_buffer_pct": 10,
    "max_position_pct": 75,
    "max_order_inr": 40000,
    "min_order_inr": 100,
    "max_loss_per_trade_inr": 500,
    "max_daily_loss_inr": 1500,
    "max_trades_per_day": 3,
    "max_orders_per_day": 10,
    "min_reward_risk": 1.0,
    "require_stop": True,
    "no_entries_before": "09:30",
    "no_entries_after": "14:45",
    "flat_by": "auto",
    "product": "MIS",
    "exchange": "NSE",
    "broker": "zerodha",
    "paper": True,
    "blocked": [],
    "allow_options": False,
    "allow_margin": False,
    "allow_short": False,
    "cas_symbols_extra": [],
    "non_cas_symbols": [],
}
EPS = 1e-9
FRESH_MINUTES = 15
ORDER_TAG = "indiabudget"          # Kite: alphanumeric, max 20 characters
GTT_LIMIT_BELOW_PCT = 1.0          # CNC GTT stop leg: limit this far under the trigger so it can fill
SETUP_WORDS = {"orb": "opening-range breakout", "flag": "flag", "bounce": "oversold bounce"}
IST = datetime.timezone(datetime.timedelta(hours=5, minutes=30))


class SizeError(Exception):
    pass


# ---------------------------------------------------------------- time (IST is UTC+05:30, no DST)

def to_ist(utc):
    """naive UTC -> naive IST wall clock."""
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
        raise SizeError("--now wants a time like \"2026-10-19 09:50\" (IST) or ...Z (UTC)")
    if dt.tzinfo is None:
        return ist_to_utc(dt)
    return (dt - dt.utcoffset()).replace(tzinfo=None)


def parse_utc(text):
    """"2026-10-19T04:20:00Z" or "...+05:30" -> naive UTC, or None."""
    if not text:
        return None
    try:
        dt = datetime.datetime.fromisoformat(str(text).strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is not None:
        dt = (dt - dt.utcoffset()).replace(tzinfo=None)
    return dt


def utc_iso(now_utc=None):
    now_utc = now_utc or datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None)
    return now_utc.replace(microsecond=0).strftime("%Y-%m-%dT%H:%M:%SZ")


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
    """₹50,000 for whole rupees, ₹413.84 otherwise."""
    s = inr(x)
    return s[:-3] if s.endswith(".00") else s


def _p(x):
    return "%.2f" % float(x)


def _num(x):
    x = round(float(x), 2)
    if abs(x - round(x)) < 0.005:
        return str(int(round(x)))
    return "%.2f" % x


def paise(x):
    return int(round(float(x) * 100))


# ---------------------------------------------------------------- inputs

def read_json(path, what):
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except FileNotFoundError:
        raise SizeError("no %s at %s" % (what, path))
    except json.JSONDecodeError as e:
        raise SizeError("%s is not valid JSON: %s" % (path, e))


def load_config(path):
    cfg = dict(DEFAULTS)
    if path and os.path.exists(path):
        cfg.update(read_json(path, "config"))
    return cfg


def _f(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def parse_symbol(text, exchange=None):
    """'RELIANCE', 'RELIANCE.NS', 'NSE:RELIANCE', 'RELIANCE.BO', 'BSE:RELIANCE' -> (SYMBOL, EXCHANGE)."""
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
    return s, (ex or "NSE")


def price_keys(sym, exchange):
    return [sym, "%s:%s" % (exchange, sym), sym + (".BO" if exchange == "BSE" else ".NS")]


def price_entry(doc, sym, exchange="NSE"):
    """(price, detail) for a symbol from a prices.json doc, trying RELIANCE, NSE:RELIANCE, RELIANCE.NS."""
    prices = (doc or {}).get("prices") or {}
    detail = (doc or {}).get("detail") or {}
    for k in price_keys(sym, exchange):
        if k in prices:
            return _f(prices[k]), detail.get(k) or {}
    return None, {}


def fresh_price(doc, sym, now_utc, exchange="NSE", minutes=FRESH_MINUTES):
    """(price, time_utc, fresh) for sym. The time is the quote's own time, else when it was fetched,
    else the file's as_of."""
    if not isinstance(doc, dict):
        return None, None, False
    p, det = price_entry(doc, sym, exchange)
    if p is None or p <= 0:
        return None, None, False
    t = parse_utc(det.get("time")) or parse_utc(det.get("fetched")) or parse_utc(doc.get("as_of"))
    if t is None:
        return p, None, False
    age = (now_utc - t).total_seconds()
    return p, t, -60 <= age <= minutes * 60


# ---------------------------------------------------------------- plan mode (the default)

def shares_for(inr_amount, per_share):
    """Whole shares that fit in an amount at per_share, in exact paise."""
    u, p = paise(inr_amount), paise(per_share)
    if p <= 0 or u <= 0:
        return 0
    return u // p


def loss_at_stop(qty, entry, stop, product="MIS", exchange="NSE"):
    """(rupees lost if the stop fills exactly at the stop, the charges dict). Charges: buy at the
    entry, sell at the stop."""
    if qty <= 0:
        return 0.0, charges(0, 0, product, exchange)
    ch = charges(qty * entry, qty * stop, product, exchange)
    return round(qty * (paise(entry) - paise(stop)) / 100.0 + ch["total"], 2), ch


def max_loss_shares(cap, entry, stop, product="MIS", exchange="NSE"):
    """The most whole shares whose loss at the stop, charges included, fits in cap."""
    risk = (paise(entry) - paise(stop)) / 100.0
    if cap is None or cap <= 0 or risk <= 0:
        return 0
    n = int(math.floor(cap / risk + EPS))
    while n > 0 and loss_at_stop(n, entry, stop, product, exchange)[0] > cap + 0.005:
        n -= 1
    return n


def size_trade(plan, cfg, budget=None, limit=None, price=None, cash=None, today=None, price_source="yahoo",
               fo=None, cas_flag=None, kite_inst=None):
    """Pure function: one plan + your rules -> the sizing (a dict). qty may be 0.

    limit  the limit price for a LIMIT order (None = MARKET)
    price  a FRESH latest price; a market order is sized at the higher of it and the plan's entry
    cash   the cash in trader-in/portfolio.json, if there is one
    today  IST date (YYYY-MM-DD); a plan for another day is refused
    price_source  where the fresh price is from: "kite" (Kite, live), "yahoo" (free, can lag), "manual"
    fo / cas_flag  the NSE F&O list (a set) and --cas yes|no: the closing-auction (CAS) session rules
    kite_inst  this symbol's row from a Kite search_instruments file for TODAY (tick, circuit limits)"""
    c = dict(DEFAULTS)
    c.update(cfg or {})
    if not isinstance(plan, dict):
        raise SizeError("the plan is not a JSON object")
    sym, exchange = parse_symbol(plan.get("symbol"), plan.get("exchange") or c.get("exchange"))
    if not sym:
        raise SizeError("the plan has no symbol (run india-exit)")
    if exchange not in ("NSE", "BSE"):
        raise SizeError("this pack trades NSE and BSE equities only; the plan says %r" % exchange)
    product = str(plan.get("product") or c.get("product") or "MIS").upper()
    if product not in ("MIS", "CNC"):
        raise SizeError("product must be MIS (intraday) or CNC (delivery), got %r" % product)
    side = str(plan.get("side") or "buy").strip().lower()
    if side != "buy":
        raise SizeError("this pack sizes long (buy) day trades only; the plan says %r" % side)
    if today and plan.get("date") and str(plan["date"]) != today:
        raise SizeError("the plan is for %s and today is %s in IST: make a new plan with india-exit"
                        % (plan["date"], today))
    entry_plan, stop, target = _f(plan.get("entry")), _f(plan.get("stop")), _f(plan.get("target"))
    for name, v in (("entry", entry_plan), ("stop", stop), ("target", target)):
        if v is None or v <= 0:
            raise SizeError("the plan needs a positive %s (run india-exit); an order needs entry, "
                            "stop and target" % name)
    blocked = {str(b).upper() for b in (c.get("blocked") or [])}
    if sym in blocked:
        raise SizeError("%s is on your blocked list" % sym)
    series = str(plan.get("series") or "").upper()
    if product == "MIS" and series in ("BE", "BZ"):
        raise SizeError("%s is series %s (trade-to-trade): no intraday (MIS) allowed, delivery only"
                        % (sym, series))

    budget = float(budget if budget is not None else (c.get("budget") or 0))
    if budget <= 0:
        raise SizeError("no budget: pass --budget or set \"budget\" in trader-in/config.json")
    buf_pct = float(c.get("cash_buffer_pct") or 0)
    if not 0 <= buf_pct < 100:
        raise SizeError("cash_buffer_pct must be between 0 and 100")
    warns = []
    cas, cas_from = cas_status(sym, plan, c, fo, cas_flag)
    rules = session_rules(cas, str(plan.get("date") or today or datetime.date.today().isoformat()))
    if cas is None:
        warns.append("CAS status of %s unknown (no F&O list): using the stricter CAS rules, Zerodha square-off "
                     "%s" % (sym, rules["square_off"]))
    flat_by, fw = resolve_flat_by(plan.get("flat_by") or c.get("flat_by"), rules)
    if fw:
        warns.append(fw)
    ktick = (kite_inst or {}).get("tick") or None

    if limit is not None:
        ref, ref_from = round_to_tick(limit, "nearest", ktick), "limit"
    elif price is not None and round_to_tick(price, "nearest", ktick) > round_to_tick(entry_plan, "nearest", ktick):
        ref, ref_from = round_to_tick(price, "nearest", ktick), "price"
    else:
        ref, ref_from = round_to_tick(entry_plan, "nearest", ktick), "plan"
    stop, target = round_to_tick(stop, "down", ktick), round_to_tick(target, "down", ktick)
    if stop >= ref:
        raise SizeError("the stop %s is not below the entry %s; a long trade needs its stop underneath"
                        % (_p(stop), _p(ref)))
    if target <= ref:
        raise SizeError("the target %s is not above the entry %s: the move has already happened. "
                        "Skip it or make a new plan with india-exit" % (_p(target), _p(ref)))
    risk_c = paise(ref) - paise(stop)
    reward_c = paise(target) - paise(ref)
    risk = risk_c / 100.0

    cash_buffer = round(budget * buf_pct / 100.0, 2)
    available = budget - cash_buffer
    cash_note = "budget %s minus the %s cash buffer" % (money(budget), money(cash_buffer))
    if cash is not None and cash - cash_buffer < available - 0.005:
        available = cash - cash_buffer
        cash_note = "cash %s in trader-in/portfolio.json minus the %s buffer" % (money(cash), money(cash_buffer))
    available = max(round(available, 2), 0.0)

    limits = []
    max_loss = _f(c.get("max_loss_per_trade_inr"))
    if max_loss:
        n = max_loss_shares(max_loss, ref, stop, product, exchange)
        at_n = loss_at_stop(n, ref, stop, product, exchange)[0]
        limits.append({"name": "max loss", "inr": max_loss, "per": risk, "shares": n,
                       "how": "%s max loss, %s risk a share + charges (%d -> %s)"
                              % (money(max_loss), _p(risk), n, money(at_n))})
    cap_pct = _f(c.get("max_position_pct"))
    if cap_pct:
        cap = round(budget * cap_pct / 100.0, 2)
        limits.append({"name": "position cap", "inr": cap, "per": ref, "shares": shares_for(cap, ref),
                       "how": "%s%% of %s = %s / %s" % (_num(cap_pct), money(budget), money(cap), _p(ref))})
    max_order = _f(c.get("max_order_inr"))
    if max_order:
        limits.append({"name": "max order", "inr": max_order, "per": ref, "shares": shares_for(max_order, ref),
                       "how": "%s / %s" % (money(max_order), _p(ref))})
    limits.append({"name": "cash", "inr": available, "per": ref, "shares": shares_for(available, ref),
                   "how": "%s = %s / %s" % (cash_note, money(available), _p(ref))})

    qty = max(0, min(l["shares"] for l in limits))
    bound = [l["name"] for l in limits if l["shares"] == qty]
    for l in limits:
        l["binds"] = l["name"] == bound[0]
    cost = round(qty * ref, 2)
    loss, ch = loss_at_stop(qty, ref, stop, product, exchange)
    refusal = None
    if qty == 0:
        if bound[0] == "max loss":
            one = loss_at_stop(1, ref, stop, product, exchange)[0]
            refusal = ("one share loses %s at the stop (entry %s minus stop %s, plus charges), more than your "
                       "%s max loss per trade. No order. Skip this trade, or plan a closer stop with india-exit."
                       % (money(one), _p(ref), _p(stop), money(max_loss)))
        else:
            refusal = ("one share of %s costs %s, more than your %s limit (%s). No order."
                       % (sym, money(ref), bound[0], limits[[l["name"] for l in limits].index(bound[0])]["how"]))
    elif _f(c.get("min_reward_risk")) and reward_c < _f(c["min_reward_risk"]) * risk_c - EPS:
        refusal = ("reward:risk %.2f (target %s, entry %s, stop %s) is under your minimum %s, so india-risk "
                   "would block it. No order. Skip it, or make a new plan with india-exit."
                   % (reward_c / float(risk_c), _p(target), _p(ref), _p(stop), _num(c["min_reward_risk"])))
    elif kite_inst and kite_inst.get("upper") and ref >= kite_inst["upper"] * 0.99 - 0.005:
        refusal = ("the entry %s is at or within 1%% of today's upper circuit %s (Kite instrument limits %s-%s): "
                   "india-risk would block it. No order." % (_p(ref), _p(kite_inst["upper"]),
                                                             _p(kite_inst.get("lower") or 0), _p(kite_inst["upper"])))
    elif cost < float(c.get("min_order_inr") or 0) - 0.005:
        refusal = ("%d share%s of %s is %s, under your %s minimum order. No order."
                   % (qty, "" if qty == 1 else "s", sym, money(cost), money(c["min_order_inr"])))
    return {
        "symbol": sym, "exchange": exchange, "product": product, "side": "buy",
        "setup": plan.get("setup", ""), "date": plan.get("date"),
        "entry_time": plan.get("entry_time", ""), "entry_plan": round_to_tick(entry_plan),
        "entry": ref, "entry_from": ref_from, "price_source": price_source if ref_from == "price" else ref_from,
        "stop": stop, "target": target, "tick": ktick or tick_size(ref),
        "tick_from": "Kite instrument" if ktick else "NSE tick table",
        "circuit": ({"lower": kite_inst.get("lower"), "upper": kite_inst.get("upper"), "source": "Kite instrument"}
                    if kite_inst and kite_inst.get("upper") else None),
        "cas": cas, "cas_from": cas_from, "session": rules, "warnings": warns,
        "risk_per_share": risk, "reward_risk": round(reward_c / float(risk_c), 2),
        "stop_label": plan.get("stop_label", ""), "target_label": plan.get("target_label", ""),
        "budget": budget, "cash_buffer_pct": buf_pct, "cash_buffer": cash_buffer, "cash_available": available,
        "max_loss_per_trade_inr": max_loss, "limits": limits, "qty": qty, "bound_by": bound[0],
        "cost": cost, "charges": ch, "price_loss": round(qty * risk_c / 100.0, 2), "max_loss": loss,
        "flat_by": flat_by,
        "order_type": "LIMIT" if limit is not None else "MARKET", "refusal": refusal,
    }


def exit_plan(sym, exchange, product, qty, stop, target, tag=ORDER_TAG):
    """The exit legs india-broker places AFTER the entry fills.

    MIS: a SL-M SELL (stop) and a LIMIT SELL (target), two place_order calls; when one fills the other
         must be cancelled (cancel_order), india-alerts watches it.
    CNC: one place_gtt_order two-leg (OCO) SELL: upper = target, lower = stop. A GTT places a LIMIT
         order when triggered, so the stop leg's limit sits 1% under the trigger to give it room to fill."""
    if product == "CNC":
        lower_limit = round_to_tick(stop * (1 - GTT_LIMIT_BELOW_PCT / 100.0), "down")
        return {"kind": "gtt-oco",
                "when": "after the entry fills (CNC): one GTT, two legs, whichever triggers first cancels the other",
                "gtt": {"exchange": exchange, "tradingsymbol": sym, "last_price": None,
                        "transaction_type": "SELL", "product": "CNC", "trigger_type": "two-leg",
                        "upper_trigger_value": target, "upper_quantity": qty, "upper_limit_price": target,
                        "lower_trigger_value": stop, "lower_quantity": qty, "lower_limit_price": lower_limit}}
    leg = {"variety": "regular", "exchange": exchange, "tradingsymbol": sym, "transaction_type": "SELL",
           "quantity": qty, "product": product, "validity": "DAY", "tag": tag}
    stop_leg = dict(leg, order_type="SL-M", trigger_price=stop)
    target_leg = dict(leg, order_type="LIMIT", price=target)
    return {"kind": "sl-m+limit",
            "when": "after the entry fills (MIS): place both; when one fills, cancel the other at once",
            "stop_leg": stop_leg, "target_leg": target_leg}


def entry_order(s):
    """The one proposed order: Kite place_order arguments for the entry, plus exit_plan, why and plan."""
    if s["refusal"] or s["qty"] <= 0:
        return None
    o = {"variety": "regular", "exchange": s["exchange"], "tradingsymbol": s["symbol"],
         "transaction_type": "BUY", "quantity": int(s["qty"]), "product": s["product"],
         "order_type": s["order_type"]}
    if s["order_type"] == "LIMIT":
        o["price"] = s["entry"]
    o.update({"validity": "DAY", "tag": ORDER_TAG})
    setup = SETUP_WORDS.get(s["setup"], s["setup"] or "day trade")
    bound = {"max loss": "%s max loss per trade" % money(s["max_loss_per_trade_inr"] or 0),
             "position cap": "position cap", "max order": "max order",
             "cash": "cash after the buffer"}[s["bound_by"]]
    why = ("%s on %s: %d shares, bound by your %s (%d x %s + %s charges = %s at the stop)"
           % (setup, s["symbol"], s["qty"], bound, s["qty"], _p(s["risk_per_share"]),
              money(s["charges"]["total"]), money(s["max_loss"])))
    o["exit_plan"] = exit_plan(s["symbol"], s["exchange"], s["product"], int(s["qty"]), s["stop"], s["target"])
    o["why"] = why
    o["plan"] = {"symbol": s["symbol"], "exchange": s["exchange"], "currency": "INR", "tick": s["tick"],
                 "entry": s["entry"], "stop": s["stop"], "target": s["target"],
                 "risk_per_share": s["risk_per_share"], "charges": s["charges"]["total"],
                 "max_loss": s["max_loss"], "setup": s["setup"], "date": s["date"],
                 "entry_time": s["entry_time"], "flat_by": s["flat_by"], "cas": s["cas"],
                 "square_off": s["session"]["square_off"], "session_close": s["session"]["close"]}
    return o


def plan_report(s, order, notes=()):
    lines = []
    setup = SETUP_WORDS.get(s["setup"], s["setup"] or "setup")
    lines.append("%s (%s) buy, %s, %s, entry %s%s" % (s["symbol"], s["exchange"], s["product"], setup,
                                                    _p(s["entry_plan"]),
                                                    (" at " + s["entry_time"] + " IST") if s["entry_time"] else ""))
    lines.append("Stop %s%s. Target %s%s. Tick %s." % (
        _p(s["stop"]), (": " + s["stop_label"]) if s["stop_label"] else "", _p(s["target"]),
        (": " + s["target_label"]) if s["target_label"] else "", _num(s["tick"]) if s["tick"] >= 1 else s["tick"]))
    lines.append("Session: %s (CAS status from %s)." % (s["session"]["label"], s["cas_from"]))
    for n in list(notes) + ["Note: " + w for w in s.get("warnings", [])]:
        lines.append(n)
    src = {"plan": "the plan's entry", "price": "the latest price (%s), above the plan's entry"
           % SOURCE_WORDS.get(s.get("price_source"), s.get("price_source")),
           "limit": "your limit price"}[s["entry_from"]]
    lines.append("Sized at %s (%s). Risk %s a share (entry %s minus stop %s). Reward:risk %.2f."
                 % (_p(s["entry"]), src, _p(s["risk_per_share"]), _p(s["entry"]), _p(s["stop"]), s["reward_risk"]))
    lines.append("")
    lines.append("Limits (whole shares; the smallest one wins):")
    for l in s["limits"]:
        lines.append("  %-13s %-62s %4d shares%s" % (l["name"], l["how"], l["shares"],
                                                      "  <- binds" if l["binds"] else ""))
    lines.append("")
    if order is None:
        lines.append("No order: " + s["refusal"])
        lines.append("Education and tooling, not financial advice.")
        return "\n".join(lines)
    ch = s["charges"]
    lines.append("Size: %d shares of %s = %s at %s." % (s["qty"], s["symbol"], money(s["cost"]), _p(s["entry"])))
    lines.append("Charges for the round trip at the stop: %s (brokerage %s, STT %s, exchange %s, SEBI %s, "
                 "stamp %s, GST %s; Zerodha's list as of 2026-10, check yours)."
                 % (money(ch["total"]), money(ch["brokerage"]), money(ch["stt"]), money(ch["exchange"]),
                    money(ch["sebi"]), money(ch["stamp"]), money(ch["gst"])))
    lines.append("Max loss at the stop: %d x %s = %s + %s charges = %s."
                 % (s["qty"], _p(s["risk_per_share"]), money(s["price_loss"]), money(ch["total"]),
                    money(s["max_loss"])))
    lines.append("A stop is an order, not a promise: in a fast market it can fill below %s." % _p(s["stop"]))
    lines.append("")
    lines.append("Proposed entry (Kite place_order arguments, nothing has been placed):")
    how = "LIMIT %s" % _p(order["price"]) if order["order_type"] == "LIMIT" else "MARKET"
    lines.append("  BUY %d %s:%s, %s, %s, DAY" % (order["quantity"], order["exchange"], order["tradingsymbol"],
                                              how, order["product"]))
    ep = order["exit_plan"]
    if ep["kind"] == "gtt-oco":
        g = ep["gtt"]
        lines.append("Exit plan after the fill: one GTT two-leg SELL, target %s, stop trigger %s (limit %s)."
                     % (_p(g["upper_trigger_value"]), _p(g["lower_trigger_value"]), _p(g["lower_limit_price"])))
    else:
        lines.append("Exit plan after the fill: SL-M SELL trigger %s and LIMIT SELL %s. When one fills, "
                     "cancel the other (Zerodha has no bracket orders)." % (_p(ep["stop_leg"]["trigger_price"]),
                                                                             _p(ep["target_leg"]["price"])))
    if s["flat_by"]:
        lines.append("Flat by %s IST: anything still open then gets closed (india-alerts rings, india-broker "
                     "flatten). Zerodha squares off MIS in %s itself at %s for a fee."
                     % (s["flat_by"], s["symbol"], s["session"]["square_off"]))
    lines.append("")
    lines.append("Next: india-risk checks it, then india-broker asks you yes or no.")
    lines.append("Education and tooling, not financial advice.")
    return "\n".join(lines)


# ---------------------------------------------------------------- weights mode (v1)

def parse_weights(data):
    """Accept a weights file, a {symbol: weight} dict or a list of rows -> [{ticker, name, weight}]."""
    if isinstance(data, dict) and "weights" in data:
        data = data["weights"]
    rows = []
    if isinstance(data, dict):
        for t, w in data.items():
            rows.append({"ticker": t, "name": "", "weight": w})
    elif isinstance(data, list):
        for i, r in enumerate(data):
            if not isinstance(r, dict):
                raise SizeError("weights[%d] is not an object" % i)
            t = r.get("ticker") or r.get("symbol")
            w = r.get("weight", r.get("weight_pct", r.get("pct")))
            if not t or w is None:
                raise SizeError("weights[%d] needs a symbol and a weight" % i)
            rows.append({"ticker": t, "name": r.get("name", ""), "weight": w})
    else:
        raise SizeError("weights must be a list or an object")
    out = []
    for r in rows:
        try:
            w = float(r["weight"])
        except (TypeError, ValueError):
            raise SizeError("weight for %s is not a number: %r" % (r["ticker"], r["weight"]))
        out.append({"ticker": parse_symbol(r["ticker"])[0], "name": r["name"] or "", "weight": w})
    return out


def weights_from_arg(text):
    """RELIANCE=40,TCS=30 -> {symbol: weight}. M&M and BAJAJ-AUTO are fine."""
    out = {}
    for part in text.split(","):
        part = part.strip()
        if not part:
            continue
        if "=" not in part:
            raise SizeError("--weights wants SYMBOL=WEIGHT pairs, got %r" % part)
        t, w = part.rsplit("=", 1)
        out[t.strip().upper()] = w.strip()
    return out


def load_prices(path):
    if not path:
        return {}
    data = read_json(path, "prices")
    prices = data.get("prices", data) if isinstance(data, dict) else {}
    out = {}
    for t, p in prices.items():
        try:
            out[parse_symbol(t)[0]] = float(p)
        except (TypeError, ValueError):
            continue
    return out


def allocate(rows, investable, cap):
    """Exact rupees per symbol with the cap enforced; the excess over the cap is shared out again."""
    alloc, capped = {}, set()
    active = list(rows)
    left = investable
    while active:
        wsum = sum(r["weight"] for r in active)
        trial = {r["ticker"]: left * r["weight"] / wsum for r in active}
        over = [r for r in active if trial[r["ticker"]] > cap + EPS]
        if not over:
            alloc.update(trial)
            break
        for r in over:
            alloc[r["ticker"]] = cap
            capped.add(r["ticker"])
            left -= cap
        active = [r for r in active if r["ticker"] not in capped]
        if left <= EPS:
            for r in active:
                alloc[r["ticker"]] = 0.0
            break
    return alloc, capped


def whole_shares(slices, prices, order, cap, cash):
    """Floor each slice, then buy one more share at a time for the stock furthest below its slice
    while cash lasts and the cap holds."""
    shares = {t: int(math.floor(slices[t] / prices[t] + EPS)) if slices[t] > 0 else 0 for t in order}
    left = cash - sum(shares[t] * prices[t] for t in order)
    while True:
        best, gap = None, None
        for t in order:
            p = prices[t]
            if p > left + EPS or (shares[t] + 1) * p > cap + EPS:
                continue
            g = slices[t] - shares[t] * p
            if best is None or g > gap + EPS:
                best, gap = t, g
        if best is None:
            break
        shares[best] += 1
        left -= prices[best]
    return shares


def size(rows, cfg, budget=None, prices=None, top=None):
    """Weights mode, whole shares (no fractions in India). Pure function."""
    c = dict(DEFAULTS)
    c.update(cfg or {})
    budget = float(budget if budget is not None else (c.get("budget") or 0))
    if budget <= 0:
        raise SizeError("no budget: pass --budget or set \"budget\" in trader-in/config.json")
    buf_pct = float(c.get("cash_buffer_pct", 10))
    cap_pct = float(c.get("max_position_pct", 25))
    min_order = float(c.get("min_order_inr", 100) or 0)
    if not 0 <= buf_pct < 100:
        raise SizeError("cash_buffer_pct must be between 0 and 100")
    if not 0 < cap_pct <= 100:
        raise SizeError("max_position_pct must be above 0 and at most 100")
    prices = prices or {}
    blocked = {str(b).upper() for b in (c.get("blocked") or [])}
    dropped, keep, seen = [], [], set()
    for r in rows:
        if r["ticker"] in seen:
            raise SizeError("%s is listed twice in the weights" % r["ticker"])
        seen.add(r["ticker"])
        if r["ticker"] in blocked:
            dropped.append({"ticker": r["ticker"], "reason": "on your blocked list"})
        elif r["weight"] <= 0:
            dropped.append({"ticker": r["ticker"], "reason": "weight is zero or negative"})
        else:
            keep.append(dict(r, source_weight=r["weight"]))
    keep.sort(key=lambda r: -r["weight"])
    if top:
        for r in keep[top:]:
            dropped.append({"ticker": r["ticker"], "reason": "outside the top %d" % top})
        keep = keep[:top]
    if not keep:
        raise SizeError("nothing to size: no weights left after the blocked list")
    missing = [r["ticker"] for r in keep if not prices.get(r["ticker"])]
    if missing:
        raise SizeError("whole shares need a price for: %s (add them to trader-in/prices.json, e.g. with "
                        "india-alerts: alerts.py quote %s --write)" % (", ".join(missing), " ".join(missing)))
    cash_buffer = round(budget * buf_pct / 100.0, 2)
    investable = round(budget - cash_buffer, 2)
    cap = round(budget * cap_pct / 100.0, 2)
    while True:
        tot = sum(r["source_weight"] for r in keep)
        live = [dict(r, weight=100.0 * r["source_weight"] / tot) for r in keep]
        exact, capped = allocate(live, investable, cap)
        order = [r["ticker"] for r in sorted(live, key=lambda r: -exact[r["ticker"]])]
        shares = whole_shares(exact, prices, order, cap, investable)
        rupees = {t: round(shares[t] * prices[t], 2) for t in order}
        small = [r for r in live if rupees[r["ticker"]] < min_order - EPS]
        if not small:
            break
        worst = min(small, key=lambda r: (r["source_weight"], r["ticker"]))
        if shares[worst["ticker"]] == 0:
            reason = "one share costs %s, more than its slice" % money(prices[worst["ticker"]])
        else:
            reason = "its slice (%s) is under your %s minimum order" % (money(rupees[worst["ticker"]]),
                                                                       money(min_order))
        dropped.append({"ticker": worst["ticker"], "reason": reason})
        keep = [r for r in keep if r["ticker"] != worst["ticker"]]
        if not keep:
            raise SizeError("every slice is under your minimum order; raise the budget or hold fewer stocks")
    positions = []
    for r in sorted(live, key=lambda r: -r["weight"]):
        t = r["ticker"]
        positions.append({"ticker": t, "name": r["name"], "weight": round(r["weight"], 2),
                          "source_weight": r["source_weight"], "inr": rupees[t], "price": prices[t],
                          "shares": shares[t], "capped": t in capped})
    invested = round(sum(p["inr"] for p in positions), 2)
    return {"budget": budget, "cash_buffer_pct": buf_pct, "cash_buffer": cash_buffer, "investable": investable,
            "max_position_pct": cap_pct, "cap": cap, "min_order_inr": min_order, "positions": positions,
            "invested": invested, "idle": round(investable - invested, 2), "dropped": dropped}


def orders_from(result, exchange="NSE", source=""):
    """Weights mode: CNC MARKET buys, one per position, in Kite place_order shape. No stop."""
    out = []
    for p in result["positions"]:
        if p["shares"] <= 0:
            continue
        why = "%.2f%% of the split" % p["weight"]
        if source:
            why += ", weights from %s" % source
        if p["capped"]:
            why += "; capped at %s%% of the %s budget" % (_num(result["max_position_pct"]), money(result["budget"]))
        out.append({"variety": "regular", "exchange": exchange, "tradingsymbol": p["ticker"],
                    "transaction_type": "BUY", "quantity": int(p["shares"]), "product": "CNC",
                    "order_type": "MARKET", "validity": "DAY", "tag": ORDER_TAG, "why": why,
                    "plan": {"entry": p["price"]}})
    return out


def report(result, orders, source=""):
    r = result
    lines = []
    if source:
        lines.append("Weights from: %s" % source)
    lines.append("Budget %s, cash buffer %s%% = %s, investing %s, cap %s%% = %s per stock"
                 % (money(r["budget"]), _num(r["cash_buffer_pct"]), money(r["cash_buffer"]),
                    money(r["investable"]), _num(r["max_position_pct"]), money(r["cap"])))
    lines.append("")
    lines.append("%-12s %8s %14s %10s %7s  note" % ("symbol", "weight", "rupees", "price", "shares"))
    for p in r["positions"]:
        lines.append("%-12s %7.2f%% %14s %10s %7d%s" % (p["ticker"], p["weight"], money(p["inr"]), _p(p["price"]),
                                                       p["shares"], "  capped" if p["capped"] else ""))
    lines.append("%-12s %8s %14s" % ("invested", "", money(r["invested"])))
    lines.append("%-12s %8s %14s" % ("cash buffer", "", money(r["cash_buffer"])))
    lines.append("%-12s %8s %14s" % ("left idle", "", money(r["idle"])))
    for d in r["dropped"]:
        lines.append("Dropped %s: %s" % (d["ticker"], d["reason"]))
    lines.append("")
    lines.append("Proposed orders (Kite place_order arguments, nothing has been placed):")
    for o in orders:
        lines.append("  BUY %d %s:%s, MARKET, CNC, DAY" % (o["quantity"], o["exchange"], o["tradingsymbol"]))
    lines.append("")
    lines.append("These buys have no stop. india-risk blocks them while \"require_stop\" is true.")
    lines.append("Next: india-risk checks every order, then india-broker asks you yes or no for each one.")
    lines.append("Education and tooling, not financial advice.")
    return "\n".join(lines)


# ---------------------------------------------------------------- orders file

def write_orders(path, orders, cfg, source="", now_utc=None):
    d = os.path.dirname(path)
    if d:
        os.makedirs(d, exist_ok=True)
    doc = {
        "created_by": "india-budget",
        "created_at": utc_iso(now_utc),
        "status": "proposed",
        "paper": bool(cfg.get("paper", True)),
        "broker": cfg.get("broker", "zerodha"),
        "currency": "INR",
        "note": ("Proposals only. Nothing is placed until india-risk passes each order "
                 "and you say yes to it in the chat."),
        "source": source,
        "orders": orders,
    }
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(doc, fh, indent=2, ensure_ascii=False)
        fh.write("\n")
    os.replace(tmp, path)


# ---------------------------------------------------------------- main

def run_plan(a, cfg):
    now = parse_now(a.now)
    today = to_ist(now).date().isoformat()
    plan = read_json(a.plan, "plan (run india-exit first, or use --weights for a split)")
    limit = None
    if a.limit is not None:
        if a.limit == "plan":
            limit = _f((plan or {}).get("entry"))
        else:
            limit = _f(a.limit)
            if limit is None or limit <= 0:
                raise SizeError("--limit wants a price, like --limit 1227.50")
    sym, ex = parse_symbol((plan or {}).get("symbol"), (plan or {}).get("exchange"))
    notes, price, price_source = [], None, "yahoo"
    kite = {}
    try:
        if a.kite_ltp:
            kite.update(load_kite_ltp(read_json(a.kite_ltp, "Kite get_ltp file")))
        kite.update(parse_price_args(a.price))
    except ValueError as e:
        raise SizeError(str(e))
    krow = kite_lookup(kite, sym, ex) if sym else None
    ppath = a.prices or ("trader-in/prices.json" if os.path.exists("trader-in/prices.json") else None)
    entry_p = _f((plan or {}).get("entry")) or 0
    if krow and limit is None:
        price, price_source = krow["price"], ("kite" if a.kite_ltp else "manual")
        notes.append("%s last %s (%s, just fetched) is %s the plan's entry %s%s." % (
            sym, _p(price), SOURCE_WORDS[price_source], "above" if round_to_tick(price) > round_to_tick(entry_p)
            else "not above", _p(entry_p), ", so the risk is bigger: sized at %s" % _p(round_to_tick(price))
            if round_to_tick(price) > round_to_tick(entry_p) else ": sized at the plan's entry"))
    elif ppath and limit is None and sym:
        pdoc = read_json(ppath, "prices")
        p, t, fresh = fresh_price(pdoc, sym, now, ex)
        det = price_entry(pdoc, sym, ex)[1]
        src = "kite" if str(det.get("source") or pdoc.get("source") or "").lower().startswith("kite") else "yahoo"
        if p is not None and fresh:
            price, price_source = p, src
            if round_to_tick(p) > round_to_tick(entry_p):
                notes.append("%s last %s (%s, as of %s IST) is above the plan's entry %s, so the risk is bigger: "
                             "sized at %s." % (sym, _p(p), SOURCE_WORDS[src], to_ist(t).strftime("%H:%M"),
                                               _p(plan.get("entry")), _p(round_to_tick(p))))
        elif p is not None:
            notes.append("%s in %s is %s: not used (older than %d minutes). Sized at the plan's entry."
                         % (sym, ppath, ("from " + to_ist(t).strftime("%Y-%m-%d %H:%M") + " IST") if t
                            else "undated", FRESH_MINUTES))
    cash = None
    if a.portfolio and os.path.exists(a.portfolio):
        pf = read_json(a.portfolio, "portfolio")
        cash = _f(pf.get("cash")) if isinstance(pf, dict) else None
    fo, fetched = load_fo_list(a.fo_list)
    kinst = None
    if a.kite_instrument:
        try:
            table, kday = load_kite_instrument(a.kite_instrument)
        except (OSError, ValueError) as e:
            raise SizeError("cannot read the Kite instrument file %s: %s" % (a.kite_instrument, e))
        row = kite_lookup(table, sym, ex)
        pday = str((plan or {}).get("date") or today)
        if row and kday == pday:
            kinst = row
            notes.append("Tick %s and circuit %s-%s from Kite (search_instruments, %s)."
                         % (row.get("tick"), _p(row.get("lower") or 0), _p(row.get("upper") or 0), kday))
        elif row:
            notes.append("The Kite instrument file is from %s, the plan is for %s: its circuit limits are for one day "
                         "only, so they are not used (NSE tick table instead)." % (kday, pday))
    s = size_trade(plan, cfg, a.budget, limit, price, cash, today, price_source, fo, a.cas, kinst)
    order = entry_order(s)
    if order and a.write:
        src = "trader-in/plan.json (%s %s %s)" % (s["symbol"], s["setup"] or "plan", s["date"] or today)
        write_orders(a.write, [order], cfg, src, now)
    if a.json:
        print(json.dumps({"sizing": s, "orders": [order] if order else [], "notes": notes}, indent=2,
                         ensure_ascii=False))
    else:
        print(plan_report(s, order, notes))
        if order and a.write:
            print("Wrote 1 proposed order (entry + exit plan) to %s" % a.write)
    return 0 if order else 1


def run_weights(a, cfg):
    source = ""
    if a.weights:
        rows = parse_weights(weights_from_arg(a.weights))
    else:
        data = read_json(a.targets, "weights file")
        rows = parse_weights(data)
        if isinstance(data, dict):
            source = data.get("source", "")
    prices = load_prices(a.prices or ("trader-in/prices.json" if os.path.exists("trader-in/prices.json") else None))
    result = size(rows, cfg, a.budget, prices, a.top)
    orders = orders_from(result, cfg.get("exchange", "NSE"), source)
    if a.write:
        write_orders(a.write, orders, cfg, source, parse_now(a.now))
    if a.json:
        print(json.dumps({"result": result, "orders": orders}, indent=2, ensure_ascii=False))
    else:
        print(report(result, orders, source))
        if a.write:
            print("Wrote %d proposed orders to %s" % (len(orders), a.write))
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(description="Size a day trade to your budget and max loss (india-budget).")
    ap.add_argument("--plan", default="trader-in/plan.json", help="the trade plan (default trader-in/plan.json)")
    ap.add_argument("--config", default="trader-in/config.json", help="rules (default trader-in/config.json)")
    ap.add_argument("--budget", type=float, help="override the budget in the config (rupees)")
    ap.add_argument("--limit", nargs="?", const="plan", metavar="PRICE",
                    help="a LIMIT order instead of MARKET, at the plan's entry or at PRICE")
    ap.add_argument("--prices", help="prices JSON (default trader-in/prices.json if it exists)")
    ap.add_argument("--portfolio", default="trader-in/portfolio.json",
                    help="your account; its cash caps the size if it exists")
    ap.add_argument("--kite-ltp", metavar="FILE", help="a saved Kite get_ltp / get_quotes answer: the fresh price")
    ap.add_argument("--price", action="append", metavar="SYM=PRICE", help="a price Claude just got from Kite get_ltp")
    ap.add_argument("--kite-instrument", metavar="FILE",
                    help="a saved Kite search_instruments answer: today's tick and circuit limits")
    ap.add_argument("--fo-list", default="trader-in/fo_list.txt",
                    help="NSE F&O symbols (CAS stocks): trader-in/fo_list.txt or fo_mktlots.csv")
    ap.add_argument("--cas", choices=["yes", "no"], help="override: is this a closing-auction (F&O) stock?")
    ap.add_argument("--now", help="pretend it is this IST time, e.g. \"2026-10-19 09:50\"")
    ap.add_argument("--write", nargs="?", const="trader-in/orders.json", metavar="PATH",
                    help="write the proposed order (default trader-in/orders.json)")
    ap.add_argument("--json", action="store_true", help="print JSON instead of the report")
    g = ap.add_argument_group("weights mode (split a budget across a list)")
    g.add_argument("--weights", help="inline weights: RELIANCE=40,TCS=30")
    g.add_argument("--targets", help="a weights file: {\"weights\": [{\"symbol\", \"weight\"}]}")
    g.add_argument("--top", type=int, help="keep only the N biggest weights")
    a = ap.parse_args(argv)
    try:
        cfg = load_config(a.config)
        if a.weights or a.targets:
            return run_weights(a, cfg)
        if a.top:
            raise SizeError("--top works with --weights or --targets")
        return run_plan(a, cfg)
    except SizeError as e:
        print("size.py: %s" % e, file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
