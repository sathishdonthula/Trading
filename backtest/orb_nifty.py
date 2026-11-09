#!/usr/bin/env python3
"""NIFTY 15-minute opening-range-breakout backtest (index points).

Rules (one trade a day):
  * Range = high/low of 09:15-09:30 (the first three 5-min bars).
  * From the 09:30 bar on, the first 5-min bar that CLOSES above the range high
    -> buy a call (long); closes below the range low -> buy a put (short).
    Entry at that bar's close. No new entries at or after the cutoff.
  * Stop 60 index points, target 120 index points, checked from the next bar.
    A bar that touches both counts as the stop (conservative).
  * Otherwise exit at the close of the bar that ends at the cutoff (15:00).
  * P&L = index points x lot size x delta, minus charges per round trip.
    delta 1.0 = futures-like; ~0.5 is closer to an ATM option.

Input CSV: datetime,open,high,low,close  (IST, 5-minute bars, bar start time).
Usage: python3 orb_nifty.py nifty_5m.csv [--lot 65] [--delta 1.0] [--charges 70]
"""
import argparse, csv, datetime as dt
from collections import defaultdict


def load(path):
    days = defaultdict(list)
    with open(path, newline="") as f:
        for r in csv.DictReader(f):
            k = {c.lower().strip(): v for c, v in r.items()}
            ts = k.get("datetime") or k.get("date") or k.get("timestamp")
            t = dt.datetime.fromisoformat(ts.strip().replace("T", " ")[:19])
            days[t.date()].append((t, float(k["open"]), float(k["high"]), float(k["low"]), float(k["close"])))
    for d in days:
        days[d].sort()
    return dict(sorted(days.items()))


def trade_day(bars, sl, tgt, cutoff):
    rng = [b for b in bars if dt.time(9, 15) <= b[0].time() < dt.time(9, 30)]
    if len(rng) < 3:
        return None
    hi, lo = max(b[2] for b in rng), min(b[3] for b in rng)
    rest = [b for b in bars if b[0].time() >= dt.time(9, 30)]
    last_ok = (dt.datetime.combine(dt.date.min, cutoff) - dt.timedelta(minutes=5)).time()  # bar ending at cutoff
    for i, b in enumerate(rest):
        if b[0].time() >= last_ok:
            return {"side": None}
        side = 1 if b[4] > hi else -1 if b[4] < lo else 0
        if not side:
            continue
        entry, stop, target = b[4], b[4] - side * sl, b[4] + side * tgt
        for c in rest[i + 1:]:
            low_hit = c[3] <= stop if side == 1 else c[2] >= stop
            tgt_hit = c[2] >= target if side == 1 else c[3] <= target
            if low_hit:
                return dict(side=side, entry=entry, time=b[0], exit="stop", pts=-sl)
            if tgt_hit:
                return dict(side=side, entry=entry, time=b[0], exit="target", pts=tgt)
            if c[0].time() >= last_ok:
                return dict(side=side, entry=entry, time=b[0], exit="15:00", pts=side * (c[4] - entry))
        return dict(side=side, entry=entry, time=b[0], exit="eod", pts=side * (rest[-1][4] - entry))
    return {"side": None}


def summarize(name, trades, lot, delta, charges):
    t = [x for x in trades if x["side"]]
    if not t:
        return f"{name:<8} no trades"
    pts = sum(x["pts"] for x in t)
    gross = pts * lot * delta
    net = gross - charges * len(t)
    w = sum(1 for x in t if x["pts"] > 0)
    peak = dd = run = 0.0
    for x in t:
        run += x["pts"] * lot * delta - charges
        peak = max(peak, run); dd = min(dd, run - peak)
    n = lambda k: sum(1 for x in t if x["exit"] == k)
    return (f"{name:<8} trades {len(t):>4} | target {n('target'):>3} stop {n('stop'):>3} 15:00 {n('15:00')+n('eod'):>3} "
            f"| win {100*w/len(t):5.1f}% | points {pts:+9.1f} | gross ₹{gross:+12,.0f} | net ₹{net:+12,.0f} | max DD ₹{dd:,.0f}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("csv")
    ap.add_argument("--sl", type=float, default=60)
    ap.add_argument("--target", type=float, default=120)
    ap.add_argument("--cutoff", default="15:00")
    ap.add_argument("--lot", type=int, default=65)
    ap.add_argument("--delta", type=float, default=1.0)
    ap.add_argument("--charges", type=float, default=70.0, help="₹ per round trip")
    ap.add_argument("--trades", action="store_true", help="print every trade")
    a = ap.parse_args()
    cutoff = dt.time.fromisoformat(a.cutoff)
    days = load(a.csv)
    res = []
    for d, bars in days.items():
        r = trade_day(bars, a.sl, a.target, cutoff)
        if r:
            r["date"] = d
            res.append(r)
    end = max(days)
    print(f"NIFTY ORB 09:15-09:30 | SL {a.sl:g} / target {a.target:g} pts | cutoff {a.cutoff} | "
          f"lot {a.lot} x delta {a.delta:g} | charges ₹{a.charges:g}/trade | data {min(days)} → {end}")
    for name, days_back in [("1 month", 30), ("6 months", 182), ("1 year", 365), ("5 years", 1826)]:
        start = end - dt.timedelta(days=days_back)
        part = [r for r in res if r["date"] > start]
        cover = "" if min(days) <= start else "  (data starts later: partial)"
        print(summarize(name, part, a.lot, a.delta, a.charges) + cover)
    if a.trades:
        for r in res:
            if r["side"]:
                print(r["date"], "CALL" if r["side"] == 1 else "PUT ", r["time"].time(), r["entry"], r["exit"], f"{r['pts']:+.2f}")


if __name__ == "__main__":
    main()
