import datetime
import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stdout, redirect_stderr

from _load import load, ROOT, FIX

check = load("india-risk", "check")
ticket = load("india-broker", "ticket")

CFG_PATH = os.path.join(ROOT, "india-budget", "config.example.json")
ORDERS_PATH = os.path.join(FIX, "india-risk", "orders-reliance.json")
SEC_PATH = os.path.join(FIX, "india-risk", "sec_list-2026-10-23-cut.csv")
HOL_PATH = os.path.join(FIX, "india-risk", "nse-holidays.json")
NOW = "2026-10-19 09:50"
TODAY = "2026-10-19"
QUOTE_TIME = datetime.datetime(2026, 10, 19, 4, 52, 0)     # 10:22 IST


def at(text):
    return check.parse_now(text)


def cfg(**over):
    with open(CFG_PATH) as fh:
        c = json.load(fh)
    c.update(over)
    return c


def rel(**over):
    with open(ORDERS_PATH, encoding="utf-8") as fh:
        o = json.load(fh)["orders"][0]
    o.update(over)
    return o


def with_qty(o, q):
    o = json.loads(json.dumps(o))
    o["quantity"] = q
    for leg in ("stop_leg", "target_leg"):
        o["exit_plan"][leg]["quantity"] = q
    return o


def entry(sym, qty, price, stop, target, product="MIS"):
    leg = {"variety": "regular", "exchange": "NSE", "tradingsymbol": sym, "transaction_type": "SELL",
           "quantity": qty, "product": product, "validity": "DAY"}
    return {"variety": "regular", "exchange": "NSE", "tradingsymbol": sym, "transaction_type": "BUY",
            "quantity": qty, "product": product, "order_type": "MARKET", "validity": "DAY",
            "exit_plan": {"kind": "sl-m+limit", "stop_leg": dict(leg, order_type="SL-M", trigger_price=stop),
                          "target_leg": dict(leg, order_type="LIMIT", price=target)},
            "plan": {"entry": price, "stop": stop, "target": target}}


def sell(sym="RELIANCE", qty=30, product="MIS"):
    return {"variety": "regular", "exchange": "NSE", "tradingsymbol": sym, "transaction_type": "SELL",
            "quantity": qty, "product": product, "order_type": "MARKET", "validity": "DAY"}


def placed(sym, side="buy", day=TODAY, max_loss=413.82, kind=None, status="placed"):
    r = {"symbol": sym, "side": side, "status": status, "date": day, "at": day + "T09:55:00+05:30"}
    if max_loss is not None:
        r["max_loss"] = max_loss
    if kind:
        r["kind"] = kind
    return r


def verdicts(res):
    return [r["verdict"] for r in res["results"]]


def reasons(res, i=0):
    return " | ".join(res["results"][i]["reasons"])


def warnings(res, i=0):
    return " | ".join(res["results"][i]["warnings"])


SEC = check.load_sec_list(SEC_PATH)
SHARED = os.path.join(FIX, "shared")
FO_PATH = os.path.join(SHARED, "fo_mktlots-2026-10-23.csv")
FO = check.load_fo_list(FO_PATH)[0]
KITE_LTP = os.path.join(SHARED, "kite-ltp-2026-10-23.json")
KITE_INST = os.path.join(SHARED, "kite-instruments-RELIANCE-2026-10-23.json")


class TheExample(unittest.TestCase):
    def test_the_reliance_order_passes_every_check(self):
        res = check.check([rel()], cfg(), now=at(NOW), sec_list=SEC, fo=FO)
        r = res["results"][0]
        self.assertEqual(r["verdict"], "PASS", r["reasons"])
        self.assertEqual((r["cas"], r["square_off"], r["session_close"], r["price_source"]),
                         (True, "15:12", "15:15", "plan"))
        self.assertEqual((r["reasons"], r["warnings"]), ([], []))
        self.assertEqual((r["kind"], r["entry_ref"], r["stop"], r["target"]), ("entry", 1227.8, 1215.3, 1240.3))
        self.assertEqual((r["risk_per_share"], r["charges"], r["max_loss"], r["reward_risk"]),
                         (12.5, 38.82, 413.82, 1.0))
        self.assertEqual((r["value_inr"], r["series"]), (36834.0, "EQ"))
        self.assertEqual(res["cash_after"], 13166.0)
        self.assertEqual((res["date"], res["paper"], res["warnings"]), (TODAY, True, []))

    def test_37_shares_is_blocked_by_the_max_loss_with_charges(self):
        roomy = cfg(max_position_pct=100, max_order_inr=100000, cash_buffer_pct=0)
        res = check.check([with_qty(rel(), 36)], roomy, now=at(NOW), sec_list=SEC)
        self.assertEqual(verdicts(res), ["PASS"])
        self.assertEqual(res["results"][0]["max_loss"], 496.58)
        res = check.check([with_qty(rel(), 37)], roomy, now=at(NOW), sec_list=SEC)
        self.assertEqual(res["results"][0]["reasons"],
                         ["loss: 37 shares x 12.50 risk a share = ₹462.50 + ₹47.88 charges = ₹510.38 at the stop, "
                          "over your ₹500 max loss per trade"])

    def test_fingerprint_is_shared_with_the_broker(self):
        o = rel()
        self.assertEqual(check.fingerprint(o), ticket.fingerprint(o))
        self.assertEqual(check.fingerprint(dict(o, why="anything", plan={})), check.fingerprint(o))
        self.assertNotEqual(check.fingerprint(dict(o, quantity=31)), check.fingerprint(o))
        moved = json.loads(json.dumps(o))
        moved["exit_plan"]["stop_leg"]["trigger_price"] = 1210.0
        self.assertNotEqual(check.fingerprint(moved), check.fingerprint(o))
        self.assertEqual((check.KITE_FIELDS, check.GTT_FIELDS), (ticket.KITE_FIELDS, ticket.GTT_FIELDS))

    def test_cli(self):
        with tempfile.TemporaryDirectory() as d:
            out = os.path.join(d, "state", "risk.json")
            none = os.path.join(d, "none")
            buf = io.StringIO()
            with redirect_stdout(buf):
                code = check.main(["--orders", ORDERS_PATH, "--config", CFG_PATH, "--portfolio", none,
                                   "--prices", none, "--state", none, "--sec-list", SEC_PATH,
                                   "--holidays", HOL_PATH, "--now", NOW, "--out", out])
            text = buf.getvalue()
            self.assertEqual(code, 0)
            self.assertIn("PASS  #1 BUY NSE:RELIANCE 30 shares MIS, entry", text)
            self.assertIn("entry 1227.80 (the plan's entry), stop 1215.30, target 1240.30: risk 12.50 a share", text)
            self.assertIn("max loss ₹413.82 incl. ₹38.82 charges (limit ₹500), reward:risk 1.00 (min 1)", text)
            self.assertIn("1 passed, 0 blocked at 2026-10-19 09:50 IST", text)
            self.assertIn("not financial advice", text)
            with open(out, encoding="utf-8") as fh:
                rec = json.load(fh)
            self.assertEqual(rec["date"], TODAY)
            self.assertEqual(rec["results"][0]["fingerprint"], check.fingerprint(rel()))


class DayTradeChecks(unittest.TestCase):
    def one(self, order, now=NOW, **over):
        return check.check([order], cfg(**over), now=at(now), sec_list=SEC)

    def test_a_stop_is_required(self):
        o = rel()
        o.pop("exit_plan")
        o["plan"] = {"entry": 1227.8}
        self.assertIn("stop: no stop", reasons(self.one(o)))
        res = self.one(o, require_stop=False, min_reward_risk=0)
        self.assertEqual(verdicts(res), ["PASS"])
        self.assertIn("no stop", warnings(res))

    def test_a_plan_only_stop_counts_but_warns(self):
        o = rel()
        o.pop("exit_plan")
        res = self.one(o)
        self.assertEqual(verdicts(res), ["PASS"])
        self.assertIn("in the plan only", warnings(res))

    def test_stop_target_and_reward_risk(self):
        o = rel()
        o["exit_plan"]["stop_leg"]["trigger_price"] = 1230.0
        self.assertIn("must be below the entry 1227.80", reasons(self.one(o)))
        o = rel()
        o["exit_plan"]["target_leg"]["price"] = 1235.0
        self.assertIn("reward:risk: 0.58", reasons(self.one(o)))
        self.assertEqual(verdicts(self.one(o, min_reward_risk=0.5)), ["PASS"])

    def test_a_fresh_higher_price_is_the_entry(self):
        res = check.check([rel()], cfg(), prices={"RELIANCE": 1233.84}, now=at("2026-10-19 10:25"),
                          price_times={"RELIANCE": QUOTE_TIME}, sec_list=SEC)
        r = res["results"][0]
        self.assertEqual((r["entry_ref"], r["entry_ref_from"], r["risk_per_share"]), (1233.8, "price", 18.5))
        self.assertIn("reward:risk: 0.35", reasons(res))
        stale = check.check([rel()], cfg(), prices={"RELIANCE": 1233.84}, now=at("2026-10-19 11:00"),
                            price_times={"RELIANCE": QUOTE_TIME}, sec_list=SEC)
        self.assertEqual(verdicts(stale), ["PASS"])
        self.assertIn("older than 15 minutes", warnings(stale))

    def test_daily_loss_and_trades_per_day(self):
        two = {"orders": [placed("TCS"), placed("INFY")]}
        res = check.check([rel()], cfg(), state=two, now=at(NOW), sec_list=SEC)
        self.assertEqual(verdicts(res), ["PASS"])
        self.assertIn("today before this: 2 of 3 trades, ₹827.64 of ₹1,500 daily loss", res["results"][0]["info"][-1])
        three = {"orders": [placed("TCS"), placed("INFY"), placed("SBIN")]}
        why = reasons(check.check([rel()], cfg(), state=three, now=at(NOW), sec_list=SEC))
        self.assertIn("trades: 3 entries placed today", why)
        self.assertIn("daily loss: ₹1,241.46 already at risk today plus ₹413.82 for this one = ₹1,655.28", why)
        yesterday = {"orders": [placed(s, day="2026-10-16") for s in ("TCS", "INFY", "SBIN")]}
        self.assertEqual(verdicts(check.check([rel()], cfg(), state=yesterday, now=at(NOW), sec_list=SEC)), ["PASS"])

    def test_the_batch_counts_toward_the_daily_limits(self):
        orders = [entry(s, 1, 100.0, 90.0, 120.0) for s in ("SBIN", "INFY", "TCS", "ITC")]
        res = check.check(orders, cfg(), now=at(NOW))
        self.assertEqual(verdicts(res), ["PASS", "PASS", "PASS", "BLOCK"])
        self.assertIn("trades: 0 entries placed today plus 3 passed earlier", reasons(res, 3))

    def test_entry_window_ist(self):
        cases = [("2026-10-19 09:29", "no new entries before 09:30 IST (it is 09:29)"),
                 ("2026-10-19 14:46", "no new entries after 14:45 IST (it is 14:46)"),
                 ("2026-10-19 09:10", "the market is closed (Mon 2026-10-19 09:10"),
                 ("2026-10-19 15:35", "the market is closed"),
                 ("2026-10-18 10:15", "the market is closed today (weekend"),
                 ("2026-10-20 10:15", "the market is closed today (NSE holiday: Dussehra"),
                 ("2026-11-10 10:15", "the market is closed today (NSE holiday: Diwali-Balipratipada"),
                 ("2026-10-19T03:50:00Z", "no new entries before 09:30 IST (it is 09:20)")]
        for now, want in cases:
            self.assertIn("window: " + want, reasons(self.one(rel(), now=now)), now)
        for now in ("2026-10-19 09:30", "2026-10-19 14:45", "2026-10-19T04:20:00Z", "2026-10-19T09:50:00+05:30"):
            self.assertEqual(verdicts(self.one(rel(), now=now)), ["PASS"], now)
        self.assertEqual(verdicts(self.one(rel(), now="2026-10-18 10:15", no_entries_before=None,
                                           no_entries_after=None)), ["PASS"])
        with self.assertRaises(check.RiskError):
            self.one(rel(), no_entries_before="9.30am")
        self.assertIn("not published", " ".join(self.one(rel(), now="2027-01-04 10:00")["warnings"]))

    def test_ist_is_fixed_utc_plus_0530(self):
        self.assertEqual(at("2026-10-19 09:50"), datetime.datetime(2026, 10, 19, 4, 20))
        self.assertEqual(check.to_ist(at("2026-12-01 09:50")), datetime.datetime(2026, 12, 1, 9, 50))

    def test_holidays_file(self):
        with open(HOL_PATH) as fh:
            hol = check.load_holidays(json.load(fh))
        self.assertEqual(len(hol), 20)
        self.assertEqual(hol["2026-10-20"], "Dussehra")
        self.assertEqual(check.load_holidays(["2027-01-26"]), {"2027-01-26": "NSE holiday"})
        res = check.check([rel()], cfg(), now=at("2027-01-26 10:00"), sec_list=SEC,
                          holidays={"2027-01-26": "Republic Day"})
        self.assertIn("NSE holiday: Republic Day", reasons(res))


class IndiaChecks(unittest.TestCase):
    def test_mis_square_off_per_symbol_blocks_even_an_exit(self):
        # RELIANCE has F&O: a closing-auction (CAS) stock since 2026-08-03, Zerodha squares MIS off at 15:12
        pf = {"cash": 13166.0, "positions": [{"ticker": "RELIANCE", "shares": 30}, {"ticker": "20MICRONS", "shares": 10}]}
        res = check.check([sell()], cfg(), portfolio=pf, now=at("2026-10-19 15:12"), fo=FO)
        self.assertEqual(res["results"][0]["kind"], "exit")
        self.assertIn("square-off: it is 15:12 IST, at or after Zerodha's 15:12 MIS auto square-off for RELIANCE",
                      reasons(res))
        self.assertEqual(verdicts(check.check([sell()], cfg(), portfolio=pf, now=at("2026-10-19 15:11"), fo=FO)),
                         ["PASS"])
        # 20MICRONS has no F&O: trades to 15:30, square-off 15:25
        small = sell("20MICRONS", 10)
        self.assertEqual(verdicts(check.check([small], cfg(), portfolio=pf, now=at("2026-10-19 15:24"), fo=FO,
                                              prices={"20MICRONS": 220.0})), ["PASS"])
        self.assertIn("for 20MICRONS (non-CAS", reasons(check.check([small], cfg(), portfolio=pf,
                                                                    now=at("2026-10-19 15:25"), fo=FO)))
        # unknown CAS status (no F&O list): the strict 15:12, and a warning that says so
        res = check.check([small], cfg(), portfolio=pf, now=at("2026-10-19 15:13"), prices={"20MICRONS": 220.0})
        self.assertIn("square-off: it is 15:13 IST, at or after Zerodha's 15:12", reasons(res))
        self.assertIn("CAS status of 20MICRONS unknown", warnings(res))
        self.assertEqual(verdicts(check.check([small], cfg(), portfolio=pf, now=at("2026-10-19 15:13"),
                                              prices={"20MICRONS": 220.0}, cas_flag="no")), ["PASS"])
        # before CAS began every stock squared off at 15:25
        self.assertEqual(verdicts(check.check([sell()], cfg(), portfolio=pf, now=at("2026-07-31 15:20"), fo=FO,
                                              prices={"RELIANCE": 1400.0})), ["PASS"])
        self.assertEqual(verdicts(check.check([sell(product="CNC")], cfg(), portfolio=pf,
                                              now=at("2026-10-19 15:27"), fo=FO)), ["PASS"])

    def test_flat_by_at_or_after_the_square_off_blocks_an_mis_entry(self):
        o = rel()
        o["plan"]["flat_by"] = "15:15"
        self.assertIn("flat_by: 15:15 is at or after Zerodha's 15:12 MIS square-off for RELIANCE",
                      reasons(check.check([o], cfg(), now=at(NOW), fo=FO)))
        self.assertEqual(verdicts(check.check([o], cfg(), now=at(NOW), fo=FO, cas_flag="no")), ["PASS"])
        o["plan"]["flat_by"] = "auto"
        self.assertEqual(verdicts(check.check([o], cfg(), now=at(NOW), fo=FO)), ["PASS"])
        self.assertIn("flat_by: 15:20", reasons(check.check([dict(rel(), plan=dict(rel()["plan"], flat_by=None))],
                                                            cfg(flat_by="15:20"), now=at(NOW), fo=FO)))

    def test_no_entries_after_the_cas_close_even_without_a_window(self):
        res = check.check([rel()], cfg(no_entries_before=None, no_entries_after=None), now=at("2026-10-19 15:16"),
                          fo=FO, cas_flag=None)
        self.assertIn("continuous trading in RELIANCE ended at 15:15 IST", reasons(res))

    def test_kite_ltp_is_the_fresh_price_and_says_so(self):
        kite = {"NSE:RELIANCE": {"price": 1233.84}}
        res = check.check([rel()], cfg(), now=at("2026-10-19 10:25"), kite_ltp=kite, fo=FO)
        r = res["results"][0]
        self.assertEqual((r["entry_ref"], r["price_source"]), (1233.8, "kite"))
        self.assertIn("RELIANCE last 1233.84 (Kite, live) is above the plan's entry 1227.80", " ".join(r["info"]))
        self.assertIn("entry 1233.80 (the latest price, Kite, live)", " ".join(r["info"]))
        lower = check.check([rel()], cfg(), now=at(NOW), kite_ltp=check.load_kite_ltp(json.load(open(KITE_LTP))),
                            fo=FO)
        self.assertEqual(verdicts(lower), ["PASS"])
        self.assertIn("RELIANCE last 1190.00 (Kite, live) is not above", " ".join(lower["results"][0]["info"]))
        free = check.check([rel()], cfg(), prices={"RELIANCE": 1233.84}, now=at(NOW), fo=FO)
        self.assertIn("(Yahoo, free, can lag)", " ".join(free["results"][0]["info"]))

    def test_kite_circuit_limits_for_today(self):
        table, day = check.load_kite_instrument(KITE_INST)
        near = entry("RELIANCE", 10, 1295.0, 1285.0, 1310.0)
        res = check.check([near], cfg(), now=at("2026-10-23 10:00"), sec_list=SEC, fo=FO, kite_inst=table,
                          kite_day=day)
        self.assertIn("circuit: the entry 1295.00 is within 1% of today's upper circuit 1302.90 (Kite instrument "
                      "limits for 2026-10-23 1066.10-1302.90)", reasons(res))
        ok = entry("RELIANCE", 10, 1195.0, 1185.0, 1205.0)
        res = check.check([ok], cfg(), now=at("2026-10-23 10:00"), sec_list=SEC, fo=FO, kite_inst=table, kite_day=day)
        self.assertEqual(verdicts(res), ["PASS"], reasons(res))
        self.assertEqual(res["results"][0]["circuit_source"], "Kite instrument limits for 2026-10-23")
        # another day: the limits are not used, and it says so
        res = check.check([near], cfg(), now=at("2026-10-19 10:00"), sec_list=SEC, fo=FO, kite_inst=table,
                          kite_day=day)
        self.assertNotIn("circuit:", reasons(res))
        self.assertIn("its circuit limits are for one day only", " ".join(res["warnings"]))

    def test_trade_to_trade_series_is_blocked_for_mis(self):
        o = entry("ABAN", 10, 30.0, 28.0, 34.0)
        res = check.check([o], cfg(), now=at(NOW), sec_list=SEC)
        self.assertIn("series: ABAN is series BE (trade-to-trade): no intraday (MIS)", reasons(res))
        cnc = entry("ABAN", 10, 30.0, 28.0, 34.0, product="CNC")
        cnc["exit_plan"] = {"kind": "gtt-oco", "gtt": {
            "exchange": "NSE", "tradingsymbol": "ABAN", "last_price": None, "transaction_type": "SELL",
            "product": "CNC", "trigger_type": "two-leg", "upper_trigger_value": 34.0, "upper_quantity": 10,
            "upper_limit_price": 34.0, "lower_trigger_value": 28.0, "lower_quantity": 10, "lower_limit_price": 27.72}}
        self.assertNotIn("series:", reasons(check.check([cnc], cfg(), now=at(NOW), sec_list=SEC)))
        self.assertIn("series: X is series BZ", reasons(check.check([dict(entry("X", 1, 30.0, 28.0, 34.0),
                                                                          plan={"entry": 30.0, "stop": 28.0,
                                                                                "target": 34.0, "series": "BZ"})],
                                                                    cfg(), now=at(NOW))))

    def test_near_the_upper_circuit_is_blocked(self):
        # 20MICRONS has a 20% band; previous close 200.00 -> upper circuit 240.00
        o = entry("20MICRONS", 10, 238.0, 230.0, 250.0)
        res = check.check([o], cfg(min_reward_risk=0), now=at(NOW), sec_list=SEC, prev_close={"20MICRONS": 200.0})
        self.assertIn("circuit: the entry 238.00 is within 1% of the upper circuit 240.00 (20% band on the 200.00 "
                      "previous close)", reasons(res))
        ok = entry("20MICRONS", 10, 220.0, 212.0, 230.0)
        res = check.check([ok], cfg(), now=at(NOW), sec_list=SEC, prev_close={"20MICRONS": 200.0})
        self.assertEqual(verdicts(res), ["PASS"], reasons(res))
        self.assertIn("circuit 160.00 to 240.00 (NSE sec_list 20% band on the 200.00 previous close)",
                      " ".join(res["results"][0]["info"]))
        low = entry("20MICRONS", 10, 170.0, 158.0, 190.0)
        res = check.check([low], cfg(), now=at(NOW), sec_list=SEC, prev_close={"20MICRONS": 200.0})
        self.assertIn("below the lower circuit 160.00", warnings(res))
        res = check.check([ok], cfg(), now=at(NOW), sec_list=SEC)
        self.assertIn("no previous close", warnings(res))
        self.assertEqual(SEC["RELIANCE"]["band"], None)        # "No Band": no circuit for F&O stocks

    def test_kite_format(self):
        def one(o):
            return reasons(check.check([o], cfg(), now=at(NOW)))
        self.assertIn("not on the tick (0.1)", one(rel(order_type="LIMIT", price=1227.83)))
        self.assertIn("needs a price", one(rel(order_type="LIMIT")))
        self.assertIn("needs a trigger_price", one(rel(order_type="SL-M")))
        self.assertIn("whole number of shares", one(with_qty(rel(), 2.5)))
        self.assertIn("product NRML is margin", one(rel(product="NRML")))
        self.assertIn("derivatives exchange", one(rel(exchange="NFO")))
        self.assertIn("future or an option", one(rel(tradingsymbol="RELIANCE26OCT1300CE")))
        self.assertIn("tag must be", one(rel(tag="india budget order #1 too long")))
        self.assertIn("validity must be", one(rel(validity="GTC")))
        o = rel()
        o["exit_plan"]["stop_leg"]["quantity"] = 29
        self.assertIn("does not match the entry quantity 30", one(o))
        o = rel()
        o["exit_plan"]["target_leg"]["product"] = "CNC"
        self.assertIn("does not match the entry's MIS", one(o))
        self.assertEqual(verdicts(check.check([rel(order_type="LIMIT", price=1227.8)], cfg(), now=at(NOW))),
                         ["PASS"])
        self.assertIn("as a number", warnings(check.check([rel(quantity="30")], cfg(), now=at(NOW))))

    def test_margin_is_blocked(self):
        res = check.check([rel()], cfg(cash_buffer_pct=0, max_position_pct=100),
                          portfolio={"cash": 20000, "positions": []}, now=at(NOW))
        self.assertIn("that would be margin", reasons(res))


class Exits(unittest.TestCase):
    PF = {"cash": 13166.0, "positions": [{"ticker": "RELIANCE", "shares": 30,
                                           "lots": [{"date": TODAY, "shares": 30, "cost": 36834.0}]}]}

    def test_getting_out_is_never_blocked_by_the_day_limits(self):
        full = {"orders": [placed("RELIANCE"), placed("TCS"), placed("INFY")] +
                          [placed("T%d" % i, max_loss=1) for i in range(6)] +
                          [placed("RELIANCE", side="sell", kind="exit", max_loss=None)]}
        c = cfg(blocked=["RELIANCE"], max_order_inr=100, max_daily_loss_inr=50)
        res = check.check([sell()], c, portfolio=self.PF, state=full, now=at("2026-10-19 15:10"))
        r = res["results"][0]
        self.assertEqual((r["verdict"], r["kind"]), ("PASS", "exit"), r["reasons"])
        for word in ("blocked:", "duplicate:", "daily limit:"):
            self.assertIn("exit, not blocked: " + word, warnings(res))
        self.assertEqual(verdicts(check.check([sell(product="CNC")], cfg(), portfolio=self.PF,
                                              now=at("2026-10-18 12:00"))), ["PASS"])

    def test_short_still_applies_to_a_sell(self):
        res = check.check([sell(qty=31)], cfg(), portfolio=self.PF, now=at(NOW))
        self.assertIn("short: selling more RELIANCE than you hold (30 shares)", reasons(res))
        self.assertIn("record the fill first", reasons(check.check([sell()], cfg(), now=at(NOW))))

    def test_buy_then_sell_in_one_batch(self):
        res = check.check([rel(), sell()], cfg(), now=at(NOW))
        self.assertEqual(verdicts(res), ["PASS", "PASS"])
        self.assertEqual(res["results"][1]["kind"], "exit")


class Cli(unittest.TestCase):
    def test_prices_file_with_prev_close_and_freshness(self):
        with tempfile.TemporaryDirectory() as d:
            pp = os.path.join(d, "prices.json")
            with open(pp, "w") as fh:
                json.dump({"as_of": "2026-10-19T04:53:00Z", "prices": {"NSE:RELIANCE": 1233.84},
                           "detail": {"NSE:RELIANCE": {"time": "2026-10-19T04:52:00Z", "prev_close": 1184.5}}}, fh)
            none = os.path.join(d, "none")
            base = ["--orders", ORDERS_PATH, "--config", CFG_PATH, "--portfolio", none, "--prices", pp,
                    "--state", none, "--sec-list", SEC_PATH, "--holidays", HOL_PATH, "--no-write"]
            buf = io.StringIO()
            with redirect_stdout(buf):
                self.assertEqual(check.main(base + ["--now", "2026-10-19 10:25"]), 1)
            self.assertIn("reward:risk: 0.35", buf.getvalue())
            with redirect_stdout(io.StringIO()):
                self.assertEqual(check.main(base + ["--now", "2026-10-19 11:00"]), 0)

    def test_kite_files_on_the_command_line(self):
        with tempfile.TemporaryDirectory() as d:
            none = os.path.join(d, "none")
            base = ["--orders", ORDERS_PATH, "--config", CFG_PATH, "--portfolio", none, "--prices", none,
                    "--state", none, "--sec-list", SEC_PATH, "--holidays", HOL_PATH, "--fo-list", FO_PATH,
                    "--no-write", "--json"]
            buf = io.StringIO()
            with redirect_stdout(buf):
                self.assertEqual(check.main(base + ["--now", "2026-10-19 10:25", "--price", "RELIANCE=1233.84"]), 1)
            r = json.loads(buf.getvalue())["results"][0]
            self.assertEqual((r["price_source"], r["square_off"]), ("kite", "15:12"))
            buf = io.StringIO()
            with redirect_stdout(buf):
                self.assertEqual(check.main(base + ["--now", NOW, "--kite-ltp", KITE_LTP,
                                                    "--kite-instrument", KITE_INST]), 0)
            self.assertIn("not today", " ".join(json.loads(buf.getvalue())["warnings"]))

    def test_errors_are_one_line(self):
        err = io.StringIO()
        with redirect_stderr(err):
            self.assertEqual(check.main(["--orders", "/nonexistent.json", "--no-write"]), 1)
        self.assertIn("no orders file", err.getvalue())
        err = io.StringIO()
        with redirect_stderr(err):
            self.assertEqual(check.main(["--orders", ORDERS_PATH, "--now", "noon", "--no-write"]), 1)
        self.assertIn("--now wants", err.getvalue())
        self.assertNotIn("Traceback", err.getvalue())


if __name__ == "__main__":
    unittest.main()
