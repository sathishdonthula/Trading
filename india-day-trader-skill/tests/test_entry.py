"""india-entry tests. Offline only. Fixtures:

tests/fixtures/shared/RELIANCE-60d5m.json.gz  REAL Yahoo chart JSON v8, RELIANCE.NS, range=60d interval=5m,
                         fetched Fri 2026-10-23 11:24:35 IST (the 11:20 bar in progress plus Yahoo's live
                         tick at 11:24:35). The worked example is Mon 2026-10-19.
tests/fixtures/india-entry/DEMO-1m-synthetic.json  SYNTHETIC one-minute bars (not market data) on the
                         09:15 IST session: a wick above the range high, a close AT the high, a null bar,
                         a live tick.
"""

import copy
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

from _load import load, FIX, ROOT

entry = load("india-entry", "entry")

REL = os.path.join(FIX, "shared", "RELIANCE-60d5m.json.gz")
DEMO = os.path.join(FIX, "india-entry", "DEMO-1m-synthetic.json")
OPEN_TS = 1792381500       # 2026-10-19 09:15 IST = 03:45 UTC
DT = datetime.datetime
DAY = datetime.date(2026, 10, 19)


def ist(text):
    return entry.parse_now(text)


def doc(path=REL):
    if path.endswith(".gz"):
        with gzip.open(path, "rt", encoding="utf-8") as fh:
            return json.load(fh)
    with open(path) as fh:
        return json.load(fh)


_DOC = doc()


def chart(d=None, sym="RELIANCE"):
    return entry.parse_chart(json.dumps(d if d is not None else _DOC), sym)


def at(text, d=None, **kw):
    return entry.evaluate(chart(d), ist(text), **kw)


def no_breakout(d=None):
    """Every Oct 19 bar after the range stays under the 1,223.40 high."""
    d = copy.deepcopy(d if d is not None else _DOC)
    r = d["chart"]["result"][0]
    q = r["indicators"]["quote"][0]
    for i, t in enumerate(r["timestamp"]):
        if OPEN_TS + 3 * 300 <= t < OPEN_TS + 75 * 300 and q["close"][i] is not None:
            q["close"][i] = min(q["close"][i], 1223.00)
            q["high"][i] = min(q["high"][i], 1223.20)
            q["open"][i] = min(q["open"][i], 1223.00)
    return d


def run(argv):
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = entry.main(argv)
    return code, out.getvalue(), err.getvalue()


class TheExample(unittest.TestCase):
    def test_triggers_at_0950_on_the_0945_bar(self):
        r = at("2026-10-19 09:50")
        self.assertEqual(r["status"], "triggered")
        self.assertEqual((r["symbol"], r["exchange"], r["yahoo_symbol"]), ("RELIANCE", "NSE", "RELIANCE.NS"))
        self.assertEqual(r["opening_range"]["high"], 1223.4)
        self.assertEqual(r["opening_range"]["low"], 1207.3)
        self.assertEqual(r["opening_range"]["bars"], 3)
        t = r["trigger"]
        self.assertEqual(t["time"], "9:50 AM")
        self.assertEqual(t["bar_start"], "09:45")
        self.assertEqual(t["bar_index"], 6)
        self.assertEqual(t["price"], 1227.8)
        self.assertEqual(t["close"], 1227.800048828125)
        self.assertEqual(t["pct_over"], 0.36)
        self.assertEqual(r["level"], 1223.4)
        self.assertEqual((r["prev_close"], r["prev_close_source"]), (1211.2, "last bar 2026-10-16"))

    def test_at_0947_the_0945_bar_is_still_forming(self):
        r = at("2026-10-19 09:47")
        self.assertEqual(r["status"], "waiting")
        self.assertIsNone(r["trigger"])
        self.assertEqual(r["last"]["bar_start"], "09:40")
        self.assertEqual(r["last"]["close"], 1221.8)
        self.assertEqual(r["forming"]["bar_start"], "09:45")
        self.assertIn("₹1.60 under the range high ₹1,223.40", r["reason"])
        self.assertEqual(at("2026-10-19 09:49:59")["status"], "waiting")

    def test_before_the_open_and_while_the_range_forms(self):
        r = at("2026-10-19 09:05")
        self.assertEqual((r["status"], r["phase"]), ("waiting", "pre-open"))
        self.assertIn("pre-open", r["reason"])
        r = at("2026-10-19 09:29")
        self.assertEqual((r["status"], r["phase"]), ("waiting", "forming"))
        self.assertEqual(r["opening_range"]["bars"], 2)
        self.assertFalse(r["opening_range"]["complete"])
        r = at("2026-10-19 09:31")
        self.assertEqual((r["status"], r["phase"]), ("waiting", "open"))
        self.assertEqual(r["opening_range"]["bars"], 3)

    def test_the_live_tick_and_the_in_progress_bar_are_dropped(self):
        c = chart()
        self.assertEqual(c["bars"][-1]["t"] % 300, 275)              # 11:24:35 is in the file
        r = entry.evaluate(c, entry.utc_of(c["data_time"]))          # read as of the file's own time
        self.assertEqual(r["now"], "2026-10-23 11:24:35")
        self.assertEqual(r["_bars"][-1]["slot"], 24)                 # the 11:15 bar
        self.assertEqual(r["forming"]["bar_start"], "11:20")
        self.assertEqual(r["bars_closed"], 25)
        self.assertEqual(r["prev_close_source"], "yahoo")            # today's chart: Yahoo's previousClose
        self.assertEqual(r["prev_close"], 1184.5)
        r = at("2026-10-23 11:25")
        self.assertEqual(r["_bars"][-1]["slot"], 24)
        self.assertIn("Yahoo's last trade is 11:24:35", r["lag_note"])
        r = at("2026-10-23 11:26")
        self.assertEqual(r["_bars"][-1]["slot"], 25)
        self.assertEqual(r["bars_closed"], 26)
        self.assertTrue(all(b["t"] % 300 == 0 for b in r["_bars"]))

    def test_empty_closing_bars_are_skipped_not_fatal(self):
        # CAS rules (RELIANCE is an F&O stock; unknown is treated the same): continuous trading ends 15:15,
        # so the session holds 72 five-minute bars and Yahoo's auction-time 15:15-15:25 bars are left out
        r = at("2026-10-19 15:35")
        slots = [b["slot"] for b in r["_bars"]]
        self.assertEqual((slots[-1], len(slots), r["session_close"]), (71, 72, "15:15"))
        self.assertEqual(r["status"], "triggered")
        # non-CAS rules: to 15:30, the empty 15:20 bar skipped, never fatal
        r = at("2026-10-19 15:35", cas=False)
        slots = [b["slot"] for b in r["_bars"]]
        self.assertNotIn(73, slots)                                  # 15:20 came as nulls
        self.assertEqual(slots[-1], 74)                              # 15:25
        self.assertEqual((r["status"], r["session_close"], r["square_off"]), ("triggered", "15:30", "15:25"))

    def test_after_the_close_it_stays_triggered_and_says_it_is_late(self):
        r = at("2026-10-19 15:40")
        self.assertEqual((r["status"], r["phase"]), ("triggered", "after-close"))
        self.assertEqual(r["trigger"]["time"], "9:50 AM")
        text = "\n".join(entry.describe(r))
        self.assertIn("minutes ago", text)
        self.assertNotIn("This is the moment", text)


class Rules(unittest.TestCase):
    def test_volume_leaves_out_the_zero_volume_0915_bar(self):
        r = at("2026-10-19 10:30", min_volume_x=1.5)
        self.assertEqual(r["opening_range"]["avg_volume"], 294168)  # (230154 + 358182) / 2, the 09:15 bar has 0
        t = r["trigger"]
        self.assertEqual((t["time"], t["bar_index"], t["volume_x"]), ("9:50 AM", 6, 1.52))
        r = at("2026-10-19 09:50", min_volume_x=2)
        self.assertEqual(r["status"], "waiting")
        self.assertIn("1.52x the range's volume, under your 2.00x", r["reason"])

    def test_level_setup(self):
        r = at("2026-10-19 10:30", setup="level", level=1225)
        self.assertEqual((r["trigger"]["time"], r["trigger"]["bar_index"]), ("9:50 AM", 6))
        # a level under the open: the first eligible close is the 9:25 bar's, at 9:30 (no_entries_before)
        r = at("2026-10-19 10:30", setup="level", level=1200)
        self.assertEqual((r["trigger"]["time"], r["trigger"]["bar_index"]), ("9:30 AM", 2))
        with self.assertRaises(entry.EntryError):
            at("2026-10-19 10:30", setup="level")

    def test_no_breakout_waits_then_expires_at_the_cutoff(self):
        d = no_breakout()
        self.assertEqual(at("2026-10-19 10:30", d)["status"], "waiting")
        self.assertEqual(at("2026-10-19 14:44", d)["status"], "waiting")
        r = at("2026-10-19 14:45", d)
        self.assertEqual(r["status"], "expired")
        self.assertIn("by 14:45", r["reason"])
        self.assertEqual(at("2026-10-19 15:31", d)["phase"], "after-close")

    def test_config_cutoffs(self):
        cfg = {"no_entries_before": "09:30", "no_entries_after": "09:45"}
        self.assertEqual(at("2026-10-19 09:50", cfg=cfg)["status"], "expired")
        cfg = {"no_entries_before": "10:00", "no_entries_after": "14:45"}
        r = at("2026-10-19 10:30", cfg=cfg)
        self.assertEqual(r["trigger"]["time"], "10:05 AM")       # the 10:00 bar, first close above at or after 10:00

    def test_or_minutes_30(self):
        r = at("2026-10-19 10:30", or_minutes=30)
        self.assertEqual(r["opening_range"]["bars"], 6)
        self.assertEqual(r["opening_range"]["high"], 1223.4)
        self.assertEqual(r["trigger"]["time"], "9:50 AM")

    def test_bad_inputs_and_ist_parsing(self):
        with self.assertRaises(entry.EntryError):
            at("2026-10-19 09:50", or_minutes=7)
        with self.assertRaises(entry.EntryError):
            at("2026-10-19 09:50", interval="15m")
        with self.assertRaises(entry.EntryError):
            entry.parse_now("tomorrow")
        with self.assertRaises(entry.EntryError):
            entry.parse_hhmm("25:00", "x")
        self.assertEqual(entry.parse_now("2026-10-19 09:50"), DT(2026, 10, 19, 4, 20))
        self.assertEqual(entry.parse_now("2026-10-19 09:50 IST"), DT(2026, 10, 19, 4, 20))
        self.assertEqual(entry.parse_now("2026-10-19T04:20:00Z"), DT(2026, 10, 19, 4, 20))
        self.assertEqual(entry.parse_now("2026-10-19T09:50:00+05:30"), DT(2026, 10, 19, 4, 20))

    def test_rupees_in_indian_grouping(self):
        self.assertEqual(entry.inr(1227.8), "₹1,227.80")
        self.assertEqual(entry.inr(123456.78), "₹1,23,456.78")
        self.assertEqual(entry.inr(12345678), "₹1,23,45,678.00")
        self.assertEqual(entry.inr(0.5), "₹0.50")


class Symbols(unittest.TestCase):
    def test_nse_and_bse_forms(self):
        p = entry.parse_symbol
        self.assertEqual(p("reliance"), ("RELIANCE", "NSE"))
        self.assertEqual(p("RELIANCE.NS"), ("RELIANCE", "NSE"))
        self.assertEqual(p("NSE:RELIANCE"), ("RELIANCE", "NSE"))
        self.assertEqual(p("RELIANCE.BO"), ("RELIANCE", "BSE"))
        self.assertEqual(p("BSE:RELIANCE"), ("RELIANCE", "BSE"))
        self.assertEqual(p("500325"), ("500325", "BSE"))
        self.assertEqual(p("M&M"), ("M&M", "NSE"))
        self.assertEqual(p("bajaj-auto"), ("BAJAJ-AUTO", "NSE"))
        with self.assertRaises(entry.EntryError):
            p("RELI ANCE")

    def test_yahoo_paths(self):
        self.assertIn("/chart/M%26M.NS?", entry.chart_path("M&M", "1d", "5m"))
        self.assertIn("/chart/RELIANCE.BO?", entry.chart_path("RELIANCE", "1d", "5m", "BSE"))
        self.assertIn("/chart/500325.BO?", entry.chart_path("500325", "1d", "5m", "BSE"))


class OneMinute(unittest.TestCase):
    def c(self):
        return entry.parse_chart(json.dumps(doc(DEMO)), "DEMO")

    def test_wick_and_close_at_the_high_do_not_trigger(self):
        c = self.c()
        r = entry.evaluate(c, entry.utc_of(c["data_time"]), interval="1m")
        self.assertEqual(r["now"], "2026-10-19 09:51:30")
        self.assertEqual(r["opening_range"], {"high": 100.5, "low": 99.0, "minutes": 15, "bars": 15, "of": 15,
                                              "complete": True, "avg_volume": 10667})
        t = r["trigger"]
        self.assertEqual((t["time"], t["bar_start"], t["bar_index"], t["price"]), ("9:44 AM", "09:43", 28, 100.62))
        r = entry.evaluate(c, ist("2026-10-19 09:43:59"), interval="1m")
        self.assertEqual(r["status"], "waiting")
        self.assertEqual(r["forming"]["bar_start"], "09:43")
        self.assertEqual(r["last"]["close"], 100.5)             # the 9:42 bar closed AT the high

    def test_null_bar_tick_and_volume(self):
        c = self.c()
        r = entry.evaluate(c, entry.utc_of(c["data_time"]), interval="1m", min_volume_x=2)
        self.assertEqual((r["trigger"]["time"], r["trigger"]["bar_index"]), ("9:47 AM", 31))
        slots = [b["slot"] for b in r["_bars"]]
        self.assertNotIn(19, slots)                               # 9:34 came as nulls
        self.assertEqual(slots[-1], 35)                           # 9:51 still forming, 9:51:30 tick dropped
        self.assertEqual(r["forming"]["bar_start"], "09:51")

    def test_the_file_and_the_interval_must_agree(self):
        code, _, err = run(["check", "DEMO", "--offline", DEMO])
        self.assertEqual(code, 1)
        self.assertIn("1m bars, not 5m", err)


class Calendar(unittest.TestCase):
    def test_weekend_holiday_and_empty_data(self):
        r = at("2026-10-17 10:30")
        self.assertEqual((r["status"], r["phase"]), ("no-data", "closed"))
        self.assertEqual(r["next_open"], "2026-10-19 09:15")
        r = at("2026-10-20 10:00")
        self.assertIn("NSE holiday: Dussehra", r["reason"])
        self.assertEqual(r["next_open"], "2026-10-21 09:15")
        self.assertIn("Diwali", at("2026-11-10 10:00")["reason"])
        empty = copy.deepcopy(_DOC)
        res = empty["chart"]["result"][0]
        del res["timestamp"]
        res["indicators"] = {"quote": [{}]}
        r = at("2026-10-19 09:50", empty)
        self.assertEqual((r["status"], r["phase"]), ("no-data", "open"))
        r = at("2026-10-26 10:15")                                # Yahoo sent nothing for that day
        self.assertEqual(r["status"], "no-data")
        r = at("2027-03-01 10:30")
        self.assertFalse(r["calendar_known"])
        self.assertIn("No NSE holiday list for 2027", "\n".join(entry.describe(r)))

    def test_nse_holiday_answer_and_cache(self):
        nse = {"CM": [{"tradingDate": "26-Jan-2027", "weekDay": "Tuesday", "description": "Republic Day"}],
               "FO": [{"tradingDate": "01-Jan-2027", "description": "not equities"}]}
        self.assertEqual(entry.holidays_from_doc(nse), {"2027-01-26": "Republic Day"})
        self.assertEqual(entry.holidays_from_doc({"holidays": {"2027-03-22": "Holi"}}), {"2027-03-22": "Holi"})
        self.assertEqual(entry.holidays_from_doc(["2027-04-14"]), {"2027-04-14": "NSE trading holiday"})
        d = tempfile.mkdtemp()
        saved = (dict(entry.NSE_HOLIDAYS), set(entry.CALENDAR_YEARS), entry._nse_session_get)
        try:
            calls = []

            def fake(url, timeout=15):
                calls.append(url)
                return json.dumps(nse)
            entry._nse_session_get = fake
            path = os.path.join(d, "holidays.json")
            self.assertEqual(entry.load_holidays(path, fetch=True), "")
            self.assertEqual(calls, [entry.HOLIDAY_URL])
            with open(path) as fh:
                self.assertEqual(json.load(fh)["holidays"], {"2027-01-26": "Republic Day"})
            self.assertFalse(entry.trading_day(datetime.date(2027, 1, 26))[0])
            self.assertIn(2027, entry.CALENDAR_YEARS)
            entry.load_holidays(path, fetch=True)                  # a fresh cache: no second fetch
            self.assertEqual(len(calls), 1)

            def broken(url, timeout=15):
                raise urllib.error.URLError("403 Forbidden")
            entry._nse_session_get = broken
            self.assertIn("using the built-in 2026 list", entry.load_holidays(os.path.join(d, "x.json")))
        finally:
            entry.NSE_HOLIDAYS.clear()
            entry.NSE_HOLIDAYS.update(saved[0])
            entry.CALENDAR_YEARS.clear()
            entry.CALENDAR_YEARS.update(saved[1])
            entry._nse_session_get = saved[2]
            shutil.rmtree(d)

    def test_yahoo_errors(self):
        bad = json.dumps({"chart": {"result": None, "error": {"code": "Not Found",
                                                              "description": "No data found, symbol may be delisted"}}})
        with self.assertRaises(entry.EntryError) as cm:
            entry.parse_chart(bad, "ZZZZ")
        self.assertIn("No data found", str(cm.exception))
        with self.assertRaises(entry.EntryError):
            entry.parse_chart("<html>", "RELIANCE")


class Network(unittest.TestCase):
    """Yahoo and NSE are never called: _open_url, _sleep and the NSE session are swapped for fakes."""

    def setUp(self):
        self.calls, self.sleeps = [], []
        self._open, self._sleep, self._nse = entry._open_url, entry._sleep, entry._nse_session_get
        entry._sleep = lambda s: self.sleeps.append(s)

        def no_nse(url, timeout=15):
            raise urllib.error.URLError("offline test")
        entry._nse_session_get = no_nse
        self._fetch = entry._fetch_text

        def no_fetch(url, timeout=20):
            raise OSError("offline test")
        entry._fetch_text = no_fetch
        self.body = json.dumps(_DOC)
        self.d = tempfile.mkdtemp()
        self.cwd = os.getcwd()
        os.chdir(self.d)

    def tearDown(self):
        entry._open_url, entry._sleep, entry._nse_session_get = self._open, self._sleep, self._nse
        entry._fetch_text = self._fetch
        os.chdir(self.cwd)
        shutil.rmtree(self.d)

    def answers(self, *codes):
        seq = list(codes)

        def fake(url, timeout=20):
            self.calls.append(url)
            code = seq.pop(0)
            if code == 200:
                return self.body
            raise urllib.error.HTTPError(url, code, "x", {}, io.BytesIO(b'{"chart":{"result":null,'
                                                                           b'"error":{"description":"No data found"}}}'))
        entry._open_url = fake

    def test_url_and_429_retry_once_after_a_pause(self):
        self.answers(429, 200)
        c = entry.get_chart("RELIANCE", "5m", datetime.date(2026, 10, 23), today=datetime.date(2026, 10, 23))
        self.assertEqual(len(c["bars"]), sum(1 for x in _DOC["chart"]["result"][0]["indicators"]["quote"][0]["close"]
                                             if x is not None))
        self.assertEqual(self.calls, ["https://query1.finance.yahoo.com/v8/finance/chart/RELIANCE.NS?range=1d&interval=5m"] * 2)
        self.assertEqual(self.sleeps, [entry.BACKOFF])

    def test_429_twice_falls_back_to_query2(self):
        self.answers(429, 429, 200)
        entry.get_chart("RELIANCE", "5m", datetime.date(2026, 10, 23), today=datetime.date(2026, 10, 23))
        self.assertEqual([u.split("/")[2] for u in self.calls],
                         ["query1.finance.yahoo.com", "query1.finance.yahoo.com", "query2.finance.yahoo.com"])

    def test_5xx_falls_back_without_waiting(self):
        self.answers(503, 200)
        entry.get_chart("RELIANCE", "5m", datetime.date(2026, 10, 23), today=datetime.date(2026, 10, 23))
        self.assertEqual(self.sleeps, [])
        self.assertIn("query2", self.calls[1])

    def test_still_busy_is_one_plain_error(self):
        self.answers(429, 429, 429, 429)
        code, out, err = run(["check", "RELIANCE", "--now", "2026-10-19 09:50"])
        self.assertEqual(code, 1)
        self.assertIn("HTTP 429", err)
        self.assertNotIn("Traceback", err)

    def test_unknown_symbol_404_and_wrong_file(self):
        self.answers(404)
        with self.assertRaises(entry.EntryError) as cm:
            entry.get_chart("ZZZZ", "5m", datetime.date(2026, 10, 23), today=datetime.date(2026, 10, 23))
        self.assertIn("No data found", str(cm.exception))
        self.answers(200)
        with self.assertRaises(entry.EntryError) as cm:
            entry.get_chart("RELIANCE", "5m", datetime.date(2026, 10, 23), today=datetime.date(2026, 10, 23),
                            exch="BSE")
        self.assertIn("holds RELIANCE.NS, not RELIANCE.BO", str(cm.exception))

    def test_ranges_for_past_sessions(self):
        today = datetime.date(2026, 10, 23)
        self.assertEqual(entry.chart_range(today, today, "5m"), "1d")
        self.assertEqual(entry.chart_range(datetime.date(2026, 10, 19), today, "5m"), "5d")
        self.assertEqual(entry.chart_range(datetime.date(2026, 10, 1), today, "5m"), "1mo")
        self.assertEqual(entry.chart_range(datetime.date(2026, 9, 1), today, "5m"), "60d")
        with self.assertRaises(entry.EntryError):
            entry.chart_range(datetime.date(2026, 10, 1), today, "1m")


def live_provider(d, lag_until=None, calls=None):
    """Bars the way Yahoo serves them at `now`: closed bars final, the in-progress bar
    showing a spike to 1,225.00 (above the high, then it closes under), plus a live tick.
    lag_until: before this UTC time Yahoo's last trade lags 30 seconds behind."""
    base = chart(d)

    def provider(now):
        if calls is not None:
            calls.append(now)
        nts = entry.ts_of(now)
        lagging = lag_until is not None and now < lag_until
        data_ts = nts - 30 if lagging else nts
        bars = []
        for b in base["bars"]:
            if b["t"] % 300 or b["t"] > data_ts:
                continue
            b = dict(b)
            if b["t"] + 300 > data_ts:
                b["c"], b["h"] = 1225.00, max(b["h"], 1225.00)
            bars.append(b)
        if data_ts % 300:
            bars.append({"t": data_ts, "o": 1225.0, "h": 1225.0, "l": 1225.0, "c": 1225.0, "v": 0})
        return {"symbol": "RELIANCE", "exchange": "NSE", "yahoo": "RELIANCE.NS", "meta": {}, "bars": bars,
                "prev_close": 1211.2, "data_time": data_ts}
    return provider


class Watch(unittest.TestCase):
    OPTS = {"setup": "orb", "or_minutes": 15, "interval": "5m", "level": None, "min_volume_x": None, "cfg": None}

    def loop(self, provider, start, **kw):
        clock = entry.SimClock(ist(start))
        lines, fired = [], []
        code, res = entry.watch_loop(provider, clock.now, clock.sleep, lines.append, dict(self.OPTS),
                                     on_trigger=lambda r, now: fired.append((r, now)), **kw)
        return code, res, lines, fired, clock

    def test_fires_on_the_first_closed_bar_not_the_spike_in_progress(self):
        calls = []
        code, res, lines, fired, clock = self.loop(live_provider(_DOC, calls=calls), "2026-10-19 09:42", every=60)
        self.assertEqual(code, 0)
        self.assertEqual(len(fired), 1)
        self.assertEqual(fired[0][0]["trigger"]["time"], "9:50 AM")
        self.assertEqual(fired[0][0]["trigger"]["price"], 1227.8)
        self.assertEqual([entry.to_ist(c).strftime("%H:%M:%S") for c in calls], ["09:42:00", "09:45:02", "09:50:02"])
        text = "\n".join(lines)
        self.assertIn(" 9:40 bar  closed ₹1,221.80  ₹1.60 under ₹1,223.40", text)
        self.assertIn(" 9:45 bar  closed ₹1,227.80  TRIGGERED", text)
        self.assertEqual(text.count("TRIGGERED"), 1)

    def test_waits_for_yahoo_to_catch_up(self):
        lag = ist("2026-10-19 09:50:30")
        calls = []
        code, res, lines, fired, clock = self.loop(live_provider(_DOC, lag_until=lag, calls=calls),
                                                   "2026-10-19 09:47", every=30)
        self.assertEqual(code, 0)
        times = [entry.to_ist(c).strftime("%H:%M:%S") for c in calls]
        self.assertEqual(times, ["09:47:00", "09:50:02", "09:50:32"])
        self.assertEqual(fired[0][0]["trigger"]["time"], "9:50 AM")

    def test_from_before_the_open_prints_the_range_bar_by_bar(self):
        code, res, lines, fired, clock = self.loop(live_provider(_DOC), "2026-10-19 08:55", every=30)
        self.assertEqual(code, 0)
        text = "\n".join(lines)
        self.assertIn("not open yet", lines[0])
        self.assertIn(" 9:25 bar  closed ₹1,208.40  opening range set: high ₹1,223.40, low ₹1,207.30", text)
        self.assertEqual(sum(1 for x in lines if " bar  closed " in x), 7)    # 9:15 ... 9:45, one line each

    def test_a_computer_that_dozes_off_catches_up_when_it_wakes(self):
        class Dozing(entry.SimClock):
            naps = 0

            def sleep(self, seconds):
                entry.SimClock.sleep(self, seconds)
                self.naps += 1
                if self.naps == 2:                      # the laptop sleeps for 12 minutes mid-wait
                    self.t += datetime.timedelta(minutes=12)
        clock = Dozing(ist("2026-10-19 09:31"))
        calls, lines = [], []
        code, res = entry.watch_loop(live_provider(_DOC, calls=calls), clock.now, clock.sleep, lines.append,
                                     dict(self.OPTS), every=30)
        self.assertEqual(code, 0)
        times = [entry.to_ist(c).strftime("%H:%M:%S") for c in calls]
        self.assertEqual(times[:2], ["09:31:00", "09:44:00"])   # straight after waking, not a long sleep later
        text = "\n".join(lines)
        for start in (" 9:30", " 9:35", " 9:40"):
            self.assertIn(start + " bar  closed", text)          # the bars it slept through, one line each
        self.assertEqual(res["trigger"]["time"], "9:50 AM")

    def test_expires_and_until(self):
        code, res, lines, fired, clock = self.loop(live_provider(no_breakout()), "2026-10-19 14:00", every=300)
        self.assertEqual(code, 1)
        self.assertEqual(res["status"], "expired")
        self.assertEqual(fired, [])
        self.assertEqual(entry.to_ist(clock.now()).strftime("%H:%M"), "14:45")
        until = DT(2026, 10, 19, 11, 0)
        code, res, lines, fired, clock = self.loop(live_provider(no_breakout()), "2026-10-19 10:30", every=300,
                                                   until=until)
        self.assertEqual(code, 1)
        self.assertIn("--until 11:00", lines[-1])

    def test_closed_day_and_repeated_errors(self):
        code, _, lines, _, _ = self.loop(live_provider(_DOC), "2026-10-17 10:00")
        self.assertEqual(code, 1)
        self.assertIn("weekend", lines[0])
        code, _, lines, _, _ = self.loop(live_provider(_DOC), "2026-10-20 10:00")
        self.assertIn("Dussehra", lines[0])

        def broken(now):
            raise entry.EntryError("Yahoo did not answer")
        code, _, lines, _, _ = self.loop(broken, "2026-10-19 10:00", every=30)
        self.assertEqual(code, 1)
        self.assertIn("Stopping after 5 failed reads", lines[-1])


class Cli(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.cwd = os.getcwd()
        os.chdir(self.d)

    def tearDown(self):
        os.chdir(self.cwd)
        shutil.rmtree(self.d)

    def test_check_write_is_the_contract_shape(self):
        code, text, _ = run(["check", "RELIANCE", "--offline", REL, "--now", "2026-10-19 09:50", "--write"])
        self.assertEqual(code, 0)
        self.assertIn("TRIGGERED 9:50 AM at ₹1,227.80", text)
        self.assertIn("/india-exit", text)
        with open(os.path.join("trader-in", "entry.json"), encoding="utf-8") as fh:
            e = json.load(fh)
        want = {"created_by": "india-entry", "symbol": "RELIANCE", "exchange": "NSE", "yahoo_symbol": "RELIANCE.NS",
                "currency": "INR", "setup": "orb", "side": "buy", "date": "2026-10-19",
                "status": "triggered", "time": "9:50 AM", "bar_start": "09:45", "bar_index": 6, "price": 1227.8,
                "level": 1223.4, "interval": "5m", "opening_range": {"high": 1223.4, "low": 1207.3, "minutes": 15},
                "prev_close": 1211.2, "source": "yahoo", "checked_at": "2026-10-19T04:20:00Z"}
        for k, v in want.items():
            self.assertEqual(e[k], v, k)
        self.assertFalse(os.path.exists(os.path.join("trader-in", "holidays.json")))   # --offline: no fetch

    def test_check_waiting_json_and_exit_codes(self):
        code, text, _ = run(["check", "RELIANCE", "--offline", REL, "--now", "2026-10-19 09:47", "--json"])
        self.assertEqual(code, 1)
        r = json.loads(text)
        self.assertEqual(r["status"], "waiting")
        self.assertNotIn("_bars", r)
        code, text, _ = run(["check", "RELIANCE", "--offline", REL, "--now", "2026-10-19 09:47", "--write", "e.json"])
        with open("e.json") as fh:
            self.assertEqual(json.load(fh)["status"], "waiting")
        self.assertIn("still forming", text)

    def test_offline_folder_and_mismatch(self):
        os.mkdir("charts")
        shutil.copy(REL, os.path.join("charts", "RELIANCE.json.gz"))
        code, text, _ = run(["check", "reliance.ns", "--offline", "charts", "--now", "2026-10-19 09:50"])
        self.assertEqual(code, 0)
        code, _, err = run(["check", "TCS", "--offline", REL])
        self.assertEqual(code, 1)
        self.assertIn("holds RELIANCE.NS, not TCS.NS", err)
        code, _, err = run(["check", "BSE:RELIANCE", "--offline", REL])
        self.assertIn("not RELIANCE.BO", err)

    def test_longs_only_and_flag_errors(self):
        code, _, err = run(["check", "RELIANCE", "--offline", REL, "--side", "short"])
        self.assertEqual(code, 1)
        self.assertIn("longs only", err)
        code, _, err = run(["check", "RELIANCE", "--offline", REL, "--level", "1300"])
        self.assertIn("--level is for --setup level", err)
        code, _, err = run(["check", "RELIANCE", "--offline", REL, "--setup", "level"])
        self.assertIn("needs --level", err)

    def test_config_is_read(self):
        os.mkdir("trader-in")
        with open(os.path.join("trader-in", "config.json"), "w") as fh:
            json.dump({"no_entries_after": "09:45"}, fh)
        code, text, _ = run(["check", "RELIANCE", "--offline", REL, "--now", "2026-10-19 09:50"])
        self.assertEqual(code, 1)
        self.assertIn("EXPIRED", text)
        with open(os.path.join("trader-in", "config.json"), "w") as fh:
            fh.write("{nope")
        code, _, err = run(["check", "RELIANCE", "--offline", REL])
        self.assertIn("not valid JSON", err)

    def test_watch_replays_the_file_and_writes_the_entry(self):
        code, text, _ = run(["watch", "RELIANCE", "--offline", REL, "--now", "2026-10-19 09:30", "--every", "30"])
        self.assertEqual(code, 0)
        self.assertIn(" 9:45 bar  closed ₹1,227.80  TRIGGERED", text)
        with open(os.path.join("trader-in", "entry.json")) as fh:
            self.assertEqual(json.load(fh)["bar_index"], 6)
        code, text, _ = run(["watch", "RELIANCE", "--offline", REL, "--now", "2026-10-19 09:30", "--until", "09:40",
                             "--out", "x.json"])
        self.assertEqual(code, 1)
        self.assertFalse(os.path.exists("x.json"))

    def test_watchlist(self):
        os.mkdir("charts")
        shutil.copy(REL, os.path.join("charts", "RELIANCE.json.gz"))
        shutil.copy(DEMO, os.path.join("charts", "DEMO.json"))
        os.mkdir("trader-in")
        with open(os.path.join("trader-in", "watchlist.json"), "w") as fh:
            json.dump({"created_by": "india-scanner", "hits": [
                {"symbol": "RELIANCE", "exchange": "NSE", "setups": ["orb"], "or_high": 1223.4, "or_low": 1207.3},
                {"symbol": "ZZZZ", "setups": ["orb"]}, {"symbol": "DEMO", "setups": ["orb"]}]}, fh)
        code, text, _ = run(["watchlist", "--offline", "charts", "--now", "2026-10-19 09:50"])
        self.assertEqual(code, 0)
        rows = text.splitlines()
        self.assertTrue(rows[1].startswith("RELIANCE      triggered  9:50 AM   ₹1,227.80   ₹1,223.40"), rows[1])
        self.assertIn("ZZZZ          error: no offline file", rows[2])
        self.assertIn("DEMO          error: DEMO has 1m bars", rows[3])
        self.assertIn("1 of 3 triggered", text)
        code, text, _ = run(["watchlist", "--offline", "charts", "--now", "2026-10-19 09:47", "--json"])
        self.assertEqual(code, 1)
        self.assertEqual(json.loads(text)[0]["result"]["status"], "waiting")
        code, _, err = run(["watchlist", "--watchlist", "missing.json"])
        self.assertIn("run /india-scanner first", err)


KITE_OPEN = os.path.join(FIX, "shared", "kite-RELIANCE-5minute-2026-10-19-open.json")
KITE_CLOSE = os.path.join(FIX, "shared", "kite-RELIANCE-5minute-2026-10-19-close.json")
KITE_LTP = os.path.join(FIX, "shared", "kite-ltp-2026-10-23.json")
FO_CSV = os.path.join(FIX, "shared", "fo_mktlots-2026-10-23.csv")


class SessionRules(unittest.TestCase):
    def test_cas_and_non_cas_days(self):
        r = entry.session_rules(True, DAY)
        self.assertEqual((r["close"], r["square_off"], r["flat_by_default"], r["bars_5m"]), ("15:15", "15:12", "15:00", 72))
        r = entry.session_rules(False, DAY)
        self.assertEqual((r["close"], r["square_off"], r["flat_by_default"], r["bars_5m"]), ("15:30", "15:25", "15:10", 75))
        self.assertEqual(entry.session_rules(None, DAY)["close"], "15:15")          # unknown -> strict
        old = datetime.date(2026, 7, 30)                                            # before CAS started (2026-08-03)
        self.assertEqual((entry.session_rules(True, old)["close"], entry.session_rules(True, old)["bars_5m"]), ("15:30", 75))
        self.assertEqual(entry.session_rules(None, DAY)["pre_open"], "09:00-09:10")
        self.assertEqual(entry.session_rules(None, datetime.date(2026, 9, 4))["pre_open"], "09:00-09:08")

    def test_the_fo_list(self):
        with open(FO_CSV, encoding="utf-8") as fh:
            fo = entry.parse_fo_list(fh.read())
        self.assertIn("RELIANCE", fo)
        self.assertIn("M&M", fo)
        self.assertIn("SBIN", fo)
        for idx in ("NIFTY", "BANKNIFTY", "FINNIFTY", "NIFTYFPI", "SYMBOL"):
            self.assertNotIn(idx, fo)
        self.assertNotIn("20MICRONS", fo)
        self.assertTrue(entry.cas_status("RELIANCE", None, {}, fo))
        self.assertFalse(entry.cas_status("20MICRONS", None, {}, fo))
        self.assertIsNone(entry.cas_status("20MICRONS", None, {}, None))
        self.assertFalse(entry.cas_status("RELIANCE", None, {"non_cas_symbols": ["reliance"]}, fo))
        self.assertTrue(entry.cas_status("20MICRONS", None, {"cas_symbols_extra": ["20MICRONS"]}, fo))
        self.assertFalse(entry.cas_status("RELIANCE", "no", {}, fo))
        self.assertEqual(entry.parse_fo_list("# fetched 2026-10-23\nRELIANCE\nSBIN\n"), {"RELIANCE", "SBIN"})
        d = tempfile.mkdtemp()
        saved = entry._fetch_text
        try:
            with open(FO_CSV, encoding="utf-8") as fh:
                body = fh.read()
            entry._fetch_text = lambda url, timeout=20: body
            path = os.path.join(d, "fo_list.txt")
            got, note = entry.load_fo_list(path, fetch=True)
            self.assertEqual((note, "RELIANCE" in got), ("", True))
            with open(path) as fh:
                self.assertTrue(fh.readline().startswith("# fetched "))

            def down(url, timeout=20):
                raise OSError("403")
            entry._fetch_text = down
            got, note = entry.load_fo_list(os.path.join(d, "none.txt"), fetch=True)
            self.assertIsNone(got)
            self.assertIn("stricter CAS rules", note)
        finally:
            entry._fetch_text = saved
            shutil.rmtree(d)

    def test_expired_and_not_open_use_the_stocks_close(self):
        d = no_breakout()
        r = at("2026-10-19 15:16", d)
        self.assertEqual((r["status"], r["phase"]), ("expired", "after-close"))
        self.assertIn("continuous trading ended at 15:15 (CAS stock)", r["reason"])
        r = at("2026-10-19 15:16", d, cas=False)
        self.assertEqual((r["status"], r["phase"]), ("expired", "after-cutoff"))
        r = at("2026-10-19 09:05")
        self.assertIn("pre-open order entry 09:00-09:10", r["reason"])
        self.assertIn("continuous trading ends 15:15 today (CAS stock)", r["reason"])
        self.assertIn("15:30 today (non-CAS stock)", at("2026-10-19 09:05", cas=False)["reason"])
        self.assertIn("F&O list unknown", "\n".join(entry.describe(at("2026-10-19 09:50"))))
        self.assertNotIn("F&O list unknown", "\n".join(entry.describe(at("2026-10-19 09:50", cas=True))))


class Kite(unittest.TestCase):
    """The Kite worked example: get_historical_data output for RELIANCE (token 738561), 19 Oct, 5minute."""

    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.cwd = os.getcwd()
        os.chdir(self.d)

    def tearDown(self):
        os.chdir(self.cwd)
        shutil.rmtree(self.d)

    def test_kite_candles_give_the_kite_example(self):
        ch = entry.kite_chart([KITE_OPEN], "RELIANCE")
        self.assertEqual((ch["source"], ch["meta"]["dataGranularity"], len(ch["bars"])), ("kite", "5m", 9))
        r = entry.evaluate(ch, ist("2026-10-19 09:50"), cas=True)
        self.assertEqual((r["opening_range"]["high"], r["opening_range"]["low"]), (1223.6, 1207.1))
        self.assertEqual(r["opening_range"]["avg_volume"], 401214)       # Kite books the 09:15 volume: 615,358
        t = r["trigger"]
        self.assertEqual((t["time"], t["bar_index"], t["price"]), ("9:50 AM", 6, 1227.7))
        text = "\n".join(entry.describe(r))
        self.assertIn("on 5-minute bars (Kite candles)", text)
        self.assertIn("TRIGGERED 9:50 AM at ₹1,227.70", text)
        self.assertIn("+0.34%; Kite candles)", text)
        self.assertNotIn("Yahoo", text)

    def test_open_and_close_chunks_merge_by_date(self):
        ch = entry.kite_chart([KITE_OPEN, KITE_CLOSE, KITE_OPEN], "RELIANCE")
        self.assertEqual(len(ch["bars"]), 14)                             # 9 + 5, the repeat de-duplicated
        r = entry.evaluate(ch, ist("2026-10-19 15:20"), cas=True)
        self.assertEqual(r["_bars"][-1]["slot"], 71)                      # the 15:10 bar: Kite ends at 15:15
        self.assertEqual(r["trigger"]["time"], "9:50 AM")

    def test_a_kite_file_saved_mid_bar_waits_for_a_fresh_one(self):
        with open(KITE_OPEN) as fh:
            candles = json.load(fh)[:7]                                   # as saved at 09:48:30: 09:15 ... 09:45 (forming)
        with open("k.json", "w") as fh:
            json.dump(candles, fh)
        saved = entry.ts_of(ist("2026-10-19 09:48:30"))
        os.utime("k.json", (saved, saved))
        ch = entry.kite_chart(["k.json"], "RELIANCE")
        r = entry.evaluate(ch, ist("2026-10-19 09:55"), cas=True)          # minutes later: still not counted
        self.assertEqual(r["status"], "waiting")
        self.assertIn("Kite candles were saved at 09:48:30", r["lag_note"])

    def test_cli_flags_offline_detection_and_ltp(self):
        code, text, err = run(["check", "RELIANCE", "--kite-bars", KITE_OPEN, "--kite-bars", KITE_CLOSE,
                               "--now", "2026-10-19 09:50", "--kite-ltp", KITE_LTP, "--fo-list", FO_CSV,
                               "--no-fetch", "--write"])
        self.assertEqual(code, 0, err)
        self.assertIn("Last price ₹1,190.00 (Kite, live)", text)
        with open(os.path.join("trader-in", "entry.json")) as fh:
            e = json.load(fh)
        self.assertEqual((e["source"], e["price"], e["level"], e["cas"], e["session_close"], e["square_off"]),
                         ("kite", 1227.7, 1223.6, True, "15:15", "15:12"))
        self.assertEqual(e["opening_range"], {"high": 1223.6, "low": 1207.1, "minutes": 15})
        code, text, _ = run(["check", "RELIANCE", "--offline", KITE_OPEN, "--now", "2026-10-19 09:47"])
        self.assertEqual(code, 1)
        self.assertIn("(Kite candles)", text)
        self.assertIn("₹1.90 under the range high ₹1,223.60", text)
        code, _, err = run(["check", "RELIANCE", "--kite-bars", KITE_OPEN, "--interval", "1m", "--no-fetch"])
        self.assertIn("5m bars, not 1m", err)
        code, text, _ = run(["check", "RELIANCE", "--offline", REL, "--now", "2026-10-19 09:50",
                             "--price", "RELIANCE=1229.5"])
        self.assertIn("Last price ₹1,229.50 (your price)", text)
        self.assertIn("(Yahoo, free, can lag)", text)
        self.assertEqual(entry.kite_ltp(KITE_LTP, "RELIANCE", "BSE"), 1190.0)
        with self.assertRaises(entry.EntryError):
            entry.kite_ltp(KITE_LTP, "INFY")
        self.assertIsNone(entry.kite_candles({"chart": {}}))
        raw = {"status": "success", "data": {"candles": [["2026-10-19T09:15:00+05:30", 1, 2, 0.5, 1.5, 10]]}}
        self.assertEqual(entry.kite_candles(raw)[0]["close"], 1.5)


class SkillDoc(unittest.TestCase):
    def test_skill_md(self):
        with open(os.path.join(ROOT, "india-entry", "SKILL.md"), encoding="utf-8") as fh:
            text = fh.read()
        self.assertTrue(text.startswith("---\nname: india-entry\n"))
        self.assertGreater(len(text.splitlines()), 60)
        safety = " ".join(text[text.index("## Safety"):].lower().split())
        for must in ("not financial advice", "india-broker", "india-risk", "needs approval", "no order without your yes"):
            self.assertIn(must, safety)
        risk = os.path.join(ROOT, "india-risk", "SKILL.md")
        low = " ".join(text.lower().split())
        for must in ("bar by bar", "can lag", "day trading loses most people money", "/india-exit", "longs only",
                     "in the background", "/loop", "nothing was placed", "9:50 am", "₹1,227.80", "bar 6",
                     "09:15", "ist", "kite mcp", "free yahoo data", "₹1,215.30", "₹1,240.30",
                     "\u20b91,252.80", "trader-in/entry.json", "with zerodha kite connected", "get_historical_data",
                     "search_instruments", "get_ltp", "trader-in/kite/", "--kite-bars", "\u20b91,227.70", "1,207.10",
                     "\u20b91,240.10", "15:15", "15:12"):
            self.assertIn(must, low)
        for never in ("\u2014", "tick " + "by tick", "real " + "time", "pro" + "fit", "new york", "trader/"):
            self.assertNotIn(never, low)
        if os.path.exists(risk):            # the pack's skills share one safety wording where they can
            with open(risk, encoding="utf-8") as fh:
                self.assertIn("## Safety", fh.read())


if __name__ == "__main__":
    unittest.main()
