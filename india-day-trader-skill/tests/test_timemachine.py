import gzip
import hashlib
import io
import json
import os
import random
import shutil
import tempfile
import unittest
import urllib.error
from contextlib import redirect_stdout, redirect_stderr

from _load import load, FIX

tm = load("india-timemachine", "timemachine")

RELIANCE = os.path.join(FIX, "shared", "RELIANCE-60d5m.json.gz")  # Yahoo v8 chart RELIANCE.NS, 60d 5m, fetched 2026-10-23 11:24 IST
with gzip.open(RELIANCE, "rt", encoding="utf-8") as _fh:
    TEXT = _fh.read()
NOW = "2026-10-23 11:25"
NOW_UTC = tm.parse_now(NOW)

# the worked example, as india-exit writes trader-in/plan.json: Monday 2026-10-19, opening range
# 09:15-09:30 1207.30 to 1223.40, the 09:45 bar closes above it at 1227.80 (entry at 09:50, bar 6),
# stop = range midpoint 1215.35 rounded DOWN to the 0.10 tick = 1215.30, 1R target 1240.30
PLAN = {"created_by": "india-exit", "symbol": "RELIANCE", "exchange": "NSE", "currency": "INR", "tick": 0.1,
        "side": "buy", "date": "2026-10-19", "setup": "orb",
        "entry": 1227.8, "entry_time": "09:50", "entry_bar": 6, "interval": "5m",
        "opening_range": {"high": 1223.4, "low": 1207.3, "minutes": 15},
        "stop": 1215.3, "stop_rule": "or_mid", "stop_label": "opening-range midpoint",
        "target": 1240.3, "target_r": 1.0, "target_label": "1R (one times the risk)",
        "risk_per_share": 12.5, "reward_risk": 1.0, "flat_by": "15:00", "product": "MIS"}

# fingerprints of the seed-19 run: any change to the draws, the pool or the rounding shows here
BANDS = "6d92518f14df75a2"
PATHS = "f1056f046c750577"
TRADES = "d390f2cfacdb264c"
HIST = {"lo": 1207.65, "hi": 1250.17,
        "bins": [123, 118, 159, 227, 271, 383, 495, 542, 667, 773, 721, 758, 774, 692, 666, 579, 490, 420,
                 309, 237, 171, 123, 83, 64]}


def sha(x):
    return hashlib.sha256(json.dumps(x).encode("utf-8")).hexdigest()[:16]


def plan(**kw):
    raw = dict(PLAN)
    raw.update(kw)
    return tm.normalize_plan(raw)


def machine(p=None, text=TEXT, now=NOW_UTC, **kw):
    return tm.time_machine(text, p or plan(), now, **kw)


def edited(fn):
    """The fixture's chart JSON, changed by fn(result), back as text."""
    doc = json.loads(TEXT)
    fn(doc["chart"]["result"][0])
    return json.dumps(doc)


def keep_bars(result, keep):
    q = result["indicators"]["quote"][0]
    idx = [i for i, ts in enumerate(result["timestamp"]) if keep(ts)]
    result["timestamp"] = [result["timestamp"][i] for i in idx]
    for k in q:
        q[k] = [q[k][i] for i in idx]


def ist(ts):
    return tm.to_ist(tm.utc_of(ts))


def run(argv):
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = tm.main(argv)
    return code, out.getvalue(), err.getvalue()


class Example(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.doc = machine()

    def test_the_example_odds(self):
        m = self.doc["mc"]
        self.assertEqual((m["replays"], m["seed"], m["window"], m["until"]), (10000, 19, 2, "15:00"))
        self.assertEqual(m["pool_sessions"], 56)
        self.assertEqual(m["bars_left"], 62)
        self.assertEqual((m["pool_from"], m["pool_to"]), ("2026-07-29", "2026-10-16"))
        self.assertEqual(m["p_target"], 0.1916)
        self.assertEqual(m["p_stop"], 0.1388)
        self.assertEqual(m["p_neither"], 0.6696)
        self.assertEqual(m["p_green"], 0.522)
        self.assertEqual(m["median_close"], 1228.22)
        self.assertEqual(m["p5_close"], 1213.28)
        self.assertEqual(m["p95_close"], 1243.47)
        self.assertEqual(m["avg_R"], 0.047)
        self.assertEqual(m["r_at_target"], 1.0)

    def test_net_of_charges(self):
        m = self.doc["mc"]
        self.assertEqual((m["qty"], m["product"]), (40, "MIS"))          # Rs 500 risk / Rs 12.50 a share
        self.assertEqual(m["charges_round_trip"], 52.08)
        self.assertEqual(m["charges_R"], 0.104)
        self.assertEqual(m["avg_R_net"], -0.058)
        self.assertEqual(m["charges_parts"], {"brokerage": 29.47, "stt": 12.28, "exchange": 2.92, "sebi": 0.1,
                                              "stamp": 1.47, "gst": 5.85, "total": 52.08})

    def test_fingerprints(self):
        m = self.doc["mc"]
        self.assertEqual(sha(m["bands"]), BANDS)
        self.assertEqual(len(m["sample_paths"]), 160)
        self.assertEqual(sha(m["sample_paths"]), PATHS)
        self.assertEqual(m["hist"], HIST)
        self.assertEqual(m["bands"][-1], [1214.18, 1222.37, 1228.3, 1234.39, 1242.82])

    def test_the_history_replay(self):
        h = self.doc["history"]
        self.assertEqual((h["sessions"], h["triggered"]), (56, 27))
        self.assertEqual((h["target"], h["stop"], h["close"], h["green"]), (14, 10, 3, 15))
        self.assertEqual((h["avg_R"], h["avg_R_net"]), (0.119, -0.08))
        self.assertEqual(h["rule"], {"setup": "orb", "or_minutes": 15, "stop_rule": "or_mid", "target_r": 1.0})
        slim = [{"date": t["date"], "entry": t["entry"], "outcome": t["outcome"], "R": t["R"]} for t in h["trades"]]
        self.assertEqual(sha(slim), TRADES)
        first = h["trades"][0]
        self.assertEqual((first["date"], first["time"], first["entry"], first["stop"], first["target"],
                          first["outcome"], first["qty"]),
                         ("2026-07-29", "09:35", 1276.8, 1274.0, 1279.6, "stop", 178))
        for t in h["trades"]:   # every past stop and target sits on the 0.10 tick
            self.assertEqual(round(t["stop"] * 10, 6) % 1, 0, t)
            self.assertEqual(round(t["target"] * 10, 6) % 1, 0, t)

    def test_the_verdict(self):
        self.assertEqual(self.doc["verdict"], "Leans toward the target: target first 19.2 % vs stop first 13.9 %.")
        gap, share, even = tm.lean(self.doc["mc"])
        self.assertEqual((round(share, 3), even), (0.58, 0.5))
        self.assertEqual(self.doc["verdict_notes"], [
            "67.0 % of the replays hit neither by 15:00; the price at 15:00 decides those.",
            "After charges the average R is -0.058: on 40 shares the round trip costs about 0.10R a trade."])

    def test_bands_start_at_the_entry(self):
        m = self.doc["mc"]
        self.assertEqual(m["bands"][0], [1227.8] * 5)
        self.assertEqual(len(m["bands"]), 63)
        self.assertEqual((m["band_times"][0], m["band_times"][-1], len(m["band_times"])), ("09:50", "15:00", 63))
        self.assertTrue(all(p[0] == 1227.8 and len(p) == 63 for p in m["sample_paths"]))

    def test_the_plan_is_carried_through(self):
        p = self.doc["plan"]
        self.assertEqual((p["symbol"], p["exchange"], p["currency"], p["entry"], p["stop"], p["target"],
                          p["entry_bar"], p["entry_time"]),
                         ("RELIANCE", "NSE", "INR", 1227.8, 1215.3, 1240.3, 6, "09:50"))
        self.assertEqual((p["risk_per_share"], p["target_r"], p["flat_by"], p["tick"]), (12.5, 1.0, "15:00", 0.1))
        self.assertEqual(self.doc["warnings"], [])
        self.assertEqual(self.doc["data"]["symbol"], "RELIANCE.NS")

    def test_the_text_report(self):
        text = "\n".join(tm.report(self.doc))
        for line in ("RELIANCE (NSE) buy, Monday 2026-10-19: entry 1227.80 at 09:50 (bar 6), stop 1215.30, "
                     "target 1240.30 (1R, risk ₹12.50 a share)",
                     "Session: CAS stock (F&O): continuous trading ends 15:15, Zerodha squares off MIS at 15:12",
                     "Data: the plan day's bars (Yahoo, free, can lag); the 56-session history (Yahoo, free, can lag).",
                     "10,000 replays", "09:50 to 15:00 IST (your flat-by; 62 five-minute bars)", "seed 19",
                     "56 full sessions",
                     "target first   19.2 %", "stop first     13.9 %", "neither        67.0 %   still open at 15:00",
                     "green          52.2 %", "median 1228.22, 90 % of replays between 1213.28 and 1243.47",
                     "Average R: +0.047 gross", "Average R: -0.058 net of charges, for 40 shares",
                     "about ₹52.08 a round trip (MIS, NSE) = 0.104R",
                     "fired 27 times: 14 target, 10 stop, 3 neither. Average R +0.119 gross, -0.080 net of charges",
                     "Leans toward the target: target first 19.2 % vs stop first 13.9 %.\n67.0 % of the replays "
                     "hit neither by 15:00",
                     "Closing auction: on 53 session(s) from 2026-08-03 the 15:15 to 15:30 bars are the auction",
                     "Real gaps: filled 2 empty 5-min bar(s) in 1 session(s)",
                     "check your broker's", "not a prediction", "--seed 19"):
            self.assertIn(line, text)
        for word in ("profit", "P&L", "guarantee", "real time", "tick by tick", "$"):
            self.assertNotIn(word, text)


class Seeds(unittest.TestCase):
    def test_same_seed_same_numbers(self):
        self.assertEqual(machine(replays=2000, seed=5)["mc"], machine(replays=2000, seed=5)["mc"])

    def test_another_seed_gives_other_numbers(self):
        m = machine(seed=7)["mc"]
        self.assertEqual((m["p_target"], m["p_stop"], m["p_neither"]), (0.1732, 0.1502, 0.6766))
        self.assertAlmostEqual(m["p_target"] + m["p_stop"] + m["p_neither"], 1.0, places=6)
        self.assertNotEqual(sha(m["bands"]), BANDS)

    def test_the_draws_are_stable_across_pythons(self):
        r = random.Random(19)
        self.assertEqual([r.randint(-2, 2) for _ in range(8)], [-2, 2, -2, 2, -1, 1, 0, 2])
        self.assertEqual(r.choice(range(56)), 18)

    def test_window_and_replays(self):
        a = machine(replays=500, window=0)["mc"]
        self.assertEqual((a["replays"], a["window"]), (500, 0))
        self.assertNotEqual(a["bands"], machine(replays=500)["mc"]["bands"])
        with self.assertRaises(tm.TMError):
            machine(replays=0)
        with self.assertRaises(tm.TMError):
            machine(window=11)


class Bars(unittest.TestCase):
    def setUp(self):
        self.result = tm.chart_result(TEXT, "RELIANCE.NS")

    def test_the_in_progress_tick_is_dropped(self):
        meta, bars, dropped = tm.parse_bars(self.result, NOW_UTC)
        self.assertEqual(dropped, {"no_price": 112, "off_grid": 1, "not_complete": 1, "outside_hours": 0})
        last = bars[-1]
        self.assertEqual((last["date"], last["start"], last["slot"]), ("2026-10-23", "11:15", 24))
        self.assertEqual(tm.parse_bars(self.result, tm.parse_now(None))[1][-1]["start"], "11:15")
        entry = [b for b in bars if b["date"] == "2026-10-19" and b["slot"] == 6][0]
        self.assertEqual((entry["start"], entry["c"]), ("09:45", 1227.800048828125))

    def test_ist_is_a_fixed_offset(self):
        self.assertEqual(ist(1792727100).strftime("%Y-%m-%d %H:%M"), "2026-10-23 09:15")
        self.assertEqual(tm.parse_now("2026-10-23 09:15"), tm.utc_of(1792727100))
        self.assertEqual(tm.parse_now("2026-10-23T03:45Z"), tm.utc_of(1792727100))
        res = {"meta": {"symbol": "X.NS"}, "timestamp": [1792727100 - 300, 1792727100, 1792749600 - 300, 1792749600],
               "indicators": {"quote": [{"open": [1] * 4, "high": [1] * 4, "low": [1] * 4, "close": [1] * 4,
                                         "volume": [0] * 4}]}}
        meta, bars, dropped = tm.parse_bars(res, tm.parse_now("2026-10-24 00:00"))
        self.assertEqual([(b["start"], b["slot"]) for b in bars], [("09:15", 0), ("15:25", 74)])
        self.assertEqual(dropped["outside_hours"], 2)

    def test_now_pins_the_clock(self):
        bars = tm.parse_bars(self.result, tm.parse_now("2026-10-19 09:47"))[1]
        self.assertEqual(len([b for b in bars if b["date"] == "2026-10-19"]), 6)
        with self.assertRaises(tm.TMError) as cm:
            machine(now=tm.parse_now("2026-10-19 09:47"))
        self.assertIn("not in the data", str(cm.exception))
        self.assertIn("09:40 bar", str(cm.exception))
        self.assertEqual(machine(now=tm.parse_now("2026-10-19 09:50"), replays=200)["mc"]["pool_sessions"], 56)

    def test_the_closing_auction_is_cut_not_filled(self):
        days = tm.split_sessions(tm.parse_bars(self.result, NOW_UTC)[1])
        sessions, short, filled, auction = tm.full_sessions(days, "2026-10-19", True)
        self.assertEqual((len(sessions), short), (56, []))
        # 53 CAS days from 2026-08-03 lose their three 15:15-15:30 auction bars; the three July days keep 75
        self.assertEqual((sum(auction.values()), len(auction), min(auction)), (159, 53, "2026-08-03"))
        self.assertEqual([len(r) for d, r in sessions[:4]], [75, 75, 75, 72])
        self.assertTrue(all(len(r) == 72 for d, r in sessions if d >= "2026-08-03"))
        # the only real gap: two empty bars mid-session, filled flat at the previous close
        self.assertEqual(sum(filled.values()), 2)
        rows = dict(sessions)[list(filled)[0]]
        gap = [b for b in rows if b.get("filled")]
        self.assertEqual([b["start"] for b in gap], ["11:45", "11:50"])
        prev = rows[gap[0]["slot"] - 1]["c"]
        self.assertEqual((gap[0]["o"], gap[0]["h"], gap[0]["l"], gap[0]["c"], gap[0]["v"]), (prev,) * 4 + (0,))

    def test_a_non_cas_stock_keeps_75_bars(self):
        days = tm.split_sessions(tm.parse_bars(self.result, NOW_UTC)[1])
        sessions, short, filled, auction = tm.full_sessions(days, "2026-10-19", False)
        self.assertEqual((len(sessions), auction, sum(filled.values())), (56, {}, 108))
        self.assertTrue(all(len(r) == 75 for d, r in sessions))

    def test_a_short_session_is_left_out(self):
        def cut(res):  # 2026-09-04 ends at 13:00, like a short special session
            keep_bars(res, lambda ts: not (ist(ts).date().isoformat() == "2026-09-04" and ist(ts).hour >= 13))
        doc = machine(text=edited(cut), replays=300)
        self.assertEqual(doc["mc"]["pool_sessions"], 55)
        self.assertEqual(doc["data"]["short_sessions_left_out"][0]["date"], "2026-09-04")
        self.assertNotIn("2026-09-04", [t["date"] for t in doc["history"]["trades"]])
        self.assertIn("Left out 1 short session", "\n".join(tm.report(doc)))

    def test_a_missing_opening_bar_leaves_the_session_out(self):
        def hole(res):
            res["indicators"]["quote"][0]["close"][0] = None   # 2026-07-29 09:15
        doc = machine(text=edited(hole), replays=200)
        self.assertEqual(doc["data"]["dropped"]["no_price"], 113)
        self.assertEqual(doc["mc"]["pool_sessions"], 55)
        self.assertEqual(doc["mc"]["pool_from"], "2026-07-30")

    def test_today_is_not_in_the_pool(self):
        doc = machine(replays=200)
        self.assertEqual(doc["mc"]["pool_to"], "2026-10-16")
        self.assertNotIn("2026-10-19", [t["date"] for t in doc["history"]["trades"]])

    def test_empty_and_broken_data(self):
        empty = json.dumps({"chart": {"result": [{"meta": {"symbol": "RELIANCE.NS"}, "timestamp": [],
                                                  "indicators": {"quote": [{}]}}], "error": None}})
        with self.assertRaises(tm.TMError) as cm:
            machine(text=empty)
        self.assertIn("no complete 5-minute bars", str(cm.exception))
        gone = json.dumps({"chart": {"result": None, "error": {"code": "Not Found",
                                                               "description": "No data found, symbol may be delisted"}}})
        with self.assertRaises(tm.TMError) as cm:
            machine(text=gone)
        self.assertIn("No data found", str(cm.exception))
        with self.assertRaises(tm.TMError):
            machine(text="<html>rate limited</html>")

    def test_fewer_than_20_sessions_is_refused(self):
        def trim(res):
            keep_bars(res, lambda ts: ist(ts).date().isoformat() >= "2026-09-21")
        with self.assertRaises(tm.TMError) as cm:
            machine(text=edited(trim))
        self.assertIn("only 19 full session(s)", str(cm.exception))
        self.assertIn("at least 20", str(cm.exception))

        def twenty(res):
            keep_bars(res, lambda ts: ist(ts).date().isoformat() >= "2026-09-18")
        self.assertEqual(machine(text=edited(twenty), replays=200)["mc"]["pool_sessions"], 20)

    def test_the_data_must_be_the_plans_symbol(self):
        with self.assertRaises(tm.TMError) as cm:
            machine(plan(symbol="TCS"))
        self.assertIn("data is for RELIANCE.NS but the plan is for TCS.NS", str(cm.exception))
        with self.assertRaises(tm.TMError) as cm:
            machine(plan(exchange="BSE"))
        self.assertIn("RELIANCE.BO", str(cm.exception))


class Plans(unittest.TestCase):
    def test_an_entry_not_in_the_data_is_refused(self):
        with self.assertRaises(tm.TMError) as cm:
            machine(plan(date="2026-10-20"))
        self.assertIn("not in the data", str(cm.exception))
        self.assertIn("an NSE trading holiday", str(cm.exception))
        with self.assertRaises(tm.TMError) as cm:
            machine(plan(date="2026-10-18"))
        self.assertIn("a Sunday", str(cm.exception))
        with self.assertRaises(tm.TMError) as cm:      # 11:20 has not closed in this data yet
            machine(plan(date="2026-10-23", entry_time="11:25", entry_bar=None))
        self.assertIn("the data stops at the 11:15 bar", str(cm.exception))

    def test_plan_checks(self):
        cases = [
            (dict(entry_time="09:52", entry_bar=None), "5-minute grid"),
            (dict(entry_time="15:30", entry_bar=None), "between 09:20 and 15:25"),
            (dict(entry_bar=9), "disagrees with itself"),
            (dict(entry_time=None, entry_bar=None), "entry_time"),
            (dict(interval="1m"), "7 days of 1-min bars"),
            (dict(stop=1230.0), "stop < entry < target"),
            (dict(side="sell"), "long only"),
            (dict(side="hold"), "side must be buy"),
            (dict(symbol=""), "NSE/BSE symbol"),
            (dict(exchange="NYSE"), "NSE or BSE"),
            (dict(currency="USD"), "INR"),
            (dict(product="NRML"), "MIS or CNC"),
            (dict(entry="abc"), "number for entry"),
            (dict(date="19 Oct"), "date like"),
            (dict(opening_range={"minutes": 7}), "multiple of 5"),
        ]
        for change, words in cases:
            with self.assertRaises(tm.TMError, msg=change) as cm:
                plan(**change)
            self.assertIn(words, str(cm.exception), change)

    def test_entry_bar_or_entry_time_alone(self):
        self.assertEqual(plan(entry_time=None)["entry_time"], "09:50")
        self.assertEqual(plan(entry_bar=None)["entry_bar"], 6)
        self.assertEqual(plan(entry_time="9:50 AM", entry_bar=None)["entry_bar"], 6)
        self.assertEqual(plan(entry_time="09:50am", entry_bar=None)["entry_bar"], 6)
        # the bar's start time with the bar number also reads as bar 6
        self.assertEqual(plan(entry_time="09:45")["entry_time"], "09:50")

    def test_symbols(self):
        self.assertEqual(tm.parse_symbol("reliance"), ("RELIANCE", "NSE"))
        self.assertEqual(tm.parse_symbol("RELIANCE.NS"), ("RELIANCE", "NSE"))
        self.assertEqual(tm.parse_symbol("NSE:M&M"), ("M&M", "NSE"))
        self.assertEqual(tm.parse_symbol("RELIANCE.BO"), ("RELIANCE", "BSE"))
        self.assertEqual(tm.parse_symbol("BSE:RELIANCE"), ("RELIANCE", "BSE"))
        self.assertEqual(tm.parse_symbol("500325"), ("500325", "BSE"))
        self.assertEqual(tm.parse_symbol("BAJAJ-AUTO", "NSE"), ("BAJAJ-AUTO", "NSE"))
        self.assertEqual(tm.yahoo_symbol("M&M", "NSE"), "M&M.NS")
        self.assertEqual(tm.yahoo_symbol("500325", "BSE"), "500325.BO")

    def test_until_cuts_the_replay(self):
        # a CAS stock: continuous trading ends 15:15, Zerodha squares MIS off at 15:12
        doc = machine(until="15:15", replays=500)
        m = doc["mc"]
        self.assertEqual((m["until"], m["bars_left"], m["band_times"][-1]), ("15:15", 65, "15:15"))
        text = "\n".join(tm.report(doc))
        self.assertIn("still open at 15:15", text)
        self.assertIn("Zerodha squares MIS positions off at 15:12 for this stock", text)
        self.assertEqual(machine(until="3:15 PM", replays=500)["mc"], m)
        self.assertEqual(machine(plan(flat_by="14:30"), replays=100)["mc"]["until"], "14:30")
        for fb in (None, "auto", ""):
            self.assertEqual(machine(plan(flat_by=fb), replays=100)["mc"]["until"], "15:00", fb)
        self.assertEqual(machine(plan(product="CNC"), until="15:15", replays=100)["warnings"], [])
        for bad, words in (("09:50", "nothing to replay"), ("15:20", "no later than 15:15"),
                           ("15:12", "5-minute grid"), ("noon", "IST time")):
            with self.assertRaises(tm.TMError) as cm:
                machine(until=bad, replays=10)
            self.assertIn(words, str(cm.exception), bad)

    def test_a_flat_by_at_or_after_the_square_off_is_clamped(self):
        for fb in ("15:15", "15:20", "15:25"):
            doc = machine(plan(flat_by=fb), replays=100)
            self.assertEqual(doc["mc"]["until"], "15:00", fb)
            self.assertIn("at or after Zerodha's 15:12 MIS square-off", doc["warnings"][0])
        self.assertEqual(machine(plan(flat_by="15:10"), replays=100)["warnings"], [])

    def test_a_non_cas_stock_trades_to_1530(self):
        doc = machine(plan(flat_by=None), cas=False, replays=500)
        self.assertEqual((doc["mc"]["until"], doc["session"]["square_off"]), ("15:10", "15:25"))
        self.assertEqual((doc["data"]["auction_bars"], doc["data"]["filled_bars"]), (0, 108))
        doc = machine(plan(flat_by=None), cas=False, until="15:30", replays=200)
        self.assertEqual(doc["mc"]["bars_left"], 68)
        self.assertIn("off at 15:25 for this stock", doc["warnings"][0])

    def test_an_entry_outside_its_bar_is_flagged(self):
        doc = machine(plan(entry=1235.0, stop=1222.0, target=1248.0), replays=200)
        self.assertIn("outside that bar's range", doc["warnings"][0])

    def test_tick_rounding(self):
        self.assertEqual(tm.round_to_tick((1223.4000244140625 + 1207.300048828125) / 2, "down"), 1215.3)
        self.assertEqual(tm.round_to_tick(1215.35, "down"), 1215.3)
        self.assertEqual(tm.round_to_tick(1215.35, "up"), 1215.4)
        self.assertEqual(tm.round_to_tick(1227.800048828125), 1227.8)
        self.assertEqual(tm.round_to_tick(1215.3, "down"), 1215.3)
        self.assertEqual(tm.round_to_tick(412.33, "down"), 412.3)
        self.assertEqual(tm.round_to_tick(412.33, "up"), 412.35)
        self.assertEqual(tm.round_to_tick(99.999, "down"), 99.99)
        self.assertEqual(tm.round_to_tick(7123.7, "down"), 7123.5)
        self.assertEqual([tm.tick_size(x) for x in (249.99, 250, 999, 1000, 4999, 5000, 10000, 20000, 25000)],
                         [0.01, 0.05, 0.05, 0.10, 0.10, 0.50, 1.00, 5.00, 5.00])

    def test_charges(self):
        c = tm.charges(1227.8 * 40, 1240.3 * 40)
        self.assertEqual(c["brokerage"], 29.62)         # 0.03 % of each side, under the Rs 20 cap
        self.assertEqual(c["stt"], 12.4)                # 0.025 % of the sell
        self.assertEqual(c["total"], 52.4)
        big = tm.charges(200000, 200000)
        self.assertEqual(big["brokerage"], 40.0)        # Rs 20 an order cap
        self.assertEqual(big["stamp"], 6.0)
        cnc = tm.charges(100000, 100000, "CNC")
        self.assertEqual((cnc["brokerage"], cnc["stt"], cnc["stamp"]), (0.0, 200.0, 15.0))
        self.assertGreater(tm.charges(100000, 100000, exchange="BSE")["exchange"],
                           tm.charges(100000, 100000)["exchange"])

    def test_quantity_for_the_charges(self):
        self.assertEqual(tm.resolve_qty(plan())[0], 40)
        self.assertEqual(tm.resolve_qty(plan(), risk_inr=1000)[0], 80)
        self.assertEqual(tm.resolve_qty(plan(), qty=100)[0], 100)
        self.assertEqual(tm.resolve_qty(plan(qty=25))[0], 25)
        self.assertEqual(tm.resolve_qty(plan(), risk_inr=5)[0], 1)
        doc = machine(qty=100, replays=10000)
        self.assertEqual((doc["mc"]["qty"], doc["mc"]["charges_round_trip"], doc["mc"]["avg_R_net"]),
                         (100, 90.47, -0.026))
        self.assertEqual(doc["mc"]["avg_R"], 0.047)      # gross R does not depend on the quantity
        self.assertTrue(all(t["qty"] == 100 for t in doc["history"]["trades"]))
        with self.assertRaises(tm.TMError):
            tm.resolve_qty(plan(), qty=0)

    def test_inr_grouping(self):
        self.assertEqual(tm.inr(123456.78), "₹1,23,456.78")
        self.assertEqual(tm.inr(12345678.9), "₹1,23,45,678.90")
        self.assertEqual(tm.inr(999), "₹999.00")
        self.assertEqual(tm.inr(-52.08), "-₹52.08")

    def test_the_stop_rule_is_worked_out_when_not_given(self):
        self.assertEqual(machine(plan(stop_rule=None), replays=100)["history"]["rule"]["stop_rule"], "or_mid")
        rule = machine(plan(stop_rule=None, stop=1207.3, target=1248.3), replays=100)["history"]["rule"]
        self.assertEqual((rule["stop_rule"], rule["target_r"]), ("or_low", 1.0))
        rule = machine(plan(stop_rule="atr", stop=1221.0, target=1241.4), replays=100)["history"]["rule"]
        self.assertEqual((rule["stop_rule"], rule["stop_pct"], rule["target_r"]), ("pct", 0.5538, 2.0))

    def test_a_setup_other_than_orb_skips_the_history(self):
        doc = machine(plan(setup="vwap"), replays=200)
        self.assertIsNone(doc["history"]["rule"])
        self.assertIn("orb", doc["history"]["note"])
        self.assertEqual(doc["mc"]["pool_sessions"], 56)
        self.assertIn("History: skipped", "\n".join(tm.report(doc)))

    def test_the_verdict_thresholds_at_1r(self):
        self.assertTrue(tm.verdict({"p_target": 0.30, "p_stop": 0.26}).startswith("Close to a coin flip"))
        self.assertTrue(tm.verdict({"p_target": 0.40, "p_stop": 0.30}).startswith("Leans toward the target"))
        self.assertTrue(tm.verdict({"p_target": 0.20, "p_stop": 0.30}).startswith("Leans toward the stop"))
        self.assertTrue(tm.verdict({"p_target": 0.60, "p_stop": 0.30}).startswith("Strongly toward the target"))
        self.assertEqual(tm.verdict({"p_target": 0.10, "p_stop": 0.60, "r_at_target": 1.0}),
                         "Strongly toward the stop: target first 10.0 % vs stop first 60.0 %.")
        self.assertEqual(tm.verdict({"p_target": 0.0, "p_stop": 0.0, "until": "15:10"}),
                         "No replay reached the target or the stop by 15:10: the price at 15:10 decides every one.")

    def test_a_2r_target_is_judged_against_its_break_even(self):
        mc = {"p_target": 0.241, "p_stop": 0.505, "p_neither": 0.254, "r_at_target": 2.0, "avg_R": 0.095,
              "avg_R_net": 0.01}
        self.assertEqual(tm.verdict(mc), "Close to break-even: at 2R the target has to come first in 33 % of the "
                                         "replays that hit one side; it did in 32 % (target first 24.1 % vs stop "
                                         "first 50.5 %).")
        self.assertEqual(tm.verdict_notes(mc), [])
        lean = dict(mc, p_target=0.15, p_stop=0.55)
        self.assertEqual(tm.verdict_notes(lean), ["Average R is +0.095 all the same: the replays that hit neither "
                                                  "pull it the other way."])
        self.assertTrue(tm.verdict(dict(mc, p_target=0.40, p_stop=0.40)).startswith("Strongly toward the target"))
        self.assertIn("at 1.5R the target has to come first in 40 %", tm.verdict(dict(mc, r_at_target=1.5)))

    def test_2r_on_the_fixture(self):
        doc = machine(plan(target=1252.8))
        m = doc["mc"]
        self.assertEqual((m["r_at_target"], m["p_target"], m["p_stop"], m["p_neither"], m["avg_R"], m["avg_R_net"]),
                         (2.0, 0.0104, 0.1389, 0.8507, 0.047, -0.057))
        self.assertEqual(doc["verdict"], "Strongly toward the stop: at 2R the target has to come first in 33 % of "
                                         "the replays that hit one side; it did in 7 % (target first 1.0 % vs stop "
                                         "first 13.9 %).")
        self.assertEqual(doc["verdict_notes"], [
            "85.1 % of the replays hit neither by 15:00; the price at 15:00 decides those.",
            "Average R is +0.047 all the same: the replays that hit neither pull it the other way.",
            "After charges the average R is -0.057: on 40 shares the round trip costs about 0.10R a trade."])


class Network(unittest.TestCase):
    def setUp(self):
        self.calls, self.sleeps = [], []
        self._get, self._sleep = tm._get, tm._sleep
        tm._sleep = self.sleeps.append

    def tearDown(self):
        tm._get, tm._sleep = self._get, self._sleep

    def answer(self, codes):
        codes = list(codes)

        def fake(url, timeout=20):
            self.calls.append(url.split(".finance")[0].split("//")[1])
            code = codes.pop(0)
            if code != 200:
                raise urllib.error.HTTPError(url, code, "x", {}, io.BytesIO(b""))
            return TEXT
        tm._get = fake

    def test_url_and_user_agent(self):
        self.assertEqual(tm.chart_url("RELIANCE.NS"),
                         "https://query1.finance.yahoo.com/v8/finance/chart/RELIANCE.NS?range=60d&interval=5m")
        self.assertIn("/chart/M%26M.NS?", tm.chart_url("M&M.NS", "query2"))
        self.assertEqual(tm.UA, "Mozilla/5.0 (compatible; india-day-trader-skill/1.0; +https://github.com/)")

    def test_429_waits_retries_then_falls_back_to_query2(self):
        self.answer([429, 429, 200])
        self.assertEqual(tm.fetch_chart("RELIANCE.NS"), TEXT)
        self.assertEqual(self.calls, ["query1", "query1", "query2"])
        self.assertEqual(self.sleeps, [tm.BACKOFF, tm.PAUSE])

    def test_5xx_falls_back(self):
        self.answer([503, 200])
        self.assertEqual(tm.fetch_chart("RELIANCE.NS"), TEXT)
        self.assertEqual(self.calls, ["query1", "query2"])

    def test_rate_limited_everywhere_is_one_plain_line(self):
        self.answer([429, 429, 429, 429])
        with self.assertRaises(tm.TMError) as cm:
            tm.fetch_chart("RELIANCE.NS")
        self.assertIn("rate limiting", str(cm.exception))

    def test_404(self):
        self.answer([404])
        with self.assertRaises(tm.TMError) as cm:
            tm.fetch_chart("ZZZZ.NS")
        self.assertIn("check the symbol", str(cm.exception))

    def test_run_over_the_network_path(self):
        self.answer([200])
        d = tempfile.mkdtemp()
        try:
            p = os.path.join(d, "plan.json")
            with open(p, "w") as fh:
                json.dump(PLAN, fh)
            code, text, _ = run(["run", "--plan", p, "--now", NOW, "--replays", "300"])
            self.assertEqual(code, 0)
            self.assertEqual(self.calls, ["query1"])
        finally:
            shutil.rmtree(d)


class Cli(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.plan = os.path.join(self.d, "trader-in", "plan.json")
        os.makedirs(os.path.dirname(self.plan))
        with open(self.plan, "w") as fh:
            json.dump(PLAN, fh, indent=2)
        self.out = os.path.join(self.d, "trader-in", "timemachine.json")

    def tearDown(self):
        shutil.rmtree(self.d)

    def test_run_with_the_plan_end_to_end(self):
        code, text, err = run(["run", "--plan", self.plan, "--offline", RELIANCE, "--now", NOW, "--write", self.out])
        self.assertEqual(code, 0, err)
        self.assertIn("target first   19.2 %", text)
        self.assertIn("Leans toward the target: target first 19.2 % vs stop first 13.9 %.", text)
        self.assertIn("Wrote %s." % self.out, text)
        self.assertIn("/india-budget", text)
        with open(self.out, encoding="utf-8") as fh:
            doc = json.load(fh)
        self.assertEqual(doc["created_by"], "india-timemachine")
        self.assertEqual(doc["source"], "offline")
        self.assertEqual((doc["mc"]["p_target"], doc["mc"]["avg_R"], doc["mc"]["avg_R_net"]), (0.1916, 0.047, -0.058))
        self.assertEqual(sha(doc["mc"]["sample_paths"]), PATHS)
        self.assertEqual((doc["history"]["triggered"], doc["history"]["avg_R"]), (27, 0.119))
        self.assertEqual(doc["plan"]["from"], self.plan)

    def test_json_output(self):
        code, text, _ = run(["run", "--plan", self.plan, "--offline", RELIANCE, "--now", NOW, "--json"])
        self.assertEqual(code, 0)
        doc = json.loads(text)
        self.assertEqual(doc["mc"]["p_stop"], 0.1388)
        self.assertNotIn("sample_paths", doc["mc"])
        self.assertEqual(doc["verdict"][:23], "Leans toward the target")
        self.assertFalse(os.path.exists(self.out))

    def test_the_plan_as_flags_matches_the_file(self):
        code, text, err = run(["run", "--symbol", "reliance", "--date", "2026-10-19", "--entry", "1227.8",
                               "--stop", "1215.3", "--target", "1240.3", "--entry-time", "09:50",
                               "--offline", RELIANCE, "--now", NOW, "--json"])
        self.assertEqual(code, 0, err)
        doc = json.loads(text)
        self.assertEqual((doc["mc"]["p_target"], doc["mc"]["p_green"], doc["mc"]["avg_R_net"]), (0.1916, 0.522, -0.058))
        self.assertEqual(doc["plan"]["stop_rule"], "or_mid")
        self.assertEqual((doc["history"]["target"], doc["history"]["stop"], doc["history"]["avg_R"]), (14, 10, 0.119))
        code, text, err = run(["run", "--symbol", "RELIANCE.NS", "--date", "2026-10-19", "--entry", "1227.8",
                               "--stop", "1215.3", "--target", "1240.3", "--entry-time", "09:50", "--qty", "100",
                               "--offline", RELIANCE, "--now", NOW, "--json"])
        self.assertEqual(json.loads(text)["mc"]["avg_R_net"], -0.026)
        code, _, err = run(["run", "--symbol", "RELIANCE", "--entry", "1227.8", "--offline", RELIANCE])
        self.assertEqual(code, 1)
        self.assertIn("--stop", err)

    def test_offline_folder_and_default_paths(self):
        shutil.copy(RELIANCE, self.d)
        cwd = os.getcwd()
        os.chdir(self.d)
        try:
            code, text, err = run(["run", "--offline", ".", "--now", NOW, "--replays", "500", "--write"])
            self.assertEqual(code, 0, err)
            self.assertTrue(os.path.exists(os.path.join("trader-in", "timemachine.json")))
            code, text, err = run(["chart", "--from"])
            self.assertEqual(code, 0, err)
            self.assertIn("* median", text)
            with open(os.path.join("trader-in", "timemachine.json"), encoding="utf-8") as fh:
                self.assertEqual(json.load(fh)["mc"]["replays"], 500)
        finally:
            os.chdir(cwd)

    def test_errors_are_one_plain_line(self):
        for argv in (["run", "--plan", os.path.join(self.d, "nope.json")],
                     ["run", "--plan", self.plan, "--offline", os.path.join(self.d, "missing.json.gz")],
                     ["run", "--plan", self.plan, "--offline", RELIANCE, "--until", "9:00"],
                     ["chart", "--from", os.path.join(self.d, "nope.json")]):
            code, text, err = run(argv)
            self.assertEqual(code, 1, argv)
            self.assertEqual(len(err.strip().splitlines()), 1, err)
            self.assertTrue(err.startswith("timemachine.py: "), err)
            self.assertNotIn("Traceback", err)
        self.assertIn("/india-exit", run(["run", "--plan", os.path.join(self.d, "nope.json")])[2])
        self.assertEqual(run([])[0], 1)

    def test_chart(self):
        run(["run", "--plan", self.plan, "--offline", RELIANCE, "--now", NOW, "--write", self.out])
        code, text, _ = run(["chart", "--from", self.out])
        self.assertEqual(code, 0)
        lines = text.splitlines()
        self.assertIn("2,000 replay paths from the 09:50 entry (seed 19)", lines[0])
        self.assertTrue(any(l.startswith("  1240.30 |") and l.endswith("  target") for l in lines))
        self.assertTrue(any(l.startswith("  1215.30 |") and l.endswith("  stop") for l in lines))
        self.assertTrue(any(l.startswith("  1227.80 |*") and l.endswith("  entry") for l in lines))
        self.assertIn("09:50", text)
        self.assertIn("15:00", text)
        self.assertEqual(lines[-3], "Leans toward the target: target first 19.2 % vs stop first 13.9 %.")
        code, again, _ = run(["chart", "--plan", self.plan, "--offline", RELIANCE, "--now", NOW])
        self.assertEqual(again, text)


class CasAndKite(unittest.TestCase):
    SHARED = os.path.join(FIX, "shared")
    KITE = [os.path.join(FIX, "shared", "kite-RELIANCE-5minute-2026-10-19-%s.json" % x) for x in ("open", "close")]

    def test_session_rules(self):
        r = tm.session_rules(True, "2026-10-19")
        self.assertEqual((r["close"], r["square_off"], r["flat_by_default"], r["bars_5m"]), ("15:15", "15:12", "15:00", 72))
        r = tm.session_rules(False, "2026-10-19")
        self.assertEqual((r["close"], r["square_off"], r["flat_by_default"], r["bars_5m"]), ("15:30", "15:25", "15:10", 75))
        self.assertEqual(tm.session_rules(None, "2026-10-19")["bars_5m"], 72)       # unknown -> strict
        self.assertEqual(tm.session_rules(True, "2026-07-31")["bars_5m"], 75)       # before the CAS started
        self.assertEqual(tm.session_rules(True, "2026-08-03")["bars_5m"], 72)

    def test_the_fo_list(self):
        with open(os.path.join(self.SHARED, "fo_mktlots-2026-10-23.csv"), encoding="utf-8") as fh:
            syms = tm.parse_fo_csv(fh.read())
        for s in ("RELIANCE", "TCS", "M&M"):
            self.assertIn(s, syms)
        for s in ("NIFTY", "BANKNIFTY", "FINNIFTY", "NIFTYFPI", "SYMBOL", ""):
            self.assertNotIn(s, syms)
        d = tempfile.mkdtemp()
        try:
            cache = os.path.join(d, "fo_list.txt")
            with open(cache, "w") as fh:
                fh.write("# fetched 2026-10-23\nRELIANCE\nTCS\n")
            today = tm.datetime.date(2026, 10, 23)
            self.assertEqual(tm.cas_status("RELIANCE", fo_path=cache, today=today, online=False)[0], True)
            self.assertEqual(tm.cas_status("IRCTC", fo_path=cache, today=today, online=False)[0], False)
            self.assertEqual(tm.cas_status("RELIANCE", "no", fo_path=cache, today=today)[0], False)
            self.assertEqual(tm.cas_status("IRCTC", config={"cas_symbols_extra": ["irctc"]}, fo_path=cache,
                                           today=today, online=False), (True, "config cas_symbols_extra"))
            self.assertEqual(tm.cas_status("TCS", config={"non_cas_symbols": ["TCS"]}, fo_path=cache,
                                           today=today, online=False)[0], False)
            cas, how = tm.cas_status("RELIANCE", fo_path=os.path.join(d, "none.txt"), today=today, online=False)
            self.assertIsNone(cas)
            self.assertIn("stricter CAS rules", how)
        finally:
            shutil.rmtree(d)

    def test_kite_bars_replace_only_the_plan_day(self):
        kite = tm.read_kite_bars(self.KITE, NOW_UTC)
        self.assertEqual(len(kite), 14)
        self.assertEqual((kite[0]["start"], kite[0]["h"], kite[0]["v"]), ("09:15", 1223.6, 615358))
        self.assertEqual(kite[6]["c"], 1227.7)              # Kite's 09:45 close (Yahoo says 1227.80)
        self.assertEqual(tm.read_kite_bars(self.KITE + self.KITE, NOW_UTC), kite)   # merged, de-duplicated
        doc = machine(kite_bars=kite)
        self.assertEqual(doc["data"]["plan_day_source"], "Kite candles")
        self.assertEqual(doc["plan"]["stop_rule"], "or_mid")   # Kite OR 1207.10-1223.60: mid 1215.35 -> 1215.30
        self.assertEqual((doc["mc"]["p_target"], doc["mc"]["p_stop"]), (0.1916, 0.1388))   # the pool is Yahoo
        self.assertEqual(doc["warnings"], [])
        self.assertIn("plan day's bars (Kite candles); the 56-session history (Yahoo, free, can lag)",
                      "\n".join(tm.report(doc)))
        other = machine(plan(date="2026-10-21", entry_time="09:50"), kite_bars=kite, replays=100)
        self.assertIn("Kite candles for 2026-10-19 ignored", other["warnings"][0])
        self.assertEqual(other["data"]["plan_day_source"], "Yahoo, free, can lag")
        # the session so far: at 09:47 the 09:45 bar has not closed
        with self.assertRaises(tm.TMError) as cm:
            machine(kite_bars=tm.read_kite_bars(self.KITE, tm.parse_now("2026-10-19 09:47")),
                    now=tm.parse_now("2026-10-19 09:47"))
        self.assertIn("not in the data", str(cm.exception))

    def test_kite_format_checks(self):
        d = tempfile.mkdtemp()
        try:
            one = os.path.join(d, "one.json")
            with open(one, "w") as fh:
                json.dump([{"date": "2026-10-19T09:15:00+05:30", "open": 1, "high": 1, "low": 1, "close": 1},
                           {"date": "2026-10-19T09:16:00+05:30", "open": 1, "high": 1, "low": 1, "close": 1}], fh)
            with self.assertRaises(tm.TMError) as cm:
                tm.read_kite_bars([one], NOW_UTC)
            self.assertIn("1-minute bars", str(cm.exception))
            wrapped = os.path.join(d, "wrapped.json")
            with open(self.KITE[0]) as fh:
                rows = json.load(fh)
            with open(wrapped, "w") as fh:
                json.dump({"data": rows}, fh)
            self.assertEqual(len(tm.read_kite_bars([wrapped], NOW_UTC)), 9)
        finally:
            shutil.rmtree(d)
        with self.assertRaises(tm.TMError) as cm:
            tm.read_offline(self.KITE[0], "RELIANCE")
        self.assertIn("--kite-bars", str(cm.exception))

    def test_kite_bars_on_the_command_line(self):
        code, text, err = run(["run", "--symbol", "RELIANCE", "--date", "2026-10-19", "--entry", "1227.7",
                               "--stop", "1215.3", "--target", "1240.1", "--entry-time", "09:50", "--cas", "yes",
                               "--offline", RELIANCE, "--now", NOW, "--replays", "300",
                               "--kite-bars", self.KITE[0], "--kite-bars", self.KITE[1]])
        self.assertEqual(code, 0, err)
        self.assertIn("(Kite candles)", text)
        self.assertIn("(--cas yes)", text)
        self.assertIn("09:50 to 15:00 IST (the default flat-by; 62 five-minute bars)", text)


if __name__ == "__main__":
    unittest.main()
