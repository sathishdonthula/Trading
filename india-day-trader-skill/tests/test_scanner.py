import datetime
import gzip
import io
import json
import os
import shutil
import tempfile
import unittest
import urllib.error
from contextlib import redirect_stdout, redirect_stderr

from _load import load, FIX

scan = load("india-scanner", "scan")
# hermetic: never read a trader-in/fo_list.txt cache from wherever the tests run
scan.DEFAULT_FO_LIST = os.path.join(tempfile.gettempdir(), "india-scanner-tests-no-cache", "fo_list.txt")

SDIR = os.path.join(FIX, "india-scanner")
WALL = os.path.join(SDIR, "nifty50-5m-2026-10-19.json.gz")      # NIFTY 50, real Yahoo 5-min, Oct 16 tail + Oct 19
DAILY = os.path.join(SDIR, "daily-spark-2026-10-23.json")       # 40 NIFTY 50 names, real 3mo daily spark
SPARK5 = os.path.join(SDIR, "spark-5m-live-2026-10-23-1129.json")  # real, fetched 11:29:51 AM IST Oct 23
REL5 = os.path.join(SDIR, "RELIANCE-5m-live-2026-10-23-1129.json")  # real v8, fetched with it
NSE = os.path.join(SDIR, "nse")                                  # trimmed real NSE CSVs of Oct 23
SHARED = os.path.join(FIX, "shared", "RELIANCE-60d5m.json.gz")   # the pack's shared RELIANCE chart
FO = os.path.join(FIX, "shared", "fo_mktlots-2026-10-23.csv")    # NSE F&O list = the CAS stocks
KITE_OPEN = os.path.join(FIX, "shared", "kite-RELIANCE-5minute-2026-10-19-open.json")    # Kite get_historical_data
KITE_CLOSE = os.path.join(FIX, "shared", "kite-RELIANCE-5minute-2026-10-19-close.json")
KITE_LTP = os.path.join(FIX, "shared", "kite-ltp-2026-10-23.json")                       # Kite get_ltp
DT = datetime.datetime
D = datetime.date
MON = D(2026, 10, 19)


def run(argv):
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = scan.main(argv)
    return code, out.getvalue(), err.getvalue()


def ts(ist_text):
    utc = scan.ist_to_utc(DT.strptime(ist_text, "%Y-%m-%d %H:%M"))
    return int((utc - DT(1970, 1, 1)).total_seconds())


def bars_series(day, closes, highs=None, lows=None, start="09:15"):
    t0 = DT.strptime(day + " " + start, "%Y-%m-%d %H:%M")
    t = [ts((t0 + datetime.timedelta(minutes=5 * i)).strftime("%Y-%m-%d %H:%M")) for i in range(len(closes))]
    return {"t": t, "c": list(closes), "h": list(highs or [c + 0.1 if c is not None else None for c in closes]),
            "l": list(lows or [c - 0.1 if c is not None else None for c in closes]), "o": None,
            "v": [1000] * len(closes), "meta": {}}


def read(path):
    with open(path, encoding="utf-8") as fh:
        return fh.read()


class Symbols(unittest.TestCase):
    def test_parse_and_yahoo(self):
        self.assertEqual(scan.parse_symbol("reliance"), ("RELIANCE", "NSE"))
        self.assertEqual(scan.parse_symbol("RELIANCE.NS"), ("RELIANCE", "NSE"))
        self.assertEqual(scan.parse_symbol("NSE:M&M"), ("M&M", "NSE"))
        self.assertEqual(scan.parse_symbol("RELIANCE.BO"), ("RELIANCE", "BSE"))
        self.assertEqual(scan.parse_symbol("BSE:RELIANCE"), ("RELIANCE", "BSE"))
        self.assertEqual(scan.parse_symbol("500325"), ("500325", "BSE"))
        self.assertEqual(scan.to_yahoo("M&M"), "M%26M.NS")
        self.assertEqual(scan.to_yahoo("BAJAJ-AUTO"), "BAJAJ-AUTO.NS")
        self.assertEqual(scan.to_yahoo("BSE:500325"), "500325.BO")
        self.assertEqual(scan.spark_path(["M&M", "TCS"], "3mo", "1d"),
                         "/v7/finance/spark?symbols=M%26M.NS,TCS.NS&range=3mo&interval=1d")
        self.assertEqual(scan.chart_path("RELIANCE", "1d", "5m"),
                         "/v8/finance/chart/RELIANCE.NS?range=1d&interval=5m")

    def test_ticks_bands_and_rupees(self):
        self.assertEqual([scan.tick_size(p) for p in (249.99, 250, 999, 1000, 5000, 10000, 20001)],
                         [0.01, 0.05, 0.05, 0.10, 0.50, 1.00, 5.00])
        self.assertEqual(scan.round_to_tick(1223.47, "down"), 1223.4)
        self.assertEqual(scan.round_to_tick(1223.41, "up"), 1223.5)
        # MRF, band 20 %, closed 1,18,860 the session before: upper 1,42,632 -> 1,42,630 on the 5-rupee tick
        self.assertEqual(scan.circuits(118860.0, 20.0), (142630.0, 95090.0))
        self.assertEqual(scan.circuits(100.0, None), (None, None))
        f = scan.band_fields(119.0, 100.0, 20.0)
        self.assertEqual((f["upper_circuit"], f["near_upper_circuit"]), (120.0, True))   # within 1 % of 120
        self.assertFalse(scan.band_fields(118.7, 100.0, 20.0)["near_upper_circuit"])
        self.assertEqual(scan.inr(123456.78), "₹1,23,456.78")
        self.assertEqual(scan.inr(117915), "₹1,17,915.00")
        self.assertEqual(scan.indian(12345678, 0), "1,23,45,678")
        self.assertEqual(scan.crore(5e7), "₹5 crore")


class Universe(unittest.TestCase):
    def texts(self):
        return {f: read(os.path.join(NSE, f)) for f in os.listdir(NSE)}

    def test_eq_kept_t2t_flagged(self):
        t = self.texts()
        kept, counts = scan.build_universe(t["EQUITY_L.csv"], t["sec_list.csv"])
        syms = [k[0] for k in kept]
        self.assertEqual(counts["rows"], 22)
        self.assertEqual(len(kept), 17)
        self.assertEqual((counts["t2t_BE"], counts["t2t_BZ"]), (3, 2))
        self.assertNotIn("3IINFOLTD", syms)                       # BE: trade-to-trade
        self.assertNotIn("AGSTRA", syms)                          # BZ
        d = {k[0]: k for k in kept}
        self.assertEqual(d["M&M"], ("M&M", "Mahindra & Mahindra Limited", "EQ", None, None))  # No Band, no F&O list
        self.assertEqual(d["MRF"][3], 20.0)
        self.assertEqual(d["21STCENMGM"][3], 5.0)
        kept, counts = scan.build_universe(t["EQUITY_L.csv"], t["sec_list.csv"], include_t2t=True)
        d = {k[0]: k for k in kept}
        self.assertEqual(len(kept), 22)
        self.assertEqual(d["AGSTRA"][2:], ("BZ", 2.0, None))

    def test_cas_from_the_fo_list(self):
        t = self.texts()
        fo = scan.parse_fo_list(read(FO))
        self.assertEqual(len(fo), 213)                            # stocks only: index rows and the header skipped
        self.assertTrue({"RELIANCE", "M&M", "BAJAJ-AUTO", "TCS"} <= fo)
        self.assertFalse({"NIFTY", "BANKNIFTY", "SYMBOL", "MRF", "IRCTC"} & fo)
        kept, _ = scan.build_universe(t["EQUITY_L.csv"], t["sec_list.csv"], fo=fo)
        d = {k[0]: k for k in kept}
        self.assertEqual(d["RELIANCE"][4], True)
        self.assertEqual(d["MRF"][4], False)
        self.assertEqual(sum(1 for k in kept if k[4]), 9)
        self.assertEqual(scan.parse_fo_list("# fetched 2026-10-23\nRELIANCE\nM&M\n"), {"RELIANCE", "M&M"})

    def test_session_rules(self):
        cas = scan.session_rules(True, MON)
        self.assertEqual((cas["close"], cas["square_off"], cas["flat_by_default"], cas["bars_5m"]),
                         ("15:15", "15:12", "15:00", 72))
        non = scan.session_rules(False, MON)
        self.assertEqual((non["close"], non["square_off"], non["flat_by_default"], non["bars_5m"]),
                         ("15:30", "15:25", "15:10", 75))
        self.assertEqual(scan.session_rules(None, MON)["close"], "15:15")          # unknown: the strict rules
        self.assertEqual(scan.session_rules(True, D(2026, 7, 31))["bars_5m"], 75)  # before CAS began on Aug 3

    def test_index_filter(self):
        t = self.texts()
        kept, counts = scan.build_universe(t["EQUITY_L.csv"], t["sec_list.csv"], t["ind_nifty50list.csv"])
        self.assertEqual([k[0] for k in kept], ["BAJAJ-AUTO", "HDFCBANK", "INFY", "M&M", "RELIANCE", "TCS"])
        self.assertEqual(counts["index_members"], 50)
        kept, _ = scan.build_universe(t["EQUITY_L.csv"], t["sec_list.csv"], t["ind_nifty500list.csv"])
        self.assertEqual(len(kept), 12)
        self.assertIn("IRCTC", [k[0] for k in kept])

    def test_not_nse_files(self):
        with self.assertRaises(scan.ScanError):
            scan.parse_equity_list("<html>Access Denied</html>")
        with self.assertRaises(scan.ScanError):
            scan.parse_bands("a,b\n1,2\n")


class Time(unittest.TestCase):
    def test_ist_clock(self):
        self.assertEqual(scan.ts_to_ist(ts("2026-10-19 09:15")), DT(2026, 10, 19, 9, 15))
        self.assertEqual(scan.parse_now("2026-10-19 10:15"), DT(2026, 10, 19, 4, 45))
        self.assertEqual(scan.parse_now("2026-10-19T04:45:00Z"), DT(2026, 10, 19, 4, 45))
        self.assertEqual(scan.parse_now("2026-12-01 10:15"), DT(2026, 12, 1, 4, 45))   # no daylight saving
        self.assertEqual(scan.clock_label(DT(2026, 10, 19, 9, 50)), "9:50 AM")
        self.assertEqual(scan.clock_label(datetime.time(15, 30)), "3:30 PM")
        with self.assertRaises(scan.ScanError):
            scan.parse_now("tomorrow")
        with self.assertRaises(scan.ScanError):
            scan.parse_hhmm("25:00")

    def test_holidays(self):
        self.assertEqual(scan.trading_day(D(2026, 10, 20)), (False, "NSE holiday: Dussehra"))
        self.assertTrue(scan.trading_day(MON)[0])
        self.assertEqual(scan.trading_day(D(2026, 11, 8))[1], "weekend")   # Muhurat Sunday
        with open(os.path.join(SDIR, "holiday-master-2026-10-23.json"), encoding="utf-8") as fh:
            live = scan.parse_holidays(json.load(fh))
        self.assertEqual(len(live), 20)
        for d in live:                                   # the built-in list is NSE's list
            self.assertIn(d, scan.NSE_HOLIDAYS)
        self.assertEqual(live["2026-11-08"], "Diwali Laxmi Pujan")
        self.assertEqual(scan.parse_holidays({"holidays": {"2027-01-26": "Republic Day"}}),
                         {"2027-01-26": "Republic Day"})
        self.assertEqual(scan.parse_holidays(["2027-03-22"]), {"2027-03-22": "NSE holiday"})

    def test_2027_not_published_warns_offline(self):
        msg = scan.ensure_holidays(D(2027, 1, 4), None, offline=True)
        self.assertIn("2027 holiday list is not in this tool", msg)
        self.assertEqual(scan.ensure_holidays(MON, None, offline=True), "")


class Network(unittest.TestCase):
    def setUp(self):
        self.calls, self.sleeps = [], []
        self._get, self._sleep = scan.http_get, scan.SLEEP
        scan.SLEEP = self.sleeps.append
        self.body = read(DAILY)

    def tearDown(self):
        scan.http_get, scan.SLEEP = self._get, self._sleep

    def answers(self, codes):
        codes = list(codes)

        def fake(url, timeout=20, ua=scan.UA):
            self.calls.append((url.split("/v")[0] if "yahoo" in url else url, ua))
            code = codes.pop(0) if codes else 200
            if code != 200:
                raise urllib.error.HTTPError(url, code, "x", {}, io.BytesIO(b""))
            return self.body
        scan.http_get = fake

    def test_429_falls_back_then_waits_once(self):
        self.answers([429, 429, 200])
        self.assertEqual(scan.yahoo_get("/v7/finance/spark?symbols=TCS.NS&range=3mo&interval=1d"), self.body)
        self.assertEqual([c[0] for c in self.calls], ["https://query1.finance.yahoo.com",
                                                      "https://query2.finance.yahoo.com",
                                                      "https://query1.finance.yahoo.com"])
        self.assertTrue(all(c[1] == scan.UA for c in self.calls))   # Yahoo: the compatible UA
        self.assertEqual(self.sleeps, [scan.BACKOFF])

    def test_a_404_is_final(self):
        self.answers([404])
        with self.assertRaises(scan.ScanError):
            scan.yahoo_get("/v8/finance/chart/NOPE.NS?range=1d&interval=5m")
        self.assertEqual(len(self.calls), 1)

    def test_nse_browser_ua_first_then_compatible(self):
        self.answers([403, 200])
        scan.nse_get(scan.NSE_ARCHIVES + "equities/EQUITY_L.csv")
        self.assertEqual([c[1] for c in self.calls], [scan.NSE_UA, scan.UA])
        self.answers([403, 403])
        with self.assertRaises(scan.ScanError) as cm:
            scan.nse_get(scan.NSE_ARCHIVES + "equities/EQUITY_L.csv")
        self.assertIn("EQUITY_L.csv (HTTP 403)", str(cm.exception))

    def test_429_for_good_is_reported_as_partial(self):
        self.answers([200] + [429] * 10)
        data0, _ = scan.load_offline(DAILY)
        syms = sorted(data0)[:20] + ["X%02d" % i for i in range(20)]
        # the fake answers every batch with the same 40-name spark: batch 1 keeps its 20
        data, failed = scan.fetch_spark(syms, "3mo", "1d", 0.3)
        self.assertEqual(len(data), 20)
        self.assertEqual(len(failed), 20)
        self.assertIn("HTTP 429", failed["X00"])
        self.assertEqual(self.sleeps, [0.3, scan.BACKOFF])


class Spark(unittest.TestCase):
    def test_a_real_live_spark(self):
        data = scan.parse_spark(read(SPARK5))
        self.assertEqual(len(data), 19)                  # 20 asked; Yahoo drops the made-up ZZZQXNOPE
        self.assertIn("BAJAJ-AUTO", data)
        t = data["BAJAJ-AUTO"]
        self.assertEqual(len(t["t"]), 28)                # 9:15 ... 11:25 (in progress) + the 11:29:47 tick
        self.assertIsNone(t["h"])                        # spark is closes only
        cut = DT(2026, 10, 23, 11, 29)
        bars = scan.session_bars(t, D(2026, 10, 23), cut)
        self.assertEqual(bars[-1]["start"], DT(2026, 10, 23, 11, 20))
        self.assertEqual(len(bars), 26)

    def test_spark_errors(self):
        with self.assertRaises(scan.ScanError):
            scan.parse_spark('{"spark":{"result":null,"error":{"code":"Bad Request","description":"less than 20"}}}')
        self.assertEqual(scan.parse_spark('{"spark":{"result":[],"error":null}}'), {})
        with self.assertRaises(scan.ScanError):
            scan.parse_spark('{"chart":{}}')


class Daily(unittest.TestCase):
    # Closes through Fri Oct 16 2026 (Yahoo 1d bars), hand-checked:
    EXPECT = {
        "INFY": ["breakout_20d"],        # 1161.20 > 1110.00, the best close of the 20 before
        "BHARTIARTL": ["breakout_20d"],  # 1905.00 > 1893.30
        "ICICIBANK": ["breakout_20d"],   # 1397.00 > 1389.40
        "AXISBANK": ["breakout_20d"],    # 1290.00 > 1288.00
        "HCLTECH": ["breakout_20d"],     # 1380.00 > 1360.00
        "BAJFINANCE": ["reclaim_ma20"],  # 965.25 under its average, then 982.10 over 980.81
        "M&M": ["reclaim_ma20"],         # 2861.70 under, then 2920.90 over 2913.55
        "MAXHEALTH": ["oversold"],       # RSI 24.5
    }

    def setUp(self):
        self.data, _ = scan.load_offline(DAILY)
        self.syms = sorted(self.data)
        self.mon = scan.parse_now("2026-10-19 09:00")

    def test_setups_before_the_open(self):
        hits, counts, last = scan.scan_daily(self.data, self.syms, self.mon, 0, 0)
        self.assertEqual({h["symbol"]: h["setups"] for h in hits}, self.EXPECT)
        self.assertEqual(last, D(2026, 10, 16))
        self.assertEqual(counts, {"requested": 40, "fetched": 40, "t2t": 0, "illiquid": 0, "short": 0,
                                  "scanned": 40, "cas_unknown": 40, "hits": 8})
        infy = [h for h in hits if h["symbol"] == "INFY"][0]
        self.assertEqual((infy["exchange"], infy["price"], infy["rsi"], infy["close_date"], infy["intraday"]),
                         ("NSE", 1161.2, 70.3, "2026-10-16", True))

    def test_the_in_progress_daily_bar_is_dropped(self):
        rel = self.data["RELIANCE"]
        self.assertEqual(scan.daily_closes(rel, scan.parse_now("2026-10-23 15:29"))[-1], (D(2026, 10, 22), 1184.5))
        self.assertEqual(scan.daily_closes(rel, scan.parse_now("2026-10-23 15:30"))[-1], (D(2026, 10, 23), 1170.1))
        days = [d for d, _ in scan.daily_closes(rel, self.mon)]
        self.assertNotIn(D(2026, 10, 20), days)          # Dussehra: no bar

    def test_turnover_filter(self):
        # meta is Yahoo's at 11:29 AM Oct 23, so traded value is the day's so far; 200 crore keeps the biggest
        hits, counts, _ = scan.scan_daily(self.data, self.syms, self.mon, 20, 0, 2e9)
        self.assertEqual((counts["illiquid"], counts["scanned"]), (26, 14))
        self.assertNotIn("MAXHEALTH", [h["symbol"] for h in hits])

    def test_t2t_and_bands_from_meta(self):
        meta = {"INFY": {"series": "BE", "band": 5.0}}
        hits, counts, _ = scan.scan_daily(self.data, self.syms, self.mon, 0, 0, meta=meta)
        self.assertEqual(counts["t2t"], 1)
        hits, _, _ = scan.scan_daily(self.data, self.syms, self.mon, 0, 0, meta=meta, include_t2t=True)
        infy = [h for h in hits if h["symbol"] == "INFY"][0]
        # 1110.00 -> 1161.20 is +4.6 %: within 1 % of the 5 % upper circuit at 1165.50
        self.assertEqual((infy["intraday"], infy["upper_circuit"], infy["near_upper_circuit"]), (False, 1165.5, True))

    def test_rsi(self):
        self.assertIsNone(scan.rsi([1.0] * 10))
        closes = [44.34, 44.09, 44.15, 43.61, 44.33, 44.83, 45.10, 45.42, 45.84, 46.08, 45.89, 46.03, 45.61,
                  46.28, 46.28]
        self.assertAlmostEqual(scan.rsi(closes), 70.46, places=1)


RULES_STRICT = {"session_close": "15:15", "square_off": "15:12", "flat_by_default": "15:00",
                "no_entries_after_default": "14:45"}
RELIANCE_HIT = dict({"symbol": "RELIANCE", "exchange": "NSE", "setups": ["orb"], "price": 1227.8, "at": "9:50 AM",
                     "bar_start": "09:45", "bar_index": 6, "or_high": 1223.4, "or_low": 1207.3, "pct_over": 0.36,
                     "band": None, "prev_close": 1211.2, "upper_circuit": None, "lower_circuit": None,
                     "near_upper_circuit": False, "series": "EQ", "cas": None, "source": "yahoo"}, **RULES_STRICT)
RELIANCE_HIT_CAS = dict(RELIANCE_HIT, cas=True)   # with the F&O list: a CAS stock, same strict times


class Orb(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.data, cls.errors = scan.load_offline(WALL)
        cls.syms = sorted(cls.data)

    def at(self, hh, mm, syms=None, or_minutes=15, min_price=0):
        return scan.scan_orb(self.data, syms or self.syms, MON, DT(2026, 10, 19, hh, mm), or_minutes, min_price)

    def test_nifty50_at_1015(self):
        hits, counts, _ = self.at(10, 15)
        self.assertEqual(len(self.syms), 50)
        self.assertEqual((counts["scanned"], counts["candidates"], len(hits)), (50, 24, 8))
        self.assertEqual([(h["symbol"], h["at"]) for h in hits],
                         [("SBIN", "9:35 AM"), ("ULTRACEMCO", "9:35 AM"), ("HCLTECH", "9:40 AM"),
                          ("MARUTI", "9:40 AM"), ("TCS", "9:45 AM"), ("RELIANCE", "9:50 AM"), ("TECHM", "9:50 AM"),
                          ("TITAN", "9:50 AM")])
        self.assertEqual([h for h in hits if h["symbol"] == "RELIANCE"][0], RELIANCE_HIT)

    def test_reliance_matches_the_shared_fixture(self):
        shared, _ = scan.load_offline(SHARED)
        cut = DT(2026, 10, 19, 10, 15)
        a = scan.session_bars(shared["RELIANCE"], MON, cut)
        b = scan.session_bars(self.data["RELIANCE"], MON, cut)
        self.assertEqual(a, b)
        self.assertEqual([round(x["c"], 1) for x in a[:7]], [1218.2, 1215.0, 1208.4, 1215.0, 1218.8, 1221.8, 1227.8])
        hits, _, _ = scan.scan_orb(shared, ["RELIANCE"], MON, cut, 15)
        self.assertEqual(hits, [RELIANCE_HIT])

    def test_in_progress_bar(self):
        self.assertEqual(self.at(9, 49, ["RELIANCE"])[0], [])   # the 9:45 bar is not done until 9:50
        self.assertEqual(self.at(9, 50, ["RELIANCE"])[0][0]["at"], "9:50 AM")

    def test_cas_session_and_empty_bars(self):
        r = self.data["RELIANCE"]
        empty = [str(scan.ts_to_ist(t)) for t, c in zip(r["t"], r["c"]) if c is None]
        self.assertEqual(empty, ["2026-10-16 15:20:00", "2026-10-19 15:20:00"])   # the closing auction, on Yahoo
        cut = DT(2026, 10, 19, 15, 30)
        cas_close = datetime.time(15, 15)
        bars = scan.session_bars(r, MON, cut, cas_close)          # a CAS stock: continuous trading ends 15:15
        self.assertEqual((len(bars), bars[-1]["start"]), (72, DT(2026, 10, 19, 15, 10)))
        self.assertTrue(scan.usable(bars, MON, cut, 3, cas_close))
        bars = scan.session_bars(r, MON, cut)                     # read as non-CAS: 74 of 75, one empty bar
        self.assertEqual((len(bars), bars[-1]["start"]), (74, DT(2026, 10, 19, 15, 25)))
        self.assertTrue(scan.usable(bars, MON, cut, 3))
        hits, counts, _ = self.at(15, 30)                         # no F&O list: strict rules for all 50
        self.assertEqual((counts["unusable"], counts["scanned"], counts["cas_unknown"], len(hits)), (0, 50, 50, 16))
        hits, counts, _ = scan.scan_orb(self.data, self.syms, MON, cut, 15, meta={k: {"cas": False} for k in self.syms})
        self.assertEqual((counts["unusable"], len(hits)), (0, 16))
        rel = [h for h in hits if h["symbol"] == "RELIANCE"][0]
        self.assertEqual((rel["cas"], rel["session_close"], rel["square_off"], rel["flat_by_default"]),
                         (False, "15:30", "15:25", "15:10"))
        s = bars_series("2026-10-21", [10, 10.2, 10.1] + [None] * 4 + [10.5] * 5)
        self.assertFalse(scan.usable(scan.session_bars(s, D(2026, 10, 21), DT(2026, 10, 21, 10, 15)),
                                     D(2026, 10, 21), DT(2026, 10, 21, 10, 15), 3))   # 4 empty: a halt
        s = bars_series("2026-10-21", [10, None, 10.1] + [10.5] * 9)
        self.assertFalse(scan.usable(scan.session_bars(s, D(2026, 10, 21), DT(2026, 10, 21, 10, 15)),
                                     D(2026, 10, 21), DT(2026, 10, 21, 10, 15), 3))   # a hole in the range

    def test_price_floor_and_30_minute_range(self):
        hits, counts, _ = self.at(10, 15, min_price=1500)
        self.assertEqual((len(hits), counts["illiquid"]), (5, 28))
        hits, _, _ = self.at(10, 15, or_minutes=30)
        self.assertEqual(len(hits), 5)
        rel = [h for h in hits if h["symbol"] == "RELIANCE"][0]
        self.assertEqual((rel["at"], rel["or_high"]), ("9:50 AM", 1223.4))   # range 9:15 to 9:45

    def test_stage_two_cap_takes_the_most_traded(self):
        spark_like = {k: dict(v, h=None, l=None) for k, v in self.data.items()}
        asked = []

        def confirm(k):
            asked.append(k)
            return self.data[k]
        hits, counts, _ = scan.scan_orb(spark_like, self.syms, MON, DT(2026, 10, 19, 10, 15), 15, 0, 0, confirm, 5)
        self.assertEqual((counts["confirmed"], counts["unconfirmed"], len(hits)), (5, 19, 3))
        self.assertEqual(asked, ["TCS", "HDFCBANK", "AXISBANK", "RELIANCE", "SBIN"])
        hits, counts, _ = scan.scan_orb(spark_like, self.syms, MON, DT(2026, 10, 19, 10, 15), 15, 0, 0, confirm, 0)
        self.assertEqual(len(hits), 8)

    def test_stage_one_is_a_necessary_condition(self):
        cut = DT(2026, 10, 19, 10, 15)
        for k, s in self.data.items():
            bars = scan.session_bars(s, MON, cut)
            if scan.orb_hit(bars, 3):
                self.assertTrue(scan.orb_candidate(bars, 3), k)

    def test_t2t_skipped_and_circuit_flag(self):
        hits, counts, _ = scan.scan_orb(self.data, ["RELIANCE", "SBIN"], MON, DT(2026, 10, 19, 10, 15), 15,
                                        meta={"SBIN": {"series": "BE", "band": 5.0},
                                              "RELIANCE": {"series": "EQ", "band": 2.0}})
        self.assertEqual(counts["t2t"], 1)
        rel = hits[0]
        # a pretend 2 % band on 1211.20: upper circuit 1235.40, and 1227.80 is within 1 % of it
        self.assertEqual((rel["upper_circuit"], rel["near_upper_circuit"]), (1235.4, True))

    def test_kite_candles(self):
        kite = scan.load_kite_bars([KITE_OPEN, KITE_CLOSE])          # two chunks, merged by date
        s = kite["RELIANCE"]
        self.assertEqual((len(s["t"]), s["v"][0]), (14, 615358))     # Kite has the 9:15 volume; Yahoo says 0
        self.assertEqual(scan.ts_to_ist(s["t"][-1]), DT(2026, 10, 19, 15, 10))   # Kite's last bar: CAS ends 15:15
        hits, counts, _ = scan.scan_orb(kite, ["RELIANCE"], MON, DT(2026, 10, 19, 10, 15), 15,
                                        meta={"RELIANCE": {"cas": True}}, kite={"RELIANCE"})
        h = hits[0]
        self.assertEqual((h["price"], h["or_low"], h["or_high"], h["at"], h["source"]),
                         (1227.7, 1207.1, 1223.6, "9:50 AM", "kite"))
        self.assertEqual(scan.kite_symbol("kite-BAJAJ-AUTO-5minute-2026-10-19"), "BAJAJ-AUTO")
        data, _ = scan.load_offline(KITE_OPEN)                       # auto-detected by --offline
        self.assertEqual(list(data), ["RELIANCE"])
        self.assertEqual(scan.parse_kite_ltp(json.loads(read(KITE_LTP))),
                         {"BSE:RELIANCE": 1190.0, "RELIANCE": 1190.0, "TCS": 2530.0})

    def test_live_reliance_today(self):
        v8 = scan.load_offline(REL5)[0]["RELIANCE"]
        day, cut = scan.orb_window(scan.parse_now("2026-10-23T05:59:51Z"), None, 15, datetime.time(14, 45))
        self.assertEqual(cut, DT(2026, 10, 23, 11, 29))
        self.assertEqual(scan.prev_close(v8, day), 1184.5)          # range=1d: Yahoo's chartPreviousClose
        hits, counts, _ = scan.scan_orb({"RELIANCE": v8}, ["RELIANCE"], day, cut, 15)
        self.assertEqual((counts["scanned"], hits), (1, []))      # range 1183.00 to 1201.60, never closed above

    def test_window_messages(self):
        until = datetime.time(14, 45)

        def msg(now, at=None, until=until):
            with self.assertRaises(scan.ScanError) as cm:
                scan.orb_window(scan.parse_now(now), at, 15, until)
            return str(cm.exception)
        self.assertIn("weekend", msg("2026-10-17 10:15"))
        self.assertIn("Dussehra", msg("2026-10-20 10:15"))
        self.assertIn("Run orb again after 9:35 AM", msg("2026-10-19 09:30"))
        self.assertIn("expired", msg("2026-10-19 14:50"))
        self.assertIn("closed at 3:30 PM", msg("2026-10-19 15:45", until=None))
        self.assertIn("later than now", msg("2026-10-19 10:00", at=datetime.time(10, 15)))
        self.assertIn("after the 3:30 PM close", msg("2026-10-19 17:00", at=datetime.time(15, 45)))
        self.assertEqual(scan.orb_window(scan.parse_now("2026-10-19 16:30"), datetime.time(10, 15), 15, until),
                         (MON, DT(2026, 10, 19, 10, 15)))
        self.assertEqual(scan.orb_window(scan.parse_now("2026-10-19 09:35"), None, 15, until)[1],
                         DT(2026, 10, 19, 9, 35))
        with self.assertRaises(scan.ScanError):
            scan.orb_window(scan.parse_now("2026-10-19 10:15"), None, 7, until)


class Cli(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.t = os.path.join(self.d, "trader-in")
        self.wl = os.path.join(self.t, "watchlist.json")
        self.uni = os.path.join(self.t, "universe.txt")

    def tearDown(self):
        shutil.rmtree(self.d)

    def doc(self, path):
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)

    def test_universe_then_orb_the_worked_example(self):
        code, text, _ = run(["universe", "--offline", NSE, "--out", self.uni, "--fo-list", FO])
        self.assertEqual(code, 0)
        self.assertIn("9 CAS stocks end continuous trading at 15:15", text)
        self.assertIn("Kept 17 NSE equities (series EQ).", text)
        self.assertIn("Trade-to-trade (no intraday): 3 BE, 2 BZ (left out", text)
        lines = read(self.uni).splitlines()
        self.assertIn("RELIANCE|Reliance Industries Limited|EQ|No Band|yes", lines)
        self.assertIn("MRF|MRF Limited|EQ|20|no", lines)

        code, text, _ = run(["orb", "--offline", WALL, "--now", "2026-10-19 10:15", "--universe", self.uni,
                             "--out", self.wl, "--kite-ltp", KITE_LTP])
        self.assertEqual(code, 0)
        self.assertIn("Fetched 6 of 17 charts (11 failed or had no data)", text)
        self.assertIn("2 of 6 charts closed above their opening-range high by 10:15 AM", text)
        self.assertIn("9:50 AM  RELIANCE       ₹1,227.80  range 1,207.30 to 1,223.40  +0.36% over", text)
        self.assertIn("(Yahoo, free, can lag)", text)
        self.assertIn("Last price now (Kite, live): TCS ₹2,530.00, RELIANCE ₹1,190.00.", text)
        self.assertNotIn("CAS status unknown", text)
        doc = self.doc(self.wl)
        self.assertEqual({k: doc[k] for k in ("created_by", "created_at", "mode", "as_of", "currency")},
                         {"created_by": "india-scanner", "created_at": "2026-10-19T04:45:00Z", "mode": "orb",
                          "as_of": "2026-10-19 10:15 IST", "currency": "INR"})
        self.assertEqual([h["symbol"] for h in doc["hits"]], ["TCS", "RELIANCE"])
        self.assertIn(dict(RELIANCE_HIT_CAS, ltp=1190.0, ltp_source="kite"), doc["hits"])

        # the hits read back as a watchlist (symbol + exchange), with --at instead of --now
        code, text, _ = run(["orb", "--offline", WALL, "--at", "10:15", "--watchlist", self.wl, "--json",
                             "--no-write"])
        out = json.loads(text)
        self.assertEqual((code, out["universe"], len(out["hits"])), (0, 2, 2))

    def test_whole_nifty50_and_the_shared_fixture(self):
        code, text, _ = run(["orb", "--offline", WALL, "--now", "2026-10-19 10:15", "--no-write"])
        self.assertIn("CAS status unknown for 50 stocks", text)
        self.assertIn("Fetched 50 of 50 charts; 50 usable; 24 candidates, 24 confirmed", text)
        self.assertIn("8 of 50 charts closed above their opening-range high by 10:15 AM", text)
        code, text, _ = run(["orb", "--offline", SHARED, "--now", "2026-10-19 10:15", "--json", "--no-write"])
        self.assertEqual(json.loads(text)["hits"], [RELIANCE_HIT])
        code, text, _ = run(["orb", "--offline", SHARED, "--now", "2026-10-19 10:15", "--json", "--no-write",
                             "--fo-list", FO])
        self.assertEqual(json.loads(text)["hits"], [RELIANCE_HIT_CAS])
        code, text, _ = run(["orb", "--offline", SHARED, "--now", "2026-10-19 10:15", "--no-write", "--symbols",
                             "TCS", "--kite-bars", KITE_OPEN, "--kite-bars", KITE_CLOSE, "--cas", "yes"])
        self.assertIn("9:50 AM  RELIANCE       ₹1,227.70  range 1,207.10 to 1,223.60  +0.34% over  (Kite candles)",
                      text)
        self.assertIn("1 from Kite candles", text)

    def test_daily_with_bands(self):
        code, text, _ = run(["daily", "--offline", DAILY, "--now", "2026-10-19 09:00", "--out", self.wl,
                             "--bands", os.path.join(NSE, "sec_list.csv")])
        self.assertEqual(code, 0)
        self.assertIn("8 charts matched a setup: 5 20-day closing-high breakout, 2 reclaimed the 20-day average, "
                      "1 oversold (RSI under 30).", text)
        self.assertIn("M&M            ₹2,920.90  RSI  46.5  reclaimed the 20-day average", text)
        doc = self.doc(self.wl)
        self.assertEqual((doc["mode"], doc["as_of"], doc["universe"], doc["scanned"]),
                         ("daily", "2026-10-16 close", 40, 40))
        mm = [h for h in doc["hits"] if h["symbol"] == "M&M"][0]
        self.assertEqual((mm["exchange"], mm["series"], mm["band"]), ("NSE", "EQ", None))
        code, text, _ = run(["daily", "--offline", DAILY, "--now", "2026-10-19 09:00", "--out", self.wl,
                             "--fo-list", FO])
        mm = [h for h in self.doc(self.wl)["hits"] if h["symbol"] == "M&M"][0]
        self.assertEqual((mm["cas"], mm["square_off"], mm["flat_by_default"]), (True, "15:12", "15:00"))

    def test_partial_stage_two_is_said(self):
        """The networked path with a fake Yahoo that answers from the Oct 19 bundle."""
        with gzip.open(WALL, "rt", encoding="utf-8") as fh:
            bundle = json.load(fh)
        calls = []

        def get(path):
            calls.append(path)
            if path.startswith("/v7/finance/spark"):
                res = []
                for y in path.split("symbols=")[1].split("&")[0].split(","):
                    k = scan.norm(scan.urllib.parse.unquote(y))
                    r = bundle[k]["chart"]["result"]
                    closes = r[0]["indicators"]["quote"][0]["close"]
                    res.append({"symbol": scan.urllib.parse.unquote(y),
                                "response": [dict(r[0], indicators={"quote": [{"close": closes}]})]})
                return json.dumps({"spark": {"result": res, "error": None}})
            return json.dumps(bundle[scan.norm(scan.urllib.parse.unquote(path.split("/chart/")[1].split("?")[0]))])
        old_get, old_sleep, real_now = scan.yahoo_get, scan.SLEEP, scan.parse_now
        scan.yahoo_get, scan.SLEEP = get, (lambda s: None)
        scan.parse_now = lambda text: real_now(text or "2026-10-19 10:20")
        uni = os.path.join(self.d, "u.txt")
        with open(uni, "w", encoding="utf-8") as fh:
            fh.write("\n".join(sorted(bundle)) + "\n")
        try:
            code, text, _ = run(["orb", "--universe", uni, "--now", "2026-10-19 10:15", "--max-confirm", "5",
                                 "--no-write", "--fo-list", FO])
        finally:
            scan.yahoo_get, scan.SLEEP, scan.parse_now = old_get, old_sleep, real_now
        self.assertEqual(code, 0)
        self.assertIn("50 usable; 24 candidates, 5 confirmed", text)
        self.assertIn("PARTIAL: 19 more candidates were not checked", text)
        self.assertIn("3 of the 5 checked charts closed above", text)
        self.assertEqual(sum(1 for c in calls if c.startswith("/v8/")), 5)
        self.assertEqual(sum(1 for c in calls if c.startswith("/v7/")), 3)    # 50 symbols, 20 a request
        self.assertTrue(any("M%26M.NS" in c for c in calls))

    def test_plain_messages_and_exit_codes(self):
        code, _, err = run(["orb", "--offline", WALL, "--now", "2026-10-17 10:15", "--out", self.wl])
        self.assertEqual(code, 1)
        self.assertIn("No session on Sat 2026-10-17 (weekend)", err)
        self.assertNotIn("Traceback", err)
        self.assertFalse(os.path.exists(self.wl))
        code, _, err = run(["orb", "--offline", WALL, "--now", "2026-10-19 09:32"])
        self.assertIn("after 9:35 AM", err)
        os.makedirs(self.t)
        cfg = os.path.join(self.t, "config.json")
        with open(cfg, "w") as fh:
            json.dump({"no_entries_after": "10:00"}, fh)
        code, _, err = run(["orb", "--offline", WALL, "--now", "2026-10-19 10:15", "--config", cfg])
        self.assertIn("expired", err)
        code, _, err = run(["orb", "--offline", WALL])
        self.assertIn("--at HH:MM", err)
        code, text, _ = run(["orb", "--offline", WALL, "--now", "2026-10-19 09:45", "--symbols", "reliance.ns",
                             "--out", self.wl])
        self.assertEqual(code, 1)
        self.assertIn("No chart closed above its opening-range high by 9:45 AM", text)
        self.assertEqual(self.doc(self.wl)["hits"], [])
        code, _, err = run(["daily", "--universe", os.path.join(self.d, "nope.txt")])
        self.assertIn("scan.py universe", err)
        code, _, err = run(["daily", "--offline", os.path.join(self.d, "nothing-here")])
        self.assertIn("no offline file", err)

    def test_symbol_lists(self):
        txt = os.path.join(self.d, "list.txt")
        with open(txt, "w", encoding="utf-8") as fh:
            fh.write("# mine\nRELIANCE|Reliance Industries Limited|EQ|No Band\nm&m\nBSE:500325\n")
        self.assertEqual(scan.symbols_from_file(txt), ["RELIANCE", "M&M", "BSE:500325"])
        js = os.path.join(self.d, "wl.json")
        with open(js, "w") as fh:
            json.dump({"hits": [{"symbol": "tcs", "exchange": "NSE"}, {"symbol": "RELIANCE", "exchange": "BSE"}]}, fh)
        self.assertEqual(scan.symbols_from_file(js), ["TCS", "BSE:RELIANCE"])


if __name__ == "__main__":
    unittest.main()
