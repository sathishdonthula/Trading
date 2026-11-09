import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stdout, redirect_stderr

from _load import load, ROOT, FIX

size = load("india-budget", "size")

CFG = os.path.join(ROOT, "india-budget", "config.example.json")
PLAN = os.path.join(FIX, "india-budget", "plan-reliance.json")
ORDERS = os.path.join(FIX, "india-risk", "orders-reliance.json")
NOW = "2026-10-19 09:50"          # IST: the 09:45 bar has closed at 1227.80
TODAY = "2026-10-19"
CONTRACT_CFG = {
    "budget": 50000, "cash_buffer_pct": 10, "max_position_pct": 75, "max_order_inr": 40000, "min_order_inr": 100,
    "max_loss_per_trade_inr": 500, "max_daily_loss_inr": 1500, "max_trades_per_day": 3, "max_orders_per_day": 10,
    "min_reward_risk": 1.0, "require_stop": True,
    "no_entries_before": "09:30", "no_entries_after": "14:45", "flat_by": "auto",
    "product": "MIS", "exchange": "NSE", "broker": "zerodha", "paper": True, "blocked": [],
    "allow_options": False, "allow_margin": False, "allow_short": False,
    "cas_symbols_extra": [], "non_cas_symbols": [],
}
SHARED = os.path.join(FIX, "shared")
FO = os.path.join(SHARED, "fo_mktlots-2026-10-23.csv")
KITE_LTP = os.path.join(SHARED, "kite-ltp-2026-10-23.json")
KITE_INST = os.path.join(SHARED, "kite-instruments-RELIANCE-2026-10-23.json")


def cfg(**over):
    with open(CFG) as fh:
        c = json.load(fh)
    c.update(over)
    return c


def plan(**over):
    with open(PLAN) as fh:
        p = json.load(fh)
    p.update(over)
    return p


def run(argv):
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = size.main(argv)
    return code, out.getvalue(), err.getvalue()


def prices_doc(price, quote_time="2026-10-19T04:52:00Z"):      # 10:22 IST
    return {"as_of": quote_time, "source": "yahoo", "prices": {"RELIANCE": price},
            "detail": {"RELIANCE": {"prev_close": 1184.5, "time": quote_time, "source": "yahoo"}}}


class Helpers(unittest.TestCase):
    def test_ticks(self):
        self.assertEqual(size.tick_size(249.99), 0.01)
        self.assertEqual(size.tick_size(250), 0.05)
        self.assertEqual(size.tick_size(1227.8), 0.10)
        self.assertEqual(size.tick_size(5000), 0.50)
        self.assertEqual(size.tick_size(15000), 1.00)
        self.assertEqual(size.tick_size(25000), 5.00)
        # the opening-range midpoint (1223.40 + 1207.30) / 2 = 1215.35 -> the stop rounds DOWN to 1215.30
        self.assertEqual(size.round_to_tick((1223.4 + 1207.3) / 2, "down"), 1215.3)
        self.assertEqual(size.round_to_tick(1227.84), 1227.8)
        self.assertEqual(size.round_to_tick(512.33, "up"), 512.35)

    def test_charges(self):
        ch = size.charges(30 * 1227.8, 30 * 1215.3, "MIS", "NSE")
        self.assertEqual(ch["total"], 38.82)
        self.assertEqual((ch["brokerage"], ch["stt"], ch["stamp"]), (21.99, 9.11, 1.11))
        self.assertEqual(size.charges(100000, 100000, "MIS")["brokerage"], 40.0)    # Rs 20 cap per order
        self.assertEqual(size.charges(10000, 10000, "CNC")["brokerage"], 0.0)
        self.assertEqual(size.charges(10000, 10000, "CNC")["stt"], 20.0)

    def test_indian_grouping(self):
        self.assertEqual(size.inr(123456.78), "₹1,23,456.78")
        self.assertEqual(size.inr(12345678), "₹1,23,45,678.00")
        self.assertEqual(size.money(50000), "₹50,000")
        self.assertEqual(size.parse_symbol("RELIANCE.BO"), ("RELIANCE", "BSE"))
        self.assertEqual(size.parse_symbol("NSE:M&M"), ("M&M", "NSE"))


class TheExample(unittest.TestCase):
    """RELIANCE, Mon 2026-10-19: entry 1227.80, stop 1215.30 (OR midpoint), target 1240.30 (1R).
    ₹50,000 budget, 10% buffer, ₹500 max loss incl. charges -> 30 shares, bound by the position cap."""

    def test_config_example_is_the_contract(self):
        with open(CFG) as fh:
            self.assertEqual(json.load(fh), CONTRACT_CFG)

    def test_reliance_is_30_shares_bound_by_the_position_cap(self):
        s = size.size_trade(plan(), cfg(), today=TODAY)
        self.assertEqual(s["qty"], 30)
        self.assertEqual(s["bound_by"], "position cap")
        self.assertEqual(s["risk_per_share"], 12.5)
        self.assertEqual(s["cost"], 36834.0)
        self.assertEqual(s["charges"]["total"], 38.82)
        self.assertEqual(s["max_loss"], 413.82)
        self.assertEqual(s["reward_risk"], 1.0)
        self.assertEqual(s["cash_buffer"], 5000)
        self.assertEqual([(l["name"], l["shares"]) for l in s["limits"]],
                         [("max loss", 36), ("position cap", 30), ("max order", 32), ("cash", 36)])
        self.assertIsNone(s["refusal"])

    def test_the_max_loss_limit_counts_the_charges(self):
        # 500 / 12.50 = 40 shares on price alone; charges bring it to 36 (36 -> ₹496.58, 37 -> over ₹500)
        self.assertEqual(size.max_loss_shares(500, 1227.8, 1215.3), 36)
        self.assertLessEqual(size.loss_at_stop(36, 1227.8, 1215.3)[0], 500)
        self.assertGreater(size.loss_at_stop(37, 1227.8, 1215.3)[0], 500)
        s = size.size_trade(plan(), cfg(max_position_pct=100), today=TODAY)
        self.assertEqual((s["qty"], s["bound_by"]), (32, "max order"))
        s = size.size_trade(plan(), cfg(max_position_pct=100, max_order_inr=100000), today=TODAY)
        self.assertEqual((s["qty"], s["bound_by"], s["max_loss"]), (36, "max loss", 496.58))

    def test_an_unrounded_stop_rounds_down_to_the_tick(self):
        s = size.size_trade(plan(stop=1215.35), cfg(), today=TODAY)
        self.assertEqual((s["stop"], s["risk_per_share"]), (1215.3, 12.5))

    def test_the_order_is_kite_place_order_plus_an_exit_plan(self):
        o = size.entry_order(size.size_trade(plan(), cfg(), today=TODAY))
        why, p, ep = o.pop("why"), o.pop("plan"), o.pop("exit_plan")
        self.assertEqual(o, {"variety": "regular", "exchange": "NSE", "tradingsymbol": "RELIANCE",
                             "transaction_type": "BUY", "quantity": 30, "product": "MIS", "order_type": "MARKET",
                             "validity": "DAY", "tag": "indiabudget"})
        self.assertEqual(ep["kind"], "sl-m+limit")
        self.assertEqual(ep["stop_leg"], {"variety": "regular", "exchange": "NSE", "tradingsymbol": "RELIANCE",
                                          "transaction_type": "SELL", "quantity": 30, "product": "MIS",
                                          "validity": "DAY", "tag": "indiabudget", "order_type": "SL-M",
                                          "trigger_price": 1215.3})
        self.assertEqual((ep["target_leg"]["order_type"], ep["target_leg"]["price"]), ("LIMIT", 1240.3))
        self.assertEqual((p["entry"], p["stop"], p["target"], p["max_loss"], p["charges"], p["flat_by"]),
                         (1227.8, 1215.3, 1240.3, 413.82, 38.82, "15:10"))
        self.assertIn("bound by your position cap", why)
        self.assertIn("₹413.82", why)

    def test_the_fixture_orders_file_is_what_it_writes(self):
        with open(ORDERS, encoding="utf-8") as fh:
            fixture = json.load(fh)["orders"][0]
        self.assertEqual(size.entry_order(size.size_trade(plan(), cfg(), today=TODAY)), fixture)

    def test_cnc_uses_a_two_leg_gtt(self):
        s = size.size_trade(plan(product="CNC"), cfg(), today=TODAY)
        o = size.entry_order(s)
        g = o["exit_plan"]["gtt"]
        self.assertEqual((o["product"], o["exit_plan"]["kind"], g["trigger_type"]), ("CNC", "gtt-oco", "two-leg"))
        self.assertEqual((g["upper_trigger_value"], g["lower_trigger_value"], g["lower_limit_price"]),
                         (1240.3, 1215.3, 1203.1))
        self.assertEqual((g["upper_quantity"], g["lower_quantity"]), (s["qty"], s["qty"]))
        self.assertGreater(s["charges"]["stt"], 20)        # delivery STT is 0.1% on both sides

    def test_a_limit_order(self):
        o = size.entry_order(size.size_trade(plan(), cfg(), limit=1227.84, today=TODAY))
        self.assertEqual((o["order_type"], o["price"], o["quantity"]), ("LIMIT", 1227.8, 30))

    def test_cli_writes_one_order(self):
        with tempfile.TemporaryDirectory() as d:
            out = os.path.join(d, "trader-in", "orders.json")
            code, text, _ = run(["--plan", PLAN, "--config", CFG, "--portfolio", os.path.join(d, "none"),
                                 "--now", NOW, "--write", out])
            self.assertEqual(code, 0)
            self.assertIn("Size: 30 shares of RELIANCE = ₹36,834 at 1227.80", text)
            self.assertIn("30 x 12.50 = ₹375 + ₹38.82 charges = ₹413.82", text)
            self.assertIn("<- binds", text)
            self.assertIn("not financial advice", text)
            with open(out, encoding="utf-8") as fh:
                doc = json.load(fh)
            self.assertEqual((doc["status"], doc["paper"], doc["currency"]), ("proposed", True, "INR"))
            self.assertEqual(doc["created_at"], "2026-10-19T04:20:00Z")
            self.assertEqual(len(doc["orders"]), 1)

    def test_cli_json_and_limit(self):
        code, text, _ = run(["--plan", PLAN, "--config", CFG, "--portfolio", "/nonexistent", "--now", NOW,
                             "--limit", "--json"])
        self.assertEqual(code, 0)
        out = json.loads(text)
        self.assertEqual(out["orders"][0]["price"], 1227.8)


class PlanRules(unittest.TestCase):
    def test_zero_shares_is_a_plain_refusal(self):
        s = size.size_trade(plan(stop=600.0), cfg(), today=TODAY)
        self.assertEqual(s["qty"], 0)
        self.assertIn("more than your ₹500 max loss per trade", s["refusal"])
        self.assertIsNone(size.entry_order(s))
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "plan.json")
            with open(p, "w") as fh:
                json.dump(plan(stop=600.0), fh)
            out = os.path.join(d, "orders.json")
            code, text, _ = run(["--plan", p, "--config", CFG, "--portfolio", os.path.join(d, "none"),
                                 "--now", NOW, "--write", out])
            self.assertEqual(code, 1)
            self.assertIn("No order:", text)
            self.assertFalse(os.path.exists(out))

    def test_each_limit_can_bind(self):
        self.assertEqual(size.size_trade(plan(), cfg(max_order_inr=20000), today=TODAY)["bound_by"], "max order")
        s = size.size_trade(plan(), cfg(), cash=15000, today=TODAY)
        self.assertEqual((s["qty"], s["bound_by"], s["cash_available"]), (8, "cash", 10000))
        s = size.size_trade(plan(), cfg(max_loss_per_trade_inr=200), today=TODAY)
        self.assertEqual((s["qty"], s["bound_by"]), (14, "max loss"))
        self.assertLessEqual(s["max_loss"], 200)

    def test_a_fresh_higher_price_means_a_bigger_risk(self):
        s = size.size_trade(plan(), cfg(min_reward_risk=0), price=1233.84, today=TODAY)
        self.assertEqual((s["entry"], s["entry_from"], s["risk_per_share"]), (1233.8, "price", 18.5))
        self.assertEqual(s["reward_risk"], 0.35)
        s = size.size_trade(plan(), cfg(), price=1233.84, today=TODAY)
        self.assertIn("reward:risk 0.35 (target 1240.30, entry 1233.80, stop 1215.30) is under your minimum 1",
                      s["refusal"])
        lower = size.size_trade(plan(), cfg(), price=1220.0, today=TODAY)
        self.assertEqual((lower["entry"], lower["qty"]), (1227.8, 30))

    def test_prices_file_is_used_only_when_fresh(self):
        with tempfile.TemporaryDirectory() as d:
            pp = os.path.join(d, "prices.json")
            with open(pp, "w") as fh:
                json.dump(prices_doc(1233.84), fh)
            base = ["--plan", PLAN, "--config", CFG, "--portfolio", os.path.join(d, "none"), "--prices", pp, "--json"]
            code, text, _ = run(base + ["--now", "2026-10-19 10:25"])
            out = json.loads(text)
            self.assertEqual((code, out["orders"]), (1, []))
            self.assertIn("above the plan's entry", out["notes"][0])
            out = json.loads(run(base + ["--now", "2026-10-19 11:00"])[1])
            self.assertEqual(out["sizing"]["qty"], 30)
            self.assertIn("not used", out["notes"][0])

    def test_bad_plans(self):
        for p, word in ((plan(stop=1230.0), "not below the entry"),
                        (plan(target=1220.0), "already happened"),
                        (plan(target=None), "positive target"),
                        (plan(side="sell"), "long (buy) day trades only"),
                        (plan(symbol=""), "no symbol"),
                        (plan(series="BE"), "trade-to-trade"),
                        (plan(product="NRML"), "MIS (intraday) or CNC")):
            with self.assertRaises(size.SizeError) as e:
                size.size_trade(p, cfg(), today=TODAY)
            self.assertIn(word, str(e.exception))
        with self.assertRaises(size.SizeError) as e:
            size.size_trade(plan(), cfg(blocked=["reliance"]), today=TODAY)
        self.assertIn("blocked list", str(e.exception))
        with self.assertRaises(size.SizeError) as e:
            size.size_trade(plan(), cfg(), today="2026-10-21")
        self.assertIn("make a new plan", str(e.exception))

    def test_under_the_minimum_order(self):
        cheap = plan(symbol="IDEA", entry=8.0, stop=7.0, target=9.5)
        s = size.size_trade(cheap, cfg(max_loss_per_trade_inr=10), today=TODAY)
        self.assertIn("under your ₹100 minimum order", s["refusal"])

    def test_errors_are_one_line(self):
        code, _, err = run(["--plan", "/nonexistent/plan.json", "--config", CFG, "--now", NOW])
        self.assertEqual(code, 1)
        self.assertIn("no plan", err)
        code, _, err = run(["--plan", PLAN, "--config", CFG, "--now", "tomorrow"])
        self.assertEqual(code, 1)
        self.assertNotIn("Traceback", err)


class CasAndKite(unittest.TestCase):
    def test_session_rules(self):
        r = size.session_rules(True, "2026-10-19")
        self.assertEqual((r["close"], r["square_off"], r["flat_by_default"], r["bars_5m"]), ("15:15", "15:12", "15:00", 72))
        r = size.session_rules(False, "2026-10-19")
        self.assertEqual((r["close"], r["square_off"], r["flat_by_default"], r["bars_5m"]), ("15:30", "15:25", "15:10", 75))
        self.assertEqual(size.session_rules(None, "2026-10-19")["square_off"], "15:12")     # unknown -> strict
        self.assertEqual(size.session_rules(True, "2026-07-31")["square_off"], "15:25")     # before CAS began

    def test_fo_list_and_cas_status(self):
        fo, day = size.load_fo_list(FO)
        self.assertIn("RELIANCE", fo)
        self.assertIn("IDEA", fo)
        self.assertNotIn("NIFTY", fo)
        self.assertNotIn("BANKNIFTY", fo)
        self.assertNotIn("SYMBOL", fo)
        self.assertEqual(day, "2026-10-23")
        self.assertEqual(size.cas_status("RELIANCE", fo=fo)[0], True)
        self.assertEqual(size.cas_status("20MICRONS", fo=fo)[0], False)
        self.assertEqual(size.cas_status("20MICRONS")[0], None)
        self.assertEqual(size.cas_status("RELIANCE", cfg={"non_cas_symbols": ["reliance"]}, fo=fo)[0], False)
        self.assertEqual(size.cas_status("RELIANCE", plan={"cas": False}, fo=fo, flag="yes")[0], True)
        with tempfile.TemporaryDirectory() as d:
            txt = os.path.join(d, "fo_list.txt")
            with open(txt, "w") as fh:
                fh.write("# fetched 2026-10-23\nRELIANCE\nTCS\n")
            self.assertEqual(size.load_fo_list(txt), ({"RELIANCE", "TCS"}, "2026-10-23"))

    def test_flat_by_auto_and_the_clamp(self):
        fo = size.load_fo_list(FO)[0]
        s = size.size_trade(plan(flat_by=None), cfg(), today=TODAY, fo=fo)
        self.assertEqual((s["flat_by"], s["cas"], s["session"]["square_off"]), ("15:00", True, "15:12"))
        s = size.size_trade(plan(flat_by=None), cfg(), today=TODAY, cas_flag="no")
        self.assertEqual(s["flat_by"], "15:10")
        s = size.size_trade(plan(flat_by="15:20"), cfg(), today=TODAY, fo=fo)
        self.assertEqual(s["flat_by"], "15:00")
        self.assertIn("at or after Zerodha's 15:12 MIS square-off", " ".join(s["warnings"]))
        o = size.entry_order(size.size_trade(plan(), cfg(), today=TODAY, fo=fo))
        self.assertEqual((o["plan"]["cas"], o["plan"]["square_off"], o["plan"]["session_close"], o["plan"]["flat_by"]),
                         (True, "15:12", "15:15", "15:10"))

    def test_kite_ltp_is_the_fresh_price(self):
        code, text, _ = run(["--plan", PLAN, "--config", CFG, "--portfolio", "/none", "--now", NOW,
                             "--kite-ltp", KITE_LTP, "--fo-list", FO])
        self.assertEqual(code, 0)
        self.assertIn("RELIANCE last 1190.00 (Kite, live, just fetched) is not above the plan's entry 1227.80", text)
        self.assertIn("Zerodha squares off MIS at 15:12", text)
        code, text, _ = run(["--plan", PLAN, "--config", CFG, "--portfolio", "/none", "--now", NOW,
                             "--price", "NSE:RELIANCE=1233.84", "--json"])
        out = json.loads(text)
        self.assertEqual((out["sizing"]["entry"], out["sizing"]["price_source"], out["orders"]), (1233.8, "manual", []))
        code, _, err = run(["--plan", PLAN, "--config", CFG, "--now", NOW, "--price", "RELIANCE"])
        self.assertEqual(code, 1)
        self.assertIn("SYMBOL=PRICE", err)

    def test_kite_instrument_circuit_and_tick(self):
        table, day = size.load_kite_instrument(KITE_INST)
        row = table["NSE:RELIANCE"]
        self.assertEqual((day, row["tick"], row["lower"], row["upper"]), ("2026-10-23", 0.1, 1066.1, 1302.9))
        today = plan(date="2026-10-23", entry=1295.0, stop=1285.0, target=1310.0)
        s = size.size_trade(today, cfg(), today="2026-10-23", kite_inst=row)
        self.assertIn("within 1% of today's upper circuit 1302.90 (Kite instrument limits 1066.10-1302.90)", s["refusal"])
        ok = size.size_trade(plan(date="2026-10-23", entry=1195.0, stop=1185.0, target=1205.0), cfg(),
                             today="2026-10-23", kite_inst=row)
        self.assertIsNone(ok["refusal"])
        self.assertEqual((ok["tick_from"], ok["circuit"]["upper"]), ("Kite instrument", 1302.9))
        self.assertEqual(size.round_to_tick(1215.37, "down", 0.05), 1215.35)
        # the file is for 2026-10-23: a plan for 2026-10-19 does not use its limits
        code, text, _ = run(["--plan", PLAN, "--config", CFG, "--portfolio", "/none", "--now", NOW,
                             "--kite-instrument", KITE_INST])
        self.assertIn("its circuit limits are for one day only, so they are not used", text)


class WeightsMode(unittest.TestCase):
    PRICES = {"RELIANCE": 1227.8, "TCS": 3050.0, "HDFCBANK": 990.0}

    def test_whole_shares_inside_the_cap(self):
        w = size.parse_weights(size.weights_from_arg("RELIANCE=40,TCS=30,HDFCBANK=30"))
        r = size.size(w, cfg(max_position_pct=40), prices=self.PRICES)
        self.assertEqual(r["investable"], 45000)
        for p in r["positions"]:
            self.assertEqual(p["inr"], round(p["shares"] * self.PRICES[p["ticker"]], 2))
            self.assertLessEqual(p["inr"], 20000)
        self.assertAlmostEqual(r["invested"] + r["idle"], 45000, places=2)
        orders = size.orders_from(r)
        self.assertTrue(all(o["product"] == "CNC" and isinstance(o["quantity"], int) for o in orders))

    def test_prices_are_required_and_mm_parses(self):
        with self.assertRaises(size.SizeError):
            size.size(size.parse_weights({"RELIANCE": 1}), cfg())
        self.assertEqual(size.weights_from_arg("M&M=50,BAJAJ-AUTO=50"), {"M&M": "50", "BAJAJ-AUTO": "50"})


if __name__ == "__main__":
    unittest.main()
