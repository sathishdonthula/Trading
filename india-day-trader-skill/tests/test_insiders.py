import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout

from _load import FIX, load

ins = load("india-insiders", "insiders")
DIR = os.path.join(FIX, "india-insiders")
PIT = os.path.join(DIR, "pit-RELIANCE.json")


def fixture(name):
    with open(os.path.join(DIR, name), encoding="utf-8") as fh:
        return fh.read()


def run(args):
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = ins.main(args)
    return code, out.getvalue(), err.getvalue()


class Basics(unittest.TestCase):
    def test_dates(self):
        self.assertEqual(ins.parse_date("13-Feb-2026"), "2026-02-13")
        self.assertEqual(ins.parse_date("22-OCT-2026"), "2026-10-22")
        self.assertEqual(ins.parse_date("18-Feb-2026 19:06"), "2026-02-18")
        self.assertEqual(ins.parse_date("2026-10-22"), "2026-10-22")
        self.assertIsNone(ins.parse_date("Nil"))
        self.assertIsNone(ins.parse_date("31-Feb-2026"))
        self.assertEqual(ins.nse_date("2026-10-02"), "02-10-2026")

    def test_trading_days_skip_weekends_and_holidays(self):
        self.assertEqual(ins.trading_days_between("2026-02-13", "2026-02-18"), 3)  # Fri -> Wed
        self.assertEqual(ins.trading_days_between("2026-10-19", "2026-10-21"), 1)  # 20th Diwali
        self.assertEqual(ins.trading_days_between("2026-10-22", "2026-10-22"), 0)

    def test_indian_money(self):
        self.assertEqual(ins.group_in(12345678), "1,23,45,678")
        self.assertEqual(ins.group_in(999), "999")
        self.assertEqual(ins.rupees(502259191.21), "₹50,22,59,191 (₹50.23 Cr)")
        self.assertEqual(ins.rupees(3294168), "₹32,94,168 (₹32.94 lakh)")
        self.assertEqual(ins.rupees(5000), "₹5,000")

    def test_urls(self):
        self.assertEqual(ins.pit_url("reliance"),
                         "https://www.nseindia.com/api/corporates-pit?index=equities&symbol=RELIANCE")
        self.assertIn("from_date=01-01-2026&to_date=23-10-2026",
                      ins.pit_url("RELIANCE", "2026-01-01", "2026-10-23"))
        self.assertEqual(
            ins.hist_url("bulk", "2026-10-22", "2026-10-22", "RAVITA"),
            "https://www.nseindia.com/api/historicalOR/bulk-block-short-deals?optionType=bulk_deals"
            "&from=22-10-2026&to=22-10-2026&symbol=RAVITA")
        self.assertEqual(ins.weekdays("2026-10-16", "2026-10-23"),
                         ["2026-10-16", "2026-10-19", "2026-10-21", "2026-10-22", "2026-10-23"])


class Pit(unittest.TestCase):
    def setUp(self):
        self.rows = ins.parse_pit(json.loads(fixture("pit-RELIANCE.json")))

    def test_clean_rows_newest_first(self):
        self.assertEqual(len(self.rows), 11)
        top = self.rows[0]
        self.assertEqual(top["disclosed"], "2026-02-18")
        sell = next(r for r in self.rows if r["id"] == "pit:563850")
        self.assertEqual(sell["who"], "BALANADU NARAYAN")
        self.assertEqual(sell["symbol"], "RELIANCE")
        self.assertEqual(sell["exchange"], "NSE")
        self.assertEqual((sell["action"], sell["mode"]), ("sell", "off-market"))
        self.assertEqual((sell["shares"], sell["value_inr"]), (2320, 3294168))
        self.assertEqual((sell["before_shares"], sell["after_shares"]), (3920, 1600))
        self.assertEqual((sell["trade_to"], sell["told_company"], sell["disclosed"]),
                         ("2026-02-13", "2026-02-16", "2026-02-18"))
        self.assertEqual((sell["lag_days"], sell["lag_trading_days"]), (5, 3))
        self.assertFalse(sell["late"])
        self.assertAlmostEqual(sell["avg_price"], 1419.9)

    def test_market_sale_over_three_days(self):
        r = next(r for r in self.rows if r["id"] == "pit:551138")
        self.assertEqual((r["who"], r["category"]), ("Shobha Agarwal", "Immediate relative"))
        self.assertEqual((r["mode"], r["traded_on"]), ("market", "NSE"))
        self.assertEqual((r["trade_from"], r["trade_to"]), ("2025-09-09", "2025-09-11"))
        self.assertEqual(r["value_inr"], 138500000)
        self.assertEqual((r["before_pct"], r["after_pct"]), (0.01, 0.01))

    def test_pledges_gifts_and_promoter(self):
        by = {r["id"]: r for r in self.rows}
        self.assertEqual(by["pit:561508"]["action"], "pledge revoked")
        self.assertEqual(by["pit:471170"]["action"], "pledge created")  # NSE type says "Pledge Invoke"
        gift = by["pit:489950"]
        self.assertEqual((gift["action"], gift["mode"], gift["value_inr"]), ("sell", "gift", None))
        prom = by["pit:475186"]
        self.assertEqual(prom["category"], "Promoter Group")
        self.assertEqual((prom["shares"], prom["before_shares"], prom["after_pct"]),
                         (240942006, None, 3.56))

    def test_late_flag(self):
        r = next(r for r in self.rows if r["id"] == "pit:562998")  # traded 2 Feb, on NSE 9 Feb
        self.assertEqual(r["lag_trading_days"], 5)
        self.assertTrue(r["late"])

    def test_filters(self):
        self.assertEqual([r["id"] for r in ins.select_pit(self.rows, category="promoter")],
                         ["pit:475186"])
        self.assertEqual(len(ins.select_pit(self.rows, category="relative")), 7)
        self.assertEqual(len(ins.select_pit(self.rows, category="kmp")), 0)
        self.assertEqual(len(ins.select_pit(self.rows, action="pledge")), 2)
        self.assertEqual(len(ins.select_pit(self.rows, since="2026-01-01")), 4)
        self.assertEqual(len(ins.select_pit(self.rows, who="shobha agrawal")), 3)  # spelling

    def test_render_says_public_and_flags_holdings(self):
        out = ins.render_pit(self.rows[:3], watch={"RELIANCE"})
        self.assertIn("you hold RELIANCE", out)
        self.assertIn("Not inside information", out)
        self.assertIn("not a reason to buy", out)
        self.assertIn("₹32,94,168", out)
        self.assertIn("5 days, 3 trading days later", out)
        self.assertNotIn(chr(0x2014), out)

    def test_cli(self):
        code, out, _ = run(["pit", "RELIANCE", "--offline", PIT, "--category", "promoter",
                            "--portfolio", "/nonexistent"])
        self.assertEqual(code, 0)
        self.assertIn("Reliance Industrial Investments and Holdings Limited (promoter group)", out)
        code, out, _ = run(["pit", "RELIANCE", "--offline", PIT, "--json", "--limit", "0"])
        self.assertEqual(len(json.loads(out)), 11)
        code, _, _ = run(["pit", "RELIANCE", "--offline", PIT, "--category", "director"])
        self.assertEqual(code, 1)
        with self.assertRaises(ins.InsidersError):
            ins.parse_pit({"oops": 1})


class Deals(unittest.TestCase):
    def setUp(self):
        self.bulk = ins.parse_deals_csv(fixture("bulk.csv"), "bulk")
        self.block = ins.parse_deals_csv(fixture("block.csv"), "block")

    def test_csv_rows(self):
        self.assertEqual((len(self.bulk), len(self.block)), (24, 4))
        lt = next(r for r in self.block if r["client"] == "LARSEN & TOUBRO LIMITED")
        self.assertEqual((lt["date"], lt["symbol"], lt["action"], lt["shares"], lt["price"]),
                         ("2026-10-22", "CAPINVIT", "buy", 6644519, 75.59))
        self.assertAlmostEqual(lt["value_inr"], 502259191.21)
        self.assertEqual(lt["exchange"], "NSE")
        with self.assertRaises(ins.InsidersError):
            ins.parse_deals_csv("a,b\n1,2\n", "bulk")

    def test_history_json_is_the_same_shape(self):
        hist = ins.parse_deals_hist(json.loads(fixture("hist-bulk-RAVITA-2026-10-22.json")), "bulk")
        csv_rows = [r for r in self.bulk if r["symbol"] == "RAVITA"]
        self.assertEqual(sorted(r["id"] for r in hist), sorted(r["id"] for r in csv_rows))
        blocks = ins.parse_deals_hist(json.loads(fixture("hist-block-2026-10-16_23.json")), "block")
        self.assertEqual(len(blocks), 14)
        self.assertIn("PI OPPORTUNITIES AIF V LLP", [r["client"] for r in blocks])

    def test_fuzzy_investor_names(self):
        self.assertTrue(ins.name_match("ASHISH RAMESHCHANDRA KACHOLIA", "kacholia"))
        self.assertTrue(ins.name_match("ASHISH KACHOLIA", "Ashish Kacholia"))
        self.assertTrue(ins.name_match("MUKUL MAHAVIR AGRAWAL", "mukul agarwal"))
        self.assertTrue(ins.name_match("REKHA RAKESH JHUNJHUNWALA", "rekha jhunjunwala"))
        self.assertTrue(ins.name_match("FINAVENUE GROWTH FUND", "finavnue"))
        self.assertFalse(ins.name_match("A.K. CAPITAL FINANCE LIMITED", "finavnue"))
        self.assertFalse(ins.name_match("VIJAY KISHANLAL KEDIA", "dolly khanna"))
        self.assertFalse(ins.name_match("ANYONE", ""))

    def test_select_by_investor_and_symbol(self):
        rows = self.bulk + self.block
        hp = ins.select_deals(rows, investors=["hitesh patel"])
        self.assertEqual([r["client"] for r in hp],
                         ["YASHVI HITESH PATEL", "YASH HITESH PATEL", "BHAVNA HITESH PATEL"])
        self.assertEqual(hp[0]["matched"], ["hitesh patel"])
        self.assertEqual(len(ins.select_deals(rows, symbols={"CAPINVIT"})), 4)
        self.assertEqual(len(ins.select_deals(rows, symbols={"CAPINVIT"}, kinds=["block"])), 2)
        self.assertEqual(len(ins.select_deals(rows, side="sell", symbols={"OMNI"})), 2)

    def test_same_day_both_sides_is_churn(self):
        ins.mark_churn(self.bulk)
        jump = [r for r in self.bulk if r["client"].startswith("JUMP TRADING")]
        self.assertTrue(all(r["same_day_both_sides"] for r in jump))
        fin = next(r for r in self.bulk if r["client"] == "FINAVENUE GROWTH FUND")
        self.assertFalse(fin["same_day_both_sides"])
        self.assertIn("trading, not a position", ins.describe_deal(jump[0]))

    def test_cli_latest_and_history(self):
        code, out, _ = run(["deals", "--offline", DIR, "--investor", "finavenue",
                            "--tickers", "RAVITA", "--portfolio", "/nonexistent"])
        self.assertEqual(code, 0)
        self.assertIn("RAVITA: FINAVENUE GROWTH FUND buy 3,66,000 shares at ₹112.58", out)
        self.assertIn("you hold RAVITA", out)
        self.assertIn("not a reason to buy", out)
        code, out, _ = run(["deals", "--offline", DIR, "--kind", "block", "--date", "2026-10-21",
                            "--json"])
        self.assertEqual(len(json.loads(out)["deals"]), 10)
        code, out, _ = run(["deals", "--offline", DIR, "--investor", "dolly khanna"])
        self.assertEqual(code, 1)
        self.assertIn("no deals matched", out)

    def test_saved_investor_list(self):
        with tempfile.TemporaryDirectory() as tmp:
            f = os.path.join(tmp, "investors.json")
            run(["investors", "add", "Finavenue", "Hitesh Patel", "--investors-file", f])
            run(["investors", "add", "finavenue", "--investors-file", f])
            code, out, _ = run(["investors", "list", "--investors-file", f])
            self.assertEqual(out.split("\n")[:2], ["Finavenue", "Hitesh Patel"])
            code, out, _ = run(["deals", "--offline", DIR, "--saved", "--investors-file", f,
                                "--json"])
            self.assertEqual(len(json.loads(out)["deals"]), 4)
            run(["investors", "remove", "hitesh patel", "--investors-file", f])
            self.assertEqual(ins.load_investors(f), ["Finavenue"])


class NewSinceLastCheck(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        t = self.tmp.name
        self.state = os.path.join(t, "state", "insiders.json")
        self.pf = os.path.join(t, "portfolio.json")
        with open(self.pf, "w") as fh:
            json.dump({"cash": 0, "positions": [{"symbol": "RELIANCE", "exchange": "NSE",
                                                 "shares": 10}]}, fh)
        self.inv = os.path.join(t, "investors.json")
        with open(self.inv, "w") as fh:
            json.dump({"investors": ["hitesh patel"]}, fh)
        self.base = ["new", "--offline", DIR, "--state", self.state, "--portfolio", self.pf,
                     "--investors-file", self.inv, "--watchlist", os.path.join(t, "none.json")]

    def tearDown(self):
        self.tmp.cleanup()

    def test_alerts_once(self):
        code, out, _ = run(self.base + ["--today", "2026-02-20"])
        self.assertEqual(code, 0)
        # first run: PIT filings from the last 30 days only
        self.assertIn("BALANADU NARAYAN", out)
        self.assertNotIn("Shobha Agarwal", out)
        self.assertIn("you hold RELIANCE", out)
        self.assertIn("YASHVI HITESH PATEL", out)
        self.assertNotIn("JUMP TRADING", out)
        with open(self.state) as fh:
            saved = json.load(fh)
        self.assertIn("pit:551138", saved["seen"])  # old rows are marked seen, not shown
        self.assertEqual(len(saved["seen"]), 11 + 28)
        code, out, _ = run(self.base + ["--today", "2026-02-20"])
        self.assertEqual(code, 1)
        self.assertIn("Nothing new", out)

    def test_a_new_filing_is_new(self):
        os.makedirs(os.path.dirname(self.state), exist_ok=True)
        rows = ins.parse_pit(json.loads(fixture("pit-RELIANCE.json")))
        seen = [r["id"] for r in rows if r["id"] != "pit:475186"]
        with open(self.state, "w") as fh:
            json.dump({"seen": seen, "last_check": "2026-10-22T18:00:00+05:30"}, fh)
        code, text, _ = run(self.base + ["--no-deals", "--json"])
        got = json.loads(text)
        self.assertEqual(code, 0)
        self.assertEqual([r["id"] for r in got["pit"]], ["pit:475186"])

    def test_all_deals_and_dry_run(self):
        code, text, _ = run(self.base + ["--no-pit", "--all-deals", "--dry-run", "--json"])
        self.assertEqual(len(json.loads(text)["deals"]), 28)
        self.assertFalse(os.path.exists(self.state))

    def test_nothing_to_watch_says_so(self):
        empty = os.path.join(self.tmp.name, "empty.json")
        with open(empty, "w") as fh:
            json.dump({"investors": []}, fh)
        code, out, _ = run(["new", "--offline", DIR, "--state", self.state,
                            "--portfolio", "/nonexistent", "--investors-file", empty,
                            "--watchlist", "/nonexistent", "--dry-run"])
        self.assertEqual(code, 1)
        self.assertIn("No stocks to check", out)
        self.assertIn("investors add", out)

    def test_watchlist_hits_are_checked(self):
        wl = os.path.join(self.tmp.name, "watchlist.json")
        with open(wl, "w") as fh:
            json.dump({"hits": [{"symbol": "CAPINVIT", "exchange": "NSE"}]}, fh)
        self.assertEqual(ins.watch_symbols(wl), {"CAPINVIT"})
        self.assertEqual(ins.held_symbols(self.pf), {"RELIANCE"})


if __name__ == "__main__":
    unittest.main()
