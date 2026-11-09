#!/usr/bin/env python3
"""Round 2: robust filters on the 30-min opening-range PUT idea, and the CALL mirror.

Select on 2015-2020 (TRAIN), confirm on 2021-2026 (TEST). Index points after COST.
A rule is "robust" only if it is positive in TRAIN, positive in TEST, and positive
in at least 9 of the 12 calendar years.
"""
import csv, datetime as dt, itertools, sys
from collections import defaultdict

COST = 2.0
SPLIT = dt.date(2021, 1, 1)

def load(path):
    days = defaultdict(list)
    for r in csv.DictReader(open(path)):
        t = dt.datetime.fromisoformat(r["datetime"])
        days[t.date()].append((t.hour * 60 + t.minute, float(r["open"]), float(r["high"]), float(r["low"]), float(r["close"])))
    return {d: sorted(b) for d, b in days.items() if len(b) >= 70 and b and min(x[0] for x in b) == 555}

def main():
    days = load(sys.argv[1]); ds = sorted(days)
    close = {d: days[d][-1][4] for d in ds}
    info = {}
    for i, d in enumerate(ds):
        if i < 20: continue
        p = ds[i - 1]
        sma20 = sum(close[x] for x in ds[i - 20:i]) / 20
        info[d] = dict(prev=close[p], sma20=sma20, prev_ret=close[p] / close[ds[i - 2]] - 1)
    grid = itertools.product(
        [-1, 1],                       # side: put / call
        [30, 45, 60],                  # opening-range minutes
        [9e9, 0.6, 0.4],               # max range width, % of price
        ["none", "trend", "countertrend"],  # trend vs 20-day average at prev close
        [12 * 60, 13 * 60 + 30, 15 * 60],   # last entry time
        ["range", "mid", "fixed40"],   # stop: opposite side / range midpoint / 40 pts
        [0, 2, 3],                     # target R
        [13 * 60, 15 * 60],            # exit time
    )
    out = []
    for side, orb, wmax, trend, last_in, stopk, tr, cut in grid:
        if last_in >= cut: continue
        res = {}
        for d, inf in info.items():
            bars = days[d]
            if abs(bars[0][1] / inf["prev"] - 1) > 0.005: continue
            up = inf["prev"] > inf["sma20"]
            if trend == "trend" and up != (side == 1): continue
            if trend == "countertrend" and up == (side == 1): continue
            end = 555 + orb
            rng = [b for b in bars if b[0] < end]
            hi = max(b[2] for b in rng); lo = min(b[3] for b in rng)
            if (hi - lo) / lo * 100 > wmax: continue
            for i, b in enumerate(bars):
                if b[0] < end or b[0] + 5 > last_in: continue
                c = b[4]
                if not ((side == 1 and c > hi) or (side == -1 and c < lo)): continue
                if stopk == "range": stop = lo if side == 1 else hi
                elif stopk == "mid": stop = (hi + lo) / 2
                else: stop = c - side * 40
                risk = side * (c - stop)
                if risk < 10: stop = c - side * 10; risk = 10
                tgt = c + side * risk * tr if tr else None
                r = None
                for x in bars[i + 1:]:
                    if x[0] + 5 > cut: break
                    if (side == 1 and x[3] <= stop) or (side == -1 and x[2] >= stop): r = -risk; break
                    if tgt and ((side == 1 and x[2] >= tgt) or (side == -1 and x[3] <= tgt)): r = risk * tr; break
                    last = x[4]
                if r is None:
                    prior = [x for x in bars[i + 1:] if x[0] + 5 <= cut]
                    r = side * ((prior[-1][4] if prior else c) - c)
                res[d] = r - COST
                break
        tr_ = [v for d, v in res.items() if d < SPLIT]; te_ = [v for d, v in res.items() if d >= SPLIT]
        yrs = defaultdict(float)
        for d, v in res.items(): yrs[d.year] += v
        pos_years = sum(1 for v in yrs.values() if v > 0)
        if len(tr_) < 150 or len(te_) < 150: continue
        out.append(dict(key=("PUT" if side < 0 else "CALL", orb, wmax, trend, last_in, stopk, tr, cut),
                        tr=(len(tr_), sum(tr_) / len(tr_)), te=(len(te_), sum(te_) / len(te_)),
                        pos=pos_years, ny=len(yrs), yrs=dict(sorted(yrs.items())), avg_risk=0))
    robust = [o for o in out if o["tr"][1] > 0 and o["te"][1] > 0 and o["pos"] >= 9]
    robust.sort(key=lambda o: -min(o["tr"][1], o["te"][1]))
    print(f"{len(out)} variants tested; {len(robust)} robust (train>0, test>0, >=9 of 12 years positive)")
    fmt = lambda m: f"{m // 60:02d}:{m % 60:02d}"
    for o in robust[:20]:
        k = o["key"]
        print(f"{k[0]:<4} orb{k[1]} wmax {('any' if k[2] > 100 else str(k[2]) + '%'):<5} {k[3]:<12} lastin {fmt(k[4])} stop {k[5]:<7} tgt {k[6]}R exit {fmt(k[7])}"
              f" | TRAIN {o['tr'][0]} {o['tr'][1]:+.1f} | TEST {o['te'][0]} {o['te'][1]:+.1f} | years+ {o['pos']}/{o['ny']}")
    if robust:
        print("\nBest robust rule, by year (points):", {y: round(v) for y, v in robust[0]["yrs"].items()})

if __name__ == "__main__":
    main()
