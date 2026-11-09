---
name: india-budget
description: >-
  Budget Manager for NSE / BSE day trades. Sizes every position to your
  budget and your max loss per trade in rupees, charges included. Reads the
  trade plan from india-exit (entry, stop, target), finds the whole shares
  that fit inside four limits (max loss at the stop plus Zerodha's round-trip
  charges, position cap, max order, cash after your buffer, no MIS margin),
  says which limit decided it, and writes ONE order proposal to
  trader-in/orders.json: the entry in the exact shape of Zerodha Kite's
  place_order plus an exit plan (SL-M stop and LIMIT target for MIS, a
  two-leg GTT for CNC). A stop too far away for your max loss is a plain
  refusal, not a smaller order. Nothing is placed. Use when the user says
  "how many shares", "size this trade", "how much should I buy", "I have
  ₹50,000", "I only want to risk ₹500 a trade", "set my rules", "keep 10% in
  cash", or after /india-exit writes a plan.
argument-hint: "[budget in rupees]"
---

# india-budget

The Budget Manager turns a plan ("buy RELIANCE at 1227.80, stop 1215.30")
into an exact order ("buy 30 shares"), inside rules the user sets once. The
stop decides the size: the further away the stop, the fewer shares. In India
the charges matter too, so the max loss counts them. It writes a proposal
only. india-risk checks it and india-broker asks the user yes or no. Day
trading loses most people money (SEBI's own studies say so). Sizing to a max
loss keeps each loss small; it does not make a trade win.

`${CLAUDE_SKILL_DIR}` below is this skill's folder (where this SKILL.md lives).

```bash
python3 "${CLAUDE_SKILL_DIR}/size.py"                          # trader-in/plan.json + trader-in/config.json
python3 "${CLAUDE_SKILL_DIR}/size.py" --write                  # write trader-in/orders.json
python3 "${CLAUDE_SKILL_DIR}/size.py" --limit --write          # a LIMIT order at the plan's entry
python3 "${CLAUDE_SKILL_DIR}/size.py" --limit 1227.50 --json   # a LIMIT order at 1227.50
python3 "${CLAUDE_SKILL_DIR}/size.py" --weights RELIANCE=40,TCS=30,HDFCBANK=30   # weights mode, below
```

`--now "2026-10-19 09:50"` pins the clock (IST, UTC+05:30). A plan for
another day is refused: make a new one with india-exit.

## What it reads and writes

| file | what |
| --- | --- |
| `trader-in/config.json` | the rules (start from `config.example.json` in this folder) |
| `trader-in/plan.json` | the trade: symbol, exchange, product, entry, stop, target, flat-by (india-exit writes it) |
| `trader-in/prices.json` | optional: a fresh price above the plan's entry means a bigger risk (india-alerts, or Kite `get_ltp`) |
| `trader-in/portfolio.json` | optional: its cash caps the size (india-broker keeps it) |
| `trader-in/orders.json` | the one proposed order it writes with `--write` |

The script itself never calls Kite: Claude saves the Kite answers to files and
passes them in (below).

## With Zerodha Kite connected

If the user has Zerodha's Kite MCP connected (setup in india-broker), use it for the prices of the trade
itself. Read-only tools only here; orders stay in india-broker.

1. **Check the login** with a cheap read: `get_ltp` with instruments `["NSE:RELIANCE"]`. If it answers
   "log in first" (or any session error), call `login` and give the user the link. They sign in on
   Zerodha's own page in their browser. Never ask for their password, TOTP or any token.
2. **Make the calls**: `get_ltp` with `["NSE:SYMBOL"]` for the last price, and `search_instruments` with
   query `NSE:SYMBOL` for today's tick size and circuit limits (RELIANCE: instrument token 738561, tick 0.1,
   circuit 1066.10-1302.90 on 2026-10-23).
3. **Save the answers verbatim** (no editing) under `trader-in/kite/`, for example
   `trader-in/kite/RELIANCE-ltp-2026-10-23.json` and `trader-in/kite/RELIANCE-instrument-2026-10-23.json`.
   Keep the date in the instrument file name: its circuit limits are for that one day.
4. **Pass them in**: `python3 "${CLAUDE_SKILL_DIR}/size.py" --kite-ltp trader-in/kite/RELIANCE-ltp-2026-10-23.json
   --kite-instrument trader-in/kite/RELIANCE-instrument-2026-10-23.json`, or `--price RELIANCE=1190` for one
   price you just read. A Kite price counts as fresh (you just fetched it). Kite's tick and circuit limits win
   over the NSE table and `sec_list.csv`, but only when the file's date is the trade's date; otherwise it says
   so and falls back. Every price line says where it came from: (Kite, live), (Kite candles) or (Yahoo, free,
   can lag).

Without Kite it works on free data alone and never fails because Kite is absent.

## Session rules: the closing auction (CAS)

Since **2026-08-03** NSE runs a closing auction session (CAS, 15:15-15:35) for stocks that have F&O
contracts. For those "CAS stocks" continuous trading ends at **15:15**, and Zerodha squares off MIS at
**15:12** (support.zerodha.com, read 2026-10-23). Non-CAS stocks trade to 15:30 and Zerodha squares off MIS
at **15:25**. Each auto-squared order costs ₹50 + GST.

| | CAS stock (F&O) | non-CAS stock |
| --- | --- | --- |
| continuous trading ends | 15:15 | 15:30 |
| Zerodha MIS square-off | 15:12 | 15:25 |
| `flat_by` "auto" | 15:00 | 15:10 |
| no new entries after | 14:45 | 14:45 |

Which stocks are CAS: the symbols in NSE's F&O list (`fo_mktlots.csv`, index rows skipped), cached as
`trader-in/fo_list.txt` (one symbol a line, first line `# fetched YYYY-MM-DD`; india-alerts or india-exit
fetch it), passed with `--fo-list`. The plan's own `cas` wins; `--cas yes|no` and the config lists
`cas_symbols_extra` / `non_cas_symbols` override. With no list at all the status is unknown and the
stricter CAS times apply, and it says so. BSE listings follow the NSE status. Before 2026-08-03 every stock
used the non-CAS times.

## Step 1: set the rules once

If `trader-in/config.json` does not exist, copy `config.example.json` into
`trader-in/config.json` and ask the user for each value in plain words:

- **budget**: how many rupees this is for. Only money they can afford to lose (default ₹50,000).
- **cash_buffer_pct**: how much always stays in cash (default 10).
- **max_loss_per_trade_inr**: the most one trade may lose if the stop fills at the stop, charges included (default ₹500).
- **max_position_pct**, **max_order_inr**, **min_order_inr**: the biggest position (75% of the
  budget), the biggest order (₹40,000) and the smallest order (₹100).
- The day limits india-risk enforces: **max_daily_loss_inr** (₹1,500), **max_trades_per_day** (3),
  **min_reward_risk** (1.0), **require_stop** (true), **no_entries_before** / **no_entries_after**
  ("09:30" / "14:45") and **flat_by** ("auto"), all IST. "auto" means 15:00 for a closing-auction
  (F&O) stock and 15:10 for the rest, safely before Zerodha's MIS square-off (15:12 / 15:25, ₹50 + GST
  an order). An explicit "HH:MM" at or after the stock's square-off is pulled back to the default, with a note.
- **cas_symbols_extra** / **non_cas_symbols**: symbols to treat as closing-auction stocks or not, on top of NSE's F&O list.
- **product**: "MIS" (intraday, the default) or "CNC" (delivery). **exchange**: "NSE" (default) or "BSE".
- **blocked**: symbols they never want to trade.
- **allow_margin** stays false: MIS gives up to 5x leverage at Zerodha; this pack sizes on cash only.
- Keep **paper** true. Zerodha has no paper account, so paper means india-broker records a
  simulated fill on this computer and never calls Kite.

## Step 2: size the trade

```bash
python3 "${CLAUDE_SKILL_DIR}/size.py" --write
```

Whole shares only. Prices go on the NSE tick (from Apr 15 2025, reviewed
monthly by NSE: under ₹250 0.01, ₹250-1,000 0.05, ₹1,000-5,000 0.10,
₹5,000-10,000 0.50, ₹10,000-20,000 1.00, above 5.00). The stop and a long
target round down to the tick; a limit buy rounds to the nearest tick. The
size is the smallest of four limits:

| limit | shares |
| --- | --- |
| max loss | the most shares where shares x (entry minus stop) + round-trip charges fit in `max_loss_per_trade_inr` |
| position cap | budget x `max_position_pct` / entry |
| max order | `max_order_inr` / entry |
| cash | (budget minus the cash buffer) / entry, never more than the cash in `trader-in/portfolio.json` |

The charges (Zerodha's list as of 2026-10, check your broker's): brokerage
on intraday ₹20 or 0.03% per executed order, whichever is lower (delivery
free); STT 0.025% on the intraday sell (0.1% on both sides for delivery);
NSE transaction charge 0.00297% (BSE 0.00375%); SEBI ₹10 a crore; stamp
duty 0.003% on the intraday buy (0.015% delivery); GST 18% on brokerage,
exchange and SEBI. The max loss at the stop = shares x (entry minus stop) +
charges for buying at the entry and selling at the stop.

The entry it sizes at: the limit price with `--limit`. For a market order,
the plan's entry, or the price in `trader-in/prices.json` if it is fresh
(under 15 minutes old) and higher. A price that ran up since the plan means
a bigger loss at the same stop, so fewer shares. A lower price never shrinks
the risk.

It also refuses an entry at or within 1% of today's upper circuit when a Kite
instrument file for today is given. It refuses, and writes no order, when: the size is zero shares, the order is
under the minimum, reward:risk is under `min_reward_risk`, the stop is not
below the entry, the target is not above it (the move already happened), the
plan is for another day, the entry sits at the upper circuit, the symbol is blocked, the plan is a short, or the
stock is series BE / BZ (trade-to-trade, no intraday) with product MIS. Say
the reason to the user in one line.

## Step 3: show the user

Show the four limits, which one decided the size, the shares, the rupees,
the charges and the max loss at the stop. Say plainly that a stop is an
order, not a promise: in a fast market it can fill below the stop, and at a
lower circuit it may not fill at all. Then hand over to `/india-risk`.

## Example: RELIANCE on Monday 2026-10-19

Opening-range breakout on NSE. The opening range 09:15-09:30 was 1207.30 to
1223.40; the midpoint 1215.35 rounds down to the ₹0.10 tick: stop 1215.30.
The 09:45 five-minute bar closed at 1227.80 (entry, 09:50 IST). Target 1R:
1240.30. Risk 12.50 a share. The example config: ₹50,000 budget, 10% cash
buffer, ₹500 max loss, MIS.

| limit | the math | shares |
| --- | --- | --- |
| max loss | ₹500, 12.50 a share + charges (36 shares lose ₹496.58, 37 lose ₹510.38) | 36 |
| position cap | 75% of ₹50,000 = ₹37,500 / 1227.80 | **30 (decides it)** |
| max order | ₹40,000 / 1227.80 | 32 |
| cash | ₹50,000 minus ₹5,000 = ₹45,000 / 1227.80 | 36 |

30 shares = ₹36,834. Charges for the round trip at the stop ₹38.82
(brokerage ₹21.99, STT ₹9.11, exchange ₹2.18, SEBI ₹0.07, stamp ₹1.11, GST
₹4.36). Max loss at the stop: 30 x 12.50 = ₹375 + ₹38.82 = ₹413.82. On price
alone ₹500 / 12.50 would allow 40 shares; the charges bring it to 36. It
writes (shortened):

```json
{"variety": "regular", "exchange": "NSE", "tradingsymbol": "RELIANCE", "transaction_type": "BUY",
 "quantity": 30, "product": "MIS", "order_type": "MARKET", "validity": "DAY", "tag": "indiabudget",
 "exit_plan": {"kind": "sl-m+limit",
   "stop_leg":   {"transaction_type": "SELL", "quantity": 30, "product": "MIS", "order_type": "SL-M", "trigger_price": 1215.3, "...": "..."},
   "target_leg": {"transaction_type": "SELL", "quantity": 30, "product": "MIS", "order_type": "LIMIT", "price": 1240.3, "...": "..."}},
 "why": "opening-range breakout on RELIANCE: 30 shares, bound by your position cap (30 x 12.50 + ₹38.82 charges = ₹413.82 at the stop)",
 "plan": {"entry": 1227.8, "stop": 1215.3, "target": 1240.3, "risk_per_share": 12.5, "charges": 38.82,
          "max_loss": 413.82, "setup": "orb", "entry_time": "09:50", "flat_by": "15:10",
          "cas": true, "square_off": "15:12", "session_close": "15:15"}}
```

RELIANCE has F&O contracts, so it is a closing-auction stock: continuous trading ends at 15:15 and Zerodha
squares off its MIS at 15:12. The plan's 15:10 flat-by is before that, so it stays (with "auto" it would be
15:00). With `--kite-ltp` the 2026-10-23 Kite last price 1190.00 (Kite, live) is below the plan's entry, so
the size does not change; a Kite instrument file from 2026-10-23 is not used for a 2026-10-19 plan.

The top-level fields are exactly Kite's `place_order` arguments. Zerodha has
no bracket orders, so `exit_plan` holds the legs india-broker places after
the entry fills: for MIS an SL-M SELL and a LIMIT SELL (when one fills, the
other must be cancelled); for CNC one two-leg GTT (`place_gtt_order`, OCO),
its stop leg a limit 1% under the trigger so it has room to fill. `why`,
`exit_plan` and `plan` are never sent as part of the entry. With the stop at
600 instead, one share would lose far more than ₹500: no order. If the price
had run to 1233.84 by 10:22, the same plan sizes at 1233.80 with 18.50 risk
a share and reward:risk 0.35: refused, the move it was waiting for has
mostly happened.

## Weights mode (kept from v1)

`--weights RELIANCE=40,TCS=30,HDFCBANK=30` or `--targets FILE` splits a
budget across a list by weight: the cash buffer comes off the top, weights
are renormalised to 100, any stock over `max_position_pct` is pinned at the
cap and its excess shared out, slices under the minimum order are dropped.
Indian shares have no fractions, so it needs prices (`--prices
trader-in/prices.json`) and buys whole shares. These are plain CNC market
buys with no stop: india-risk blocks them while `require_stop` is true. Use
a separate config (`--config`) for a long-term split.

## Rules

- Proposals only. This skill never calls a broker tool.
- Never size money the user has not said is for this. Ask for the budget and the max loss.
- Every number comes from the config, the plan or the prices. Nothing is guessed.
- No projections: never say what a trade will make. A target is a plan, not a forecast.
- Free Yahoo prices are unofficial and can lag. The exchange's price is the one that fills.
- Tax, in one line: intraday profit is speculative business income; delivery held under 12 months
  is short-term capital gain at 20%. Not tax advice: ask a CA.

## What it never does

It never places, approves or schedules an order, never raises a limit on its
own, never shrinks a stop to fit more shares, and never sizes F&O, MTF / MIS
margin or short positions.

## Safety

- Education and tooling, not financial advice. A PASS is a rules check, not a recommendation.
- Paper trading by default (`"paper": true`): a simulated fill on this computer, never a Kite call.
  Going live is the user's own decision, in their own config.
- Nothing in this pack places an order except india-broker, and only when all three hold: india-risk
  passed that exact order today, the user typed an explicit yes for that one order in the chat, and the
  user clicked Allow once on Claude's own card because the Kite order tools are set to "Needs approval".
  Never set an order tool to "Always allow". No order without the user's yes.
- No F&O, margin, short selling or leverage unless the config allows it and the user confirms.
- Kite login happens in the user's browser on Zerodha's own page. Passwords, TOTP codes and API
  secrets never go in the chat.
