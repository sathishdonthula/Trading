#!/usr/bin/env python3
"""Order tickets and the decision log for india-broker. Standard library only. Python 3.9+.

This tool never talks to a broker and never places anything. It does its
work around the one moment that matters, your yes:

  show      renders each proposed order as a plain-English ticket plus the
            exact Kite MCP calls Claude would make: place_order for the entry,
            then the exit legs (MIS: place_order SL-M + place_order LIMIT;
            CNC: place_gtt_order two-leg). An order is READY only if
            india-risk passed THIS exact order today (matched by fingerprint)
            and nobody has decided on it yet.
  approve   records your explicit yes for ONE order, with your reply word for
            word. It refuses "yes to all", "sure", "ok", a yes that names a
            different stock or amount, and any order that is not READY.
            PAPER ("paper": true, the default): Zerodha has no paper account,
            so approve records a SIMULATED fill here and nothing goes to Kite.
  decline   records a no.
  result    LIVE only: records what happened after Claude's Allow once / Deny
            card: placed (with Kite's order_id), denied, or failed.
  exits     prints the exit-leg calls for a filled entry (with the fill price
            as the GTT last_price for CNC).
  fill      records a fill in trader-in/portfolio.json (cash and shares).
  flatten   builds a SELL ticket that closes a symbol's position by flat_by.
            It still goes through india-risk and your yes.
  log       prints today's decisions.

    python3 ticket.py show
    python3 ticket.py approve --index 1 --reply "yes"
    python3 ticket.py result --index 1 --status placed --broker-id 2610190001 --filled-qty 30 --filled-price 1228.10
    python3 ticket.py fill --symbol RELIANCE --side sell --qty 30 --price 1240.30
    python3 ticket.py flatten --symbol RELIANCE --qty 30
    python3 ticket.py log

Everything is written to trader-in/state/broker.json (append only). Dates are
IST dates; --now "2026-10-19 09:50" pins the clock.
"""

import argparse
import datetime
import hashlib
import json
import math
import os
import re
import sys

# Kite MCP (github.com/zerodha/kite-mcp-server, mcp/post_tools.go; hosted https://mcp.kite.trade/mcp v0.3.2,
# tools/list read 2026-10-23). Same tuples as india-risk/check.py.
KITE_FIELDS = ("variety", "exchange", "tradingsymbol", "transaction_type", "quantity", "product",
               "order_type", "price", "validity", "validity_ttl", "disclosed_quantity", "trigger_price",
               "iceberg_legs", "iceberg_quantity", "tag")
GTT_FIELDS = ("exchange", "tradingsymbol", "transaction_type", "product", "trigger_type",
              "trigger_value", "quantity", "limit_price", "upper_trigger_value", "upper_quantity",
              "upper_limit_price", "lower_trigger_value", "lower_quantity", "lower_limit_price")
GTT_CALL_FIELDS = ("exchange", "tradingsymbol", "last_price", "transaction_type", "product", "trigger_type",
                   "trigger_value", "quantity", "limit_price", "upper_trigger_value", "upper_quantity",
                   "upper_limit_price", "lower_trigger_value", "lower_quantity", "lower_limit_price")
INT_FIELDS = ("quantity", "validity_ttl", "disclosed_quantity", "iceberg_legs", "iceberg_quantity",
              "upper_quantity", "lower_quantity")
VALIDITY_WORDS = {"DAY": "good for today", "IOC": "immediate or cancel", "TTL": "for a few minutes"}
YES_WORDS = ("yes", "y")
BATCH_WORDS = {"all", "every", "everything", "rest", "remaining", "them", "both", "batch", "each"}
DOUBT_WORDS = {"no", "not", "dont", "don't", "wait", "cancel", "stop", "but", "maybe", "later",
               "hold", "unless", "if"}


class TicketError(Exception):
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
        raise TicketError("--now wants a time like \"2026-10-19 09:50\" (IST) or ...Z (UTC)")
    if dt.tzinfo is None:
        return ist_to_utc(dt)
    return (dt - dt.utcoffset()).replace(tzinfo=None)


def ist_iso(now_utc):
    """naive UTC -> "2026-10-19T09:50:00+05:30"."""
    return to_ist(now_utc).replace(microsecond=0).isoformat() + "+05:30"


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


# ---------------------------------------------------------------- ticks and money

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


def _money(x):
    s = inr(x)
    return s[:-3] if s.endswith(".00") else s


def _p(v):
    return "%.2f" % float(v)


def _f(x):
    if isinstance(x, bool):
        return None
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def _n(v):
    """A number for display: 30.0 -> "30", 1227.8 -> "1227.8"."""
    f = float(v)
    return str(int(f)) if f == int(f) else repr(f)


# ---------------------------------------------------------------- files

def read_json(path, what, required=True):
    if not path or not os.path.exists(path):
        if required:
            raise TicketError("no %s at %s" % (what, path))
        return None
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except json.JSONDecodeError as e:
        raise TicketError("%s is not valid JSON: %s" % (path, e))


def write_json(path, data):
    d = os.path.dirname(path)
    if d:
        os.makedirs(d, exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2, ensure_ascii=False)
        fh.write("\n")
    os.replace(tmp, path)


def rec_day(rec):
    return str(rec.get("date") or str(rec.get("at", ""))[:10])


# ---------------------------------------------------------------- the order

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
    """Same function as india-risk/check.py: identifies the exact order (entry + exit legs)."""
    core = {k: order[k] for k in KITE_FIELDS if k in order and order[k] is not None}
    ex = exit_core(order)
    if ex:
        core["exit_plan"] = ex
    blob = json.dumps(core, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]


def _typed(k, v):
    if k in INT_FIELDS:
        return int(float(v))
    if isinstance(v, str) and k not in ("variety", "exchange", "tradingsymbol", "transaction_type", "product",
                                        "order_type", "validity", "tag", "trigger_type"):
        f = _f(v)
        return f if f is not None else v
    return v


def tool_input(order):
    """The exact arguments for Kite MCP place_order: Kite's parameter names, quantity as a whole number,
    prices as numbers. "exit_plan", "why" and "plan" are ours and are never sent."""
    return {k: _typed(k, order[k]) for k in KITE_FIELDS if k in order and order[k] is not None}


def exit_calls(order, last_price=None):
    """[(tool, arguments)] for the exit legs, placed after the entry fills."""
    ep = order.get("exit_plan") if isinstance(order.get("exit_plan"), dict) else None
    if not ep:
        return []
    out = []
    if isinstance(ep.get("gtt"), dict):
        g = dict(ep["gtt"])
        lp = last_price if last_price is not None else g.get("last_price")
        if lp is None:
            lp = _f((order.get("plan") or {}).get("entry"))
        g["last_price"] = lp
        out.append(("place_gtt_order", {k: _typed(k, g[k]) for k in GTT_CALL_FIELDS if g.get(k) is not None}))
    for leg in ("stop_leg", "target_leg"):
        if isinstance(ep.get(leg), dict):
            out.append(("place_order", tool_input(ep[leg])))
    return out


def legs(order):
    """(stop, stop_limit, target) from the exit plan, else the plan."""
    ep = order.get("exit_plan") if isinstance(order.get("exit_plan"), dict) else {}
    plan = order.get("plan") or {}
    if isinstance(ep.get("gtt"), dict):
        g = ep["gtt"]
        return _f(g.get("lower_trigger_value")), _f(g.get("lower_limit_price")), _f(g.get("upper_trigger_value"))
    stop = _f((ep.get("stop_leg") or {}).get("trigger_price")) or _f(plan.get("stop"))
    target = _f((ep.get("target_leg") or {}).get("price")) or _f(plan.get("target"))
    return stop, None, target


def max_loss(order):
    """(rupees lost at the stop incl. charges, stop) from the plan india-budget wrote, or (None, stop)."""
    stop, _, _ = legs(order)
    plan = order.get("plan") or {}
    if str(order.get("order_type", "")).upper() == "LIMIT" and _f(order.get("price")) and stop and \
            _f(plan.get("entry")) and abs(_f(order["price"]) - _f(plan["entry"])) > 0.001:
        q = _f(order.get("quantity")) or 0
        extra = _f(plan.get("charges")) or 0.0
        return round(q * int(round((_f(order["price"]) - stop) * 100)) / 100.0 + extra, 2), stop
    if _f(plan.get("max_loss")) is not None:
        return round(_f(plan["max_loss"]), 2), stop
    return None, stop


def is_entry_with_exits(order):
    return isinstance(order.get("exit_plan"), dict)


def plain_parts(order):
    """The ticket in plain words, one part per line."""
    side = str(order.get("transaction_type", "")).upper()
    sym = order.get("tradingsymbol", "?")
    ex = order.get("exchange", "NSE")
    typ = str(order.get("order_type", "MARKET")).upper()
    product = str(order.get("product", "")).upper()
    val = VALIDITY_WORDS.get(str(order.get("validity", "DAY")).upper(), order.get("validity"))
    q = _n(order.get("quantity", 0))
    if typ == "LIMIT":
        how = "limit %s" % _p(order.get("price"))
    elif typ == "SL":
        how = "stop %s, limit %s" % (_p(order.get("trigger_price")), _p(order.get("price")))
    elif typ == "SL-M":
        how = "stop-market %s" % _p(order.get("trigger_price"))
    else:
        how = "market"
    kind = {"MIS": "intraday (MIS)", "CNC": "delivery (CNC)"}.get(product, product)
    if is_entry_with_exits(order):
        parts = ["%s %s %s:%s, %s, %s, %s" % (side, q, ex, sym, how, kind, val)]
        stop, stop_limit, target = legs(order)
        ep = order["exit_plan"]
        if stop:
            parts.append("stop-loss %s%s" % (_p(stop), (" (GTT, limit %s)" % _p(stop_limit)) if stop_limit else
                                                 " (SL-M, placed after the fill)"))
        if target:
            parts.append("target %s%s" % (_p(target), " (GTT)" if ep.get("gtt") else " (LIMIT, placed after the fill)"))
        loss, st = max_loss(order)
        if loss is not None:
            parts.append("max loss about %s incl. charges if the stop fills at %s (it can fill worse in a fast "
                         "market)" % (_money(loss), _p(st)))
        return parts
    share = "share" if q == "1" else "shares"
    return ["%s %s %s of %s:%s, %s, %s, %s" % (side, q, share, ex, sym, how, kind, val)]


def plain(order):
    return " / ".join(plain_parts(order))


def amount_numbers(order):
    nums = []
    for k in ("quantity", "price", "trigger_price"):
        v = _f(order.get(k))
        if v is not None:
            nums.append(v)
    stop, stop_limit, target = legs(order)
    nums.extend(x for x in (stop, stop_limit, target) if x)
    loss, _ = max_loss(order)
    if loss is not None:
        nums.append(loss)
    return nums


def parse_yes(reply, order, batch_symbols=()):
    """Is this reply an explicit yes for THIS one order? Returns (ok, why)."""
    raw = (reply or "").strip()
    if not raw:
        return False, "the reply is empty"
    if "?" in raw:
        return False, "the reply is a question, not a yes"
    words = re.findall(r"[a-z']+", raw.lower())
    if not words or words[0] not in YES_WORDS:
        return False, "only a reply that starts with \"yes\" counts (not ok, sure, haan, go, do it)"
    if BATCH_WORDS & set(words):
        return False, "a yes covers one order at a time, not a batch"
    if DOUBT_WORDS & set(words[1:]):
        return False, "the reply has a condition or a doubt in it; ask again"
    sym = str(order.get("tradingsymbol", "")).upper()
    tokens = set(re.findall(r"[A-Z][A-Z0-9&\-]{1,19}", raw.upper()))
    others = {str(s).upper() for s in batch_symbols} - {sym}
    named = tokens & others
    if named:
        return False, "the reply names %s, but this order is for %s" % (", ".join(sorted(named)), sym)
    nums = [float(n.replace(",", "")) for n in re.findall(r"\d[\d,]*(?:\.\d+)?", raw)]
    if nums:
        want = amount_numbers(order)
        if not any(abs(n - w) < 0.005 for n in nums for w in want):
            return False, "the reply says %s, but this order is %s" % (", ".join(_n(n) for n in nums), plain(order))
    return True, "explicit yes for %s" % plain(order)


# ---------------------------------------------------------------- state

def load_state(path):
    return read_json(path, "broker state", required=False) or {"orders": []}


def latest(state, fp, today):
    found = None
    for rec in state.get("orders", []):
        if rec.get("fingerprint") == fp and rec_day(rec) == today:
            found = rec
    return found


def risk_verdict(risk, fp, today):
    if not risk:
        return None, "india-risk has not checked these orders (run check.py)"
    if risk.get("date") != today:
        return None, "the risk check is from %s, not today; run check.py again" % risk.get("date")
    for r in risk.get("results", []):
        if r.get("fingerprint") == fp:
            return r, None
    return None, "this exact order was not in the last risk check (it changed or is new); run check.py again"


def status_of(order, risk, state, today):
    """(ready, label, risk_result)"""
    fp = fingerprint(order)
    rv, why = risk_verdict(risk, fp, today)
    if rv is None:
        return False, "NOT READY: " + why, None
    if rv.get("verdict") != "PASS":
        return False, "BLOCKED by india-risk: " + "; ".join(rv.get("reasons", [])), rv
    rec = latest(state, fp, today)
    if rec:
        st = rec.get("status")
        if st == "approved":
            return False, "APPROVED by you at %s, waiting for the place_order call" % rec.get("at"), rv
        return False, "DONE: %s%s at %s" % (st, " (paper)" if rec.get("paper_fill") else "", rec.get("at")), rv
    return True, "READY: india-risk PASS, waiting for your yes", rv


def kind_of(order, rv):
    if rv and rv.get("kind"):
        return rv["kind"]
    return "entry" if str(order.get("transaction_type", "")).upper() == "BUY" else "exit"


# ---------------------------------------------------------------- rendering

def _call_lines(tool, args, indent="    "):
    lines = [indent + tool + " with exactly:"]
    for l in json.dumps(args, indent=2, ensure_ascii=False).splitlines():
        lines.append(indent + "  " + l)
    return lines


def ltp_line(order, kite, paper):
    """The last price for the ticket, with its source. Before a live order Claude fetches Kite get_ltp."""
    row = kite_lookup(kite, order.get("tradingsymbol", ""), order.get("exchange", "NSE"))
    if row:
        return "last price %s (Kite, live, get_ltp)" % _p(row["price"]), row["price"]
    if paper:
        return "last price: none given (paper; a Kite get_ltp or india-alerts quote can be passed with --kite-ltp)", None
    return ("last price: NOT FETCHED. Before this live order, call get_ltp (read only) with [\"%s:%s\"], save it "
            "and show the ticket again with --kite-ltp" % (order.get("exchange", "NSE"), order.get("tradingsymbol"))), None


def _kite(a):
    out = {}
    try:
        if getattr(a, "kite_ltp", None):
            out.update(load_kite_ltp(read_json(a.kite_ltp, "Kite get_ltp file")))
        out.update(parse_price_args(getattr(a, "price", None)))
    except ValueError as e:
        raise TicketError(str(e))
    return out


def render(order, idx, total, cfg, risk, state, today, kite=None):
    ready, label, rv = status_of(order, risk, state, today)
    paper = cfg.get("paper", True)
    acct = "PAPER (simulated here, nothing goes to Zerodha)" if paper else "LIVE ACCOUNT, REAL MONEY"
    lines = ["ORDER %d of %d, %s, Zerodha Kite" % (idx, total, acct)]
    for part in plain_parts(order):
        lines.append("  " + part)
    if order.get("why"):
        lines.append("  why: " + order["why"])
    lines.append("  " + ltp_line(order, kite, paper)[0])
    lines.append("  " + label)
    for w in (rv or {}).get("warnings", []):
        lines.append("  note: " + w)
    if paper:
        lines.append("  Paper: after your yes, ticket.py records a simulated fill. No Kite call is made.")
        lines.append("  These are the calls a live account would make:")
    else:
        lines.append("  Step 1, Claude calls Kite MCP:")
    lines.extend(_call_lines("place_order", tool_input(order)))
    calls = exit_calls(order)
    if calls:
        lines.append("  Step 2, after the entry fills, the exit legs (same yes, each its own Allow once card):")
        for tool, args in calls:
            lines.extend(_call_lines(tool, args))
        if order["exit_plan"].get("gtt"):
            lines.append("  The GTT's last_price is the fill price. One leg triggering cancels the other.")
        else:
            lines.append("  Zerodha has no bracket orders: when the stop or the target fills, cancel the other "
                         "one at once (cancel_order).")
    if ready:
        lines.append("  Reply \"yes\" to place this one order, or \"no\" to skip it.")
        if not paper:
            lines.append("  Claude's own card then asks again: Allow once places it, Deny stops it.")
    return "\n".join(lines), ready


def ticket_json(order, idx, cfg, risk, state, today, kite=None):
    ready, label, rv = status_of(order, risk, state, today)
    loss, stop = max_loss(order)
    lt, lp = ltp_line(order, kite, cfg.get("paper", True))
    return {"index": idx, "ready": ready, "status": label, "plain": plain(order),
            "fingerprint": fingerprint(order), "paper": bool(cfg.get("paper", True)),
            "broker": cfg.get("broker", "zerodha"), "tool": "place_order", "tool_input": tool_input(order),
            "exit_calls": [{"tool": t, "tool_input": a} for t, a in exit_calls(order)],
            "why": order.get("why", ""), "max_loss": loss, "stop": stop,
            "last_price": lp, "last_price_source": "Kite, live (get_ltp)" if lp is not None else None}


# ---------------------------------------------------------------- fills -> portfolio

def apply_fill(portfolio_path, cfg, symbol, side, qty, price, date, exchange="NSE", product=None):
    """Record a filled order in trader-in/portfolio.json: cash and shares, with a cost-basis lot per buy
    (oldest lots are relieved first on a sell). Whole shares only."""
    pf = read_json(portfolio_path, "portfolio", required=False)
    if pf is None:
        budget = cfg.get("budget")
        if not budget:
            raise TicketError("no %s yet and no budget in the config to start it from" % portfolio_path)
        pf = {"currency": "INR", "cash": float(budget), "positions": []}
    symbol = symbol.upper()
    qty, price = float(qty), float(price)
    if qty <= 0 or price <= 0 or qty != int(qty):
        raise TicketError("a fill needs a positive whole-share qty and a positive price")
    qty = int(qty)
    cost = round(qty * price, 2)
    pos = None
    for p in pf.setdefault("positions", []):
        if str(p.get("ticker", "")).upper() == symbol:
            pos = p
    if side == "buy":
        if pos is None:
            pos = {"ticker": symbol, "exchange": exchange, "product": product or cfg.get("product", "MIS"),
                   "shares": 0, "lots": []}
            pf["positions"].append(pos)
        lots = pos.setdefault("lots", [])
        lots.append({"id": "%s-%d" % (symbol, len(lots) + 1), "date": date, "shares": qty, "cost": cost})
        pos["shares"] = int(pos.get("shares", 0)) + qty
        pf["cash"] = round(float(pf.get("cash", 0)) - cost, 2)
    else:
        if pos is None or float(pos.get("shares", 0)) < qty:
            raise TicketError("cannot record selling %d %s: the portfolio holds %s"
                              % (qty, symbol, pos.get("shares", 0) if pos else 0))
        left = qty
        for lot in sorted(pos.get("lots", []), key=lambda l: l.get("date", "")):
            if left <= 0:
                break
            take = min(left, int(lot["shares"]))
            per = float(lot["cost"]) / float(lot["shares"]) if float(lot["shares"]) else 0
            lot["cost"] = round(float(lot["cost"]) - per * take, 2)
            lot["shares"] = int(lot["shares"]) - take
            left -= take
        pos["lots"] = [l for l in pos.get("lots", []) if int(l["shares"]) > 0]
        pos["shares"] = int(pos["shares"]) - qty
        if pos["shares"] <= 0:
            pf["positions"].remove(pos)
        pf["cash"] = round(float(pf.get("cash", 0)) + cost, 2)
    write_json(portfolio_path, pf)
    return pf


def log_fill(state_path, now, today, symbol, side, qty, price, paper=False):
    state = load_state(state_path)
    rec = {"at": ist_iso(now), "date": today, "status": "filled", "symbol": symbol.upper(), "side": side,
           "qty": int(float(qty)), "price": float(price),
           "plain": "%s %s %s at %s" % (side.upper(), _n(qty), symbol.upper(), _p(price))}
    if paper:
        rec["paper_fill"] = True
    state.setdefault("orders", []).append(rec)
    write_json(state_path, state)


# ---------------------------------------------------------------- commands

def _clock(a):
    now = parse_now(getattr(a, "now", None))
    today = getattr(a, "today", None) or to_ist(now).date().isoformat()
    if not re.match(r"^\d{4}-\d{2}-\d{2}$", today):
        raise TicketError("--today wants a date like 2026-10-19")
    return now, today


def _load(a):
    doc = read_json(a.orders, "orders file (run india-budget, or ticket.py flatten, first)")
    orders = doc.get("orders", doc) if isinstance(doc, dict) else doc
    if not isinstance(orders, list) or not orders:
        raise TicketError("no orders in %s" % a.orders)
    cfg = read_json(a.config, "config", required=False) or {}
    risk = read_json(a.risk, "risk check", required=False)
    state = load_state(a.state)
    now, today = _clock(a)
    return orders, cfg, risk, state, today, now


def _pick(orders, index):
    if index is None:
        raise TicketError("say which order: --index N")
    if not 1 <= index <= len(orders):
        raise TicketError("there is no order %d (there are %d)" % (index, len(orders)))
    return orders[index - 1]


def cmd_show(a):
    orders, cfg, risk, state, today, now = _load(a)
    picks = [(a.index, _pick(orders, a.index))] if a.index else list(enumerate(orders, 1))
    kite = _kite(a)
    if a.json:
        out = [ticket_json(o, i, cfg, risk, state, today, kite) for i, o in picks]
        print(json.dumps(out[0] if a.index else out, indent=2, ensure_ascii=False))
        return 0 if any(t["ready"] for t in out) else 1
    blocks, any_ready = [], False
    for i, o in picks:
        text, ready = render(o, i, len(orders), cfg, risk, state, today, kite)
        blocks.append(text)
        any_ready = any_ready or ready
    print("\n\n".join(blocks))
    print("\nOne order at a time: a yes covers one ticket, never the batch. Education and tooling, "
          "not financial advice.")
    return 0 if any_ready else 1


def _record(order, a, now, today, rv, cfg, status, extra=None):
    loss, stop = max_loss(order)
    rec = {"at": ist_iso(now), "date": today, "index": a.index, "fingerprint": fingerprint(order),
           "symbol": str(order.get("tradingsymbol", "")).upper(), "exchange": order.get("exchange", "NSE"),
           "side": str(order.get("transaction_type", "")).lower(), "qty": order.get("quantity"),
           "product": order.get("product"), "kind": kind_of(order, rv), "max_loss": loss, "stop": stop,
           "plain": plain(order), "status": status, "paper": bool(cfg.get("paper", True)),
           "broker": cfg.get("broker", "zerodha")}
    rec.update(extra or {})
    return rec


def cmd_approve(a):
    orders, cfg, risk, state, today, now = _load(a)
    order = _pick(orders, a.index)
    ready, label, rv = status_of(order, risk, state, today)
    if not ready:
        raise TicketError("order %d is not ready: %s" % (a.index, label))
    ok, why = parse_yes(a.reply, order, [o.get("tradingsymbol", "") for o in orders])
    if not ok:
        print("Not recorded: %s. Ask the user again with the ticket for order %d." % (why, a.index))
        return 1
    state.setdefault("orders", []).append(_record(order, a, now, today, rv, cfg, "approved", {"reply": a.reply}))
    kite = _kite(a)
    if cfg.get("paper", True):
        price, src = _f(a.fill_price), "typed in"
        if price is None and str(order.get("order_type")).upper() == "LIMIT":
            price, src = _f(order.get("price")), "the limit price"
        if price is None:
            row = kite_lookup(kite, order.get("tradingsymbol", ""), order.get("exchange", "NSE"))
            if row:
                price, src = row["price"], "Kite, live"
        if price is None:
            price, src = _f((order.get("plan") or {}).get("entry")) or (rv or {}).get("entry_ref"), "the plan's entry"
        if not price:
            raise TicketError("paper fill needs a price: pass --fill-price (Kite get_ltp, or india-alerts quote)")
        price = round_to_tick(price)
        state["orders"].append(_record(order, a, now, today, rv, cfg, "placed",
                                       {"paper_fill": True, "broker_order_id": "PAPER-%s-%d" % (today, a.index)}))
        write_json(a.state, state)
        side = str(order.get("transaction_type", "")).lower()
        apply_fill(a.portfolio, cfg, order["tradingsymbol"], side, order["quantity"], price, today,
                   order.get("exchange", "NSE"), order.get("product"))
        log_fill(a.state, now, today, order["tradingsymbol"], side, order["quantity"], price, paper=True)
        print("Recorded your yes for order %d: %s." % (a.index, plain(order)))
        print("PAPER: simulated fill, %s %s %s at %s (%s), recorded in %s. Nothing was sent to Zerodha."
              % (side.upper(), _n(order["quantity"]), order["tradingsymbol"], _p(price), src, a.portfolio))
        if is_entry_with_exits(order):
            stop, _, target = legs(order)
            print("The paper stop %s and target %s are watched by india-alerts (alerts.py add --from-plan). "
                  "When one rings, record it: ticket.py fill --symbol %s --side sell --qty %s --price P"
                  % (_p(stop), _p(target), order["tradingsymbol"], _n(order["quantity"])))
        return 0
    write_json(a.state, state)
    print("Recorded your yes for order %d: %s." % (a.index, plain(order)))
    lt = ltp_line(order, kite, False)[0]
    print(lt[0].upper() + lt[1:] + ".")
    print("Now call Kite MCP place_order with exactly:")
    print(json.dumps(tool_input(order), indent=2, ensure_ascii=False))
    print("Claude's card shows Allow once / Deny. Then: ticket.py result --index %d --status "
          "placed|denied|failed --broker-id ORDER_ID --filled-qty Q --filled-price P" % a.index)
    return 0


def cmd_decline(a):
    orders, cfg, risk, state, today, now = _load(a)
    order = _pick(orders, a.index)
    state.setdefault("orders", []).append({
        "at": ist_iso(now), "date": today, "index": a.index, "fingerprint": fingerprint(order),
        "symbol": str(order.get("tradingsymbol", "")).upper(),
        "side": str(order.get("transaction_type", "")).lower(), "plain": plain(order),
        "status": "declined", "reply": a.reply or "no"})
    write_json(a.state, state)
    print("Recorded: you said no to order %d (%s). Nothing was sent." % (a.index, plain(order)))
    return 0


def print_exits(order, last_price):
    calls = exit_calls(order, last_price)
    if not calls:
        return
    print("Now the exit legs. Each is its own Kite call with its own Allow once card:")
    for tool, args in calls:
        print("  %s with exactly:" % tool)
        for l in json.dumps(args, indent=2, ensure_ascii=False).splitlines():
            print("    " + l)
    if not order["exit_plan"].get("gtt"):
        print("When one of them fills, cancel the other at once: get_orders, then cancel_order "
              "(variety regular, its order_id).")


def cmd_result(a):
    orders, cfg, risk, state, today, now = _load(a)
    order = _pick(orders, a.index)
    if cfg.get("paper", True):
        raise TicketError("paper account: approve already recorded the simulated fill; there is no Kite result")
    fp = fingerprint(order)
    rec = latest(state, fp, today)
    if not rec or rec.get("status") != "approved":
        raise TicketError("order %d has no yes recorded today; a result can only follow an approval" % a.index)
    new = {"at": ist_iso(now), "date": today, "index": a.index, "fingerprint": fp, "symbol": rec["symbol"],
           "side": rec["side"], "plain": rec["plain"], "status": a.status}
    for k in ("exchange", "qty", "product", "kind", "max_loss", "stop", "paper", "broker"):
        if rec.get(k) is not None:
            new[k] = rec[k]
    if a.broker_id:
        new["broker_order_id"] = a.broker_id
    if a.note:
        new["note"] = a.note
    state["orders"].append(new)
    write_json(a.state, state)
    print("Recorded order %d as %s." % (a.index, a.status))
    if a.status == "placed" and a.filled_qty and a.filled_price:
        apply_fill(a.portfolio, cfg, rec["symbol"], rec["side"], a.filled_qty, a.filled_price, today,
                   rec.get("exchange", "NSE"), rec.get("product"))
        log_fill(a.state, now, today, rec["symbol"], rec["side"], a.filled_qty, a.filled_price)
        print("Added the fill to %s." % a.portfolio)
        if rec.get("kind") == "entry":
            print_exits(order, _f(a.filled_price))
    elif a.status == "placed":
        print("When it fills (get_order_history %s), record it: ticket.py result again with --filled-qty and "
              "--filled-price, or ticket.py fill" % (a.broker_id or "ORDER_ID"))
    return 0


def cmd_exits(a):
    orders, cfg, risk, state, today, now = _load(a)
    order = _pick(orders, a.index)
    if not is_entry_with_exits(order):
        raise TicketError("order %d has no exit plan" % a.index)
    if cfg.get("paper", True):
        stop, _, target = legs(order)
        print("PAPER: no Kite calls. The stop %s and target %s are simulated: india-alerts watches them; "
              "record the one that hits with ticket.py fill." % (_p(stop), _p(target)))
        return 0
    print_exits(order, _f(a.last_price))
    return 0


def cmd_fill(a):
    cfg = read_json(a.config, "config", required=False) or {}
    now = parse_now(a.now)
    date = a.date or to_ist(now).date().isoformat()
    if not re.match(r"^\d{4}-\d{2}-\d{2}$", date):
        raise TicketError("--date wants a date like 2026-10-19")
    pf = apply_fill(a.portfolio, cfg, a.symbol, a.side, a.qty, a.price, date, a.exchange)
    log_fill(a.state, now, date, a.symbol, a.side, a.qty, a.price, paper=bool(cfg.get("paper", True)))
    print("Recorded %s %s %s at %s on %s in %s. Cash now %s." % (a.side, a.qty, a.symbol.upper(), _p(a.price),
                                                                date, a.portfolio, _money(pf["cash"])))
    return 0


def cmd_flatten(a):
    cfg = read_json(a.config, "config", required=False) or {}
    sym = a.symbol.strip().upper()
    now, today = _clock(a)
    source = "the broker, get_positions"
    pf = read_json(a.portfolio, "portfolio", required=False) or {}
    held = [p for p in pf.get("positions", [])
            if str(p.get("ticker") or p.get("symbol") or "").upper() == sym]
    if a.qty is not None:
        qty = _f(a.qty)
        if qty is None or qty <= 0 or qty != int(qty):
            raise TicketError("--qty wants the whole number of shares you hold, like --qty 30")
    else:
        qty = sum(_f(p.get("shares", p.get("qty"))) or 0 for p in held)
        source = a.portfolio
        if qty <= 0:
            raise TicketError("nothing to flatten: no %s shares in %s. Check the broker (get_positions) and "
                              "pass --qty if you do hold some" % (sym, a.portfolio))
    product = (a.product or (held[0].get("product") if held else None) or cfg.get("product") or "MIS").upper()
    exchange = (a.exchange or (held[0].get("exchange") if held else None) or "NSE").upper()
    plan = read_json(a.plan, "plan", required=False) or {}
    plan = plan if str(plan.get("symbol", "")).upper() == sym else {}
    cas, cas_from = cas_status(sym, plan, cfg, load_fo_list(a.fo_list)[0], a.cas)
    rules = session_rules(cas, today)
    flat_by, fw = resolve_flat_by(plan.get("flat_by") or cfg.get("flat_by"), rules)
    order = {"variety": "regular", "exchange": exchange, "tradingsymbol": sym, "transaction_type": "SELL",
             "quantity": int(qty), "product": product, "order_type": "MARKET", "validity": "DAY",
             "tag": "indiaflatten",
             "why": "flat by %s IST: closes the %s day trade before Zerodha's %s MIS square-off (qty from %s)"
                    % (flat_by, sym, rules["square_off"], source),
             "plan": {"symbol": sym, "exchange": exchange, "cas": cas, "square_off": rules["square_off"],
                      "session_close": rules["close"], "flat_by": flat_by}}
    old = read_json(a.orders, "orders", required=False)
    replaced = len(old.get("orders", [])) if isinstance(old, dict) else 0
    doc = {"created_by": "india-broker", "created_at": now.strftime("%Y-%m-%dT%H:%M:%SZ"), "status": "proposed",
           "paper": bool(cfg.get("paper", True)), "broker": "zerodha", "currency": "INR",
           "note": "Flatten ticket. Nothing is placed until india-risk passes it and you say yes in the chat.",
           "source": "ticket.py flatten %s on %s" % (sym, today), "orders": [order]}
    write_json(a.orders, doc)
    print("Flatten %s: %s (flat by %s IST)." % (sym, plain(order), flat_by))
    print("Session: %s (CAS status from %s)." % (rules["label"], cas_from))
    if fw:
        print("Note: " + fw)
    print("Wrote it to %s%s." % (a.orders, (" (it replaced %d earlier proposal%s)"
                                             % (replaced, "" if replaced == 1 else "s")) if replaced else ""))
    if a.qty is None:
        print("The qty comes from %s. Check it against get_positions before you say yes." % source)
    print("Next:")
    print("  1. india-risk: check.py. A sell that only closes what you hold is never blocked by the daily")
    print("     limits or the entry window (only by Zerodha's %s MIS square-off for %s, which closes it anyway)."
          % (rules["square_off"], sym))
    print("  2. ticket.py show, then the user's yes for this one order.")
    if product == "CNC":
        print("  3. Delete the open GTT first (get_gtts, then delete_gtt_order, Needs approval), so it cannot")
        print("     sell the same shares again. Then place_order with the ticket.")
    else:
        print("  3. Cancel the open stop and target legs first (get_orders, then cancel_order for each,")
        print("     Needs approval), so they cannot sell the same shares again. Then place_order with the ticket.")
    return 0


def cmd_log(a):
    state = load_state(a.state)
    now, today = _clock(a)
    rows = [r for r in state.get("orders", []) if a.all or rec_day(r) == today]
    if not rows:
        print("No decisions recorded%s." % ("" if a.all else " today"))
        return 1
    for r in rows:
        extra = (" id " + str(r["broker_order_id"])) if r.get("broker_order_id") else ""
        reply = (' reply "%s"' % r["reply"]) if r.get("reply") and r["status"] in ("approved", "declined") else ""
        loss = (" max loss %s" % _money(r["max_loss"])) if r.get("max_loss") is not None and r["status"] == "placed" \
            else ""
        idx = ("#%s" % r["index"]) if r.get("index") is not None else "  "
        paper = " (paper)" if r.get("paper_fill") else ""
        print("%s  %s %-9s %s%s%s%s%s" % (r.get("at"), idx, r.get("status"), r.get("plain"), paper, reply, loss,
                                         extra))
    placed = sum(1 for r in rows if r.get("status") == "placed")
    print("%d placed%s." % (placed, "" if a.all else " today"))
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(description="Order tickets and your yes (india-broker). Never places orders.")
    sub = ap.add_subparsers(dest="cmd")

    def clock(p):
        p.add_argument("--now", help="pretend it is this IST time, e.g. \"2026-10-19 09:50\"")
        p.add_argument("--today", help="YYYY-MM-DD (default: today in IST)")

    def common(p):
        p.add_argument("--orders", default="trader-in/orders.json")
        p.add_argument("--config", default="trader-in/config.json")
        p.add_argument("--risk", default="trader-in/state/risk.json")
        p.add_argument("--state", default="trader-in/state/broker.json")
        clock(p)

    def kite_args(p):
        p.add_argument("--kite-ltp", metavar="FILE", help="a saved Kite get_ltp answer: the ticket's last price")
        p.add_argument("--price", action="append", metavar="SYM=PRICE", help="a price Claude just got from get_ltp")

    p = sub.add_parser("show", help="render the tickets")
    common(p)
    kite_args(p)
    p.add_argument("--index", type=int)
    p.add_argument("--json", action="store_true")
    p = sub.add_parser("approve", help="record the user's explicit yes for one order")
    common(p)
    p.add_argument("--index", type=int, required=True)
    p.add_argument("--reply", required=True, help="the user's reply, word for word")
    kite_args(p)
    p.add_argument("--fill-price", help="paper only: the simulated fill price (default: limit or plan entry)")
    p.add_argument("--portfolio", default="trader-in/portfolio.json")
    p = sub.add_parser("decline", help="record a no")
    common(p)
    p.add_argument("--index", type=int, required=True)
    p.add_argument("--reply")
    p = sub.add_parser("result", help="live: record what happened after the Allow once / Deny card")
    common(p)
    p.add_argument("--index", type=int, required=True)
    p.add_argument("--status", required=True, choices=["placed", "denied", "failed"])
    p.add_argument("--broker-id")
    p.add_argument("--note")
    p.add_argument("--filled-qty")
    p.add_argument("--filled-price")
    p.add_argument("--portfolio", default="trader-in/portfolio.json")
    p = sub.add_parser("exits", help="the exit-leg Kite calls for a filled entry")
    common(p)
    p.add_argument("--index", type=int, required=True)
    p.add_argument("--last-price", help="the fill price or get_ltp, for a CNC GTT's last_price")
    p = sub.add_parser("fill", help="record a fill in trader-in/portfolio.json")
    p.add_argument("--symbol", required=True)
    p.add_argument("--exchange", default="NSE", choices=["NSE", "BSE"])
    p.add_argument("--side", required=True, choices=["buy", "sell"])
    p.add_argument("--qty", required=True)
    p.add_argument("--price", required=True)
    p.add_argument("--date", help="YYYY-MM-DD (default: today in IST)")
    p.add_argument("--now")
    p.add_argument("--portfolio", default="trader-in/portfolio.json")
    p.add_argument("--config", default="trader-in/config.json")
    p.add_argument("--state", default="trader-in/state/broker.json")
    p = sub.add_parser("flatten", help="a SELL ticket that closes a symbol's position by flat_by")
    p.add_argument("--symbol", required=True)
    p.add_argument("--qty", help="shares you hold, from get_positions (default: trader-in/portfolio.json)")
    p.add_argument("--product", choices=["MIS", "CNC"])
    p.add_argument("--exchange", choices=["NSE", "BSE"])
    p.add_argument("--orders", default="trader-in/orders.json", help="where the ticket is written")
    p.add_argument("--portfolio", default="trader-in/portfolio.json")
    p.add_argument("--plan", default="trader-in/plan.json")
    p.add_argument("--config", default="trader-in/config.json")
    p.add_argument("--fo-list", default="trader-in/fo_list.txt",
                   help="NSE F&O symbols (CAS stocks): trader-in/fo_list.txt or fo_mktlots.csv")
    p.add_argument("--cas", choices=["yes", "no"], help="override: is this a closing-auction (F&O) stock?")
    clock(p)
    p = sub.add_parser("log", help="today's decisions")
    p.add_argument("--state", default="trader-in/state/broker.json")
    clock(p)
    p.add_argument("--all", action="store_true")
    a = ap.parse_args(argv)
    if not a.cmd:
        ap.print_help()
        return 1
    try:
        return {"show": cmd_show, "approve": cmd_approve, "decline": cmd_decline, "result": cmd_result,
                "exits": cmd_exits, "fill": cmd_fill, "flatten": cmd_flatten, "log": cmd_log}[a.cmd](a)
    except TicketError as e:
        print("ticket.py: %s" % e, file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
