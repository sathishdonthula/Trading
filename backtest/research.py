#!/usr/bin/env python3
"""Pattern study on NIFTY 5-min bars: which intraday rules hold up out of sample?

Train = up to 2024-12-31, Test = 2025-01-01 onward. Results in NIFTY points per
trade after a cost of COST points (about 2 index points ~ charges + slippage on 1 lot).
Every variant: one trade a day max, exit by the cutoff.
"""
import csv, datetime as dt, itertools, sys
from collections import defaultdict

COST = 2.0
SPLIT = dt.date(2025, 1, 1)

def load(path):
    days = defaultdict(list)
    for r in csv.DictReader(open(path)):
        t = dt.datetime.fromisoformat(r["datetime"])
        days[t.date()].append((t.time(), float(r["open"]), float(r["high"]), float(r["low"]), float(r["close"])))
    return {d: sorted(b) for d, b in days.items() if len(b) >= 70}  # skip special/short sessions

def T(h, m): return dt.time(h, m)

def run_day(bars, prev_close, orb_min, entry, side_ok, stop_kind, stop_pts, tgt_r, trail, cutoff, gapf):
    end_or = (dt.datetime(2000,1,1,9,15) + dt.timedelta(minutes=orb_min)).time()
    rng = [b for b in bars if b[0] < end_or]
    hi, lo = max(b[2] for b in rng), min(b[3] for b in rng)
    if not prev_close: return None
    gap = bars[0][1] - prev_close
    if gapf == "gapdown_only" and gap > -0.1 * prev_close / 100: return None   # needs >0.1% gap down
    if gapf == "no_big_gap" and abs(gap) > 0.5 * prev_close / 100: return None
    rest = [b for b in bars if b[0] >= end_or and b[0] < cutoff]
    last = None
    for i, b in enumerate(rest):
        c = b[4]
        sig = 0
        if entry == "close":                       # first close beyond the range
            sig = 1 if c > hi else -1 if c < lo else 0
        elif entry == "confirm2":                  # two consecutive closes beyond
            if i and ((c > hi and rest[i-1][4] > hi) or (c < lo and rest[i-1][4] < lo)):
                sig = 1 if c > hi else -1
        elif entry == "fade":                      # breakout fails: close back inside -> trade the other way
            if last is None:
                last = 1 if c > hi else -1 if c < lo else None
            elif lo <= c <= hi:
                sig = -last
        if not sig or sig not in side_ok: continue
        e = c
        risk = stop_pts if stop_kind == "fixed" else max(10.0, (e - lo) if sig == 1 else (hi - e))
        stop = e - sig * risk
        tgt = e + sig * risk * tgt_r if tgt_r else None
        best = e
        for d in rest[i+1:]:
            h, l, cl = d[2], d[3], d[4]
            if (sig == 1 and l <= stop) or (sig == -1 and h >= stop):
                return sig * (stop - e)
            if tgt and ((sig == 1 and h >= tgt) or (sig == -1 and l <= tgt)):
                return sig * (tgt - e)
            if trail:                               # trail the stop `trail` points behind the best close
                best = max(best, cl) if sig == 1 else min(best, cl)
                ns = best - sig * trail
                stop = max(stop, ns) if sig == 1 else min(stop, ns)
        return sig * ((rest[-1][4] if rest else e) - e)
    return None

def stats(xs):
    if not xs: return (0, 0, 0, 0)
    net = [x - COST for x in xs]
    tot = sum(net); w = sum(1 for x in net if x > 0)
    peak = run = dd = 0
    for x in net:
        run += x; peak = max(peak, run); dd = min(dd, run - peak)
    return (len(net), tot / len(net), 100 * w / len(net), dd)

def main():
    days = load(sys.argv[1])
    ds = sorted(days)
    prev = {d: days[p][-1][4] for p, d in zip(ds, ds[1:])}
    grid = itertools.product(
        [15, 30],                                   # opening range minutes
        ["close", "confirm2", "fade"],              # entry
        [(1, -1), (1,), (-1,)],                     # both / calls only / puts only
        [("fixed", 30), ("fixed", 60), ("range", 0)],
        [0, 2, 3],                                  # target in R (0 = none)
        [0, 40, 60],                                # trailing stop points (0 = none)
        [T(13, 0), T(15, 0)],
        ["all", "no_big_gap"],
    )
    rows = []
    for orb, ent, side, (sk, sp), tr, trail, cut, gf in grid:
        if tr and trail: continue
        tr_x, te_x = [], []
        for d in ds:
            r = run_day(days[d], prev.get(d), orb, ent, side, sk, sp, tr, trail, cut, gf)
            if r is None: continue
            (tr_x if d < SPLIT else te_x).append(r)
        a, b = stats(tr_x), stats(te_x)
        rows.append(((orb, ent, {(1,-1):"both",(1,):"calls",(-1,):"puts"}[side], f"{sk}{sp or ''}", tr, trail, cut.strftime("%H:%M"), gf), a, b))
    rows.sort(key=lambda r: -r[1][1])
    print(f"{len(rows)} variants. Cost {COST} pts/trade. Sorted by TRAIN avg pts/trade. Columns: n, avg, win%, maxDD")
    print("orb entry  side  stop      tgtR trail cut   gap        | TRAIN n  avg   win%  DD     | TEST n  avg   win%  DD")
    for k, a, b in rows[:25]:
        print(f"{k[0]:<3} {k[1]:<8} {k[2]:<5} {k[3]:<9} {k[4]:<4} {k[5]:<5} {k[6]} {k[7]:<10} | {a[0]:5d} {a[1]:+6.1f} {a[2]:5.1f} {a[3]:7.0f} | {b[0]:4d} {b[1]:+6.1f} {b[2]:5.1f} {b[3]:7.0f}")
    pos_test = sum(1 for r in rows[:25] if r[2][1] > 0)
    print(f"\nOf the top 25 by train, {pos_test} stayed positive in test.")
    # the idea to check: puts on confirmed weakness, trailing, no breakout chasing
    print("\n'Buy puts on confirmed weakness + trail' family:")
    for k, a, b in rows:
        if k[2] == "puts" and k[1] == "confirm2" and k[5] and k[3] != "fixed30":
            print(f"  {k} | TRAIN {a[0]} {a[1]:+.1f} | TEST {b[0]} {b[1]:+.1f}")

if __name__ == "__main__":
    main()
