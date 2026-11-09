---
name: india-alerts
description: >-
  Price Alerts for NSE and BSE. Rings when a price, a stop or the opening bell
  hits. Turns the trade plan into three alerts in one go (the stop, the target
  and "flat by" at your flat-by time: 15:00 for F&O (closing-auction) stocks,
  15:10 for others), plus above / below /
  stop / target levels, percent moves from the previous close, upper and lower
  circuit alerts (NSE band or Kite's official limits), any time of day, the
  09:00 pre-open, the 09:15 opening bell and a reminder 3 minutes before
  Zerodha's MIS auto square-off (15:12 for CAS stocks, 15:25 for others), each
  ringing once so you are not spammed. Knows NSE hours in IST including the
  closing auction (CAS stocks trade to 15:15) and the new pre-open, the NSE holiday
  list (live from NSE, 2026 built in, Muhurat trading noted), fetches free
  delayed Yahoo quotes (.NS / .BO) or uses live prices Claude pulls from the
  Zerodha Kite MCP, and writes trader-in/prices.json for sizing and the risk
  check. Use when the user says "alert me when", "tell me if RELIANCE hits",
  "watch my stop", "watch my trade", "remind me to square off", "is the market
  open", "is tomorrow a holiday", "when does NSE open", "get me a price", or
  right after /india-exit writes a plan.
argument-hint: "[symbol] [price]"
---

# india-alerts

Price Alerts watches the numbers so the user does not have to stare at a
chart. An alert is information, never an order: when a stop alert rings,
nothing is sold by this skill. Zerodha has no bracket orders, so the stop
(SL-M) and target (LIMIT) only sit at the broker if the user placed them after
the entry filled (for CNC, one GTT OCO); the alerts tell the user what happened
and when it is time to be flat.

`${CLAUDE_SKILL_DIR}` below is this skill's folder (where this SKILL.md lives).

```bash
python3 "${CLAUDE_SKILL_DIR}/alerts.py" clock                          # NSE open or closed, next open
python3 "${CLAUDE_SKILL_DIR}/alerts.py" holidays                       # this year's NSE holidays
python3 "${CLAUDE_SKILL_DIR}/alerts.py" add --from-plan                # stop, target, flat by from trader-in/plan.json
python3 "${CLAUDE_SKILL_DIR}/alerts.py" add --pre-open                 # 09:00
python3 "${CLAUDE_SKILL_DIR}/alerts.py" add --opening-bell             # 09:15
python3 "${CLAUDE_SKILL_DIR}/alerts.py" clock --symbol RELIANCE        # that stock's close and square-off
python3 "${CLAUDE_SKILL_DIR}/alerts.py" add --mis-reminder --symbol RELIANCE   # 15:09 (CAS); 15:22 non-CAS
python3 "${CLAUDE_SKILL_DIR}/alerts.py" add --symbol RELIANCE --stop 1215.30
python3 "${CLAUDE_SKILL_DIR}/alerts.py" add --symbol RELIANCE --target 1240.30
python3 "${CLAUDE_SKILL_DIR}/alerts.py" add --at 15:10 --label "flat by" --symbol RELIANCE
python3 "${CLAUDE_SKILL_DIR}/alerts.py" add --symbol TCS --pct 3
python3 "${CLAUDE_SKILL_DIR}/alerts.py" add --symbol RPOWER --circuit  # within 1% of a circuit
python3 "${CLAUDE_SKILL_DIR}/alerts.py" check                          # exit 0 = something rang
python3 "${CLAUDE_SKILL_DIR}/alerts.py" check --price RELIANCE=1227.8  # a Kite price pushed in
python3 "${CLAUDE_SKILL_DIR}/alerts.py" check --kite-ltp trader-in/kite/ltp-2026-10-23.json \
    --kite-instrument trader-in/kite/RELIANCE-instrument-2026-10-23.json
python3 "${CLAUDE_SKILL_DIR}/alerts.py" quote RELIANCE --write         # -> trader-in/prices.json
python3 "${CLAUDE_SKILL_DIR}/alerts.py" quote --holdings --write       # portfolio + plan symbols
python3 "${CLAUDE_SKILL_DIR}/alerts.py" list
python3 "${CLAUDE_SKILL_DIR}/alerts.py" reset reliance-plan-stop
```

`--now "2026-10-19 15:10"` pins the clock (IST); `--offline DIR` reads saved
quote, holiday and band files instead of the network.

Symbols: `RELIANCE`, `NSE:RELIANCE` or `RELIANCE.NS` is NSE (the default);
`RELIANCE.BO` or `BSE:RELIANCE` is BSE. `M&M` and `BAJAJ-AUTO` work as typed.
A 6-digit BSE scrip code is understood as BSE, but Yahoo cannot quote it: use
the symbol with `.BO`, or a Kite price.

## What it reads and writes

| file | what |
| --- | --- |
| `trader-in/plan.json` | the trade from india-exit, for `add --from-plan` (`symbol`, `exchange`, `currency`, `tick`, `stop`, `target`, `flat_by`, `product`, `date`) |
| `trader-in/alerts.json` | the rules (`alerts.example.json` in this folder) |
| `trader-in/state/alerts.json` | which rules rang and when, so each rings once |
| `trader-in/prices.json` | the latest prices, merged in, for india-budget and india-risk |
| `trader-in/portfolio.json` | the symbols held, for `quote --holdings` |
| `trader-in/config.json` | `flat_by` when the plan has none (`"auto"` by default), `cas_symbols_extra`, `non_cas_symbols` |
| `trader-in/fo_list.txt` | NSE's F&O stocks (= the CAS stocks), cached for 7 days |
| `trader-in/kite/` | Kite answers Claude saved verbatim (`get_ltp`, `get_quotes`, `search_instruments`) |
| `trader-in/holidays.json` | the NSE holiday list, cached for 7 days |
| `trader-in/sec_list.csv` | NSE's price bands, cached for the day, for circuit alerts |

## Rule types

| type | rings when | rings again |
| --- | --- | --- |
| `above` / `target` | price at or above `price` | after `reset`, or on its own if `"rearm": true` and the price fell back |
| `below` / `stop` | price at or below `price` | same |
| `pct_move` | moved `pct` percent from the previous close (or `ref`), optional `direction` up or down | once per day at most |
| `circuit` | within `within` percent (default 1) of the upper or lower circuit | once per day at most |
| `time` | the IST clock passed `at` ("15:10") on a trading day; `"mis": true` adds the square-off wording | once per trading day |
| `pre_open` | the pre-open session is on (09:00 to 09:15) | once per trading day |
| `opening_bell` | the normal session is open (from 09:15) | once per trading day |

A rule with `"date"` is watched on that IST date only; `--from-plan` rules
carry the plan's date, so yesterday's stop never rings today. Each rule keeps
the plain `symbol` and its `exchange` ("NSE" or "BSE").

Circuit alerts use Kite's official `lower_circuit_limit` / `upper_circuit_limit`
from a `search_instruments` file (`--kite-instrument`) when that file is from
today; those limits win and also cover F&O stocks (RELIANCE 1,066.10 to
1,302.90 on 2026-10-23). Otherwise the band (2, 5, 10 or 20 percent) from NSE's
`sec_list.csv` and the previous close. F&O stocks show "No Band" there, so
without a Kite file the check says it has no circuit for them. A Kite file from
another day is ignored, and the check says so.
A stock in series BE or BZ is trade-to-trade: no intraday trading; the check
flags it.

## Prices

Every price the check prints says where it came from: `(Kite, live)` or
`(Yahoo, free, can lag)`.

1. **Zerodha Kite, pushed in** (preferred, see the next section): `--price
   RELIANCE=1227.8`, `--kite-ltp FILE` (same as `--kite-json`: a verbatim
   `get_ltp`, `get_quotes` or `get_ohlc` answer; `NSE:` wins over `BSE:` for
   the same symbol), or a `trader-in/prices.json` written as
   `{"as_of": "2026-10-23T09:30:00Z", "source": "kite", "prices": {"RELIANCE": 1190}}`,
   used while at most `--max-age` minutes old (default 5). When a pushed price
   has no previous close and a `pct_move` or `circuit` rule needs one, it comes
   from Yahoo (`get_quotes` and `get_ohlc` carry it in `ohlc.close`).
2. **Yahoo chart JSON** (the fallback, always there):
   `query1.finance.yahoo.com/v8/finance/chart/RELIANCE.NS` (`.BO` for BSE),
   unofficial, free and delayed, one request per symbol, half a second apart.
   On a 429 it waits and tries once more; on a 429 or a 5xx after that it
   tries `query2`. It identifies itself as `india-day-trader-skill/1.0` (a
   fake browser name gets HTTP 429).

Nothing fails because Kite is absent. Stooq was dropped (404 for NSE symbols
on 2026-10-23). Each price in `trader-in/prices.json` keeps its source and
quote time, and india-risk only uses one under 15 minutes old.

## With Zerodha Kite connected

The script never calls Kite; Claude does, with read-only tools only, and
passes the answers in. Orders stay in india-broker.

1. **Check the login** with a cheap read: `get_ltp` with
   `instruments: ["NSE:RELIANCE"]`. If it says to log in first, call `login`
   and give the user the link. They sign in on Zerodha's own page; never ask
   for a password, PIN or TOTP.
2. **Calls**: `get_ltp` (or `get_quotes`) with every symbol in today's rules,
   as `NSE:SYMBOL` / `BSE:SYMBOL`. For circuit alerts, `search_instruments`
   with query `NSE:SYMBOL` (it also gives the token, RELIANCE = 738561, and the
   tick size).
3. **Save the answers verbatim** under `trader-in/kite/`, with the date in the
   name: `trader-in/kite/ltp-2026-10-23.json`,
   `trader-in/kite/RELIANCE-instrument-2026-10-23.json`. Circuit limits are
   for that day only; the date in the name is how the check knows.
4. **Pass them**: `check --kite-ltp trader-in/kite/ltp-2026-10-23.json
   --kite-instrument trader-in/kite/RELIANCE-instrument-2026-10-23.json`, or
   for one price `check --price RELIANCE=1190`.

Kite prices are the exchange's, via your broker: prefer them for the trade
itself. Yahoo's can lag.

## The NSE clock (IST, UTC+05:30, no daylight saving)

- **Pre-open** (from 2026-09-07): 09:00-09:05 market and limit orders,
  09:05-09:10 limit orders only, price discovery and matching 09:10-09:12,
  buffer to 09:15. Before that date: orders 09:00-09:07, matching to 09:08.
- **Normal session from 09:15.** Since 2026-08-03 NSE runs a **closing auction
  session (CAS) 15:15-15:35** for stocks with F&O contracts: for those "CAS
  stocks" continuous trading ends at **15:15**. Non-CAS stocks trade to
  **15:30**. Closing session 15:40-16:00. No early closes. `clock` shows both
  closes; `clock --symbol X` shows that stock's.
- The CAS list is NSE's `fo_mktlots.csv` (stock rows, not the NIFTY index
  rows), cached in `trader-in/fo_list.txt` for 7 days. `--cas yes|no` and the
  config lists `cas_symbols_extra` / `non_cas_symbols` override it. If the list
  cannot be read, the stricter CAS times are used and the tool says so. A BSE
  listing follows the NSE status.
- **Zerodha MIS auto square-off**: **15:12** for CAS stocks, **15:25** for
  non-CAS, ₹50 + GST per order it closes. `add --mis-reminder --symbol X` rings
  3 minutes earlier (15:09 or 15:22; without a symbol, 15:09).
- **Flat by** from the plan: its `flat_by`, or `"auto"` = 15:00 for CAS stocks
  and 15:10 for non-CAS. A flat-by at or after the stock's square-off is moved
  to that default, with a warning.
- Holidays come live from the NSE holiday API (segment CM), cached in
  `trader-in/holidays.json`. If NSE blocks the script, the built-in 2026 list
  (read 2026-10-23): Jan 15, Jan 26, Feb 15, Mar 3, Mar 21, Mar 26, Mar 31,
  Apr 3, Apr 14, May 1, May 28, Jun 26, Aug 15, Sep 14, Oct 2, Oct 20
  (Dussehra), Nov 8 (Diwali Laxmi Pujan), Nov 10, Nov 24, Dec 25.
- **Muhurat trading** on Diwali (Laxmi Pujan, Sun 2026-11-08): a short special
  evening session whose timing NSE announces in a circular. The clock notes
  it; it does not know the hours, so time alerts do not ring for it.
- **2027 is not published yet** (as of 2026-10-23). For 2027 dates the clock
  says so: weekends are known, holidays are not. Run `clock --refresh` once
  NSE publishes the list.

## Steps

1. Right after india-exit writes the plan, run `add --from-plan`. Confirm the
   three alerts back to the user: the stop, the target and the flat-by time.
   For an MIS trade also offer `add --mis-reminder --symbol SYMBOL`.
2. Run `check` while the trade is open: every few minutes from a Claude
   scheduled task or `/loop`. If Kite is connected, call `get_ltp` first and
   pass the saved answer with `--kite-ltp` (or the price with `--price`). Each run fetches only the symbols
   in today's rules and rings each alert once. A Yahoo price is delayed, and a
   price can pass a level between two checks.
3. Tell the user what rang in one line each. A stop or target alert means an
   exit order may have filled: check Kite `get_positions` / `get_orders`, and
   if one leg filled make sure the other is cancelled, then record the fill
   with india-broker. "Flat by" or the MIS reminder means close anything still
   open: india-broker `flatten`, then india-risk, then the user's yes. Never
   act for them.

## Example: RELIANCE on Monday 2026-10-19

Opening range 09:15 to 09:30: high 1,223.40, low 1,207.30. Entry 1,227.80 on
the 09:45 bar, stop ₹1,215.30 (the range midpoint, on the 0.10 tick), target
₹1,240.30 (1R), flat by 15:10, MIS. RELIANCE has F&O contracts, so it is a
CAS stock: continuous trading to 15:15, Zerodha square-off at 15:12. `add --from-plan` writes
`reliance-plan-stop`, `reliance-plan-target` and `reliance-flat-by` for that
day. A `check` at 10:51 reads the 10:50 price, between the stop and the target:

```
NSE OPEN (2026-10-19 10:51 IST, normal session).
Nothing hit. 4 rule(s) watched today, RELIANCE ₹1,220.50 (Yahoo, free, can lag).
```

The 12:00 bar traded down to 1,214.10; the 12:05 check:

```
ALERT RELIANCE is ₹1,214.80 (Yahoo, free, can lag): at or below your stop ₹1,215.30 (plan stop: opening-range midpoint).
```

At 15:10 (if the trade were still open):

```
ALERT Flat by: it is 15:10 IST (set for 15:10), continuous trading in RELIANCE ends at 15:15 (closing
auction 15:15-15:35) and Zerodha squares off MIS positions at 15:12. If RELIANCE is still open, close it: india-broker flatten --symbol RELIANCE builds the
sell ticket, then india-risk and your yes.
```

Each rings once. Tuesday 2026-10-20 is Dussehra (NSE closed); with
`add --opening-bell`, the bell rings at 09:15 on Wednesday.

## Rules

- An alert is a fact about a price or the clock. Say it as a fact.
- Never present a Yahoo quote as live. Say where each price came from.
- Confirm every new rule back to the user: symbol, exchange, type, level, day.

## What it never does

It never places, cancels or changes an order, never calls Kite itself, never
claims a delayed quote is live, and never predicts where a price is going.

## Safety

- Education and tooling, not financial advice. A PASS is a rules check, not a recommendation.
- Paper trading by default (`"paper": true`). Going live is the user's own decision, in their own config.
- Nothing in this pack places an order except india-broker, and only when all three hold: india-risk
  passed that exact order today, the user typed an explicit yes for that one order in the chat, and the
  user clicked Allow once on Claude's own card because the Kite order tool is set to "Needs approval"
  (Customize > Connectors > tool permissions). Never set an order tool to "Always allow".
- Long only. No options, F&O, short selling or MIS leverage (margin) unless the config allows it and the
  user confirms.
- Kite logs in through Zerodha's own page; never paste API keys, passwords or TOTP codes into the chat.
