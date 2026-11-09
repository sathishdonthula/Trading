---
name: india-exit
description: >-
  Exit Planner for NSE and BSE stocks. Sets your stop and your target before
  you enter, so you know when to get out. Reads the entry from india-entry
  (or a price you give), puts the stop at the opening-range midpoint by
  default (or the range low, your price, a percent, or a multiple of the
  ATR), rounds it down to the NSE tick, and sets the target at 2R (or 1R, or
  your price). Refuses a stop at or above the entry, a target at or below it,
  a stop too tight to survive the spread, or a stop below the lower circuit,
  and says why. Flat by 15:00 IST for F&O (closing auction) stocks, before
  Zerodha's 15:12 MIS square-off, and 15:10 for the rest (square-off 15:25).
  With Zerodha Kite connected it takes the tick and today's circuit limits
  from Kite (recommended), else the NSE tick table and sec_list.csv.
  Writes trader-in/plan.json for the Time Machine, the Budget Manager, the
  Risk Check and Price Alerts. Long side only. Use when the user says
  "where's my stop", "set my stop and target", "plan the exit", "when do I
  get out", "stop loss", "what's my R", "risk reward", or right after
  /india-entry triggers, before /india-timemachine.
argument-hint: "[--stop or-mid|or-low|price:X|pct:X|atr:K] [--target 2R|1R|price:X]"
---

# india-exit

The Exit Planner decides how a trade ends before it starts. The stop is where
the setup has failed. The target is where you take what you came for. Both
are set BEFORE the entry, written down and handed on, so nobody has to decide
anything in the middle of a fast move. A stop is a plan, not a guarantee: a
gap, a circuit or a fast market can fill past it. Day trading loses most
people money, and a neat plan does not change that.

`${CLAUDE_SKILL_DIR}` below is this skill's folder (where this SKILL.md lives).

```bash
python3 "${CLAUDE_SKILL_DIR}/exit.py" plan                          # trader-in/entry.json: midpoint stop, 2R target
python3 "${CLAUDE_SKILL_DIR}/exit.py" plan --stop or-mid --target 1R --write
python3 "${CLAUDE_SKILL_DIR}/exit.py" plan --stop atr:1.5           # needs the session's bars (Yahoo or --offline)
python3 "${CLAUDE_SKILL_DIR}/exit.py" plan --symbol RELIANCE --entry 1227.80 --or-high 1223.40 --or-low 1207.30
python3 "${CLAUDE_SKILL_DIR}/exit.py" plan --symbol BSE:RELIANCE --entry 1227.80 --stop pct:1 --band 20
python3 "${CLAUDE_SKILL_DIR}/exit.py" plan --kite-instrument trader-in/kite/RELIANCE-instrument-2026-10-23.json
python3 "${CLAUDE_SKILL_DIR}/exit.py" show                          # the current trader-in/plan.json
```

Other flags: `--flat-by HH:MM|auto`, `--cas yes|no|auto`, `--fo-list FILE`,
`--kite-bars FILE` (repeatable, for an ATR stop), `--kite-instrument FILE`,
`--kite-ltp FILE` or `--price SYM=PRICE` (the entry with `--symbol`), `--json`, `--now "2026-10-19 09:50"` (India
time), `--entry PRICE` with a waiting entry.json to plan ahead, `--setup
level`, `--offline FILE` (a saved Yahoo chart JSON for an ATR stop or the
previous close), `--band 2|5|10|20|none`, `--prev-close PRICE`, `--no-fetch`.

## What it reads and writes

| file | what |
| --- | --- |
| `trader-in/entry.json` | the trigger from india-entry: symbol, exchange, entry price, time, bar, opening range, previous close |
| `trader-in/config.json` | `flat_by` ("auto": 15:00 CAS / 15:10 non-CAS, or "HH:MM"), `min_reward_risk` (1.0: a note here, india-risk enforces it), `cas_symbols_extra` / `non_cas_symbols` |
| `trader-in/fo_list.txt` | NSE's F&O stock list (`fo_mktlots.csv`), cached 7 days: which stocks are CAS stocks |
| `trader-in/kite/*.json` | Kite `search_instruments`, `get_historical_data` and `get_ltp` output Claude saved verbatim |
| `trader-in/sec_list.csv` | NSE's price-band list (`nsearchives.nseindia.com/content/equities/sec_list.csv`, public), fetched once a day |
| Yahoo chart API | only for an `atr:K` stop or a missing previous close: free and unofficial |
| `trader-in/plan.json` | with `--write`: entry, stop and its rule, target and its R, risk a share, reward:risk, flat by, tick, circuit. india-timemachine, india-budget, india-risk and india-alerts read it |

`plan.json` keeps the original pack's fields (`symbol`, `side`, `date`,
`setup`, `entry`, `entry_time`, `entry_bar`, `interval`, `opening_range`,
`stop`, `stop_rule`, `stop_label`, `target`, `target_r`, `target_label`,
`risk_per_share`, `reward_risk`, `flat_by`, `stop_detail`, `created_at`,
`note`) and adds `exchange`, `currency` ("INR"), `tick`, `tick_source`
("kite" or "nse-table"), `product` ("MIS", or "CNC" for a BE stock),
`prev_close`, `band_pct`, `series`, `lower_circuit`, `upper_circuit`,
`circuit_source` ("kite", "sec_list", "flags" or null), `cas` (true, false
or null when unknown), `square_off` ("15:12" or "15:25"), `session_close`
("15:15" or "15:30") and `entry_source` ("kite", "yahoo", "kite-ltp" or
"flags").

Every price line says its source: `(Kite candles)`, `(Kite, live)`,
`(Yahoo, free, can lag)` or `(your price)`; the tick and circuit lines say
Kite instrument, NSE tick-size table or the sec_list band.

## With Zerodha Kite connected

Kite is the recommended path for the trade itself: its tick size and circuit
limits come from the exchange through the broker, and it has limits for F&O
stocks that sec_list.csv shows as "No Band" (RELIANCE ₹1,066.10 to ₹1,302.90
on 2026-10-23). The script never calls MCP; Claude calls the read-only Kite
tools and saves the output.

1. **Check the login** with a cheap read: `get_ltp` for `NSE:RELIANCE`. If it
   says to log in first, call `login` and give the user the link. They sign in
   on Zerodha's own page. Never ask for a password, PIN or TOTP.
2. **The instrument**: `search_instruments` with `NSE:SYMBOL` gives the token
   (RELIANCE 738561), `tick_size`, `lower_circuit_limit` and
   `upper_circuit_limit`. Save it verbatim (Write) to
   `trader-in/kite/<SYMBOL>-instrument-<date>.json` and pass
   `--kite-instrument FILE`. Its tick and circuits win over the NSE table and
   sec_list.csv, but only on the day it was saved (a `"fetched"` date in the
   file, else the file's save date): on another day the script falls back and
   says so.
3. **Candles for an ATR stop**: `get_historical_data` with the token, today
   09:15 to now, `5minute`, saved to
   `trader-in/kite/<SYMBOL>-5minute-<date>.json`, passed with `--kite-bars`.
   The entry itself normally comes from `/india-entry`, which reads the same
   Kite candles.
4. **A live entry price**: save `get_ltp` output and pass `--kite-ltp FILE`
   with `--symbol` (or `--price SYM=PRICE`).

Read-only tools only. Orders stay in `/india-broker`. Without Kite every input
falls back to free Yahoo data and NSE's public files; nothing fails because
Kite is absent.

## Stops and targets

| stop | where it goes |
| --- | --- |
| `or-mid` | the opening-range midpoint (the default for an opening-range breakout) |
| `or-low` | the opening-range low: wider, so fewer shares for the same max loss |
| `price:X` | your price |
| `pct:X` | X percent under the entry |
| `atr:K` | K times the 14-bar ATR under the entry, on the session's bars up to the entry bar. With fewer than 14 bars by then it averages all of them and says so |

A level setup has no opening range to lean on, so it needs a stop you name.

- **The NSE tick.** Every price sits on the tick grid (NSE, from Apr 15
  2025, reviewed monthly): under ₹250 the tick is ₹0.01; ₹250 to ₹1,000 is
  ₹0.05; ₹1,000 to ₹5,000 is ₹0.10; ₹5,000 to ₹10,000 is ₹0.50; ₹10,000 to
  ₹20,000 is ₹1; above ₹20,000 is ₹5.
- **Rounding.** The stop rounds DOWN to the tick, away from the entry. Risk =
  entry minus stop. Target = entry + R x risk, rounded down to the tick (a
  long's target sits where a sell can fill). `2R` by default; `1R`, `1.5R`
  or `price:X` also work.
- **Circuit limits.** The band (2, 5, 10 or 20 percent) comes from NSE's
  sec_list.csv, or `--band`. Lower circuit = previous close x (1 - band). A
  stop below the lower circuit is refused: the price cannot trade there. An
  entry within 1% of the upper circuit is flagged (india-risk blocks it).
  "No Band" means an F&O stock with a dynamic band set by NSE: no fixed
  circuit to check, and it says so. BSE bands are not in NSE's list: use
  `--band`. Series BE (trade-to-trade) is flagged: no intraday allowed.
- **Refused, with the reason.** A stop at or above the entry. A target at or
  below it. A risk under 0.05% of the price or under one tick: too tight to
  survive the spread, normal wiggle would stop you out. A stop below the
  lower circuit. An entry.json that has not triggered (unless you give
  `--entry`). A short: this pack plans longs only.
- **Closing auction (CAS).** Since 2026-08-03 NSE runs a closing auction
  15:15 to 15:35 for stocks with F&O contracts. For those CAS stocks
  continuous trading ends at 15:15 and Zerodha squares off MIS at 15:12;
  other stocks trade to 15:30 with square-off at 15:25. CAS status comes from
  NSE's `fo_mktlots.csv` (cached in `trader-in/fo_list.txt`), `--cas`, or the
  config lists. Unknown (offline, no list) means the stricter CAS rules, and
  it says so.
- **Flat by.** `auto` by default: 15:00 IST for a CAS stock, 15:10
  otherwise. Out by then, win or lose, no overnight hold. Zerodha charges ₹50
  + GST for each order it squares off, so a flat-by at or after the stock's
  square-off (15:12 CAS, 15:25 non-CAS) is moved back to the default with a
  warning. An entry at or after the flat-by time is refused.

## Steps

1. Run it right after `/india-entry` triggers. Before that, `--entry PRICE`
   plans ahead at the level you expect.
2. Use the default stop for an opening-range breakout unless the user names
   another. A level setup needs one: ask.
3. Run `exit.py plan --write`. Read back the entry, the stop and its rule,
   the target and its rule, the risk a share, reward:risk, flat by and any
   circuit note.
4. If it refuses, say the reason and the fix (a wider stop, a farther
   target, a stop above the lower circuit). Never loosen anything on your own.
5. Say once that the stop is a plan, not a guarantee.
6. Hand off to `/india-timemachine` to replay the plan, then `/india-budget`
   to size it to the max loss per trade (charges included). Offer
   `/india-alerts` for the stop, the target and the flat-by time. Claude can
   keep `entry.py watch` running in the background or on a `/loop` until a
   setup triggers, then come here.

## Example: RELIANCE

RELIANCE (NSE), Monday 2026-10-19. The entry is ₹1,227.80 at 9:50 AM (the
09:45 bar, bar 6) and the opening range is 1,207.30 to 1,223.40. `exit.py
plan --stop or-mid --target 1R --write`:

```
Exit plan: RELIANCE (NSE) long, Mon 2026-10-19, opening-range breakout on 5-minute bars
  Entry         ₹1,227.80 at 9:50 AM (bar 6) (Yahoo, free, can lag)
  Stop          ₹1,215.30  opening-range midpoint: (₹1,223.40 + ₹1,207.30) / 2 = ₹1,215.35, rounded down to the ₹0.10 tick
  Target        ₹1,240.30  1R (one times the risk)
  Risk a share  ₹12.50 (1.02% of the entry)
  Reward:risk   1.0
  Flat by       15:00 IST, win or lose: no overnight hold (Zerodha squares off MIS at 15:12)
  Tick          ₹0.10 (NSE tick-size table)
  Session       continuous trading to 15:15 IST (CAS stock)
Note: RELIANCE has No Band in NSE's list (an F&O stock): NSE sets a dynamic price band, so there is no fixed circuit to check the stop against.
```

R is ₹12.50 a share. The default 2R target is 1,227.80 + 2 x 12.50 =
₹1,252.80. india-budget then sizes it: a ₹500 max loss per trade buys at most
floor(500 / 12.50) = 40 shares, fewer once the round-trip charges are
counted. An `atr:1.5` stop on the same bars gives ₹1,216.60 (ATR ₹7.4572 over
the 7 bars to the entry).

RELIANCE is a CAS stock, so the plan says flat by 15:00 (Zerodha squares off
at 15:12, continuous trading ends 15:15).

## Example: RELIANCE on Kite candles

The same morning planned from Kite candles (india-entry with `--kite-bars`,
the shared fixture `kite-RELIANCE-5minute-2026-10-19-open.json`). Kite's
opening range is 1,207.10 to 1,223.60 and the 09:45 bar closes at 1,227.70:

```
Exit plan: RELIANCE (NSE) long, Mon 2026-10-19, opening-range breakout on 5-minute bars
  Entry         ₹1,227.70 at 9:50 AM (bar 6) (Kite candles)
  Stop          ₹1,215.30  opening-range midpoint: (₹1,223.60 + ₹1,207.10) / 2 = ₹1,215.35, rounded down to the ₹0.10 tick
  Target        ₹1,240.10  1R (one times the risk)
  Risk a share  ₹12.40 (1.01% of the entry)
  Reward:risk   1.0
  Flat by       15:00 IST, win or lose: no overnight hold (Zerodha squares off MIS at 15:12)
```

R is ₹12.40; the 2R target is ₹1,252.50. With a same-day
`--kite-instrument` file the plan also shows the circuit from Kite (on
2026-10-23: ₹1,066.10 to ₹1,302.90) instead of "No Band".

## Rules

- The stop and the target are set before the entry, never after.
- Never move a stop further from the entry once the trade is on.
- Plain numbers only: entry, stop, target, risk a share, reward:risk. It
  never prints a P&L or a return.
- Nothing here promises winning trades or money. A 1R target is not a
  prediction that the price gets there.
- Long side only: this pack plans longs only.
- Intraday gains are speculative business income in India; that is for the
  user's tax filing, not this tool. Not tax advice.

## What it never does

It never places an order, never sizes a position, never widens a stop to
make a plan pass, and never calls a stop a guarantee.

## Safety

- Education and tooling, not financial advice. A PASS is a rules check, not a recommendation. Not tax advice.
- Paper trading by default (`"paper": true`). Going live is the user's own decision, in their own config.
- Nothing in this pack places an order except india-broker, and only when all three hold: india-risk
  passed that exact order today, the user typed an explicit yes for that one order in the chat, and the
  user clicked Allow once on Claude's own card because the Kite order tool is set to "Needs approval"
  (Customize > Connectors > tool permissions). Never set an order tool to "Always allow". No order without your yes.
- No options, margin (MIS leverage), short selling or F&O unless the config allows it and the user confirms.
- Broker keys and Kite logins live in the Kite MCP connector or environment variables, never in the chat.
