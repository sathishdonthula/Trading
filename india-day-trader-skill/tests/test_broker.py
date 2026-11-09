import io
import json
import os
import shutil
import tempfile
import unittest
from contextlib import redirect_stdout, redirect_stderr

from _load import load, ROOT, FIX

ticket = load("india-broker", "ticket")
check = load("india-risk", "check")
size = load("india-budget", "size")

CFG_PATH = os.path.join(ROOT, "india-budget", "config.example.json")
ORDERS_PATH = os.path.join(FIX, "india-risk", "orders-reliance.json")
PLAN_PATH = os.path.join(FIX, "india-budget", "plan-reliance.json")
FO_PATH = os.path.join(FIX, "shared", "fo_mktlots-2026-10-23.csv")
KITE_LTP = os.path.join(FIX, "shared", "kite-ltp-2026-10-23.json")
NOW = "2026-10-19 09:50"
TODAY = "2026-10-19"
ENTRY_CALL = {"variety": "regular", "exchange": "NSE", "tradingsymbol": "RELIANCE", "transaction_type": "BUY",
              "quantity": 30, "product": "MIS", "order_type": "MARKET", "validity": "DAY", "tag": "indiabudget"}
STOP_CALL = {"variety": "regular", "exchange": "NSE", "tradingsymbol": "RELIANCE", "transaction_type": "SELL",
             "quantity": 30, "product": "MIS", "order_type": "SL-M", "validity": "DAY", "trigger_price": 1215.3,
             "tag": "indiabudget"}
TARGET_CALL = dict(STOP_CALL, order_type="LIMIT", price=1240.3)
del TARGET_CALL["trigger_price"]
PLAIN = ("BUY 30 NSE:RELIANCE, market, intraday (MIS), good for today / stop-loss 1215.30 (SL-M, placed after "
         "the fill) / target 1240.30 (LIMIT, placed after the fill) / max loss about ₹413.82 incl. charges if the "
         "stop fills at 1215.30 (it can fill worse in a fast market)")
# the official Kite MCP server's tools (github.com/zerodha/kite-mcp-server; hosted tools/list 2026-10-23)
KITE_TOOLS = {"login", "get_profile", "get_margins", "get_holdings", "get_positions", "get_mf_holdings", "get_ltp",
              "get_quotes", "get_ohlc", "get_historical_data", "search_instruments", "get_orders",
              "get_order_history", "get_order_trades", "get_trades", "place_order", "modify_order", "cancel_order",
              "get_gtts", "place_gtt_order", "modify_gtt_order", "delete_gtt_order"}
PLACE_ORDER_PARAMS = {"variety", "exchange", "tradingsymbol", "transaction_type", "quantity", "product",
                      "order_type", "price", "validity", "validity_ttl", "disclosed_quantity", "trigger_price",
                      "iceberg_legs", "iceberg_quantity", "tag"}
GTT_PARAMS = {"exchange", "tradingsymbol", "last_price", "transaction_type", "product", "trigger_type",
              "trigger_value", "quantity", "limit_price", "upper_trigger_value", "upper_quantity",
              "upper_limit_price", "lower_trigger_value", "lower_quantity", "lower_limit_price"}


def rel(**over):
    with open(ORDERS_PATH, encoding="utf-8") as fh:
        o = json.load(fh)["orders"][0]
    o.update(over)
    return o


def run(argv):
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = ticket.main(argv)
    return code, out.getvalue(), err.getvalue()


def first_json(text):
    return json.loads(text[text.index("{"):text.index("}") + 1])


class ExplicitYes(unittest.TestCase):
    def yes(self, reply, order=None, batch=("RELIANCE", "TCS")):
        return ticket.parse_yes(reply, order or rel(), batch)[0]

    def test_plain_yes_counts(self):
        for r in ("yes", "Yes.", "YES", "y", "yes place it", "yes RELIANCE", "yes 30 RELIANCE", "yes 1215.30",
                  "yes max loss 413.82", "yes target 1240.3"):
            self.assertTrue(self.yes(r), r)

    def test_anything_else_does_not(self):
        for r in ("", "sure", "ok", "haan", "go", "do it", "yeah", "yep", "send it", "yes to all",
                  "yes, all of them", "yes both", "yes TCS", "yes 31", "yes but make it 20", "yes?", "no",
                  "not yes", "yes, wait", "yes if it is under 1230", "yes, stop 1215.30"):
            self.assertFalse(self.yes(r), r)


class TicketShape(unittest.TestCase):
    def test_tool_input_is_the_kite_place_order_call(self):
        self.assertEqual(ticket.tool_input(rel()), ENTRY_CALL)
        self.assertLessEqual(set(ticket.tool_input(rel())), PLACE_ORDER_PARAMS)
        self.assertEqual(ticket.tool_input(rel(quantity="30", price="1227.80", order_type="LIMIT"))["quantity"], 30)
        self.assertEqual(ticket.fingerprint(rel()), check.fingerprint(rel()))

    def test_mis_exit_legs(self):
        calls = ticket.exit_calls(rel())
        self.assertEqual(calls, [("place_order", STOP_CALL), ("place_order", TARGET_CALL)])
        for tool, args in calls:
            self.assertIn(tool, KITE_TOOLS)
            self.assertLessEqual(set(args), PLACE_ORDER_PARAMS)

    def test_cnc_exit_is_one_two_leg_gtt(self):
        plan = json.load(open(PLAN_PATH))
        plan["product"] = "CNC"
        with open(CFG_PATH) as fh:
            c = json.load(fh)
        o = size.entry_order(size.size_trade(plan, c, today=TODAY))
        (tool, args), = ticket.exit_calls(o, last_price=1228.1)
        self.assertEqual(tool, "place_gtt_order")
        self.assertLessEqual(set(args), GTT_PARAMS)
        self.assertEqual((args["trigger_type"], args["last_price"], args["upper_trigger_value"],
                          args["lower_trigger_value"], args["lower_limit_price"]),
                         ("two-leg", 1228.1, 1240.3, 1215.3, 1203.1))

    def test_plain_english(self):
        self.assertEqual(ticket.plain(rel()), PLAIN)
        self.assertEqual(ticket.plain({"exchange": "NSE", "tradingsymbol": "RELIANCE", "transaction_type": "SELL",
                                       "quantity": 30, "product": "MIS", "order_type": "MARKET",
                                       "validity": "DAY"}),
                         "SELL 30 shares of NSE:RELIANCE, market, intraday (MIS), good for today")


class DesktopConfig(unittest.TestCase):
    def test_example_is_the_hosted_kite_server_with_no_keys(self):
        with open(os.path.join(ROOT, "india-broker", "claude_desktop_config.example.json")) as fh:
            text = fh.read()
        server = json.loads(text)["mcpServers"]["kite"]
        self.assertEqual((server["command"], server["args"]), ("npx", ["mcp-remote", "https://mcp.kite.trade/mcp"]))
        self.assertNotIn("KEY", text.upper())
        self.assertNotIn("SECRET", text.upper())

    def test_skill_names_real_kite_tools_only(self):
        with open(os.path.join(ROOT, "india-broker", "SKILL.md"), encoding="utf-8") as fh:
            text = fh.read()
        for t in ("place_order", "place_gtt_order", "cancel_order", "get_ltp", "get_positions", "login"):
            self.assertIn("`%s`" % t, text)
        import re
        named = set(re.findall(r"`((?:get|place|modify|cancel|delete)_[a-z_]+)`", text))
        self.assertLessEqual(named, KITE_TOOLS, named - KITE_TOOLS)
        self.assertIn("https://mcp.kite.trade/mcp", text)
        self.assertIn("Needs approval", text)


class Flow(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.orders = os.path.join(self.d, "orders.json")
        shutil.copy(ORDERS_PATH, self.orders)
        self.cfg = os.path.join(self.d, "config.json")
        shutil.copy(CFG_PATH, self.cfg)
        self.risk = os.path.join(self.d, "state", "risk.json")
        self.state = os.path.join(self.d, "state", "broker.json")
        self.pf = os.path.join(self.d, "portfolio.json")
        self.none = os.path.join(self.d, "none")

    def tearDown(self):
        shutil.rmtree(self.d)

    def live(self):
        with open(self.cfg) as fh:
            c = json.load(fh)
        c["paper"] = False
        with open(self.cfg, "w") as fh:
            json.dump(c, fh)

    def args(self, *extra, **kw):
        return list(extra) + ["--orders", self.orders, "--config", self.cfg, "--risk", self.risk,
                              "--state", self.state, "--now", kw.get("now", NOW)]

    def run_risk(self, now=NOW):
        with redirect_stdout(io.StringIO()):
            return check.main(["--orders", self.orders, "--config", self.cfg, "--portfolio", self.pf,
                               "--prices", self.none, "--state", self.state, "--sec-list", self.none,
                               "--holidays", self.none, "--now", now, "--out", self.risk])

    def state_doc(self):
        with open(self.state) as fh:
            return json.load(fh)

    def portfolio(self):
        with open(self.pf) as fh:
            return json.load(fh)

    def test_nothing_is_ready_before_the_risk_check(self):
        code, text, _ = run(self.args("show"))
        self.assertEqual(code, 1)
        self.assertIn("NOT READY", text)
        code, _, err = run(self.args("approve", "--index", "1", "--reply", "yes", "--portfolio", self.pf))
        self.assertEqual(code, 1)
        self.assertIn("not ready", err)

    def test_paper_yes_is_a_local_simulated_fill(self):
        self.assertEqual(self.run_risk(), 0)
        code, text, _ = run(self.args("show", "--index", "1"))
        self.assertEqual(code, 0)
        self.assertIn("ORDER 1 of 1, PAPER (simulated here, nothing goes to Zerodha), Zerodha Kite", text)
        for part in PLAIN.split(" / "):
            self.assertIn("  " + part + "\n", text)
        self.assertIn("READY: india-risk PASS", text)
        self.assertIn('"trigger_price": 1215.3', text)
        self.assertNotIn('"plan"', text)
        self.assertNotIn('"why"', text)
        code, text, _ = run(self.args("approve", "--index", "1", "--reply", "sure", "--portfolio", self.pf))
        self.assertEqual(code, 1)
        self.assertIn("Not recorded", text)
        self.assertFalse(os.path.exists(self.state))
        code, text, _ = run(self.args("approve", "--index", "1", "--reply", "yes", "--portfolio", self.pf))
        self.assertEqual(code, 0)
        self.assertIn("PAPER: simulated fill, BUY 30 RELIANCE at 1227.80", text)
        self.assertIn("Nothing was sent to Zerodha", text)
        self.assertNotIn("Now call Kite MCP", text)
        st = self.state_doc()
        self.assertEqual([r["status"] for r in st["orders"]], ["approved", "placed", "filled"])
        self.assertEqual((st["orders"][1]["paper_fill"], st["orders"][1]["max_loss"], st["orders"][1]["kind"]),
                         (True, 413.82, "entry"))
        self.assertEqual(st["orders"][0]["at"], "2026-10-19T09:50:00+05:30")
        pf = self.portfolio()
        self.assertEqual((pf["cash"], pf["positions"][0]["shares"], pf["positions"][0]["product"]),
                         (13166.0, 30, "MIS"))
        self.assertIn("DONE: placed (paper)", run(self.args("show", "--index", "1"))[1])
        code, _, err = run(self.args("result", "--index", "1", "--status", "placed"))
        self.assertEqual(code, 1)
        self.assertIn("paper account", err)
        # india-risk now counts it: a second RELIANCE buy today is a duplicate, ₹413.82 is used
        res = check.check([rel()], {"budget": 50000}, state=st, now=check.parse_now("2026-10-19 10:30"))
        self.assertIn("already placed today", " ".join(res["results"][0]["reasons"]))
        self.assertEqual((res["today"]["entries_placed"], res["today"]["loss_at_risk"]), (1, 413.82))

    def test_live_yes_prints_the_exact_kite_call_then_the_exit_legs(self):
        self.live()
        self.run_risk()
        code, text, _ = run(self.args("show", "--index", "1"))
        self.assertIn("LIVE ACCOUNT, REAL MONEY", text)
        self.assertIn("Allow once", text)
        code, text, _ = run(self.args("approve", "--index", "1", "--reply", "yes", "--portfolio", self.pf))
        self.assertEqual(code, 0)
        self.assertIn("Now call Kite MCP place_order with exactly:", text)
        self.assertEqual(first_json(text), ENTRY_CALL)
        code, _, err = run(self.args("approve", "--index", "1", "--reply", "yes", "--portfolio", self.pf))
        self.assertIn("APPROVED", err)
        code, text, _ = run(self.args("result", "--index", "1", "--status", "placed", "--broker-id", "261019000123",
                                      "--filled-qty", "30", "--filled-price", "1228.10", "--portfolio", self.pf,
                                      now="2026-10-19 09:51"))
        self.assertEqual(code, 0)
        self.assertIn('"order_type": "SL-M"', text)
        self.assertIn('"trigger_price": 1215.3', text)
        self.assertIn("cancel_order", text)
        st = self.state_doc()
        self.assertEqual([r["status"] for r in st["orders"]], ["approved", "placed", "filled"])
        self.assertEqual(st["orders"][1]["broker_order_id"], "261019000123")
        self.assertEqual(self.portfolio()["cash"], 13157.0)
        self.assertIn("max loss ₹413.82", run(["log", "--state", self.state, "--now", NOW])[1])

    def test_flatten_by_1510(self):
        self.run_risk()
        run(self.args("approve", "--index", "1", "--reply", "yes", "--portfolio", self.pf))
        late = "2026-10-19 15:10"
        code, text, _ = run(["flatten", "--symbol", "RELIANCE", "--orders", self.orders, "--portfolio", self.pf,
                             "--plan", PLAN_PATH, "--config", self.cfg, "--now", late])
        self.assertEqual(code, 0)
        self.assertIn("SELL 30 shares of NSE:RELIANCE, market, intraday (MIS), good for today (flat by 15:10 IST)",
                      text)
        self.assertIn("replaced 1 earlier proposal", text)
        self.assertIn("cancel_order", text)
        with open(self.orders) as fh:
            o = json.load(fh)["orders"][0]
        self.assertEqual(ticket.tool_input(o), {"variety": "regular", "exchange": "NSE", "tradingsymbol": "RELIANCE",
                                                "transaction_type": "SELL", "quantity": 30, "product": "MIS",
                                                "order_type": "MARKET", "validity": "DAY", "tag": "indiaflatten"})
        self.assertEqual(self.run_risk(now=late), 0)          # after the 14:45 entry cutoff: an exit still passes
        with open(self.risk) as fh:
            self.assertEqual(json.load(fh)["results"][0]["kind"], "exit")
        self.assertEqual(run(self.args("approve", "--index", "1", "--reply", "yes sell 30 RELIANCE",
                                       "--fill-price", "1236.40", "--portfolio", self.pf, now=late))[0], 0)
        pf = self.portfolio()
        self.assertEqual((pf["cash"], pf["positions"]), (50258.0, []))
        self.assertEqual(self.run_risk(now="2026-10-19 15:26"), 1)    # Zerodha's square-off has begun
        code, _, err = run(["flatten", "--symbol", "RELIANCE", "--orders", self.orders, "--portfolio", self.pf,
                            "--now", late])
        self.assertEqual(code, 1)
        self.assertIn("nothing to flatten", err)

    def test_flatten_uses_the_symbols_square_off(self):
        base = ["--qty", "30", "--orders", self.orders, "--portfolio", self.none, "--plan", self.none,
                "--config", self.cfg, "--now", "2026-10-19 14:55"]
        code, text, _ = run(["flatten", "--symbol", "RELIANCE", "--fo-list", FO_PATH] + base)
        self.assertEqual(code, 0)
        self.assertIn("(flat by 15:00 IST)", text)                 # "auto" for a CAS stock
        self.assertIn("Zerodha squares off MIS at 15:12", text)
        with open(self.orders) as fh:
            o = json.load(fh)["orders"][0]
        self.assertIn("before Zerodha's 15:12 MIS square-off", o["why"])
        self.assertEqual((o["plan"]["cas"], o["plan"]["square_off"]), (True, "15:12"))
        self.assertNotIn("plan", ticket.tool_input(o))
        code, text, _ = run(["flatten", "--symbol", "20MICRONS", "--fo-list", FO_PATH] + base)
        self.assertIn("(flat by 15:10 IST)", text)
        self.assertIn("squares off MIS at 15:25", text)
        code, text, _ = run(["flatten", "--symbol", "20MICRONS", "--fo-list", self.none] + base)
        self.assertIn("unknown (no F&O list): the stricter CAS rules apply", text)

    def test_the_ticket_shows_the_kite_last_price(self):
        self.run_risk()
        code, text, _ = run(self.args("show", "--index", "1", "--kite-ltp", KITE_LTP))
        self.assertIn("last price 1190.00 (Kite, live, get_ltp)", text)
        self.live()
        code, text, _ = run(self.args("show", "--index", "1"))
        self.assertIn("last price: NOT FETCHED. Before this live order, call get_ltp (read only) with "
                      "[\"NSE:RELIANCE\"]", text)
        out = json.loads(run(self.args("show", "--index", "1", "--json", "--price", "RELIANCE=1228.1"))[1])
        self.assertEqual((out["last_price"], out["last_price_source"]), (1228.1, "Kite, live (get_ltp)"))

    def test_paper_fill_at_the_kite_price(self):
        self.run_risk()
        code, text, _ = run(self.args("approve", "--index", "1", "--reply", "yes", "--portfolio", self.pf,
                                      "--price", "NSE:RELIANCE=1228.1"))
        self.assertEqual(code, 0)
        self.assertIn("simulated fill, BUY 30 RELIANCE at 1228.10 (Kite, live)", text)
        self.assertEqual(self.portfolio()["cash"], 13157.0)

    def test_an_edited_order_is_not_ready(self):
        self.run_risk()
        with open(self.orders, encoding="utf-8") as fh:
            doc = json.load(fh)
        doc["orders"][0]["exit_plan"]["stop_leg"]["trigger_price"] = 1200.0
        with open(self.orders, "w", encoding="utf-8") as fh:
            json.dump(doc, fh)
        code, text, _ = run(self.args("show", "--index", "1"))
        self.assertEqual(code, 1)
        self.assertIn("was not in the last risk check", text)

    def test_yesterdays_check_does_not_count(self):
        self.run_risk(now="2026-10-16 09:50")
        self.assertIn("not today", run(self.args("show", "--index", "1"))[1])

    def test_blocked_orders_are_never_ready(self):
        self.assertEqual(self.run_risk(now="2026-10-19 09:25"), 1)
        code, text, _ = run(self.args("show", "--index", "1", now="2026-10-19 09:25"))
        self.assertIn("BLOCKED by india-risk: window: no new entries before 09:30", text)
        self.assertEqual(run(self.args("approve", "--index", "1", "--reply", "yes", "--portfolio", self.pf,
                                       now="2026-10-19 09:25"))[0], 1)

    def test_decline_is_logged(self):
        self.run_risk()
        code, _, _ = run(self.args("decline", "--index", "1", "--reply", "no thanks"))
        self.assertEqual(code, 0)
        self.assertIn("DONE: declined", run(self.args("show", "--index", "1"))[1])

    def test_fill_sell_relieves_the_oldest_lot(self):
        pf = {"cash": 0, "positions": [{"ticker": "TCS", "shares": 3, "lots": [
            {"id": "TCS-1", "date": "2025-01-02", "shares": 2, "cost": 6000.0},
            {"id": "TCS-2", "date": "2026-01-02", "shares": 1, "cost": 3100.0}]}]}
        with open(self.pf, "w") as fh:
            json.dump(pf, fh)
        code, _, _ = run(["fill", "--symbol", "TCS", "--side", "sell", "--qty", "2", "--price", "3050",
                          "--date", TODAY, "--portfolio", self.pf, "--config", self.cfg, "--state", self.state])
        self.assertEqual(code, 0)
        pf = self.portfolio()
        self.assertEqual(pf["cash"], 6100.0)
        self.assertEqual([l["id"] for l in pf["positions"][0]["lots"]], ["TCS-2"])
        code, _, err = run(["fill", "--symbol", "TCS", "--side", "sell", "--qty", "1.5", "--price", "3050",
                            "--portfolio", self.pf, "--config", self.cfg, "--state", self.state])
        self.assertEqual(code, 1)
        self.assertNotIn("Traceback", err)


if __name__ == "__main__":
    unittest.main()
