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

from _load import load, ROOT, FIX

alerts = load("india-alerts", "alerts")

QDIR = os.path.join(FIX, "india-alerts")                     # live quotes, Fri 2026-10-23 ~11:32 IST
DAY1050 = os.path.join(QDIR, "day-2026-10-19-1050")           # cut from the shared RELIANCE 5m bars
DAY1205 = os.path.join(QDIR, "day-2026-10-19-1205")
PLAN = os.path.join(QDIR, "plan-reliance.json")
DT = datetime.datetime
UA = "Mozilla/5.0 (compatible; india-day-trader-skill/1.0; +https://github.com/)"

# NSE CM trading holidays from the NSE holiday API (read 2026-10-23)
NSE_2026 = ["2026-01-15", "2026-01-26", "2026-02-15", "2026-03-03", "2026-03-21", "2026-03-26",
            "2026-03-31", "2026-04-03", "2026-04-14", "2026-05-01", "2026-05-28", "2026-06-26",
            "2026-08-15", "2026-09-14", "2026-10-02", "2026-10-20", "2026-11-08", "2026-11-10",
            "2026-11-24", "2026-12-25"]


def ist(text):
    return alerts.parse_now(text)


def run(argv):
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = alerts.main(argv)
    return code, out.getvalue(), err.getvalue()


def plan_rules():
    with open(PLAN) as fh:
        return alerts.rules_from_plan(json.load(fh))


def cal():
    return alerts.load_calendar(offline=QDIR)[0]


def status(text):
    return alerts.market_status(ist(text), cal())


class Calendar(unittest.TestCase):
    def test_holidays_match_the_published_list(self):
        self.assertEqual(sorted(alerts.NSE_HOLIDAYS_2026), NSE_2026)
        live = cal()
        self.assertEqual(sorted(live.holidays), NSE_2026)          # the live answer agrees
        self.assertEqual(live.years, {2026})
        self.assertIn("2026-11-08", live.muhurat)
        self.assertIn("2026-11-08", alerts.FALLBACK.muhurat)

    def test_ist_is_a_fixed_offset(self):
        self.assertEqual(ist("2026-10-19 09:15"), DT(2026, 10, 19, 3, 45))
        self.assertEqual(ist("2026-03-09 09:15"), DT(2026, 3, 9, 3, 45))       # no daylight saving
        self.assertEqual(ist("2026-10-23T06:02:25Z"), DT(2026, 10, 23, 6, 2, 25))
        self.assertEqual(ist("2026-10-19T10:15:00+05:30"), DT(2026, 10, 19, 4, 45))
        self.assertEqual(alerts.to_ist(DT(2026, 10, 19, 3, 45)), DT(2026, 10, 19, 9, 15))
        with self.assertRaises(alerts.AlertError):
            ist("tomorrow")

    def test_market_status(self):
        self.assertTrue(status("2026-10-19 09:15")["open"])
        self.assertFalse(status("2026-10-19 15:30")["open"])
        self.assertTrue(status("2026-10-19 15:29")["open"])
        pre = status("2026-10-19 09:03")
        self.assertEqual((pre["open"], pre["phase"]), (False, "pre_open"))
        self.assertEqual(status("2026-10-19 09:10")["phase"], "pre_open")
        self.assertEqual(status("2026-10-19 08:59")["phase"], "closed")
        self.assertEqual(status("2026-10-19 08:59")["next_open"], "2026-10-19 09:15")
        after = status("2026-10-19 15:45")
        self.assertEqual(after["phase"], "closing")
        self.assertEqual(after["next_open"], "2026-10-21 09:15")             # Tue 10-20 is Dussehra
        dus = status("2026-10-20 10:00")
        self.assertFalse(dus["open"])
        self.assertIn("Dussehra", dus["reason"])
        self.assertEqual(status("2026-10-24 10:00")["reason"], "weekend")
        self.assertEqual(status("2026-10-23 15:00")["mis_squareoff"], "15:12 (CAS) / 15:25 (non-CAS)")
        diwali = status("2026-11-08 18:15")
        self.assertIn("Muhurat", diwali["muhurat"])
        self.assertEqual(status("2026-11-09 08:00")["next_open"], "2026-11-09 09:15")
        self.assertEqual(status("2026-11-09 16:00")["next_open"], "2026-11-11 09:15")  # 11-10 Balipratipada
        xmas = status("2026-12-25 10:00")
        self.assertIn("Christmas", xmas["reason"])
        self.assertEqual(xmas["next_open"], "2026-12-28 09:15")
        y27 = status("2027-01-04 10:00")
        self.assertFalse(y27["calendar_known"])
        self.assertIn("2027", alerts.calendar_warning(y27))

    def test_live_holiday_fetch_is_cached(self):
        with open(os.path.join(QDIR, "nse-holidays.json"), "rb") as fh:
            body = fh.read()
        seen = []

        class Opener(object):
            def open(self, req, timeout=None):
                seen.append((req.full_url, req.get_header("Referer")))
                if req.full_url in alerts.NSE_COOKIE_PAGES:
                    raise urllib.error.HTTPError(req.full_url, 403, "x", {}, io.BytesIO(b""))
                return io.BytesIO(body)

        saved = (alerts._build_opener, alerts._sleep)
        alerts._build_opener = lambda *h: Opener()
        alerts._sleep = lambda s: None
        d = tempfile.mkdtemp()
        try:
            cache = os.path.join(d, "holidays.json")
            now = ist("2026-10-23 11:30")
            c, note = alerts.load_calendar(cache, None, False, now)
            self.assertEqual((note, c.source), ("", "NSE holiday API (live)"))
            self.assertEqual(seen[-1], (alerts.NSE_HOLIDAY_API, "https://www.nseindia.com/"))
            n = len(seen)
            c, _ = alerts.load_calendar(cache, None, False, ist("2026-10-25 11:30"))
            self.assertEqual(len(seen), n)                                   # from the cache
            self.assertIn("cached 2026-10-23", c.source)
            alerts._build_opener = lambda *h: (_ for _ in ()).throw(alerts.AlertError("down"))

            class Down(object):
                def open(self, req, timeout=None):
                    raise urllib.error.URLError("no route")
            alerts._build_opener = lambda *h: Down()
            c, note = alerts.load_calendar(os.path.join(d, "none.json"), None, False, now)
            self.assertIs(c, alerts.FALLBACK)
            self.assertIn("built-in 2026 list", note)
        finally:
            alerts._build_opener, alerts._sleep = saved
            shutil.rmtree(d)


class Money(unittest.TestCase):
    def test_indian_grouping(self):
        self.assertEqual(alerts.inr(1227.8), "₹1,227.80")
        self.assertEqual(alerts.inr(123456.78), "₹1,23,456.78")
        self.assertEqual(alerts.inr(12345678), "₹1,23,45,678.00")
        self.assertEqual(alerts.inr(16.32), "₹16.32")

    def test_ticks_and_circuits(self):
        self.assertEqual(alerts.tick_size(16.32), 0.01)
        self.assertEqual(alerts.tick_size(1227.8), 0.10)
        self.assertEqual(alerts.round_to_tick(1215.35, "down"), 1215.3)
        self.assertEqual(alerts.round_to_tick(1215.3, "down"), 1215.3)
        self.assertEqual(alerts.circuits(17.05, 20), (13.64, 20.46))


class Symbols(unittest.TestCase):
    def test_parse_symbol(self):
        self.assertEqual(alerts.parse_symbol("reliance"), ("RELIANCE", "NSE"))
        self.assertEqual(alerts.parse_symbol("NSE:RELIANCE"), ("RELIANCE", "NSE"))
        self.assertEqual(alerts.parse_symbol("RELIANCE.BO"), ("RELIANCE", "BSE"))
        self.assertEqual(alerts.parse_symbol("BSE:RELIANCE"), ("RELIANCE", "BSE"))
        self.assertEqual(alerts.parse_symbol("500325"), ("500325", "BSE"))
        self.assertEqual(alerts.parse_symbol("M&M"), ("M&M", "NSE"))
        with self.assertRaises(alerts.AlertError):
            alerts.parse_symbol("NYSE:TSLA")
        with self.assertRaises(alerts.AlertError):
            alerts.yahoo_symbol("500325", "BSE")

    def test_urls_and_user_agent(self):
        self.assertEqual(alerts.UA, UA)
        self.assertEqual(alerts.yahoo_url("M&M.NS"),
                         "https://query1.finance.yahoo.com/v8/finance/chart/M%26M.NS?range=1d&interval=1d")
        self.assertIn("//query2.finance.yahoo.com/", alerts.yahoo_url("RELIANCE.NS", alerts.YAHOO_HOSTS[1]))


class Quotes(unittest.TestCase):
    def test_live_yahoo_fixtures(self):
        with open(os.path.join(QDIR, "yahoo-RELIANCE.NS.json")) as fh:
            q = alerts.parse_yahoo(fh.read(), "RELIANCE")
        self.assertEqual((q["price"], q["prev_close"], q["currency"]), (1170.5, 1184.5, "INR"))
        self.assertEqual(q["time"], "2026-10-23T06:02:21Z")
        with open(os.path.join(QDIR, "yahoo-RELIANCE.BO.json")) as fh:
            q = alerts.parse_yahoo(fh.read(), "RELIANCE", "BSE")
        self.assertEqual((q["price"], q["exchange"]), (1171.2, "BSE"))

    def test_offline_quotes_and_errors(self):
        q, errors = alerts.get_quotes([("RELIANCE", "NSE"), ("M&M", "NSE"), ("RELIANCE", "BSE"),
                                       ("ZZZZNOPE", "NSE"), ("NOPE", "NSE")], QDIR)
        self.assertEqual(sorted(q), ["M&M", "RELIANCE"])
        self.assertEqual(q["M&M"]["price"], 2822.6)
        self.assertEqual(q["RELIANCE"]["exchange"], "NSE")      # one key per symbol: the first wins
        self.assertEqual(len(errors), 2)
        self.assertIn("No data found", errors[0])

    def test_the_example_day_cut(self):
        q, _ = alerts.get_quotes([("RELIANCE", "NSE")], DAY1050)
        self.assertEqual((q["RELIANCE"]["price"], q["RELIANCE"]["prev_close"]), (1220.5, 1211.2))
        self.assertEqual(q["RELIANCE"]["time"], "2026-10-19T05:20:00Z")    # 10:50 IST

    def test_offline_gz(self):
        d = tempfile.mkdtemp()
        try:
            with open(os.path.join(QDIR, "yahoo-RELIANCE.NS.json"), "rb") as src:
                with gzip.open(os.path.join(d, "yahoo-RELIANCE.NS.json.gz"), "wb") as dst:
                    dst.write(src.read())
            q, errors = alerts.get_quotes([("RELIANCE", "NSE")], d)
            self.assertEqual((q["RELIANCE"]["price"], errors), (1170.5, []))
        finally:
            shutil.rmtree(d)

    def test_pushed_prices_win(self):
        now = ist("2026-10-19 10:50")
        pushed = alerts.parse_price_args(["RELIANCE=1227.8", "NSE:TCS=2,559.70"], now)
        self.assertEqual((pushed["RELIANCE"]["price"], pushed["TCS"]["price"]), (1227.8, 2559.7))
        q, errors = alerts.get_quotes([("RELIANCE", "NSE")], "/nonexistent", pushed=pushed)
        self.assertEqual((q["RELIANCE"]["price"], q["RELIANCE"]["source"], errors), (1227.8, "kite", []))
        with self.assertRaises(alerts.AlertError):
            alerts.parse_price_args(["RELIANCE"], now)
        kite = {"data": {"NSE:RELIANCE": {"instrument_token": 738561, "last_price": 1227.8,
                                          "ohlc": {"open": 1212.2, "high": 1228.0, "low": 1207.3, "close": 1211.2}}}}
        k = alerts.parse_kite(kite, now)
        self.assertEqual((k["RELIANCE"]["price"], k["RELIANCE"]["prev_close"]), (1227.8, 1211.2))
        self.assertEqual(alerts.parse_kite({"BSE:RELIANCE": {"last_price": 1228}}, now)["RELIANCE"]["exchange"],
                         "BSE")

    def test_pushed_prices_in_prices_json_expire(self):
        d = tempfile.mkdtemp()
        try:
            p = os.path.join(d, "prices.json")
            with open(p, "w") as fh:
                json.dump({"as_of": "2026-10-19T05:20:00Z", "source": "kite",
                           "prices": {"RELIANCE": 1227.8, "NSE:TCS": 2559.7}}, fh)
            got = alerts.pushed_from_prices(p, ist("2026-10-19 10:53"), 5)
            self.assertEqual(sorted(got), ["RELIANCE", "TCS"])
            self.assertEqual(alerts.pushed_from_prices(p, ist("2026-10-19 10:56"), 5), {})
            with open(p, "w") as fh:
                json.dump({"as_of": "2026-10-19T05:20:00Z", "source": "yahoo", "prices": {"RELIANCE": 1220.5}}, fh)
            self.assertEqual(alerts.pushed_from_prices(p, ist("2026-10-19 10:51"), 5), {})   # our own cache
        finally:
            shutil.rmtree(d)

    def test_sec_list_bands(self):
        with open(os.path.join(QDIR, "sec_list.csv")) as fh:
            b = alerts.parse_sec_list(fh.read())
        self.assertEqual(b["RELIANCE"], {"band": None, "series": "EQ"})
        self.assertEqual(b["RPOWER"], {"band": 20.0, "series": "EQ"})
        self.assertEqual(b["HFCL"], {"band": 5.0, "series": "BE"})


class FakeNet(object):
    """Stands in for urllib: answers each request with the next status code."""

    def __init__(self, codes, body=None):
        self.codes, self.calls, self.sleeps = list(codes), [], []
        with open(os.path.join(QDIR, "yahoo-RELIANCE.NS.json")) as fh:
            self.body = body or fh.read()

    def urlopen(self, req, timeout=None):
        self.calls.append((req.full_url.split("/")[2], req.get_header("User-agent")))
        code = self.codes.pop(0)
        if code != 200:
            fp = io.BytesIO(b'{"chart":{"result":null,"error":{"code":"Not Found",'
                            b'"description":"No data found, symbol may be delisted"}}}' if code == 404 else b"")
            raise urllib.error.HTTPError(req.full_url, code, "x", {}, fp)
        return io.BytesIO(self.body.encode("utf-8"))


class Network(unittest.TestCase):
    def setUp(self):
        self.saved = (alerts._urlopen, alerts._sleep)

    def tearDown(self):
        alerts._urlopen, alerts._sleep = self.saved

    def net(self, codes):
        f = FakeNet(codes)
        alerts._urlopen = f.urlopen
        alerts._sleep = f.sleeps.append
        return f

    def hosts(self, f):
        return [h.split(".")[0] for h, _ in f.calls]

    def test_query1_first_with_the_contract_user_agent(self):
        f = self.net([200])
        q, errors = alerts.get_quotes([("RELIANCE", "NSE")])
        self.assertEqual((q["RELIANCE"]["price"], errors), (1170.5, []))
        self.assertEqual(f.calls, [("query1.finance.yahoo.com", UA)])

    def test_one_retry_with_a_pause_on_429(self):
        f = self.net([429, 200])
        self.assertEqual(alerts.get_quotes([("RELIANCE", "NSE")])[0]["RELIANCE"]["price"], 1170.5)
        self.assertEqual(self.hosts(f), ["query1", "query1"])
        self.assertEqual(f.sleeps, [alerts.BACKOFF])

    def test_falls_back_to_query2_on_429_and_5xx(self):
        f = self.net([429, 429, 200])
        self.assertIn("RELIANCE", alerts.get_quotes([("RELIANCE", "NSE")])[0])
        self.assertEqual(self.hosts(f), ["query1", "query1", "query2"])
        f = self.net([503, 200])
        self.assertIn("RELIANCE", alerts.get_quotes([("RELIANCE", "NSE")])[0])
        self.assertEqual(self.hosts(f), ["query1", "query2"])
        f = self.net([500, 502])
        q, errors = alerts.get_quotes([("RELIANCE", "NSE")])
        self.assertEqual(q, {})
        self.assertIn("query2.finance.yahoo.com/v8/finance/chart/RELIANCE.NS answered HTTP 502", errors[0])

    def test_a_404_is_yahoos_answer_and_a_403_is_not_retried(self):
        f = self.net([404])
        q, errors = alerts.get_quotes([("NOPE", "NSE")])
        self.assertIn("No data found", errors[0])
        self.assertEqual(self.hosts(f), ["query1"])
        f = self.net([403])
        q, errors = alerts.get_quotes([("RELIANCE", "NSE")])
        self.assertIn("HTTP 403", errors[0])
        self.assertEqual(self.hosts(f), ["query1"])

    def test_a_pause_between_symbols_and_none_for_pushed(self):
        f = self.net([200, 200])
        alerts.get_quotes([("RELIANCE", "NSE"), ("TCS", "NSE")])
        self.assertEqual(f.sleeps, [alerts.PAUSE])
        f = self.net([200])
        pushed = alerts.parse_price_args(["TCS=2559.7"], ist("2026-10-23 11:30"))
        alerts.get_quotes([("TCS", "NSE"), ("RELIANCE", "NSE")], pushed=pushed)
        self.assertEqual((len(f.calls), f.sleeps), (1, []))


class Rules(unittest.TestCase):
    def setUp(self):
        self.q, _ = alerts.get_quotes([("RELIANCE", "NSE"), ("TCS", "NSE"), ("M&M", "NSE"), ("RPOWER", "NSE")],
                                      QDIR)
        self.now = ist("2026-10-23 11:32")
        self.open = alerts.market_status(self.now, cal())

    def at(self, text, rules, state=None, quotes=None):
        now = ist(text)
        return alerts.evaluate(rules, self.q if quotes is None else quotes, alerts.market_status(now, cal()),
                               state or {}, now)

    def test_a_target_rings_once(self):
        rules = [{"id": "tcs-2550", "symbol": "TCS", "type": "above", "price": 2550}]
        rings, st = alerts.evaluate(rules, self.q, self.open, {}, self.now)
        self.assertEqual(len(rings), 1)
        self.assertIn("TCS is ₹2,559.70 (Yahoo, free, can lag): at or above your alert level ₹2,550.00", rings[0]["message"])
        rings, st = alerts.evaluate(rules, self.q, self.open, st, self.now)
        self.assertEqual(rings, [])

    def test_stop_and_rearm(self):
        rules = [{"id": "mm-stop", "symbol": "M&M", "type": "stop", "price": 2830, "rearm": True}]
        rings, st = alerts.evaluate(rules, self.q, self.open, {}, self.now)
        self.assertIn("your stop ₹2,830.00", rings[0]["message"])
        self.q["M&M"]["price"] = 2840.0
        rings, st = alerts.evaluate(rules, self.q, self.open, st, self.now)
        self.assertEqual(rings, [])
        self.assertNotIn("mm-stop", st["rung"])
        self.q["M&M"]["price"] = 2825.0
        self.assertEqual(len(alerts.evaluate(rules, self.q, self.open, st, self.now)[0]), 1)

    def test_pct_move_from_previous_close(self):
        big = [{"id": "rel-3", "symbol": "RELIANCE", "type": "pct_move", "pct": 3}]
        self.assertEqual(alerts.evaluate(big, self.q, self.open, {}, self.now)[0], [])
        small = [{"id": "rel-1", "symbol": "RELIANCE", "type": "pct_move", "pct": 1, "direction": "down"}]
        rings, st = alerts.evaluate(small, self.q, self.open, {}, self.now)
        self.assertIn("RELIANCE moved -1.18% to ₹1,170.50 (from ₹1,184.50; Yahoo, free, can lag)", rings[0]["message"])
        self.assertEqual(alerts.evaluate(small, self.q, self.open, st, self.now)[0], [])
        up = [{"id": "rel-up", "symbol": "RELIANCE", "type": "pct_move", "pct": 1, "direction": "up"}]
        self.assertEqual(alerts.evaluate(up, self.q, self.open, {}, self.now)[0], [])
        tcs = [{"id": "tcs-2", "symbol": "TCS", "type": "pct_move", "pct": 2}]
        self.assertIn("TCS moved +2.11%", alerts.evaluate(tcs, self.q, self.open, {}, self.now)[0][0]["message"])

    def test_circuit_from_the_band(self):
        rule = [{"id": "rp", "symbol": "RPOWER", "type": "circuit"}]
        q = json.loads(json.dumps(self.q))
        q["RPOWER"]["band"] = 20.0
        self.assertEqual(alerts.evaluate(rule, q, self.open, {}, self.now)[0], [])     # 16.32, band 13.64-20.46
        q["RPOWER"]["price"] = 20.30
        rings, st = alerts.evaluate(rule, q, self.open, {}, self.now)
        self.assertIn("upper circuit ₹20.46 (20% band from the previous close ₹17.05)", rings[0]["message"])
        self.assertIn("india-risk blocks new entries", rings[0]["message"])
        self.assertEqual(alerts.evaluate(rule, q, self.open, st, self.now)[0], [])      # once a day
        q["RPOWER"]["price"] = 13.70
        self.assertIn("lower circuit ₹13.64", alerts.evaluate(rule, q, self.open, {}, self.now)[0][0]["message"])
        del q["RPOWER"]["band"]
        self.assertEqual(alerts.evaluate(rule, q, self.open, {}, self.now)[0], [])      # No Band: silent

    def test_opening_bell_and_pre_open_once_per_trading_day(self):
        rules = [{"id": "bell", "type": "opening_bell"}, {"id": "pre", "type": "pre_open"}]
        self.assertEqual(self.at("2026-10-19 08:55", rules)[0], [])
        rings, st = self.at("2026-10-19 09:02", rules)
        self.assertEqual([r["id"] for r in rings], ["pre"])
        self.assertIn("opens at 09:15", rings[0]["message"])
        self.assertEqual(self.at("2026-10-19 09:14", rules, st)[0], [])
        rings, st = self.at("2026-10-19 09:15", rules, st)
        self.assertEqual([r["id"] for r in rings], ["bell"])
        self.assertIn("Opening bell: NSE is open (2026-10-19 09:15 IST)", rings[0]["message"])
        self.assertEqual(self.at("2026-10-19 11:00", rules, st)[0], [])
        self.assertEqual(self.at("2026-10-20 09:16", rules, st)[0], [])                  # Dussehra
        rings, _ = self.at("2026-10-21 09:20", rules, st)
        self.assertEqual([r["id"] for r in rings], ["bell"])                             # pre-open is over

    def test_the_plan_gives_a_stop_a_target_and_flat_by(self):
        rules = plan_rules()
        self.assertEqual([(r["id"], r["type"]) for r in rules],
                         [("reliance-plan-stop", "stop"), ("reliance-plan-target", "target"),
                          ("reliance-flat-by", "time")])
        self.assertEqual((rules[0]["price"], rules[1]["price"]), (1215.3, 1240.3))
        self.assertEqual((rules[2]["at"], rules[2]["label"]), ("15:10", "flat by"))
        self.assertTrue(all(r["date"] == "2026-10-19" and r["exchange"] == "NSE" for r in rules))
        day = alerts.get_quotes([("RELIANCE", "NSE")], DAY1050)[0]
        rings, st = self.at("2026-10-19 10:50", rules, None, day)        # 1220.50, between stop and target
        self.assertEqual(rings, [])
        day = alerts.get_quotes([("RELIANCE", "NSE")], DAY1205)[0]
        rings, st = self.at("2026-10-19 12:05", rules, st, day)          # the real 12:00 bar went to 1214.10
        self.assertEqual([r["id"] for r in rings], ["reliance-plan-stop"])
        self.assertIn("RELIANCE is ₹1,214.80 (Yahoo, free, can lag): at or below your stop ₹1,215.30 (plan stop: opening-range "
                      "midpoint)", rings[0]["message"])
        day["RELIANCE"]["price"] = 1240.3
        self.assertEqual([r["id"] for r in self.at("2026-10-19 13:00", rules, st, day)[0]], ["reliance-plan-target"])

    def test_flat_by_rings_once_per_trading_day(self):
        rules = plan_rules()[2:]
        self.assertEqual(self.at("2026-10-19 15:09", rules)[0], [])
        rings, st = self.at("2026-10-19 15:10", rules)
        self.assertEqual(len(rings), 1)
        # CAS rules (2026-08-03 on): RELIANCE has F&O, so continuous trading ends 15:15, square-off 15:12
        self.assertIn("Flat by: it is 15:10 IST (set for 15:10), continuous trading in RELIANCE ends at 15:15 "
                      "(closing auction 15:15-15:35) and Zerodha squares off MIS positions at 15:12.",
                      rings[0]["message"])
        self.assertIn("india-broker flatten --symbol RELIANCE", rings[0]["message"])
        self.assertEqual(self.at("2026-10-19 15:20", rules, st)[0], [])
        rings, _ = self.at("2026-10-19 15:40", rules)
        self.assertIn("the market has closed", rings[0]["message"])
        self.assertEqual(self.at("2026-10-21 15:11", rules, st)[0], [])     # plan rules: their day only

    def test_mis_reminder_and_holidays(self):
        rule = [{"id": "mis", "type": "time", "at": "15:20", "label": "MIS auto square-off at 15:25", "mis": True}]
        self.assertEqual(self.at("2026-10-23 15:19", rule)[0], [])
        rings, st = self.at("2026-10-23 15:20", rule)
        self.assertIn("MIS auto square-off at 15:25: it is 15:20 IST", rings[0]["message"])
        self.assertIn("₹50 + GST", rings[0]["message"])
        self.assertEqual(st["rung"]["mis"], "2026-10-23T09:50:00Z")
        self.assertEqual(self.at("2026-10-24 15:21", rule)[0], [])          # Saturday
        self.assertEqual(self.at("2026-10-20 15:21", rule)[0], [])          # Dussehra

    def test_the_example_rules_are_valid(self):
        with open(os.path.join(ROOT, "india-alerts", "alerts.example.json")) as fh:
            rules = json.load(fh)["rules"]
        for r in rules:
            alerts.validate(r)
        self.assertEqual([r for r in rules if r["id"].startswith("reliance-plan") or r["id"] == "reliance-flat-by"],
                         plan_rules())

    def test_bad_rules(self):
        with self.assertRaises(alerts.AlertError):
            alerts.evaluate([{"id": "x", "type": "moon"}], {}, self.open, {}, self.now)
        with self.assertRaises(alerts.AlertError):
            alerts.evaluate([{"id": "x", "type": "above", "symbol": "TCS"}], {}, self.open, {}, self.now)
        with self.assertRaises(alerts.AlertError):
            alerts.evaluate([{"id": "x", "type": "time", "at": "3pm"}], {}, self.open, {}, self.now)
        with self.assertRaises(alerts.AlertError):
            alerts.validate({"id": "x", "type": "above", "symbol": "TCS", "price": 1, "exchange": "NYSE"})
        with self.assertRaises(alerts.AlertError):
            alerts.rules_from_plan({"symbol": "RELIANCE", "stop": 1215.3})

    def test_plan_warns_about_a_late_flat_by(self):
        with open(PLAN) as fh:
            plan = json.load(fh)
        self.assertEqual(alerts.plan_warnings(plan, alerts.rules_from_plan(plan, cas=True), True), [])
        self.assertIn("stricter CAS times", alerts.plan_warnings(plan, alerts.rules_from_plan(plan))[0])
        plan["flat_by"] = "15:12"                                   # at RELIANCE's 15:12 square-off: clamped
        r = alerts.rules_from_plan(plan, cas=True)
        self.assertEqual((r[2]["at"], r[2]["clamped_from"]), ("15:00", "15:12"))
        self.assertIn("moved to 15:00", alerts.plan_warnings(plan, r, True)[0])
        self.assertEqual(alerts.rules_from_plan(plan, cas=False)[2]["at"], "15:12")   # non-CAS: 15:25 square-off
        plan["flat_by"] = "15:25"
        self.assertEqual(alerts.rules_from_plan(plan, cas=False)[2]["at"], "15:10")
        plan["product"] = "CNC"
        self.assertNotIn("clamped_from", alerts.rules_from_plan(plan, cas=False)[2])
        plan["product"] = "MIS"
        for flat in (None, "auto"):
            plan["flat_by"] = flat
            self.assertEqual(alerts.rules_from_plan(plan, cas=True)[2]["at"], "15:00")
            self.assertEqual(alerts.rules_from_plan(plan, cas=False)[2]["at"], "15:10")
        plan["date"] = "2026-07-20"                                 # before CAS started: 15:10 for every stock
        self.assertEqual(alerts.rules_from_plan(plan, cas=True)[2]["at"], "15:10")


class Cli(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.rules = os.path.join(self.d, "alerts.json")
        self.state = os.path.join(self.d, "state", "alerts.json")
        self.prices = os.path.join(self.d, "prices.json")

    def tearDown(self):
        shutil.rmtree(self.d)

    def check(self, now, offline=DAY1050, extra=()):
        return run(["check", "--rules", self.rules, "--state", self.state, "--prices-out", self.prices,
                    "--offline", offline, "--now", now] + list(extra))

    def test_the_reliance_trade_from_the_plan(self):
        code, text, _ = run(["add", "--from-plan", "--plan", PLAN, "--rules", self.rules, "--state", self.state,
                             "--config", os.path.join(self.d, "none"), "--offline", QDIR])
        self.assertEqual(code, 0)
        self.assertIn("Added reliance-flat-by", text)
        self.assertNotIn("warning", text)
        self.assertEqual(run(["add", "--rules", self.rules, "--opening-bell"])[0], 0)

        code, text, _ = self.check("2026-10-19 10:50")
        self.assertEqual(code, 0)                                      # the opening bell rings first
        code, text, _ = self.check("2026-10-19 10:51")
        self.assertEqual(code, 1)
        self.assertIn("NSE OPEN (2026-10-19 10:51 IST, normal session).", text)
        self.assertIn("Nothing hit. 4 rule(s) watched today, RELIANCE ₹1,220.50 (Yahoo, free, can lag).", text)
        with open(self.prices, encoding="utf-8") as fh:
            doc = json.load(fh)
        self.assertEqual(doc["prices"], {"RELIANCE": 1220.5})
        self.assertEqual(doc["detail"]["RELIANCE"]["time"], "2026-10-19T05:20:00Z")
        self.assertEqual(doc["detail"]["RELIANCE"]["fetched"], "2026-10-19T05:21:00Z")
        self.assertEqual((doc["currency"], doc["detail"]["RELIANCE"]["exchange"]), ("INR", "NSE"))

        code, text, _ = self.check("2026-10-19 12:05", DAY1205)
        self.assertEqual(code, 0)
        self.assertIn("ALERT RELIANCE is ₹1,214.80 (Yahoo, free, can lag): at or below your stop ₹1,215.30", text)
        self.assertIn("not an order", text)
        self.assertIn("no bracket orders", text)

        code, text, _ = self.check("2026-10-19 15:10")
        self.assertEqual(code, 0)
        self.assertIn("ALERT Flat by", text)
        self.assertEqual(self.check("2026-10-19 15:12")[0], 1)
        code, text, _ = self.check("2026-10-20 10:00")                 # Dussehra
        self.assertIn("NSE closed", text)
        self.assertIn("Dussehra", text)
        code, text, _ = self.check("2026-10-21 09:16")
        self.assertIn("ALERT Opening bell", text)
        code, text, _ = self.check("2026-10-21 09:20")
        self.assertIn("Nothing hit. 1 rule(s) watched today.", text)

        # a new plan replaces the plan rules and re-arms them
        run(["add", "--from-plan", "--plan", PLAN, "--rules", self.rules, "--state", self.state, "--offline", QDIR])
        with open(self.rules) as fh:
            ids = [r["id"] for r in json.load(fh)["rules"]]
        self.assertEqual(sorted(ids), ["opening-bell", "reliance-flat-by", "reliance-plan-stop",
                                       "reliance-plan-target"])
        with open(self.state) as fh:
            self.assertNotIn("reliance-flat-by", json.load(fh)["rung"])

    def test_a_kite_price_pushed_in(self):
        run(["add", "--from-plan", "--plan", PLAN, "--rules", self.rules, "--state", self.state, "--offline", QDIR])
        code, text, _ = self.check("2026-10-19 10:51", "/nonexistent", ["--price", "RELIANCE=1240.3"])
        self.assertEqual(code, 0)
        self.assertIn("ALERT RELIANCE is ₹1,240.30 (Kite, live): at or above your target", text)
        with open(self.prices) as fh:
            self.assertEqual(json.load(fh)["detail"]["RELIANCE"]["source"], "kite")
        # Claude writes Kite's get_ltp answer to a file instead
        k = os.path.join(self.d, "ltp.json")
        with open(k, "w") as fh:
            json.dump({"NSE:RELIANCE": {"instrument_token": 738561, "last_price": 1214.0}}, fh)
        code, text, _ = self.check("2026-10-19 10:52", "/nonexistent", ["--kite-json", k])
        self.assertIn("ALERT RELIANCE is ₹1,214.00 (Kite, live): at or below your stop", text)
        # a pushed price in prices.json is used while it is fresh, Yahoo after that
        code, text, _ = self.check("2026-10-19 10:54", "/nonexistent")
        self.assertIn("Nothing hit", text)
        self.assertNotIn("error", text)
        code, text, _ = self.check("2026-10-19 11:05", "/nonexistent")
        self.assertIn("error: no offline quote file", text)

    def test_a_circuit_alert_from_the_cli(self):
        run(["add", "--rules", self.rules, "--symbol", "RPOWER", "--circuit"])
        run(["add", "--rules", self.rules, "--symbol", "RELIANCE", "--circuit"])
        code, text, _ = self.check("2026-10-23 11:32", QDIR, ["--price", "RPOWER=20.31"])
        self.assertEqual(code, 0)
        self.assertIn("ALERT RPOWER ₹20.31 (Kite, live) is 0.74% from its upper circuit", text)
        self.assertIn("RELIANCE has no fixed price band", text)

    def test_add_time_mis_target_and_bse(self):
        code, _, _ = run(["add", "--rules", self.rules, "--at", "15:10", "--label", "flat by", "--symbol", "reliance"])
        self.assertEqual(code, 0)
        run(["add", "--rules", self.rules, "--symbol", "RELIANCE", "--target", "1240.30", "--date", "2026-10-19"])
        run(["add", "--rules", self.rules, "--mis-reminder"])
        run(["add", "--rules", self.rules, "--pre-open"])
        run(["add", "--rules", self.rules, "--symbol", "RELIANCE.BO", "--above", "1300"])
        with open(self.rules) as fh:
            rules = json.load(fh)["rules"]
        self.assertEqual(rules[0], {"id": "time-1510", "type": "time", "at": "15:10", "label": "flat by",
                                    "symbol": "RELIANCE", "exchange": "NSE"})
        self.assertEqual(rules[1], {"id": "reliance-target-1240.3", "symbol": "RELIANCE", "exchange": "NSE",
                                    "type": "target", "price": 1240.3, "date": "2026-10-19"})
        self.assertEqual(rules[2], {"id": "mis-squareoff", "type": "time", "at": "15:09",      # strict: no symbol
                                    "label": "MIS auto square-off at 15:12", "mis": True, "close": "15:15",
                                    "square_off": "15:12"})
        self.assertEqual(rules[3], {"id": "pre-open", "type": "pre_open"})
        self.assertEqual(rules[4]["exchange"], "BSE")
        self.assertEqual(run(["add", "--rules", self.rules, "--at", "3pm"])[0], 1)
        code, _, err = run(["add", "--rules", self.rules, "--symbol", "RELIANCE", "--target", "1240.3"])
        self.assertIn("already exists", err)
        code, _, err = run(["add", "--rules", self.rules, "--from-plan", "--plan", os.path.join(self.d, "none")])
        self.assertEqual(code, 1)
        self.assertIn("no plan", err)

    def test_list_and_reset(self):
        run(["add", "--rules", self.rules, "--opening-bell"])
        self.check("2026-10-21 09:16")
        code, text, _ = run(["list", "--rules", self.rules, "--state", self.state])
        self.assertIn("rang", text)
        self.assertEqual(run(["reset", "opening-bell", "--state", self.state])[0], 0)
        self.assertEqual(run(["reset", "opening-bell", "--state", self.state])[0], 1)

    def test_quote_write_merges(self):
        code, text, _ = run(["quote", "RELIANCE", "TCS", "--offline", QDIR, "--write", self.prices,
                             "--now", "2026-10-23T06:02:25Z"])
        self.assertEqual(code, 0)
        self.assertIn("RELIANCE   NSE ₹1,170.50 (-1.18% vs prev close ₹1,184.50)", text)
        self.assertIn("delayed", text)
        run(["quote", "M&M", "--offline", QDIR, "--write", self.prices])
        with open(self.prices) as fh:
            doc = json.load(fh)
        self.assertEqual(sorted(doc["prices"]), ["M&M", "RELIANCE", "TCS"])
        self.assertEqual(doc["detail"]["TCS"]["prev_close"], 2506.9)
        self.assertEqual(doc["detail"]["TCS"]["fetched"], "2026-10-23T06:02:25Z")

    def test_quote_holdings_from_portfolio_and_plan(self):
        pf = os.path.join(self.d, "portfolio.json")
        with open(pf, "w") as fh:
            json.dump({"cash": 0, "positions": [{"symbol": "TCS", "exchange": "NSE", "shares": 1}]}, fh)
        code, text, _ = run(["quote", "--holdings", "--portfolio", pf, "--plan", PLAN, "--offline", QDIR, "--json"])
        self.assertEqual(code, 0)
        self.assertEqual(sorted(json.loads(text)["quotes"]), ["RELIANCE", "TCS"])

    def test_clock_and_holidays(self):
        self.assertEqual(run(["clock", "--offline", QDIR, "--now", "2026-10-19 10:15"])[0], 0)
        code, text, _ = run(["clock", "--offline", QDIR, "--now", "2026-12-25T10:00"])
        self.assertEqual(code, 1)
        self.assertIn("Christmas", text)
        self.assertIn("Next open: 2026-12-28 09:15 IST.", text)
        code, text, _ = run(["clock", "--offline", QDIR, "--now", "2026-11-08 18:00"])
        self.assertIn("Muhurat trading today", text)
        code, text, _ = run(["clock", "--offline", QDIR, "--now", "2027-02-01 10:00"])
        self.assertIn("has not published a 2027 holiday list", text)
        code, text, _ = run(["holidays", "--offline", QDIR, "--now", "2026-10-23 11:00"])
        self.assertEqual(code, 0)
        self.assertIn("2026-10-20 Tue  Dussehra", text)
        self.assertIn("Muhurat", text)
        self.assertEqual(run(["holidays", "--offline", QDIR, "--year", "2027"])[0], 1)

    def test_check_without_rules(self):
        code, _, err = run(["check", "--rules", self.rules, "--offline", QDIR])
        self.assertEqual(code, 1)
        self.assertIn("no rules", err)


SHARED = os.path.join(FIX, "shared")


class Cas(unittest.TestCase):
    def test_session_rules(self):
        d = datetime.date(2026, 10, 19)
        self.assertEqual(alerts.session_rules(True, d)["close"], "15:15")
        self.assertEqual(alerts.session_rules(None, d)["square_off"], "15:12")        # unknown: strict
        non = alerts.session_rules(False, d)
        self.assertEqual((non["close"], non["square_off"], non["flat_by_default"], non["bars_5m"]),
                         ("15:30", "15:25", "15:10", 75))
        self.assertEqual(alerts.session_rules(True, datetime.date(2026, 8, 3))["bars_5m"], 72)
        self.assertEqual(alerts.session_rules(True, datetime.date(2026, 7, 31))["close"], "15:30")

    def test_fo_list(self):
        cas, src = alerts.load_cas_list(offline=QDIR)
        self.assertIn("RELIANCE", cas)
        self.assertIn("M&M", cas)
        self.assertNotIn("RPOWER", cas)
        self.assertFalse({"NIFTY", "BANKNIFTY", "FINNIFTY", "NIFTYNXT50"} & cas)
        self.assertEqual(alerts.load_cas_list(offline=os.path.join(QDIR, "day-2026-10-19-1050")), (None, ""))
        self.assertIs(alerts.cas_status("RPOWER", cas), False)
        self.assertIs(alerts.cas_status("RPOWER", cas, {"cas_symbols_extra": ["rpower"]}), True)
        self.assertIs(alerts.cas_status("RELIANCE", cas, {"non_cas_symbols": ["RELIANCE"]}), False)
        self.assertIs(alerts.cas_status("RELIANCE", None), None)
        self.assertIs(alerts.cas_status("RELIANCE", None, override="no"), False)
        d = tempfile.mkdtemp()
        try:                                                    # the fo_list.txt cache
            cache = os.path.join(d, "fo_list.txt")
            with open(cache, "w") as fh:
                fh.write("# fetched 2026-10-23\nRELIANCE\nTCS\n")
            got, src = alerts.load_cas_list(None, cache, ist("2026-10-25 10:00"))
            self.assertEqual((got, src), ({"RELIANCE", "TCS"}, "NSE F&O list (cached 2026-10-23)"))
        finally:
            shutil.rmtree(d)

    def test_pre_open_and_closing_auction(self):
        self.assertIn("market and limit", status("2026-10-19 09:04")["reason"])
        self.assertIn("limit orders only", status("2026-10-19 09:06")["reason"])
        self.assertIn("matching (09:10-09:12)", status("2026-10-19 09:11")["reason"])
        self.assertIn("buffer", status("2026-10-19 09:13")["reason"])
        self.assertIn("09:00-09:07", status("2026-09-04 09:04")["reason"])          # before 2026-09-07
        whole = status("2026-10-19 15:20")
        self.assertTrue(whole["open"])                                              # non-CAS still trade
        self.assertIn("closing auction", whole["reason"])
        rel = alerts.market_status(ist("2026-10-19 15:20"), cal(), True)
        self.assertEqual((rel["open"], rel["phase"], rel["closes"]), (False, "closing_auction", "15:15"))
        self.assertTrue(alerts.market_status(ist("2026-10-19 15:20"), cal(), False)["open"])
        self.assertTrue(alerts.market_status(ist("2026-07-20 15:20"), cal(), True)["open"])   # before CAS

    def test_clock_shows_both_closes(self):
        code, text, _ = run(["clock", "--offline", QDIR, "--now", "2026-10-19 15:20", "--symbol", "RELIANCE"])
        self.assertEqual(code, 1)                                   # RELIANCE is in the auction
        self.assertIn("CAS stocks (with F&O contracts) trade continuously to 15:15", text)
        self.assertIn("non-CAS stocks trade to 15:30", text)
        self.assertIn("09:05-09:10 limit orders only, matching 09:10-09:12", text)
        self.assertIn("15:12 for CAS stocks, 15:25 for non-CAS", text)
        self.assertIn("RELIANCE: CAS stock (F&O)", text)
        code, text, _ = run(["clock", "--offline", QDIR, "--now", "2026-10-19 15:20", "--symbol", "RPOWER"])
        self.assertEqual(code, 0)
        self.assertIn("RPOWER: non-CAS stock", text)
        code, text, _ = run(["clock", "--offline", QDIR, "--now", "2026-10-19 15:20", "--symbol", "RPOWER",
                             "--cas", "yes"])
        self.assertEqual(code, 1)

    def test_mis_reminder_three_minutes_before_the_square_off(self):
        d = tempfile.mkdtemp()
        try:
            rules = os.path.join(d, "alerts.json")
            for sym in ("RELIANCE", "RPOWER"):
                run(["add", "--rules", rules, "--mis-reminder", "--symbol", sym, "--offline", QDIR,
                     "--date", "2026-10-23"])
            with open(rules) as fh:
                got = {r["symbol"]: (r["at"], r["square_off"]) for r in json.load(fh)["rules"]}
            self.assertEqual(got, {"RELIANCE": ("15:09", "15:12"), "RPOWER": ("15:22", "15:25")})
            with open(rules) as fh:
                rel = json.load(fh)["rules"][0]
            rings, _ = alerts.evaluate([rel], {}, status("2026-10-23 15:09"), {}, ist("2026-10-23 15:09"))
            self.assertIn("MIS auto square-off at 15:12: it is 15:09 IST", rings[0]["message"])
            self.assertIn("open MIS (intraday) positions at 15:12", rings[0]["message"])
        finally:
            shutil.rmtree(d)

    def test_from_plan_uses_the_cas_status(self):
        d = tempfile.mkdtemp()
        try:
            with open(PLAN) as fh:
                plan = json.load(fh)
            plan["flat_by"] = "auto"
            pp, rules = os.path.join(d, "plan.json"), os.path.join(d, "alerts.json")
            with open(pp, "w") as fh:
                json.dump(plan, fh)
            code, text, _ = run(["add", "--from-plan", "--plan", pp, "--rules", rules, "--offline", QDIR])
            with open(rules) as fh:
                self.assertEqual(json.load(fh)["rules"][2]["at"], "15:00")
            run(["add", "--from-plan", "--plan", pp, "--rules", rules, "--offline", QDIR, "--cas", "no"])
            with open(rules) as fh:
                self.assertEqual(json.load(fh)["rules"][2]["at"], "15:10")
        finally:
            shutil.rmtree(d)


class Kite(unittest.TestCase):
    def test_verbatim_ltp_file(self):
        with open(os.path.join(SHARED, "kite-ltp-2026-10-23.json")) as fh:
            k = alerts.parse_kite(json.load(fh), ist("2026-10-23 15:00"))
        self.assertEqual(sorted(k), ["RELIANCE", "TCS"])
        self.assertEqual((k["RELIANCE"]["price"], k["RELIANCE"]["exchange"], k["RELIANCE"]["source"]),
                         (1190.0, "NSE", "kite"))                 # NSE wins over BSE:RELIANCE
        self.assertEqual(k["TCS"]["price"], 2530.0)

    def test_get_quotes_shape(self):
        quotes = {"NSE:RELIANCE": {"instrument_token": 738561, "timestamp": "2026-10-23 15:29:59",
                                   "last_trade_time": "2026-10-23 15:29:58", "last_price": 1190,
                                   "volume": 9123456, "average_price": 1189.4, "net_change": 0,
                                   "ohlc": {"open": 1184, "high": 1201.9, "low": 1167.1, "close": 1184.5},
                                   "lower_circuit_limit": 1066.1, "upper_circuit_limit": 1302.9,
                                   "depth": {"buy": [], "sell": []}}}
        k = alerts.parse_kite(quotes, ist("2026-10-23 15:30"))
        self.assertEqual((k["RELIANCE"]["price"], k["RELIANCE"]["prev_close"]), (1190.0, 1184.5))
        self.assertEqual(k["RELIANCE"]["time"], "2026-10-23 15:29:58")

    def test_instrument_circuit_limits(self):
        path = os.path.join(SHARED, "kite-instruments-RELIANCE-2026-10-23.json")
        with open(path) as fh:
            inst = alerts.parse_kite_instrument(json.load(fh), path)
        self.assertEqual(inst["RELIANCE"]["lower"], 1066.1)
        self.assertEqual(inst["RELIANCE"]["upper"], 1302.9)
        self.assertEqual((inst["RELIANCE"]["tick"], inst["RELIANCE"]["date"]), (0.1, "2026-10-23"))
        d = tempfile.mkdtemp()
        try:
            rules = os.path.join(d, "alerts.json")
            run(["add", "--rules", rules, "--symbol", "RELIANCE", "--circuit"])
            base = ["check", "--rules", rules, "--state", os.path.join(d, "s.json"), "--prices-out",
                    os.path.join(d, "p.json"), "--offline", QDIR, "--kite-instrument", path, "--no-write"]
            code, text, _ = run(base + ["--now", "2026-10-23 14:00", "--price", "RELIANCE=1295"])
            self.assertEqual(code, 0)                   # sec_list says No Band; Kite's limits win
            self.assertIn("upper circuit \u20b91,302.90 (Kite's circuit limits for today)", text)
            self.assertNotIn("No Band", text)
            code, text, _ = run(base + ["--now", "2026-10-23 14:00"])        # Yahoo 1170.50: not near
            self.assertEqual(code, 1)
            code, text, _ = run(base + ["--now", "2026-10-26 10:00", "--price", "RELIANCE=1295"])
            self.assertEqual(code, 1)                   # another day: the file's limits are stale
            self.assertIn("are from 2026-10-23, not today (2026-10-26)", text)
            self.assertIn("No Band", text)
            k = os.path.join(SHARED, "kite-ltp-2026-10-23.json")
            code, text, _ = run(base + ["--now", "2026-10-23 15:00", "--kite-ltp", k])
            self.assertIn("RELIANCE \u20b91,190.00 (Kite, live)", text)
        finally:
            shutil.rmtree(d)


if __name__ == "__main__":
    unittest.main()
