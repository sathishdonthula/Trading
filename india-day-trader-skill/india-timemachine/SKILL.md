---
name: india-timemachine
description: >-
  Time Machine for NSE and BSE. Replays every possible outcome of an intraday
  trade before you risk a rupee: 10,000 simulated sessions plus every past
  time the setup fired. Takes the plan from india-exit (entry, stop, target),
  rebuilds the rest of the day 10,000 times from the stock's own 5-minute bars
  over the last 60 sessions, and re-runs the same rule on every past session.
  Knows NSE's closing auction: F&O ("CAS") stocks trade continuously only to
  15:15 since 2026-08-03 and Zerodha squares their MIS off at 15:12. Prints the
  odds of target first, stop first, or neither by your flat-by time (default
  15:00 for CAS stocks, 15:10 for the rest),
  where the price sits then, the average R gross and net of Indian charges
  (brokerage, STT, exchange, SEBI, stamp duty, GST), the history, and one
  honest verdict, then hands off to /india-budget. Use when the user says
  "run the time machine", "replay this trade", "what are the odds", "simulate
  this trade", "monte carlo", "backtest this setup", "how often does this hit
  the target", "is this a coin flip", "are charges eating my edge", or after
  /india-exit writes a plan, before /india-budget.
argument-hint: "[path to trader-in/plan.json, or SYMBOL]"
---

# india-timemachine

The Time Machine sits between the plan and the money. india-exit has set the
entry, the stop and the target. Before anything is sized or sent, this skill
asks one question: if today plays out like the stock's own last 60 sessions,
how often does this plan hit the target first, the stop first, or neither by
the time you would get out? It answers with 10,000 replays and with the real
record of the same rule on every past session, and it says what Indian
charges do to the average result. It is a simulation built from the stock's
own recent bars, not a prediction. Most intraday traders in India lose money
(SEBI's own studies say so), and a good-looking replay does not change that.

`${CLAUDE_SKILL_DIR}` below is this skill's folder (where this SKILL.md lives).

```bash
python3 "${CLAUDE_SKILL_DIR}/timemachine.py" run                    # trader-in/plan.json, 10,000 replays, seed 19
python3 "${CLAUDE_SKILL_DIR}/timemachine.py" run --write            # also save trader-in/timemachine.json
python3 "${CLAUDE_SKILL_DIR}/timemachine.py" run --qty 40           # charges for 40 shares
python3 "${CLAUDE_SKILL_DIR}/timemachine.py" run --risk-inr 1000    # or for Rs 1,000 of risk at the stop
python3 "${CLAUDE_SKILL_DIR}/timemachine.py" run --until 15:15      # replay to the end of trading (CAS stock)
python3 "${CLAUDE_SKILL_DIR}/timemachine.py" run --kite-bars trader-in/kite/RELIANCE-5minute-2026-10-19.json
python3 "${CLAUDE_SKILL_DIR}/timemachine.py" run --json
python3 "${CLAUDE_SKILL_DIR}/timemachine.py" run --symbol RELIANCE --date 2026-10-19 \
    --entry 1227.8 --stop 1215.3 --target 1240.3 --entry-time 09:50
python3 "${CLAUDE_SKILL_DIR}/timemachine.py" chart --from           # ASCII fan chart of the saved run
```

Other flags: `--exchange NSE|BSE` (or write `RELIANCE.BO`, `BSE:RELIANCE`, a
6-digit BSE code), `--product MIS|CNC` (for the charges, default MIS),
`--flat-by 15:00` (with `--symbol`; default `auto`), `--cas yes|no` (override
NSE's F&O list), `--config trader-in/config.json` (its `cas_symbols_extra` /
`non_cas_symbols` lists), `--kite-bars FILE` (repeatable), `--replays N`, `--seed N` (default 19),
`--window N` (default 2 bars), `--offline FILE` (a saved Yahoo chart JSON,
`.json` or `.json.gz`), `--now "2026-10-19 09:55"` (IST) to pin the clock.
All times are IST (UTC+05:30, no daylight saving).

## What it reads and writes

| file | what |
| --- | --- |
| `trader-in/plan.json` | the plan from india-exit: symbol, exchange, currency, tick, date, entry, entry_time (the entry bar's close) / entry_bar, stop, stop_rule, target, target_r, opening range, flat_by, product. Or pass the plan as flags |
| Yahoo chart API | one request: 60 days of 5-minute bars for `SYMBOL.NS` / `SYMBOL.BO` (`range=60d&interval=5m`), free and unofficial, can lag |
| `trader-in/fo_list.txt` | NSE's F&O stock list (from `fo_mktlots.csv`), cached 7 days: a stock on it is a CAS stock. Fetched when missing or stale; with `--offline` and no cache the stricter CAS rules are used, and the report says so |
| `trader-in/kite/*.json` | with `--kite-bars`: Kite `get_historical_data` output, saved verbatim, for the plan day only |
| `trader-in/holidays.json` | read if india-alerts cached the live NSE holiday list; the 2026 NSE list is built in |
| `trader-in/timemachine.json` | with `--write`: `{plan, mc, history}` plus the verdict. `mc` holds the odds, percentiles, gross and net average R, the charges, the fan-chart bands, 160 sample paths and a 24-bin histogram |

## Session rules: the closing auction

Since **2026-08-03** NSE runs a Closing Auction Session (15:15 to 15:35) for
stocks that have F&O contracts. For those "CAS stocks" continuous trading ends
at 15:15 and Zerodha squares MIS positions off at **15:12**; the rest still
trade to 15:30 with the square-off at **15:25** (Rs 50 + GST an auto-squared
order). One function, `session_rules(cas, day)`, decides each day:

| | CAS stock, from 2026-08-03 | non-CAS, or any day before |
| --- | --- | --- |
| continuous trading | 09:15 to 15:15, 72 five-minute bars | 09:15 to 15:30, 75 bars |
| Zerodha MIS square-off | 15:12 | 15:25 |
| default flat-by | 15:00 | 15:10 |

Each past session is judged by the rules of ITS day, so RELIANCE's July
sessions count 75 bars and its August to October ones 72. Yahoo's empty 15:20
and 15:25 bars on CAS days are the auction, not missing data: they are cut
off, never filled, and reported apart from real gaps. A plan `flat_by` at or
after the stock's square-off is pulled back to the default flat-by with a
note; an explicit `--until` past the square-off runs but is flagged, and no
replay runs past the end of continuous trading.

## With Zerodha Kite connected

Kite is the exchange's own data through your broker, so for the plan day it
beats free Yahoo (on 19 Oct Kite's opening range was 1207.10 to 1223.60 and
its 09:45 close 1227.70; Yahoo said 1207.30 to 1223.40 and 1227.80). The
script never calls Kite itself; Claude does, read-only:

1. Check the login with a cheap read: `get_ltp` for `NSE:SYMBOL`. If it says
   to log in first, call `login` and give the user the link: they sign in on
   Zerodha's own page. Never ask for a password, PIN or TOTP.
2. `search_instruments` for `NSE:SYMBOL` gives the instrument token (RELIANCE
   = 738561). Then `get_historical_data` with that token, interval
   `5minute`, from the plan day 09:15 to now.
3. Write the tool's JSON verbatim to
   `trader-in/kite/<SYMBOL>-5minute-<date>.json` (two chunks are fine; pass
   both, they are merged and de-duplicated).
4. Run `timemachine.py run --kite-bars trader-in/kite/<file>.json`. The
   report says "the plan day's bars (Kite candles)".

`--kite-bars` replaces only the plan day's bars (the session so far: the
entry bar check and the opening range for the stop rule). **The 60-day
history stays on Yahoo**: the pool is about 4,000 five-minute bars, and every
Kite tool result passes through Claude's context, so 60 sessions of candles
would not fit, cost a lot and could be cut short. One Yahoo request brings
them straight into the script. Kite files for other days are ignored with a
note. Order tools are never used here; orders stay in india-broker.

## How it works

1. **The pool.** It keeps only FULL sessions before the plan's date, by that
   day's session rules (75 bars, or 72 for a CAS stock from 2026-08-03). Up
   to 3 empty bars of a real gap are filled flat at the previous close, and
   the report counts them apart from the auction bars it cuts off. Holidays,
   Muhurat trading, bigger gaps and today are left out. Every bar
   becomes four ratios to the bar before it: open, high, low and close over
   the previous close.
2. **One replay.** Start at the entry price. For each bar left until the
   flat-by time, pick a past bar from the same time of day (plus or minus 2
   bars) at random, and scale it to the current price. Bar by bar, it checks
   the stop first, then the target. A bar that touches both counts as the stop.
3. **10,000 replays** give four odds. Target first. Stop first. Neither,
   which means the trade was still open at `--until` (default the plan's
   `flat_by`, else 15:00 for a CAS stock and 15:10 for the rest). Green, which means target first or above the
   entry at `--until`. R is +1 at a 1R target, -1 at the stop, and otherwise
   the distance from the entry at `--until` divided by the risk. A gap
   through the stop, or a lower circuit, still counts as -1R here. A real
   fill can be worse.
4. **Charges.** Average R is printed twice: gross, and net of the Indian
   round trip for a stated quantity. The quantity is `--qty`, else
   `--risk-inr` divided by the risk a share, else the plan's `qty`, else
   Rs 500 of risk at the stop (the pack's default max loss per trade).
   Each replay pays the charges at its own exit price: brokerage (intraday
   min(Rs 20, 0.03 %) an order), STT 0.025 % on the sell, NSE exchange
   charge 0.00297 % (BSE 0.00375 %), SEBI Rs 10 a crore, stamp duty 0.003 %
   on the buy, GST 18 % on brokerage + exchange + SEBI. Zerodha's list as of
   2026-10: check your broker's.
5. **The history.** The plan's own rule (the same opening range, stop rule
   and target R, stops rounded down to the NSE tick) runs again on every one
   of those past sessions, sized the same way. That is what really happened,
   not a simulation.
6. **The verdict** is one line from the gross odds. It looks only at the
   replays that hit one side, and asks how often the target came first. A
   bigger target needs to win less often to break even: 1 in (1 + R). That
   is 50 % at 1R, 33 % at 2R, 25 % at 3R. Less than 5 points from
   break-even: "Close to a coin flip" at 1R, "Close to break-even" at any
   other R. 5 to 15 points: "Leans toward the target" or "Leans toward the
   stop". 15 or more: "Strongly toward" one of them.
7. **Below the verdict**, when it applies: a line when more than 40 % of the
   replays hit neither (the price at `--until` decides those), the average R
   when its sign goes against the lean, and a line when the charges turn a
   positive average R negative.

**The seed.** The draws come from Python's `random.Random(seed)`, seed 19 by
default. Same plan, same data, same seed: the same numbers, on any machine.
Say the seed with every result so the user can repeat it.

**5-minute bars only.** Yahoo keeps 60 days of 5-min bars but only about 7
days of 1-min bars, too few sessions for a fair pool. It refuses, in one
plain line, when there are fewer than 20 full sessions, when the entry bar is
not in the data (an NSE holiday, a weekend, not closed yet, a future day),
or when the plan does not make sense (a stop above the entry, an entry off
the 5-minute grid, a sell: this pack is long only). A live in-progress bar is
never used. Replaying an MIS trade to or past the square-off (15:12 CAS,
15:25 other) prints a note.

## Steps

1. Make sure `trader-in/plan.json` is today's plan from `/india-exit`. If
   there is none, run that first, or pass the plan as flags. If Kite is
   connected, save the plan day's 5-minute candles (see above) and add
   `--kite-bars`.
2. Run `timemachine.py run --write`. The plan's `flat_by` is used, so
   "neither" means still open at the time you would get out. Add `--qty` if
   the user already knows the size.
3. Tell the user, in this order: the session line (CAS or not, the
   square-off), the four odds, the median and the 5 to 95
   range of the price at the flat-by time, the average R gross and net of
   charges (with the quantity and the rupee charges), the history line, then
   the verdict line and any line under it exactly as printed. Say the seed.
4. If the verdict is a coin flip or close to break-even, say so plainly. If
   the charges turn the average negative, say that too: in India the round
   trip often costs 0.1R or more on a small stop. If the history and the
   replays disagree, say it.
5. Say once that this is built from the last 60 sessions and is not a
   prediction. Results, corporate announcements, circuit limits and index
   rebalancing can do things those sessions never did. Check `/india-news`
   for the stock before going on.
6. If the user still wants the trade, hand off to `/india-budget` to size it.

## Example: RELIANCE, Monday 2026-10-19

RELIANCE has F&O contracts, so it is a CAS stock: trading to 15:15, MIS
square-off 15:12, flat-by 15:00. The 15-minute opening range (09:15 to 09:30,
Yahoo) is 1207.30 to 1223.40. The 09:45 bar closes above it: entry 1227.80
at 09:50 (bar 6), stop 1215.30 (the range midpoint 1215.35, rounded down to
the 0.10 tick), target 1240.30 (1R, risk Rs 12.50 a share), flat-by 15:00.
History is the 56 full sessions from 2026-07-29 to 2026-10-16 (3 July days of
75 bars, 53 CAS days of 72; 2026-10-19 itself is never in the pool).

```
Session: CAS stock (F&O): continuous trading ends 15:15, Zerodha squares off MIS at 15:12.
10,000 replays of the rest of the session, 09:50 to 15:00 IST (your flat-by; 62 five-minute bars), seed 19.
  target first   19.2 %
  stop first     13.9 %
  neither        67.0 %   still open at 15:00
  green          52.2 %   target first, or above the entry at 15:00
  Price at 15:00: median 1228.22, 90 % of replays between 1213.28 and 1243.47.
  Average R: +0.047 gross (R = the ₹12.50 risk: +1R at the target, -1R at the stop).
  Average R: -0.058 net of charges, for 40 shares (₹500.00 risk at the stop / ₹12.50 a share):
    about ₹52.08 a round trip (MIS, NSE) = 0.104R.
History: the same rule on each of those 56 real sessions fired 27 times:
14 target, 10 stop, 3 neither. Average R +0.119 gross, -0.080 net of charges.
Leans toward the target: target first 19.2 % vs stop first 13.9 %.
67.0 % of the replays hit neither by 15:00; the price at 15:00 decides those.
After charges the average R is -0.058: on 40 shares the round trip costs about 0.10R a trade.
Closing auction: on 53 session(s) from 2026-08-03 the 15:15 to 15:30 bars are the auction, not trading.
Real gaps: filled 2 empty 5-min bar(s) in 1 session(s) flat at the previous close.
```

At 1R the target has to come first in half of the replays that hit one
side. It did in 58 %, a lean. But two thirds of the replays were still open
at 15:00, so the gross average R is only +0.047, and the ₹52 round trip on 40
shares (0.10R) turns it negative. The real history was better gross (+0.119)
and still negative after charges (-0.080). With Kite's candles for the day
(`--kite-bars`) the odds are the same, since the pool is Yahoo's history and
Kite's range gives the same 1215.30 stop. That is the honest answer for this
setup, and it is what the user should hear before `/india-budget` sizes it.

## Rules

- Every number comes from the run. Never round a coin flip up into an edge.
- The verdict line and the lines under it are printed by the tool. Repeat
  them, do not rewrite them. Never judge a 2R or 3R plan by target first vs
  stop first alone: the verdict already weighs the target's R.
- Always give the net average R next to the gross one. Charges are part of
  the trade in India, not a footnote.
- Simulated odds are not predictions and not promises. Say it once, plainly.
- Free Yahoo data is unofficial and can lag. Bars are 5-minute bars, checked
  bar by bar, never tick by tick and never live.
- Always say the seed, so any run can be repeated.
- One plan per run. A changed entry, stop, target or date is a new run.

## What it never does

It never places an order, never sizes a position for real, never changes
the plan, and never calls a result a sure thing. It never prints a P&L or a
profit. It never uses a bar that has not closed yet, and never mixes today's
bars into the pool. It replays long trades only, like the rest of the pack.

## Safety

- Education and tooling, not financial advice. A PASS is a rules check, not a recommendation.
  Intraday gains are taxed as speculative business income: not tax advice either.
- Paper trading by default (`"paper": true`). Going live is the user's own decision, in their own config.
- Nothing in this pack places an order except india-broker, and only when all three hold: india-risk
  passed that exact order today, the user typed an explicit yes for that one order in the chat, and the
  user clicked Allow once on Claude's own card because the Kite order tool is set to "Needs approval".
  No order without your yes. Never set an order tool to "Always allow".
- No options, F&O, MIS leverage, short selling or margin unless the config allows it and the user confirms.
- Broker keys and Kite sessions live in the Claude Desktop config or the Kite MCP login, never in the chat.
