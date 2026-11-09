---
name: india-risk
description: >-
  Risk Manager for NSE / BSE trades. Checks every order against your limits
  and stops anything over them, before it can reach Zerodha. For a day trade:
  a stop is required, the loss at the stop plus round-trip charges must fit
  your max loss per trade in rupees (measured from the higher of the plan's
  entry and a fresh price), reward:risk must meet your minimum, the day's
  total risk must fit your daily loss limit, no more than your trades per
  day, no new entries outside your IST entry window or on an NSE holiday,
  and no MIS order at or after Zerodha's square-off for that stock (15:12
  for closing-auction F&O stocks, 15:25 for the rest). India rules on top:
  no entry at or near the upper circuit (Kite's limits for today when
  given), no intraday on BE / BZ
  trade-to-trade stocks, prices on the NSE tick, Kite place_order format, no
  F&O or MIS margin. Getting out is never blocked by a day limit. Prints PASS
  or BLOCK with the reason and records the verdicts so india-broker only
  shows an order that passed. Use when the user says "check my order", "is
  this safe", "risk check", "does this break my rules", "can I take this
  trade", "how much have I risked today", or before /india-broker, always.
argument-hint: "[path to orders.json]"
---

# india-risk

The Risk Manager is the gate between a proposal and the broker. Every order
written by india-budget or india-broker's flatten passes through it. A BLOCK
is final for that order: it does not go to the user for a yes, and it does
not go to Zerodha. Most people who day trade lose money; SEBI's study of
intraday traders found most of them lost. These limits keep each loss and
each day's losses small. They do not make a trade win.

`${CLAUDE_SKILL_DIR}` below is this skill's folder (where this SKILL.md lives).

```bash
python3 "${CLAUDE_SKILL_DIR}/check.py"                           # trader-in/ files; writes trader-in/state/risk.json
python3 "${CLAUDE_SKILL_DIR}/check.py" --json
python3 "${CLAUDE_SKILL_DIR}/check.py" --no-write                # look without recording
python3 "${CLAUDE_SKILL_DIR}/check.py" --now "2026-10-19 09:50"  # pin the clock (IST)
python3 "${CLAUDE_SKILL_DIR}/check.py" --kite-ltp trader-in/kite/RELIANCE-ltp-2026-10-23.json \
    --kite-instrument trader-in/kite/RELIANCE-instrument-2026-10-23.json --fo-list trader-in/fo_list.txt
```

## What it reads and writes

| file | what |
| --- | --- |
| `trader-in/orders.json` | the proposed orders (india-budget, or india-broker's flatten) |
| `trader-in/config.json` | the limits (india-budget ships `config.example.json`) |
| `trader-in/portfolio.json` | cash and holdings; if missing, a fresh account funded with the budget |
| `trader-in/prices.json` | a fresh price raises the entry it measures risk from; `detail.prev_close` sets the price band |
| `trader-in/sec_list.csv` | NSE's security list (series EQ / BE / BZ and the price band 2, 5, 10, 20 or No Band) |
| `trader-in/holidays.json` | NSE's live holiday list, cached by india-alerts; a 2026 list is built in |
| `trader-in/state/broker.json` | what was placed today: duplicates, trades per day, daily loss |
| `trader-in/state/risk.json` | the verdicts it writes, with a fingerprint of each exact order |

`sec_list.csv` is public: `https://nsearchives.nseindia.com/content/equities/sec_list.csv`
(send a browser user agent first, fall back to a plain one). Without it the
series and band checks are skipped with a warning. If the user has Zerodha's
Kite MCP connected, `get_quotes` or `get_ohlc` give a live price and the
previous close (`ohlc.close`) for `trader-in/prices.json`. check.py itself
never touches the network.

## Entries and exits

An **entry** opens or adds to a position: a BUY, or a SELL that would go
short. An **exit** only reduces what you already hold. Getting out must
always be possible, so an exit is checked for format, asset, short sales and
Zerodha's MIS square-off for that stock (15:12 CAS / 15:25 non-CAS) only. Every other rule (day limits, entry
window, loss, reward:risk, blocked list, size, duplicates) turns into a
warning for an exit, never a BLOCK.

## The checks

Orders are checked in file order, as if each passed order had filled, so a
batch that is fine order by order but breaks a limit together is caught.

| check | blocks when |
| --- | --- |
| format | not Kite `place_order` shape: variety regular, exchange NSE / BSE, a tradingsymbol, transaction_type BUY / SELL, a whole-share quantity, product MIS / CNC, order_type MARKET / LIMIT / SL / SL-M with its price or trigger, validity DAY / IOC / TTL, a price off the NSE tick, a tag over 20 letters and digits; an exit plan whose legs are not SELL, not the same symbol, quantity or product, or not SL-M + LIMIT (MIS) / a two-leg GTT (CNC) |
| asset | NFO / BFO / MCX, a future or option symbol (`allow_options` false), product NRML or MTF |
| short | a sell of more than you hold (`allow_short` false) |
| square-off | any MIS order at or after the stock's Zerodha square-off: 15:12 for a closing-auction (CAS) stock, 15:25 for the rest, 15:12 when unknown; an MIS entry whose `flat_by` is at or after it; an entry after a CAS stock's 15:15 close |
| blocked | (entries) the symbol is on the `blocked` list |
| series | (entries) series BE / BZ (trade-to-trade) with product MIS: no intraday allowed |
| circuit | (entries) the entry at or within 1% of the upper circuit: Kite's limits for today (`--kite-instrument`) win, else NSE's band on the previous close |
| size | (entries) under `min_order_inr` or over `max_order_inr` |
| position | (entries) the stock would be over `max_position_pct` of the account |
| cash | (entries) the buy would leave less than the cash buffer, or need margin (MIS leverage) |
| stop | (entries) no stop while `require_stop` is true (the exit plan's stop leg, or the plan's stop); a stop on the wrong side |
| loss | (entries) qty x (entry minus stop) + round-trip charges over `max_loss_per_trade_inr` |
| reward:risk | (entries) (target minus entry) / (entry minus stop) under `min_reward_risk` |
| daily loss | (entries) the max losses at the stops of today's entries, plus this one, over `max_daily_loss_inr` |
| trades | (entries) `max_trades_per_day` entries already placed today |
| window | (entries) before `no_entries_before` (09:30) or after `no_entries_after` (14:45) IST, or the market is closed |
| duplicate | (entries) the same symbol and side twice in one batch, or already placed today |
| daily limit | (entries) `max_orders_per_day` orders already placed today |

How the numbers work:

- **The entry it measures from**: the limit price for a LIMIT order. For a
  MARKET order, the HIGHER of the plan's entry and a fresh price from
  `trader-in/prices.json` (under 15 minutes old). A plan written at 09:50
  cannot understate the risk at 10:25 after the price ran up.
- **Charges** are Zerodha's list as of 2026-10 (check yours): intraday
  brokerage min(₹20, 0.03%) per order, STT 0.025% on the sell, NSE 0.00297%,
  SEBI ₹10 a crore, stamp 0.003% on the buy, GST 18%. Delivery: no
  brokerage, STT 0.1% both sides, stamp 0.015%.
- **Daily loss** counts the full max loss of every entry placed today, win or
  lose, as india-broker logged it. Three full-size RELIANCE trades (₹413.82
  each) would be ₹1,241.46; a fourth is over the ₹1,500 day.
- **The window** is IST (UTC+05:30, no daylight saving), worked out in the
  tool, never from the computer's clock zone. The normal session is
  09:15-15:30. Weekends and NSE trading holidays are closed (2026 built in;
  2027 is not published yet, so the tool warns and the live list is needed).
  Set both `no_entries_*` to null to switch the window off.
- **The price band**: NSE gives most stocks a 2, 5, 10 or 20% band around
  the previous close; F&O stocks like RELIANCE have "No Band" (they use
  dynamic limits instead), so no circuit check. A stop below the lower
  circuit is a warning: if the stock locks at the circuit, the stop cannot fill.

Each result also says its price source (`price_source`: plan, limit, kite or yahoo; `circuit_source`)
and the stock's `square_off` and `session_close`.

Warnings do not block, but say them to the user: an unknown CAS status, a Kite instrument file from
another day, a LIVE account, a quantity
sent as text, a stop that lives only in the plan, a price too old to use, a
missing security list or previous close, a year with no holiday list.

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

## With Zerodha Kite connected

If the user has Zerodha's Kite MCP connected (setup in india-broker), use it for the prices of the trade
itself. Read-only tools only here; orders stay in india-broker.

1. **Check the login** with a cheap read: `get_ltp` with instruments `["NSE:RELIANCE"]`. If it answers
   "log in first" (or any session error), call `login` and give the user the link. They sign in on
   Zerodha's own page in their browser. Never ask for their password, TOTP or any token.
2. **Make the calls**: `get_ltp` with `["NSE:SYMBOL"]` for the last price, and `search_instruments` with
   query `NSE:SYMBOL` for today's tick size and circuit limits (RELIANCE: instrument token 738561, tick 0.1,
   circuit 1066.10-1302.90 on 2026-10-23). For the previous close, `get_quotes` (its `ohlc.close`).
3. **Save the answers verbatim** (no editing) under `trader-in/kite/`, for example
   `trader-in/kite/RELIANCE-ltp-2026-10-23.json` and `trader-in/kite/RELIANCE-instrument-2026-10-23.json`.
   Keep the date in the instrument file name: its circuit limits are for that one day.
4. **Pass them in**: `python3 "${CLAUDE_SKILL_DIR}/check.py" --kite-ltp trader-in/kite/RELIANCE-ltp-2026-10-23.json
   --kite-instrument trader-in/kite/RELIANCE-instrument-2026-10-23.json`, or `--price RELIANCE=1190` for one
   price you just read. A Kite price counts as fresh (you just fetched it). Kite's tick and circuit limits win
   over the NSE table and `sec_list.csv`, but only when the file's date is the trade's date; otherwise it says
   so and falls back. Every price line says where it came from: (Kite, live), (Kite candles) or (Yahoo, free,
   can lag).

Without Kite it works on free data alone and never fails because Kite is absent.

## Steps

1. If the price may have moved since the plan, refresh it first
   (Kite `get_ltp` saved and passed with `--kite-ltp`, or india-alerts: `alerts.py quote RELIANCE --write`).
2. Run `check.py`. Exit code 0 means every order passed, 1 means at least one
   was blocked.
3. Tell the user the result in plain words, order by order: the max loss at
   the stop with the charges, reward:risk, and how much of today's limits it
   uses. For a BLOCK, give the reason and the fix (fewer shares, a closer stop
   from india-exit, wait for the window, CNC instead of MIS for a BE stock, or
   skip the trade). Never change a limit yourself.
4. If the order changes in any way, run the check again. india-broker matches
   each order (entry and exit legs) to its verdict by fingerprint, so an
   edited order is not ready until it passes again, and a verdict from
   yesterday does not count today.

## Example: RELIANCE on Monday 2026-10-19

The order from india-budget at 09:50 IST, on a fresh ₹50,000 paper account
with the example config:

```
PASS  #1 BUY NSE:RELIANCE 30 shares MIS, entry
          entry 1227.80 (the plan's entry), stop 1215.30, target 1240.30: risk 12.50 a share
          max loss ₹413.82 incl. ₹38.82 charges (limit ₹500), reward:risk 1.00 (min 1)
          today before this: 0 of 3 trades, ₹0 of ₹1,500 daily loss

1 passed, 0 blocked at 2026-10-19 09:50 IST. Cash after the passed orders: ₹13,166 (paper account).
```

With 37 shares (and the position cap and max order raised) it is blocked:

```
        - loss: 37 shares x 12.50 risk a share = ₹462.50 + ₹47.88 charges = ₹510.38 at the stop, over your ₹500 max loss per trade
```

The same 30 shares checked at 10:25 with a 10:22 price of 1233.84 in
`trader-in/prices.json` are blocked: reward:risk 0.35. At 09:25 they are
blocked by the window (no new entries before 09:30). On 2026-10-23, with the Kite instrument file,
RELIANCE's circuit is 1066.10-1302.90 (Kite instrument limits): a buy at 1295.00 is blocked as within 1% of
the upper circuit. On Tuesday 2026-10-20
the market is closed (NSE holiday: Dussehra). With `--fo-list` RELIANCE is a closing-auction stock
(square-off 15:12, close 15:15). The 15:10 SELL that closes the
position passes, even though it is after the 14:45 entry cutoff: it is an
exit. At 15:12 even that SELL is blocked as MIS: Zerodha's square-off has
started and it closes the position itself.

## Rules

- A PASS only means the order is allowed to be shown to the user. It is not a yes.
- Every number comes from the config, the order, the portfolio, the prices or the broker log.
- Free Yahoo prices are unofficial and can lag; the exchange's price is the one that fills, and a
  stop can fill worse than its price in a fast market.

## What it never does

It never places an order, never edits an order to make it pass, never
loosens a limit, never blocks a sell that only gets the user out (outside
Zerodha's own square-off), and never treats a PASS as the user's yes.

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
