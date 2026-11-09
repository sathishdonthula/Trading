"""india-exit tests. Offline only. Bars for the ATR stop and the previous close come from
tests/fixtures/shared/RELIANCE-60d5m.json.gz (real Yahoo 5-minute bars, see test_entry.py).
tests/fixtures/india-exit/sec_list-2026-10-23.csv is a cut of NSE's real sec_list.csv
(price bands), fetched 2026-10-23: header plus eleven rows (EQ and BE, bands 2/5/10/20/No Band)."""

import io
import json
import os
import shutil
import tempfile
import unittest
import urllib.error
from contextlib import redirect_stdout, redirect_stderr
from decimal import Decimal

from _load import load, FIX, ROOT

exitp = load("india-exit", "exit")
entry = load("india-entry", "entry")

REL = os.path.join(FIX, "shared", "RELIANCE-60d5m.json.gz")
SEC = os.path.join(FIX, "india-exit", "sec_list-2026-10-23.csv")
FO_CSV = os.path.join(FIX, "shared", "fo_mktlots-2026-10-23.csv")
KITE_OPEN = os.path.join(FIX, "shared", "kite-RELIANCE-5minute-2026-10-19-open.json")
KITE_CLOSE = os.path.join(FIX, "shared", "kite-RELIANCE-5minute-2026-10-19-close.json")
KITE_INST = os.path.join(FIX, "shared", "kite-instruments-RELIANCE-2026-10-23.json")
KITE_LTP = os.path.join(FIX, "shared", "kite-ltp-2026-10-23.json")

# trader-in/plan.json for the worked example, the original contract's fields plus India's
EXAMPLE_PLAN = {"created_by": "india-exit", "symbol": "RELIANCE", "exchange": "NSE", "currency": "INR",
                "side": "buy", "date": "2026-10-19", "setup": "orb",
                "entry": 1227.8, "entry_time": "9:50 AM", "entry_bar": 6, "interval": "5m",
                "opening_range": {"high": 1223.4, "low": 1207.3, "minutes": 15},
                "stop": 1215.3, "stop_rule": "or_mid", "stop_label": "opening-range midpoint",
                "target": 1240.3, "target_r": 1.0, "target_label": "1R (one times the risk)",
                "risk_per_share": 12.5, "reward_risk": 1.0, "flat_by": "15:00", "tick": 0.1, "product": "MIS",
                "cas": True, "square_off": "15:12", "session_close": "15:15", "tick_source": "nse-table",
                "prev_close": 1211.2, "band_pct": None, "series": "EQ",
                "lower_circuit": None, "upper_circuit": None}
EXAMPLE_ENTRY = {"created_by": "india-entry", "symbol": "RELIANCE", "exchange": "NSE", "yahoo_symbol": "RELIANCE.NS",
                 "currency": "INR", "setup": "orb", "side": "buy", "date": "2026-10-19",
                 "status": "triggered", "time": "9:50 AM", "bar_start": "09:45", "bar_index": 6, "price": 1227.8,
                 "level": 1223.4, "interval": "5m", "opening_range": {"high": 1223.4, "low": 1207.3, "minutes": 15},
                 "prev_close": 1211.2, "source": "yahoo", "checked_at": "2026-10-19T04:20:00Z"}


def run(mod, argv):
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = mod.main(argv)
    return code, out.getvalue(), err.getvalue()


def c(entry_px, stop, target="2R", oh=1223.4, ol=1207.3, atr=None, n=None, lower=None):
    sr, sp = exitp.parse_stop(stop)
    tr, tp = exitp.parse_target(target)
    return exitp.compute(entry_px, sr, sp, tr, tp, oh, ol, atr, n, lower)


class Ticks(unittest.TestCase):
    def test_the_nse_tick_table(self):
        t = exitp.tick_size
        for price, tick in ((0.5, "0.01"), (249.99, "0.01"), (250, "0.05"), (999.95, "0.05"), (1000, "0.10"),
                            (4999.9, "0.10"), (5000, "0.50"), (9999.5, "0.50"), (10000, "1.00"), (19999, "1.00"),
                            (20000, "5.00"), (31415, "5.00")):
            self.assertEqual(t(price), Decimal(tick), price)

    def test_round_to_tick(self):
        r = exitp.round_to_tick
        self.assertEqual(r(1215.35, "down"), Decimal("1215.30"))
        self.assertEqual(r(1215.35, "up"), Decimal("1215.40"))
        self.assertEqual(r(1215.35, "nearest"), Decimal("1215.40"))
        self.assertEqual(r(453.12, "down"), Decimal("453.10"))
        self.assertEqual(r(453.12, "up"), Decimal("453.15"))
        self.assertEqual(r(199.999, "down"), Decimal("199.99"))
        self.assertEqual(r(7012.3, "down"), Decimal("7012.00"))
        self.assertEqual(r(25012, "down"), Decimal("25010.00"))
        self.assertEqual(r(1227.800048828125, "nearest"), Decimal("1227.80"))   # Yahoo's float

    def test_rupees(self):
        self.assertEqual(exitp.inr(1227.8), "₹1,227.80")
        self.assertEqual(exitp.inr(123456.78), "₹1,23,456.78")
        self.assertEqual(exitp.raw_inr(Decimal("1215.35")), "₹1,215.35")
        self.assertEqual(exitp.raw_inr(Decimal("1215.522")), "₹1,215.522")

    def test_circuits(self):
        self.assertEqual(exitp.circuits(210, 20), (Decimal("168.00"), Decimal("252.00")))
        self.assertEqual(exitp.circuits(1211.2, 10), (Decimal("1090.10"), Decimal("1332.30")))   # up / down to the tick
        with open(SEC, encoding="utf-8") as fh:
            table = exitp.parse_sec_list(fh.read())
        self.assertEqual(table["RELIANCE"], {"series": "EQ", "band": None, "band_text": "No Band"})
        self.assertEqual(table["20MICRONS"]["band"], 20.0)
        self.assertEqual(table["ABAN"]["series"], "BE")
        self.assertIn("M&M", table)


class Math(unittest.TestCase):
    def test_the_example_or_mid_1r(self):
        p = c(1227.8, "or-mid", "1R")
        self.assertEqual(p["stop"], Decimal("1215.30"))          # 1215.35 rounded down to the 0.10 tick
        self.assertEqual(p["risk"], Decimal("12.50"))
        self.assertEqual(p["target"], Decimal("1240.30"))
        self.assertEqual(p["reward_risk"], Decimal("1.00"))
        self.assertEqual(p["tick"], Decimal("0.10"))
        self.assertEqual(p["target_label"], "1R (one times the risk)")
        self.assertIn("₹1,215.35, rounded down to the ₹0.10 tick", p["stop_detail"])

    def test_the_default_2r(self):
        p = c(1227.8, "or-mid")
        self.assertEqual(p["target"], Decimal("1252.80"))
        self.assertEqual(p["target_label"], "2R (two times the risk)")

    def test_raw_yahoo_floats_give_the_same_prices(self):
        p = c(1227.800048828125, "or-mid", "1R", 1223.4000244140625, 1207.300048828125)
        self.assertEqual((p["entry"], p["stop"], p["target"]), (Decimal("1227.80"), Decimal("1215.30"), Decimal("1240.30")))

    def test_other_stops(self):
        p = c(1227.8, "or-low")
        self.assertEqual((p["stop"], p["risk"], p["target"]), (Decimal("1207.30"), Decimal("20.50"), Decimal("1268.80")))
        p = c(1227.8, "price:1215.39")
        self.assertEqual(p["stop"], Decimal("1215.30"))             # down to the tick, away from the entry
        p = c(1227.8, "pct:1")
        self.assertEqual((p["stop"], p["risk"], p["target"]), (Decimal("1215.50"), Decimal("12.30"), Decimal("1252.40")))
        p = c(1227.8, "atr:1.5", atr=7.4572, n=7)
        self.assertEqual(p["stop"], Decimal("1216.60"))             # 1227.80 - 11.1858 = 1216.6142
        self.assertEqual(p["stop_label"], "1.5 x ATR(7) under the entry")

    def test_targets_round_down_to_the_tick_and_price_targets(self):
        p = c(453.10, "price:450.00", "1.5R", None, None)
        self.assertEqual(p["target"], Decimal("457.75"))            # 453.10 + 4.65 = 457.75, on the 0.05 grid
        p = c(453.10, "price:450.02", "1.5R", None, None)
        self.assertEqual(p["stop"], Decimal("450.00"))
        p = c(453.10, "price:451.00", "1.5R", None, None)
        self.assertEqual(p["target"], Decimal("456.25"))            # 453.10 + 3.15 = 456.25
        p = c(453.10, "price:451.10", "1.5R", None, None)
        self.assertEqual(p["target"], Decimal("456.10"))            # 456.10 exactly
        p = c(453.10, "price:451.05", "1.7R", None, None)
        self.assertEqual(p["target"], Decimal("456.55"))            # 456.585 rounds down to 456.55
        self.assertIn("rounded down to the tick", p["target_label"])
        p = c(1227.8, "or-mid", "price:1245")
        self.assertEqual(p["reward_risk"], Decimal("1.38"))
        self.assertEqual(p["target_label"], "your price (1.38R)")

    def test_refusals_say_why(self):
        cases = [("price:1227.80", "2R", "at or above the entry"),
                 ("price:1230", "2R", "at or above the entry"),
                 ("or-mid", "price:1227", "at or below the entry"),
                 ("price:1227.30", "2R", "too tight to survive the spread"),
                 ("pct:0.04", "2R", "too tight to survive the spread")]
        for stop, target, why in cases:
            with self.assertRaises(exitp.PlanError) as cm:
                c(1227.8, stop, target)
            self.assertIn(why, str(cm.exception), stop)
        # 0.05% of 1227.80 is 0.6139: a 0.70 risk is allowed, 0.60 is not
        self.assertEqual(c(1227.8, "price:1227.10")["risk"], Decimal("0.70"))
        with self.assertRaises(exitp.PlanError):
            c(1227.8, "price:1227.20")
        with self.assertRaises(exitp.PlanError):
            c(1212.0, "or-mid")                                    # the midpoint is above this entry
        for bad in ("mid-ish", "pct:100", "price:0", "atr:-1"):
            with self.assertRaises(exitp.PlanError):
                exitp.parse_stop(bad)
        for bad in ("0R", "R", "lots", "price:x"):
            with self.assertRaises(exitp.PlanError):
                exitp.parse_target(bad)
        with self.assertRaises(exitp.PlanError):
            c(1227.8, "or-mid", oh=None, ol=None)
        with self.assertRaises(exitp.PlanError):
            c(1227.8, "or-mid", oh=1207.3, ol=1223.4)

    def test_a_stop_below_the_lower_circuit_is_refused(self):
        with self.assertRaises(exitp.PlanError) as cm:
            c(200, "pct:17", oh=None, ol=None, lower=Decimal("168.00"))
        self.assertIn("below today's lower circuit ₹168.00", str(cm.exception))
        self.assertEqual(c(200, "pct:16", oh=None, ol=None, lower=Decimal("168.00"))["stop"], Decimal("168.00"))

    def test_atr_on_the_session_bars(self):
        ch = exitp.load_chart("RELIANCE", "5m", None, REL)
        day = exitp.datetime.date(2026, 10, 19)
        bars = exitp.session_bars(ch, day, "5m", upto_slot=6)
        self.assertEqual(len(bars), 7)                              # 9:15 ... the 9:45 entry bar
        trs, prev = [], None
        for b in bars:
            r = b["h"] - b["l"]
            if prev is not None:
                r = max(r, abs(b["h"] - prev), abs(b["l"] - prev))
            trs.append(r)
            prev = b["c"]
        self.assertAlmostEqual(trs[0], 1223.4 - 1211.9, places=3)
        atr, n = exitp.atr_of(bars)
        self.assertEqual(n, 7)
        self.assertAlmostEqual(atr, sum(trs) / 7, places=9)
        self.assertAlmostEqual(atr, 7.4572, places=4)
        all_bars = exitp.session_bars(ch, day, "5m")
        self.assertEqual(len(all_bars), 72)                         # CAS rules: 72 slots to 15:15
        self.assertEqual(len(exitp.session_bars(ch, day, "5m", cas=False)), 74)   # 75 slots, the empty 15:20 skipped
        self.assertEqual(exitp.atr_of(all_bars)[1], 14)
        self.assertEqual(exitp.prev_session_close(ch, day), 1211.2)
        self.assertEqual(exitp.prev_session_close(ch, exitp.datetime.date(2026, 10, 23)), 1184.5)


class Cli(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.cwd = os.getcwd()
        os.chdir(self.d)
        os.mkdir("trader-in")
        shutil.copy(SEC, os.path.join("trader-in", "sec_list.csv"))     # today's mtime: no fetch
        shutil.copy(FO_CSV, os.path.join("trader-in", "fo_list.txt"))   # NSE's F&O list: RELIANCE is a CAS stock
        self._open, self._fetch = exitp._open_url, exitp._fetch_text

        def no_net(url, timeout=20, ua=None):
            raise urllib.error.URLError("offline test: %s" % url)
        exitp._open_url = no_net

        def no_fetch(url, timeout=20):
            raise OSError("offline test: %s" % url)
        exitp._fetch_text = no_fetch

    def tearDown(self):
        exitp._open_url, exitp._fetch_text = self._open, self._fetch
        os.chdir(self.cwd)
        shutil.rmtree(self.d)

    def write(self, path, doc):
        with open(path, "w") as fh:
            json.dump(doc, fh)

    def plan(self, *args):
        return run(exitp, ["plan", "--now", "2026-10-19 09:51"] + list(args))

    def read_plan(self):
        with open(os.path.join("trader-in", "plan.json"), encoding="utf-8") as fh:
            return json.load(fh)

    def test_entry_to_plan_end_to_end(self):
        code, _, _ = run(entry, ["check", "RELIANCE", "--offline", REL, "--now", "2026-10-19 09:50", "--write"])
        self.assertEqual(code, 0)
        code, text, err = self.plan("--stop", "or-mid", "--target", "1R", "--write")
        self.assertEqual(code, 0, err)
        p = self.read_plan()
        for k, v in EXAMPLE_PLAN.items():
            self.assertEqual(p[k], v, k)
        self.assertIn("Stop          ₹1,215.30  opening-range midpoint", text)
        self.assertIn("Target        ₹1,240.30  1R (one times the risk)", text)
        self.assertIn("Risk a share  ₹12.50 (1.02% of the entry)", text)
        self.assertIn("Reward:risk   1.0", text)
        self.assertIn("Flat by       15:00 IST", text)
        self.assertIn("Zerodha squares off MIS at 15:12", text)
        self.assertIn("Entry         \u20b91,227.80 at 9:50 AM (bar 6) (Yahoo, free, can lag)", text)
        self.assertIn("No Band", text)
        self.assertIn("not a guarantee", text)
        self.assertIn("/india-timemachine", text)
        self.assertNotIn("pro" + "fit", text.lower())
        code, text, _ = self.plan("--write")                       # the defaults: or-mid, 2R
        self.assertEqual(self.read_plan()["target"], 1252.8)
        self.assertEqual(self.read_plan()["target_r"], 2.0)
        code, text, _ = run(exitp, ["show", "--now", "2026-10-19 10:00"])
        self.assertEqual(code, 0)
        self.assertIn("₹1,252.80", text)
        self.assertNotIn("not today", text)
        code, text, _ = run(exitp, ["show", "--now", "2026-10-21 10:00"])
        self.assertIn("not today", text)

    def test_the_plan_feeds_the_time_machine_shape(self):
        self.write(os.path.join("trader-in", "entry.json"), EXAMPLE_ENTRY)
        self.plan("--target", "1R", "--write")
        p = self.read_plan()
        for k in ("symbol", "exchange", "currency", "side", "date", "setup", "entry", "entry_time", "entry_bar",
                  "interval", "opening_range", "stop", "stop_rule", "target", "target_r", "risk_per_share",
                  "reward_risk", "flat_by", "tick", "product"):
            self.assertIn(k, p, k)
        self.assertTrue(p["stop"] < p["entry"] < p["target"])

    def test_manual_flags_and_json(self):
        code, text, _ = self.plan("--symbol", "reliance", "--entry", "1227.80", "--or-high", "1223.40",
                                  "--or-low", "1207.30", "--time", "9:50 AM", "--entry-bar", "6",
                                  "--date", "2026-10-19", "--target", "1R", "--prev-close", "1211.2", "--json")
        self.assertEqual(code, 0)
        p = json.loads(text)
        for k, v in EXAMPLE_PLAN.items():
            self.assertEqual(p[k], v, k)

    def test_atr_stop_reads_bars(self):
        self.write(os.path.join("trader-in", "entry.json"), EXAMPLE_ENTRY)
        code, text, err = self.plan("--stop", "atr:1.5", "--offline", REL, "--json")
        self.assertEqual(code, 0, err)
        p = json.loads(text)
        self.assertEqual((p["stop"], p["risk_per_share"], p["target"]), (1216.6, 11.2, 1250.2))
        self.assertEqual((p["stop_rule"], p["atr_bars"]), ("atr", 7))
        self.assertAlmostEqual(p["atr"], 7.4572, places=4)
        code, _, err = self.plan("--stop", "atr:1.5", "--offline", REL, "--entry-bar", "1")
        self.assertEqual(code, 1)
        self.assertIn("at least 3 closed", err)

    def test_circuit_band_from_the_list_and_from_flags(self):
        base = ["--symbol", "20MICRONS", "--entry", "200", "--prev-close", "210"]
        code, _, err = self.plan(*(base + ["--stop", "pct:17"]))
        self.assertEqual(code, 1)
        self.assertIn("lower circuit ₹168.00", err)
        code, text, err = self.plan(*(base + ["--stop", "pct:5", "--json"]))
        self.assertEqual(code, 0, err)
        p = json.loads(text)
        self.assertEqual((p["band_pct"], p["lower_circuit"], p["upper_circuit"]), (20.0, 168.0, 252.0))
        self.assertEqual((p["stop"], p["target"], p["tick"]), (190.0, 220.0, 0.01))
        code, text, _ = self.plan(*(base + ["--stop", "pct:5", "--target", "price:255"]))
        self.assertIn("above today's upper circuit ₹252.00", text)
        code, text, _ = self.plan("--symbol", "20MICRONS", "--entry", "250.5", "--prev-close", "210",
                                  "--stop", "pct:3")
        self.assertIn("within 1% of the upper circuit", text)
        # --band overrides the list; BSE needs --band
        code, _, err = self.plan(*(base + ["--stop", "pct:12", "--band", "10"]))
        self.assertIn("lower circuit ₹189.00", err)
        code, text, _ = self.plan("--symbol", "BSE:RELIANCE", "--entry", "1227.8", "--stop", "pct:1", "--json")
        p = json.loads(text)
        self.assertEqual((p["exchange"], p["band_pct"]), ("BSE", None))
        self.assertIn("give --band", " ".join(p["warnings"]))

    def test_be_series_is_flagged_and_unknown_list_does_not_block(self):
        code, text, _ = self.plan("--symbol", "ABAN", "--entry", "30", "--stop", "pct:1", "--prev-close", "30",
                                  "--json")
        p = json.loads(text)
        self.assertEqual((p["series"], p["product"], p["band_pct"]), ("BE", "CNC", 2.0))
        self.assertIn("trade-to-trade", " ".join(p["warnings"]))
        os.remove(os.path.join("trader-in", "sec_list.csv"))
        code, text, err = self.plan("--symbol", "TCS", "--entry", "3000", "--stop", "pct:1", "--json")
        self.assertEqual(code, 0, err)
        self.assertIn("did not load", " ".join(json.loads(text)["warnings"]))

    def test_refusals_from_the_cli(self):
        self.write(os.path.join("trader-in", "entry.json"), dict(EXAMPLE_ENTRY, status="waiting", time=None,
                                                                 price=None, bar_index=None, bar_start=None))
        code, _, err = self.plan()
        self.assertEqual(code, 1)
        self.assertIn("RELIANCE is waiting, not triggered", err)
        code, text, _ = self.plan("--entry", "1223.50", "--json")    # plan ahead at the level
        self.assertEqual(code, 0)
        p = json.loads(text)
        self.assertEqual((p["entry"], p["stop"], p["entry_time"]), (1223.5, 1215.3, None))

        self.write(os.path.join("trader-in", "entry.json"), dict(EXAMPLE_ENTRY, setup="level", level=1225.0))
        code, _, err = self.plan()
        self.assertIn("level setup has no opening-range stop", err)
        self.assertEqual(self.plan("--stop", "pct:1")[0], 0)

        code, _, err = self.plan("--side", "sell")
        self.assertIn("longs only", err)
        self.write(os.path.join("trader-in", "entry.json"), dict(EXAMPLE_ENTRY, side="sell"))
        code, _, err = self.plan()
        self.assertIn("longs only", err)
        os.remove(os.path.join("trader-in", "entry.json"))
        code, _, err = self.plan()
        self.assertIn("no entry", err)
        code, _, err = self.plan("--symbol", "RELIANCE")
        self.assertIn("needs --entry", err)
        code, _, err = self.plan("--symbol", "RELIANCE", "--from", "x.json", "--entry", "1")
        self.assertIn("not both", err)
        code, _, err = self.plan("--symbol", "RELIANCE", "--entry", "1227.8", "--or-high", "1223.4")
        self.assertIn("both --or-high and --or-low", err)
        self.assertNotIn("Traceback", err)

    def test_flat_by_config_square_off_and_late_entries(self):
        self.write(os.path.join("trader-in", "entry.json"), EXAMPLE_ENTRY)
        self.write(os.path.join("trader-in", "config.json"), {"flat_by": "14:50", "min_reward_risk": 1.5})
        code, text, _ = self.plan("--target", "1R", "--json")
        p = json.loads(text)
        self.assertEqual(p["flat_by"], "14:50")
        self.assertIn("under your min_reward_risk 1.5", " ".join(p["warnings"]))
        # a config flat_by at or after RELIANCE's 15:12 square-off is clamped to 15:00
        self.write(os.path.join("trader-in", "config.json"), {"flat_by": "15:20"})
        code, text, _ = self.plan("--json")
        p = json.loads(text)
        self.assertEqual(p["flat_by"], "15:00")
        self.assertIn("15:12 MIS auto square-off", " ".join(p["warnings"]))
        os.remove(os.path.join("trader-in", "config.json"))
        for late in ("15:12", "15:25", "15:30"):
            code, text, _ = self.plan("--flat-by", late)
            self.assertEqual(code, 0)
            self.assertIn("Flat by       15:00 IST", text)
            self.assertIn("moved to 15:00", text)
        self.assertIn("Flat by       15:11 IST", self.plan("--flat-by", "15:11")[1])
        # a non-CAS stock: auto is 15:10, square-off 15:25
        base = ["--symbol", "20MICRONS", "--entry", "200", "--prev-close", "210", "--stop", "pct:5", "--json"]
        p = json.loads(self.plan(*base)[1])
        self.assertEqual((p["cas"], p["flat_by"], p["square_off"], p["session_close"]), (False, "15:10", "15:25", "15:30"))
        p = json.loads(self.plan(*(base + ["--flat-by", "15:20"]))[1])
        self.assertEqual(p["flat_by"], "15:20")
        p = json.loads(self.plan(*(base + ["--flat-by", "15:25"]))[1])
        self.assertEqual(p["flat_by"], "15:10")
        p = json.loads(self.plan(*(base + ["--cas", "yes"]))[1])
        self.assertEqual((p["cas"], p["flat_by"]), (True, "15:00"))
        os.remove(os.path.join("trader-in", "fo_list.txt"))       # no list, offline: the stricter CAS rules
        p = json.loads(self.plan(*base[:-1] + ["--no-fetch", "--json"])[1])
        self.assertEqual((p["cas"], p["flat_by"], p["session_close"]), (None, "15:00", "15:15"))
        self.assertIn("stricter CAS rules", " ".join(p["warnings"]))
        self.write(os.path.join("trader-in", "entry.json"), dict(EXAMPLE_ENTRY, time="3:15 PM"))
        code, _, err = self.plan()
        self.assertEqual(code, 1)
        self.assertIn("no time left to hold it", err)
        self.write(os.path.join("trader-in", "entry.json"), dict(EXAMPLE_ENTRY, date="2026-10-20"))
        code, text, _ = self.plan()
        self.assertIn("not an NSE trading day", text)

    def test_the_kite_example(self):
        code, _, err = run(entry, ["check", "RELIANCE", "--kite-bars", KITE_OPEN, "--now", "2026-10-19 09:50",
                                   "--fo-list", FO_CSV, "--no-fetch", "--write"])
        self.assertEqual(code, 0, err)
        code, text, err = self.plan("--stop", "or-mid", "--target", "1R", "--write")
        self.assertEqual(code, 0, err)
        p = self.read_plan()
        self.assertEqual((p["entry"], p["stop"], p["risk_per_share"], p["target"], p["entry_source"]),
                         (1227.7, 1215.3, 12.4, 1240.1, "kite"))
        self.assertEqual(p["opening_range"], {"high": 1223.6, "low": 1207.1, "minutes": 15})
        self.assertEqual((p["cas"], p["flat_by"], p["square_off"], p["session_close"]), (True, "15:00", "15:12", "15:15"))
        self.assertIn("Entry         \u20b91,227.70 at 9:50 AM (bar 6) (Kite candles)", text)
        self.assertIn("(\u20b91,223.60 + \u20b91,207.10) / 2 = \u20b91,215.35, rounded down", text)
        self.assertEqual(json.loads(self.plan("--json")[1])["target"], 1252.5)     # 2R
        # an ATR stop on the Kite candles (both chunks merged)
        p = json.loads(self.plan("--stop", "atr:1.5", "--kite-bars", KITE_OPEN, "--kite-bars", KITE_CLOSE,
                                 "--json")[1])
        self.assertEqual((p["stop"], p["atr_bars"], p["risk_per_share"]), (1215.5, 7, 12.2))
        self.assertAlmostEqual(p["atr"], 8.1286, places=4)

    def test_kite_instrument_wins_only_on_its_own_day(self):
        self.write(os.path.join("trader-in", "entry.json"), EXAMPLE_ENTRY)
        with open(KITE_INST) as fh:
            inst = json.load(fh)
        self.write("same-day.json", dict(inst, fetched="2026-10-19"))
        code, text, err = self.plan("--kite-instrument", "same-day.json", "--json")
        self.assertEqual(code, 0, err)
        p = json.loads(text)
        self.assertEqual((p["lower_circuit"], p["upper_circuit"], p["circuit_source"]), (1066.1, 1302.9, "kite"))
        self.assertEqual((p["tick"], p["tick_source"]), (0.1, "kite"))
        text = self.plan("--kite-instrument", "same-day.json")[1]
        self.assertIn("Circuit       \u20b91,066.10 to \u20b91,302.90 (Kite instrument, today's limits)", text)
        self.assertIn("Tick          \u20b90.10 (Kite instrument)", text)
        self.assertNotIn("No Band", text)                       # Kite has limits for an F&O stock
        code, _, err = self.plan("--kite-instrument", "same-day.json", "--stop", "pct:15")
        self.assertEqual(code, 1)
        self.assertIn("below today's lower circuit \u20b91,066.10", err)
        self.write("other-day.json", dict(inst, fetched="2026-10-23"))
        p = json.loads(self.plan("--kite-instrument", "other-day.json", "--json")[1])
        self.assertEqual((p["tick_source"], p["circuit_source"], p["lower_circuit"]), ("nse-table", None, None))
        self.assertIn("from 2026-10-23, not the plan's day 2026-10-19", " ".join(p["warnings"]))
        code, _, err = self.plan("--symbol", "TCS", "--entry", "3000", "--stop", "pct:1",
                                 "--kite-instrument", "same-day.json")
        self.assertIn("no NSE:TCS instrument", err)

    def test_kite_ltp_as_the_entry(self):
        code, text, err = run(exitp, ["plan", "--now", "2026-10-23 12:00", "--symbol", "RELIANCE",
                                      "--kite-ltp", KITE_LTP, "--stop", "pct:1", "--prev-close", "1184.5"])
        self.assertEqual(code, 0, err)
        self.assertIn("Entry         \u20b91,190.00 (Kite, live)", text)
        self.assertIn("Stop          \u20b91,178.10", text)
        code, text, _ = run(exitp, ["plan", "--now", "2026-10-23 12:00", "--symbol", "RELIANCE",
                                    "--price", "RELIANCE=1191.2", "--stop", "pct:1", "--json"])
        p = json.loads(text)
        self.assertEqual((p["entry"], p["entry_source"]), (1191.2, "flags"))

    def test_show_without_a_plan(self):
        code, text, _ = run(exitp, ["show"])
        self.assertEqual(code, 1)
        self.assertIn("No plan", text)


class SkillDoc(unittest.TestCase):
    def test_skill_md(self):
        with open(os.path.join(ROOT, "india-exit", "SKILL.md"), encoding="utf-8") as fh:
            text = fh.read()
        self.assertTrue(text.startswith("---\nname: india-exit\n"))
        self.assertGreater(len(text.splitlines()), 60)
        safety = " ".join(text[text.index("## Safety"):].lower().split())
        for must in ("not financial advice", "india-broker", "india-risk", "needs approval", "no order without your yes"):
            self.assertIn(must, safety)
        low = " ".join(text.lower().split())
        for must in ("before the entry", "a stop is a plan, not a guarantee", "day trading loses most people money",
                     "/india-timemachine", "/india-budget", "longs only", "₹1,215.30", "₹1,240.30",
                     "₹12.50", "₹1,252.80", "too tight to survive the spread", "lower circuit", "tick",
                     "15:10", "15:25", "trader-in/plan.json", "kite mcp", "sec_list.csv",
                     "with zerodha kite connected", "get_historical_data", "search_instruments", "get_ltp",
                     "trader-in/kite/", "--kite-instrument", "\u20b91,227.70", "\u20b91,240.10", "\u20b912.40",
                     "15:12", "15:00", "cas", "square_off", "session_close"):
            self.assertIn(must, low)
        for never in ("\u2014", "tick " + "by tick", "real " + "time", "pro" + "fit", "new york", "trader/"):
            self.assertNotIn(never, low)


if __name__ == "__main__":
    unittest.main()
