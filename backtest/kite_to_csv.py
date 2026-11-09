import json, glob, csv, sys
rows = {}
for f in glob.glob(sys.argv[1]):
    for c in json.load(open(f)):
        rows[c["date"][:19].replace("T", " ")] = (c["open"], c["high"], c["low"], c["close"])
with open("nifty_5m.csv", "w", newline="") as o:
    w = csv.writer(o); w.writerow(["datetime", "open", "high", "low", "close"])
    for k in sorted(rows): w.writerow([k, *rows[k]])
ks = sorted(rows); days = sorted({k[:10] for k in ks})
print(len(ks), "bars,", len(days), "days,", ks[0], "->", ks[-1])
# gaps between consecutive trading days > 5 calendar days
import datetime as d
for a, b in zip(days, days[1:]):
    if (d.date.fromisoformat(b) - d.date.fromisoformat(a)).days > 5: print("gap:", a, "->", b)
