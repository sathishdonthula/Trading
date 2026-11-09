---
name: india-entry
description: >-
  Entry Timer for NSE and BSE stocks. Watches a setup bar by bar and tells you
  the moment to get in. Reads free Yahoo 1-minute or 5-minute bars, builds the
  opening range (09:15 to 09:30 IST by default) and calls the entry on the
  first completed bar that CLOSES above the range high, or above a level you
  give it (like a 20-day high from the scanner), with an optional volume
  check. With Zerodha Kite connected it reads the exchange's own candles
  through Kite (the recommended path for the trade itself) and falls back to
  free Yahoo bars. Knows the 15:15 close of F&O (closing auction) stocks.
  Says waiting and how far, triggered with the time and the price,
  expired, or not open yet, knows NSE holidays, and writes
  trader-in/entry.json for /india-exit. Long side only. It never places an
  order. Use when the user says "tell me when to get in", "watch RELIANCE",
  "has it broken out", "did it break the opening range", "entry signal",
  "time my entry", "check my watchlist", or after /india-scanner and
  /india-news, before /india-exit.
argument-hint: "[SYMBOL] [--setup orb|level] [--interval 5m|1m]"
---

# india-entry

The Entry Timer turns a setup into one moment: the bar that confirms it. It
reads bars, 1-minute or 5-minute, and waits for each one to finish. A price
that pokes above the high in the middle of a bar is not an entry. A bar that
CLOSES above it is. Then it says the time and the price, writes them down for
the Exit Planner, and stops. It never buys anything. Day trading loses most
people money, and a clean signal does not change that. SEBI's own study found
most individual intraday traders in India lose money.

`${CLAUDE_SKILL_DIR}` below is this skill's folder (where this SKILL.md lives).

```bash
python3 "${CLAUDE_SKILL_DIR}/entry.py" check RELIANCE                  # 15-minute opening range, 5-minute bars
python3 "${CLAUDE_SKILL_DIR}/entry.py" check RELIANCE --write          # also save trader-in/entry.json
python3 "${CLAUDE_SKILL_DIR}/entry.py" check RELIANCE --setup level --level 1230
python3 "${CLAUDE_SKILL_DIR}/entry.py" check RELIANCE --interval 1m --min-volume-x 1.5
python3 "${CLAUDE_SKILL_DIR}/entry.py" check BSE:RELIANCE              # the BSE line (or RELIANCE.BO, or 500325)
python3 "${CLAUDE_SKILL_DIR}/entry.py" watch RELIANCE --until 14:45    # one line per closed bar, stops on the trigger
python3 "${CLAUDE_SKILL_DIR}/entry.py" watchlist                       # every hit in trader-in/watchlist.json
python3 "${CLAUDE_SKILL_DIR}/entry.py" check RELIANCE --kite-bars trader-in/kite/RELIANCE-5minute-2026-10-19.json
```

Other flags: `--or-minutes N` (default 15), `--json`, `--offline FILE` (a
saved Yahoo chart JSON, `.json` or `.json.gz`, or a folder of
`<SYMBOL>.json`), `--now "2026-10-19 09:50"` (India time) to replay a day,
`--no-fetch` (do not fetch the NSE holiday or F&O list), `--kite-bars FILE`
(repeatable), `--kite-ltp FILE`, `--price SYM=PRICE`, `--cas yes|no|auto`,
`--fo-list FILE`. `watch` with `--now` or
`--offline` runs on a simulated clock and replays the day bar by bar without
waiting.

## Symbols

Type the NSE symbol: `RELIANCE`, `M&M`, `BAJAJ-AUTO`. NSE is the default.
`RELIANCE.BO`, `BSE:RELIANCE` or a 6-digit BSE code (`500325`) means BSE.
Yahoo's symbol is `RELIANCE.NS` or `RELIANCE.BO`. Every record stores the
plain `symbol` and the `exchange` (NSE or BSE).

## What it reads and writes

| file | what |
| --- | --- |
| `trader-in/watchlist.json` | the scanner's hits (`hits[].symbol`, optional `exchange`), for `watchlist` |
| `trader-in/config.json` | `no_entries_before` (09:30) and `no_entries_after` (14:45), IST. Optional |
| `trader-in/holidays.json` | the NSE trading-holiday list, fetched from nseindia.com and cached (refreshed weekly) |
| `trader-in/kite/*.json` | Kite `get_historical_data` / `get_ltp` output that Claude saved, verbatim (`--kite-bars`, `--kite-ltp`) |
| `trader-in/fo_list.txt` | NSE's F&O stock list (`fo_mktlots.csv`), cached for 7 days: which stocks are CAS stocks |
| Yahoo chart API | the fallback: today's bars, `range=1d&interval=5m` (or `1m`): free, unofficial, can lag |
| `trader-in/entry.json` | with `check --write`, or when `watch` triggers: symbol, exchange, setup, status, time, bar, price, level, opening range, previous close |

Every line with a price says where it came from: `(Kite candles)`, `(Kite,
live)` or `(Yahoo, free, can lag)`. With no Kite file the script runs on free
Yahoo data alone and never fails because Kite is absent.

## With Zerodha Kite connected

Kite is the exchange's data through the user's broker: use it for the trade
itself when the official Kite MCP is connected. The script never calls MCP;
Claude calls the read-only Kite tools, saves their output and passes the file.

1. **Check the login** with a cheap read: `get_ltp` for `NSE:RELIANCE`. If it
   says to log in first, call `login` and give the user the link. They sign in
   on Zerodha's own page. Never ask for a password, PIN or TOTP.
2. **Find the token**: `search_instruments` with `NSE:SYMBOL` (RELIANCE is
   738561).
3. **Get the candles**: `get_historical_data` with that token, from today
   09:15 to now, interval `5minute` (or `minute` with `--interval 1m`).
4. **Save the output verbatim** (Write) to
   `trader-in/kite/<SYMBOL>-<interval>-<date>.json`, for example
   `trader-in/kite/RELIANCE-5minute-2026-10-19.json`.
5. **Pass it**: `entry.py check RELIANCE --kite-bars FILE` (repeat the flag to
   merge an earlier and a later chunk; dates are de-duplicated). `--offline
   FILE` also detects the Kite format. For a live last price, save `get_ltp`
   output and add `--kite-ltp FILE` (information only: the signal comes from
   closed bars). On a `/loop`, fetch and save again before each check: a
   candle that closed after the file was saved does not count until a fresh
   file shows it.

Read-only tools only. Orders stay in `/india-broker`. The full-market scan and
the 60-day Time Machine history stay on Yahoo: thousands of bars do not fit
through the conversation.

## The rule

- **Market hours (IST, no daylight saving).** Pre-open order entry 09:00 to
  09:10 (09:00 to 09:08 before 2026-09-07), normal session from 09:15. No
  early closes. Since 2026-08-03 NSE runs a closing auction (CAS) 15:15 to
  15:35 for stocks with F&O contracts: for those "CAS stocks" continuous
  trading ends at **15:15** and Zerodha squares off MIS at **15:12**. Other
  stocks trade to 15:30 (square-off 15:25). The F&O list comes from NSE's
  `fo_mktlots.csv` (cached); `--cas yes|no` or the config's
  `cas_symbols_extra` / `non_cas_symbols` override it. With no list the
  stricter CAS rules apply, and it says so. Weekends and NSE holidays
  are closed (the 2026 list is built in; the live list is fetched and cached).
  NSE has not published 2027 yet: the script says so and uses the live list.
  The Diwali Muhurat session is a special evening session and is not covered.
- **Opening range (orb).** The high and low of the first 15 minutes: the
  09:15, 09:20 and 09:25 five-minute bars, or fifteen 1-minute bars. The
  level to beat is the range high.
- **Level.** A price you give, like a 20-day high from `/india-scanner`.
- **The trigger** is the first completed bar after the range that CLOSES
  above the level. A wick above is not a trigger. A close exactly at the
  level is not above it.
- **The time** is that bar's close time (the 09:45 bar closes at 9:50 AM).
  The price is its close, to the paisa.
- **Only closed bars count.** A bar is closed when the clock passes its start
  plus 5 (or 1) minutes and Yahoo has caught up to that moment (it waits a
  minute at most). Yahoo's live tick, like 11:24:35, is never a bar. Bars
  Yahoo leaves empty (often 15:20 and 15:25 on NSE: that is the closing
  auction) are skipped, never fatal.
- **Volume, if you want it.** `--min-volume-x 1.5` also needs the trigger
  bar's volume at 1.5 times the range bars' average. Yahoo books no volume on
  the 09:15 bar of most NSE stocks (the pre-open auction), so zero-volume bars
  are left out of that average.
- **Time limits.** No entries before 09:30 or after 14:45 (config). That
  leaves time to get out before the flat-by (15:00 for a CAS stock, 15:10
  otherwise) and Zerodha's MIS auto square-off (15:12 or 15:25).
- **What it says.** Waiting (and how far the last close is from the level),
  triggered, expired, or no data. Before 09:15: not open yet (with the day's
  close: 15:15 for a CAS stock). After the close it says continuous trading
  ended at 15:15 or 15:30. Before 09:30:
  the range is still forming. Weekends and NSE holidays: the market is closed.

## Steps

1. Get the symbol from the user, or run `entry.py watchlist` after
   `/india-scanner`. Check `/india-news` for it first (results, board
   meetings, corporate announcements).
2. Run `entry.py check SYMBOL`. Tell the user the status in one line:
   waiting and how far, triggered at what time and price, or expired.
3. To wait for it, Claude can run `entry.py watch SYMBOL --until 14:45` in
   the background, or `entry.py check SYMBOL --write` on a `/loop` every 5
   minutes. `watch` wakes just after each bar closes, prints one line per
   bar, and exits 0 on the trigger (it writes `trader-in/entry.json`) or 1
   when the setup expires. Keep the computer awake while it watches: a
   sleeping laptop pauses it, and it catches up, late, when it wakes.
4. When it triggers, say the time and the price, and that nothing was placed.
   If the trigger is more than a bar old, say that too: a late entry is a
   different trade.
5. Hand off to `/india-exit` to set the stop and the target BEFORE anything
   is bought.

## Example: RELIANCE

RELIANCE (NSE), Monday 2026-10-19, 5-minute bars (real Yahoo data, the
shared fixture). The opening range 09:15 to 09:30 is 1,207.30 to 1,223.40. At
09:47 the 09:45 bar is still open, so it waits:

```
WAITING  last close ₹1,221.80 (9:40 bar), ₹1.60 under the range high ₹1,223.40 (-0.13%) (Yahoo, free, can lag).
The 9:45 bar is still forming (₹1,227.80 so far, closes 9:50 AM; Yahoo, free, can lag): it does not count until it closes.
```

At 09:50 the bar has closed:

```
RELIANCE (NSE)  Mon 2026-10-19 09:50 IST  opening-range breakout (15-minute range) on 5-minute bars (Yahoo, free, can lag)
Opening range 9:15-9:30: high ₹1,223.40, low ₹1,207.30 (3 of 3 bars; Yahoo, free, can lag).
TRIGGERED 9:50 AM at ₹1,227.80: the 9:45 bar (bar 6) closed above the range high ₹1,223.40 (+0.36%; Yahoo, free, can lag).
This is the moment to get in, if you take the trade. Nothing was placed.
Next: set the stop and target BEFORE you buy: /india-exit (exit.py plan --from trader-in/entry.json).
```

The 09:45 bar carried 1.52x the range's average volume, so `--min-volume-x
1.5` gives the same trigger. `/india-exit` then plans it: stop ₹1,215.30 (the
range midpoint 1,215.35 rounded down to the ₹0.10 tick), risk ₹12.50 a
share, 1R target ₹1,240.30, 2R target ₹1,252.80. The next day, Tuesday
2026-10-20, is an NSE holiday (Dussehra): `check` says the market is closed
and the next open is 2026-10-21 09:15 IST.

## Example: RELIANCE on Kite candles

The same morning from Kite (`get_historical_data`, token 738561, 5minute, the
shared fixture `kite-RELIANCE-5minute-2026-10-19-open.json`). Kite's bars are
the exchange's and differ a little from Yahoo's: the opening range is
₹1,207.10 to ₹1,223.60, and Kite books the 09:15 bar's volume (615,358; Yahoo
shows 0). `entry.py check RELIANCE --kite-bars FILE --now "2026-10-19 09:50"`:

```
RELIANCE (NSE)  Mon 2026-10-19 09:50 IST  opening-range breakout (15-minute range) on 5-minute bars (Kite candles)
Opening range 9:15-9:30: high ₹1,223.60, low ₹1,207.10 (3 of 3 bars; Kite candles).
TRIGGERED 9:50 AM at ₹1,227.70: the 9:45 bar (bar 6) closed above the range high ₹1,223.60 (+0.34%; Kite candles).
```

`/india-exit` then plans it: stop ₹1,215.30 (midpoint 1,215.35 rounded down),
R ₹12.40, 1R target ₹1,240.10. RELIANCE is a CAS stock: flat by 15:00,
continuous trading ends 15:15, Zerodha squares off at 15:12.

## Rules

- Bar by bar, on Kite candles when Kite is connected (recommended for the
  trade), else free Yahoo 1-minute or 5-minute bars. Yahoo is unofficial and
  can lag. Say so when a minute matters.
- A signal is information, not an order, and not a promise. Nothing here
  promises winning trades or money.
- Long side only: this pack plans longs only (`allow_short` stays false). If
  the user wants a short, say that.
- One trigger per setup per day: the first close above. Later closes do not
  reset it.
- Never call an entry early because a bar looks like it will close above.
- Series BE (trade-to-trade) stocks cannot be traded intraday: `/india-exit`
  flags them.

## What it never does

It never places an order, never sizes a position, never sets the stop (that
is `/india-exit`), never moves the level after the fact, and never counts a
bar that has not closed. It never runs `/india-broker` from a background
watch: nobody is there to say yes.

## Safety

- Education and tooling, not financial advice. A PASS is a rules check, not a recommendation. Not tax advice.
- Paper trading by default (`"paper": true`). Going live is the user's own decision, in their own config.
- Nothing in this pack places an order except india-broker, and only when all three hold: india-risk
  passed that exact order today, the user typed an explicit yes for that one order in the chat, and the
  user clicked Allow once on Claude's own card because the Kite order tool is set to "Needs approval"
  (Customize > Connectors > tool permissions). Never set an order tool to "Always allow". No order without your yes.
- No options, margin (MIS leverage), short selling or F&O unless the config allows it and the user confirms.
- Broker keys and Kite logins live in the Kite MCP connector or environment variables, never in the chat.
