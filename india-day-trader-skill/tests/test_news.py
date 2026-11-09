import datetime
import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout

from _load import FIX, load

news = load("india-news", "news")
DIR = os.path.join(FIX, "india-news")
NOW = "2026-10-23T11:30:00+05:30"  # Friday, during the session; fixtures fetched at 11:28 IST


def raw(name):
    with open(os.path.join(DIR, name), "rb") as fh:
        return fh.read()


def run(args):
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = news.main(args)
    return code, out.getvalue(), err.getvalue()


class Feeds(unittest.TestCase):
    def test_nse_announcements(self):
        items = news.parse_nse_announcements(raw("nse-ann-RELIANCE.json"), "RELIANCE")
        self.assertEqual(len(items), 7)
        rating = items[1]
        self.assertEqual(rating["subject"], "Credit Rating")
        self.assertEqual(rating["company"], "Reliance Industries")
        self.assertEqual(rating["id"], "nse:106823937")
        self.assertTrue(rating["important"])
        self.assertEqual(rating["ann_tags"], ["rating"])
        self.assertTrue(rating["link"].startswith("https://nsearchives.nseindia.com/corporate/"))
        # 21-Oct-2026 20:43:30 is India time: 15:13:30 UTC
        self.assertEqual(rating["time"], datetime.datetime(2026, 10, 21, 15, 13, 30,
                                                           tzinfo=datetime.timezone.utc))
        papers = [i for i in items if i["subject"] == "Copy of Newspaper Publication"]
        self.assertTrue(papers and all(i["routine"] and not i["important"] for i in papers))

    def test_announcement_tags(self):
        t = news.ann_tags
        self.assertIn("results", t("Outcome of Board Meeting",
                                   "HDFC Bank Limited has submitted to the Exchange, the financial results"))
        self.assertIn("board meeting", t("Outcome of Board Meeting", ""))
        self.assertIn("dividend/bonus/split", t("Record Date", "Record date for the purpose of Dividend"))
        self.assertIn("deal", t("Acquisition", "Update on acquisition of 100% equity stake"))
        self.assertIn("management", t("Change in Director(s)", "Resignation of Chief Financial Officer"))
        self.assertIn("regulatory", t("Action(s) taken or orders passed", "Disclosure under Regulation 30"))
        self.assertIn("clarification", t("News Verification", "The Exchange has sought clarification"))

    def test_google_news(self):
        items = news.parse_gnews(raw("gnews-HDFCBANK.xml"), "HDFCBANK")
        self.assertEqual(len(items), 25)
        fall = [i for i in items if i["title"].startswith("HDFC Bank Shares Fall 2.3%")][0]
        self.assertEqual(fall["publisher"], "Goodreturns")
        self.assertFalse(fall["title"].endswith("Goodreturns"))
        self.assertTrue(fall["link"].startswith("https://news.google.com/"))

    def test_public_rss_matches_the_company_name_only(self):
        items = news.parse_nse_rss(raw("nse-rss.xml"), {"CONTROLPR": "Control Print Limited",
                                                       "RELIANCE": "Reliance Industries Limited"})
        self.assertEqual({i["ticker"] for i in items}, {"CONTROLPR"})  # not "Reliance MF - ..."
        self.assertEqual(items[0]["subject"], "Analysts/Institutional Investor Meet/Con. Call Updates")
        self.assertEqual(news.to_ist(items[0]["time"]).strftime("%H:%M"), "11:22")

    def test_times_are_india_time(self):
        self.assertEqual(news.parse_time("21-Oct-2026 22:44:39").hour, 17)  # IST -> UTC
        self.assertEqual(news.parse_time("Fri, 23 Oct 2026 05:15:48 GMT").hour, 5)
        self.assertEqual(news.to_ist(news.parse_time("2026-10-23T05:15:48Z")).strftime("%H:%M"), "10:45")
        self.assertIsNone(news.parse_time("soon"))

    def test_symbols(self):
        self.assertEqual(news.parse_symbol("reliance"), ("RELIANCE", "NSE"))
        self.assertEqual(news.parse_symbol("RELIANCE.BO"), ("RELIANCE", "BSE"))
        self.assertEqual(news.parse_symbol("NSE:M&M"), ("M&M", "NSE"))
        self.assertEqual(news.parse_symbol("500325"), ("500325", "BSE"))
        self.assertEqual(news.yahoo_symbol("M&M"), "M%26M.NS")
        self.assertEqual(news.yahoo_symbol("BAJAJ-AUTO", "BSE"), "BAJAJ-AUTO.BO")

    def test_indian_digit_grouping(self):
        self.assertEqual(news.inr(1169.9), "1,169.90")
        self.assertEqual(news.inr(12345678.5), "1,23,45,678.50")


class Tagging(unittest.TestCase):
    def test_touches_and_tags(self):
        items = news.parse_gnews(raw("gnews-HDFCBANK.xml"), "HDFCBANK")
        watch = {"HDFCBANK": {"name": "HDFC Bank Limited"}, "ICICIBANK": {"name": "ICICI Bank Limited"}}
        news.tag(items, watch)
        vs = [i for i in items if i["title"].startswith("HDFC Bank vs ICICI Bank")][0]
        self.assertEqual(vs["touches"], ["HDFCBANK", "ICICIBANK"])
        self.assertIn("analyst", vs["tags"])  # Bernstein
        fall = [i for i in items if i["title"].startswith("HDFC Bank Shares Fall 2.3%")][0]
        self.assertEqual(fall["tags"][:2], ["results", "analyst"])
        probe = [i for i in items if "probe reveals data leak" in i["title"]][0]
        self.assertIn("regulatory", probe["tags"])
        self.assertIn("risk", probe["tags"])

    def test_india_tags(self):
        items = [{"id": "x", "source": "Google News", "kind": "headline", "ticker": "TCS",
                  "title": "RBI keeps repo rate unchanged; TCS hits upper circuit after block deal",
                  "summary": "", "link": "", "time": None}]
        tags = news.tag(items, {"TCS": {}})[0]["tags"]
        for t in ("macro", "circuit", "block/bulk deal"):
            self.assertIn(t, tags)

    def test_dedupe_merges_the_same_story(self):
        a = news.parse_gnews(raw("gnews-RELIANCE.xml"), "RELIANCE")
        b = news.parse_gnews(raw("gnews-RELIANCE.xml"), "JIOFIN")
        items = news.dedupe(news.tag(a + b, {"RELIANCE": {}, "JIOFIN": {}}))
        self.assertEqual(len(items), 25)
        self.assertEqual(items[0]["touches"], ["JIOFIN", "RELIANCE"])


class Check(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.state = os.path.join(self.tmp.name, "state", "news.json")
        none = os.path.join(self.tmp.name, "none.json")
        self.base = ["--offline", DIR, "--state", self.state, "--now", NOW,
                     "--portfolio", none, "--watchlist", none, "--plan", none]

    def tearDown(self):
        self.tmp.cleanup()

    def test_new_items_once_newest_first(self):
        code, out, err = run(["--symbols", "RELIANCE", "HDFCBANK", "--hours", "48", "--json"] + self.base)
        self.assertEqual(code, 0, err)
        data = json.loads(out)
        times = [i["time"] for i in data["items"]]
        self.assertEqual(times, sorted(times, reverse=True))
        filings = [i for i in data["items"] if i["kind"] == "filing"]
        self.assertEqual(len(filings), 4)  # newspaper copies are routine and skipped
        self.assertGreater(len(data["items"]), 30)
        code, out, _ = run(["--symbols", "RELIANCE", "HDFCBANK", "--hours", "48"] + self.base)
        self.assertEqual(code, 1)
        self.assertIn("Nothing new.", out)

    def test_the_rating_filing_and_the_price_lines(self):
        code, out, err = run(["--symbols", "RELIANCE", "HDFCBANK", "--hours", "48",
                              "--no-headlines", "--dry-run"] + self.base)
        self.assertEqual(code, 0, err)
        self.assertIn("! 2026-10-21 20:43 IST  RELIANCE   NSE            Reliance Industries: Credit Rating", out)
        self.assertIn("RELIANCE   ₹1,169.90 -1.23% vs yesterday's close ₹1,184.50 at 11:31 IST", out)
        self.assertIn("HDFCBANK   ₹690.05 -0.58% vs yesterday's close ₹694.05", out)
        self.assertNotIn("MOVING", out)
        self.assertIn("Checked 2026-10-23 11:30 IST", out)
        self.assertIn("Not financial advice", out)
        self.assertNotIn(chr(0x2014), out)
        self.assertFalse(os.path.exists(self.state))

    def test_routine_filings_on_request(self):
        code, out, _ = run(["--symbols", "RELIANCE", "--hours", "240", "--no-headlines",
                            "--no-moves", "--dry-run", "--routine"] + self.base)
        self.assertIn("Copy of Newspaper Publication", out)
        code, out, _ = run(["--symbols", "RELIANCE", "--hours", "240", "--no-headlines",
                            "--no-moves", "--dry-run"] + self.base)
        self.assertNotIn("Copy of Newspaper Publication", out)
        self.assertIn("Proposed Initial Public Offer of Jio Platforms", out)

    def test_falls_back_to_the_public_rss(self):
        wl = os.path.join(self.tmp.name, "watchlist.json")
        with open(wl, "w") as fh:
            json.dump({"created_by": "india-scanner",
                       "hits": [{"symbol": "CONTROLPR", "exchange": "NSE",
                                 "name": "Control Print Limited"}]}, fh)
        args = list(self.base)
        args[args.index("--watchlist") + 1] = wl
        code, out, err = run(["--no-headlines", "--no-moves", "--dry-run"] + args)
        self.assertEqual(code, 0, err)
        self.assertIn("CONTROLPR  NSE RSS", out)
        self.assertIn("read from the public RSS", out)

    def test_symbols_from_watchlist_and_plan(self):
        wl = os.path.join(self.tmp.name, "watchlist.json")
        with open(wl, "w") as fh:
            json.dump({"created_by": "india-scanner",
                       "hits": [{"symbol": "HDFCBANK", "exchange": "NSE", "setups": ["orb"]}]}, fh)
        plan = os.path.join(self.tmp.name, "plan.json")
        with open(plan, "w") as fh:
            json.dump({"created_by": "india-exit", "symbol": "RELIANCE", "exchange": "BSE"}, fh)
        w = news.watch_from_files("/nonexistent.json", wl, plan)
        self.assertEqual(sorted(w), ["HDFCBANK", "RELIANCE"])
        self.assertEqual(w["RELIANCE"]["exchange"], "BSE")
        args = list(self.base)
        args[args.index("--watchlist") + 1] = wl
        code, out, err = run(["--json", "--dry-run", "--hours", "24"] + args)
        self.assertEqual(code, 0, err)
        symbols = {t for i in json.loads(out)["items"] for t in i["touches"]}
        self.assertIn("HDFCBANK", symbols)
        self.assertNotIn("RELIANCE", symbols)

    def test_no_symbols_is_an_error(self):
        code, _, err = run(self.base)
        self.assertEqual(code, 2)
        self.assertIn("no symbols", err)

    def test_moves(self):
        m = news.parse_chart(raw("chart-RELIANCE.json"), "RELIANCE")
        self.assertEqual(m["prev_close"], 1184.5)
        self.assertEqual(m["pct"], -1.23)
        self.assertEqual(m["name"], "Reliance Industries Limited")
        quiet = dict(m, pct=1.2)
        self.assertNotIn("MOVING", news.render_moves([quiet])[0])
        self.assertIn("MOVING", news.render_moves([quiet], move_pct=1.0)[0])
        with self.assertRaises(news.NewsError):
            news.parse_chart(b'{"chart": {"result": null}}', "ZZZZ")
        code, out, _ = run(["--symbols", "TCS", "--dry-run"] + self.base)  # no files offline
        self.assertNotIn("vs yesterday's close", out)
        self.assertNotIn("note: price", out)

    def test_nse_block_is_explained(self):
        with self.assertRaises(news.NewsError) as ctx:
            news.parse_nse_announcements(b"<HTML><TITLE>Access Denied</TITLE></HTML>", "RELIANCE")
        self.assertIn("NSE", str(ctx.exception))

    def test_state_forgets_after_30_days(self):
        state = {"seen": {"old": "2026-08-01T00:00:00+00:00"}}
        now = news.parse_time(NOW)
        news.remember([], state, now)
        self.assertNotIn("old", state["seen"])
        self.assertEqual(state["last_check"], now.isoformat())


if __name__ == "__main__":
    unittest.main()
