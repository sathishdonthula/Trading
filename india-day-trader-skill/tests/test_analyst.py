import datetime
import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout

from _load import FIX, load

results = load("india-analyst", "results")
DIR = os.path.join(FIX, "india-analyst")
TODAY = "2026-10-23"


def raw(name):
    with open(os.path.join(DIR, name), "rb") as fh:
        return fh.read()


def run(args):
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = results.main(args)
    return code, out.getvalue(), err.getvalue()


def report(symbol, **kw):
    f = results.Fetcher(offline=DIR)
    return results.report(f, symbol, today=datetime.date(2026, 10, 23), **kw)


class Xbrl(unittest.TestCase):
    def test_ind_as_quarter(self):
        q = results.parse_xbrl(raw("INTEGRATED_FILING_INDAS_1695741_17072026075004_WEB.xml"))
        self.assertEqual((q["start"], q["end"]), ("2026-04-01", "2026-06-30"))
        self.assertEqual(q["format"], "ind-as")
        self.assertEqual(q["nature"], "Consolidated")
        v = q["values"]
        self.assertEqual(v["revenue"], 3118500000000)          # ₹3,11,850 crore
        self.assertEqual(v["profit_owners"], 209460000000)
        self.assertEqual(v["eps_diluted"], 15.48)
        self.assertEqual(q["concepts"]["eps_diluted"],
                         "DilutedEarningsLossPerShareFromContinuingAndDiscontinuedOperations")

    def test_march_filing_holds_the_quarter_and_the_year(self):
        q = results.parse_xbrl(raw("INTEGRATED_FILING_INDAS_1658776_24042026105714_WEB.xml"))
        self.assertEqual((q["start"], q["end"]), ("2026-01-01", "2026-03-31"))  # not the full year
        self.assertEqual(q["values"]["revenue"], 2986210000000)

    def test_bank_format(self):
        q = results.parse_xbrl(raw("INTEGRATED_FILING_BANKING_1740913_17102026054812_WEB.xml"))
        self.assertEqual(q["format"], "bank")
        d = results.derive(q)
        self.assertEqual(d["revenue"], 1293586900000)  # total income
        self.assertEqual(d["nii"], 951316300000 - 506674300000)
        self.assertEqual(d["operating_profit"], 328715400000)
        self.assertIsNone(d["ebitda"])

    def test_legacy_format_before_2025(self):
        q = results.parse_xbrl(raw("INDAS_117297_1348248_16012025081520.xml"))
        self.assertEqual(q["end"], "2024-12-31")
        self.assertEqual(q["values"]["revenue"], 2438650000000)

    def test_fiscal_labels(self):
        self.assertEqual(results.fiscal_label("2026-06-30"), "Q1 FY27")
        self.assertEqual(results.fiscal_label("2026-03-31"), "Q4 FY26")
        self.assertEqual(results.fiscal_label("2025-12-31"), "Q3 FY26")

    def test_crore_with_indian_grouping(self):
        self.assertEqual(results.cr(3118500000000), "3,11,850")
        self.assertEqual(results.cr(11388650000000), "11,38,865")
        self.assertEqual(results.money(-8370600000), "-₹837 cr")


class Filings(unittest.TestCase):
    def test_both_lists_merge_one_per_quarter(self):
        integ = json.loads(raw("integrated-RELIANCE.json"))
        legacy = json.loads(raw("results-RELIANCE.json"))
        rows = results.filings(integ, legacy)
        cons = [r for r in rows if r["basis"] == "consolidated"]
        ends = [r["end"] for r in cons]
        self.assertEqual(ends[:7], ["2026-06-30", "2026-03-31", "2025-12-31", "2025-09-30",
                                    "2025-06-30", "2025-03-31", "2024-12-31"])
        self.assertEqual(len(ends), len(set(ends)))
        self.assertEqual(cons[0]["list"], "integrated filing")
        self.assertEqual(cons[6]["list"], "financial results")

    def test_standalone_only_company(self):
        rows = [{"end": "2026-06-30", "basis": "standalone", "xbrl": "x"}]
        mine, used, note = results.pick_basis(rows, "consolidated")
        self.assertEqual(used, "standalone")
        self.assertIn("no consolidated results", note)


class Reliance(unittest.TestCase):
    def setUp(self):
        self.rep = report("RELIANCE")
        self.r = self.rep["result"]

    def test_latest_quarter(self):
        q = self.r["rows"][0]
        self.assertEqual(q["end"], "2026-06-30")
        self.assertEqual(q["fiscal"], "Q1 FY27")
        self.assertEqual(q["basis"], "consolidated")
        self.assertEqual(q["revenue"], 3118500000000)
        self.assertEqual(q["net_profit"], 209460000000)
        self.assertEqual(q["eps_diluted"], 15.48)
        self.assertEqual(q["revenue_yoy"], 25.4)       # vs ₹2,48,660 cr in Q1 FY26
        self.assertEqual(q["net_profit_yoy"], -22.4)   # vs ₹26,994 cr
        self.assertEqual(q["eps_yoy"], -22.4)          # vs 19.95
        self.assertEqual(q["ebitda_margin"], 15.2)
        self.assertEqual(q["operating_margin"], 10.4)
        self.assertEqual(q["net_margin"], 6.7)
        self.assertFalse(q["eps_adjusted"])

    def test_bonus_restates_the_older_eps(self):
        q = next(r for r in self.r["rows"] if r["end"] == "2025-09-30")
        self.assertTrue(q["eps_adjusted"])  # Sep 2024 EPS 24.48 was before the 1:1 bonus
        self.assertEqual(q["eps_yoy"], 9.6)  # 13.42 vs 12.24

    def test_trailing_twelve_months(self):
        t = self.r["ttm"]
        self.assertEqual(results.cr(t["revenue"]), "11,38,865")
        self.assertEqual(results.cr(t["net_profit"]), "74,727")
        self.assertEqual(t["eps_diluted"], 55.22)

    def test_what_changed_in_words(self):
        lines = results.changes(self.r)
        self.assertIn("revenue ₹3,11,850 cr (+25.4% vs a year earlier)", lines[0])
        self.assertIn("diluted EPS ₹15.48 (-22.4%)", lines[0])
        self.assertTrue(any(l.startswith("EBITDA margin 15.2% (+0.4 points") for l in lines))
        self.assertTrue(any("A year earlier other income was ₹15,119 cr, 41%" in l for l in lines))

    def test_next_results_are_due(self):
        self.assertIn("quarter ending 2026-09-30 are not filed yet", self.rep["notes"][0])
        self.assertIn("2026-11-14", self.rep["notes"][0])

    def test_next_results_with_a_board_meeting(self):
        ev = [{"date": "30-Oct-2026", "purpose": "Financial Results"}]
        note = results.next_results(self.r, ev, datetime.date(2026, 10, 23))
        self.assertIn("board meeting on 2026-10-30", note)
        late = results.next_results(self.r, [{"date": "17-Oct-2026", "purpose": "Financial Results"}],
                                    datetime.date(2026, 10, 23))
        self.assertIn("does not carry them yet", late)

    def test_standalone_on_request(self):
        r = report("RELIANCE", quarters=1, basis="standalone")["result"]
        self.assertEqual(r["basis"], "standalone")
        self.assertEqual(results.cr(r["rows"][0]["revenue"]), "1,66,013")

    def test_transcript_links(self):
        tr = report("RELIANCE", want_transcripts=True)["transcripts"]
        self.assertEqual(tr[0]["kind"], "transcript")
        self.assertTrue(tr[0]["link"].endswith("SE_Transcript.pdf"))


class Bank(unittest.TestCase):
    def test_hdfc_bank(self):
        rep = report("HDFCBANK")
        r = rep["result"]
        self.assertEqual(r["format"], "bank")
        self.assertEqual(r["entity"], "HDFC Bank Limited")
        q = r["rows"][0]
        self.assertEqual(q["fiscal"], "Q2 FY27")
        self.assertEqual(results.cr(q["revenue"]), "1,29,359")
        self.assertEqual(results.cr(q["nii"]), "44,464")
        self.assertEqual(q["nii_yoy"], 10.5)
        self.assertEqual(q["net_profit_yoy"], 5.6)
        self.assertEqual(q["eps_yoy"], 5.5)
        self.assertFalse(q["eps_adjusted"])  # Sep 2025 was already after the 2025 1:1 bonus
        self.assertTrue(r["rows"][1]["eps_adjusted"])  # Jun 2025 EPS 21.12 was before it
        self.assertEqual(r["rows"][1]["eps_yoy"], 18.6)  # 12.47 vs 21.12 / 2
        self.assertIsNone(q["ebitda_margin"])
        lines = results.changes(r)
        self.assertTrue(any(l.startswith("Net interest income ₹44,464 cr") for l in lines))
        self.assertTrue(any("Exceptional items of -₹837 cr" in l for l in lines))
        self.assertIn("2026-12-31", rep["notes"][0])


class Cli(unittest.TestCase):
    def test_table_and_metrics_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "trader-in", "metrics.json")
            code, out, err = run(["RELIANCE", "HDFCBANK", "--offline", DIR, "--today", TODAY,
                                  "--metrics", path])
            self.assertEqual(code, 0, err)
            with open(path) as fh:
                data = json.load(fh)
        rows = {r["ticker"]: r for r in data["companies"]}
        self.assertEqual(sorted(rows), ["HDFCBANK", "RELIANCE"])
        self.assertEqual(rows["RELIANCE"]["eps_ttm"], 55.22)
        self.assertEqual(rows["RELIANCE"]["revenue_ttm_cr"], 1138865)
        self.assertEqual(rows["HDFCBANK"]["format"], "bank")
        self.assertIn("2026-06-30  Q1 FY27    3,11,850   +25.4%   15.2%", out)
        self.assertIn("₹ crore except EPS", out)
        self.assertIn("wrote", out)
        self.assertNotIn(chr(0x2014), out)

    def test_unknown_symbol_and_bse_code(self):
        code, _, err = run(["ZZZZ", "--offline", DIR])
        self.assertEqual(code, 2)
        self.assertIn("ZZZZ", err)
        code, _, err = run(["500325", "--offline", DIR])
        self.assertEqual(code, 2)
        self.assertIn("BSE code", err)

    def test_json_output(self):
        code, out, err = run(["RELIANCE", "--offline", DIR, "--json", "--quarters", "4",
                              "--today", TODAY])
        self.assertEqual(code, 0, err)
        data = json.loads(out)
        self.assertEqual(len(data[0]["result"]["rows"]), 4)
        self.assertEqual(data[0]["result"]["concepts"]["revenue"], "RevenueFromOperations")

    def test_nse_block_is_explained(self):
        with tempfile.TemporaryDirectory() as tmp:
            with open(os.path.join(tmp, "integrated-RELIANCE.json"), "w") as fh:
                fh.write("<HTML>Access Denied</HTML>")
            code, _, err = run(["RELIANCE", "--offline", tmp])
        self.assertEqual(code, 2)
        self.assertIn("NSE", err)


if __name__ == "__main__":
    unittest.main()
