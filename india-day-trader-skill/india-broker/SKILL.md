---
name: india-broker
description: >-
  Order Executor for Zerodha. Sends approved orders to Kite through
  Zerodha's official Kite MCP server, paper trading by default (Zerodha has
  no paper account, so paper is a simulated fill recorded on this computer
  and never a Kite call), never without your yes. Shows each order as a
  plain ticket (entry, stop-loss, target, max loss at the stop with charges)
  plus the exact Kite place_order call and the exit-leg calls (SL-M + LIMIT
  for MIS, a two-leg place_gtt_order for CNC), records your yes word for
  word, then Claude's own Allow once / Deny card asks again. Builds the sell
  ticket that closes a day trade by your flat-by time (before Zerodha's
  MIS square-off: 15:12 for closing-auction F&O stocks, 15:25 for the rest), records fills, and logs each trade's max loss so
  india-risk can hold the daily limit. Covers connecting the hosted Kite MCP
  (https://mcp.kite.trade/mcp) to Claude Desktop or Claude Code, the Kite
  login, and setting order tools to "Needs approval". Use when the user says
  "place the order", "buy it", "execute", "send it to Zerodha", "close my
  position", "square off", "get me out", "connect Kite", "connect Zerodha",
  or after /india-risk passes.
argument-hint: "[order number]"
---

# india-broker

The Order Executor is the only skill in this pack that can lead to a real
order, so it is the strictest. An order reaches the exchange only after three
separate gates, every time, for every order:

1. **india-risk passed this exact order today** (matched by fingerprint, entry and exit legs).
2. **The user typed an explicit yes for this one order** in the chat.
3. **The user clicked Allow once** on Claude's own permission card, which
   appears because the Kite order tools are set to "Needs approval".

Two yeses from the user, one PASS from the rules. No yes, no order.

`${CLAUDE_SKILL_DIR}` below is this skill's folder (where this SKILL.md lives).

```bash
python3 "${CLAUDE_SKILL_DIR}/ticket.py" show                     # every ticket, READY or not
python3 "${CLAUDE_SKILL_DIR}/ticket.py" show --kite-ltp trader-in/kite/RELIANCE-ltp-2026-10-23.json
python3 "${CLAUDE_SKILL_DIR}/ticket.py" approve --index 1 --reply "yes"
python3 "${CLAUDE_SKILL_DIR}/ticket.py" result --index 1 --status placed --broker-id ID --filled-qty 30 --filled-price 1228.10
python3 "${CLAUDE_SKILL_DIR}/ticket.py" exits --index 1 --last-price 1228.10
python3 "${CLAUDE_SKILL_DIR}/ticket.py" fill --symbol RELIANCE --side sell --qty 30 --price 1240.30
python3 "${CLAUDE_SKILL_DIR}/ticket.py" flatten --symbol RELIANCE --qty 30 --fo-list trader-in/fo_list.txt
python3 "${CLAUDE_SKILL_DIR}/ticket.py" log
```

`ticket.py` never talks to a broker. It renders tickets, checks that a reply
is a real yes for that order, records paper fills, builds the flatten ticket,
and keeps the log in `trader-in/state/broker.json`. `--now "2026-10-19 09:50"`
pins the clock (IST).

## What it reads and writes

| file | what |
| --- | --- |
| `trader-in/orders.json` | the proposed orders (india-budget writes it; `flatten` replaces it) |
| `trader-in/state/risk.json` | india-risk's verdicts; only a PASS from today is READY |
| `trader-in/state/broker.json` | every yes, no, placed, failed and filled (paper fills marked), with each entry's max loss |
| `trader-in/portfolio.json` | cash and shares, updated by paper approvals, `fill` and `result --filled-qty` |
| `trader-in/plan.json` | the flat-by time for `flatten` |

## Paper and live

`"paper": true` (the default). Zerodha has no paper or sandbox account. So
in paper mode, after the user's yes, `ticket.py approve` records a simulated
fill at the order's price (the limit, else the plan's entry, or
`--fill-price` from a live quote) in `trader-in/portfolio.json`. **No Kite
tool is called in paper mode**, not even with Allow once. The stop and the
target are simulated too: india-alerts watches them, and the one that rings
is recorded with `ticket.py fill`.

`"paper": false` is live: real money on the user's Zerodha account. Only the
user sets it, in their own config. Say "LIVE, real money" on every ticket.

## Setup: Zerodha Kite MCP (official)

Zerodha's own server is github.com/zerodha/kite-mcp-server (MIT), hosted at
`https://mcp.kite.trade/mcp`. Checked on 2026-10-23 against its README, its
source (`mcp/post_tools.go`, `mcp/market_tools.go`) and the hosted server's
own tool list (version v0.3.2). Its tools: `login`; read only `get_profile`,
`get_margins`, `get_holdings`, `get_positions`, `get_mf_holdings`,
`get_orders`, `get_order_history`, `get_order_trades`, `get_trades`,
`get_gtts`; market data `get_ltp`, `get_quotes`, `get_ohlc`,
`get_historical_data`, `search_instruments`; and the ones that act:
`place_order`, `modify_order`, `cancel_order`, `place_gtt_order`,
`modify_gtt_order`, `delete_gtt_order`.

1. Add the server.
   - **Claude Code**: `claude mcp add --transport http kite https://mcp.kite.trade/mcp`
   - **Claude Desktop**: Customize > Connectors > Add custom connector, URL
     `https://mcp.kite.trade/mcp`. Or Settings > Developer > Edit Config and
     add the block in `claude_desktop_config.example.json` here (it uses
     `npx mcp-remote`, so Node.js must be installed), then restart Claude Desktop.
   No API key goes in that file: the hosted server needs none.
2. **Log in.** First check with a cheap read: `get_ltp` with `["NSE:RELIANCE"]`. If it answers "log in
   first" (or any session error), call `login`. It returns a link to Zerodha's own
   login page. The user opens it and logs in there, in their browser, with
   their own password and TOTP. **Never in the chat.** Never ask for the
   password, the TOTP, a request token or an access token, and if the user
   pastes one anyway, tell them to delete that message and change their
   password. The Kite session ends every day (early morning), so `login`
   again each trading day, or when a tool says the session expired.
3. **Set the tool permissions.** In Claude Desktop: Customize > Connectors,
   open kite, and for each tool pick Always allow, Needs approval or Blocked:
   - `place_order`, `place_gtt_order`: **Needs approval**. Never Always allow.
   - `modify_order`, `cancel_order`, `modify_gtt_order`, `delete_gtt_order`: Needs approval.
   - read-only and market-data tools (`get_profile`, `get_margins`, `get_positions`, `get_holdings`,
     `get_orders`, `get_order_history`, `get_gtts`, `get_ltp`, `get_quotes`, `get_ohlc`): Always allow is fine.
   In Claude Code, keep the order tools on ask in `.claude/settings.json` and never in `allow`:
   `"permissions": {"ask": ["mcp__kite__place_order", "mcp__kite__place_gtt_order", "mcp__kite__modify_order",
   "mcp__kite__cancel_order", "mcp__kite__modify_gtt_order", "mcp__kite__delete_gtt_order"]}`.
4. Check it: ask Claude for `get_profile` and `get_margins` (read only).

If `place_order` is not in the tool list: Zerodha's README says the hosted
server may leave out the tools that place or change orders. Then the user
can self-host the server with their own Kite Connect app (keys in its `.env`
file, typed by the user, never in the chat), or place the order themselves
in the Kite app from the ticket.

**Live prices for the other skills.** With Kite connected, `get_ltp` (instruments
`["NSE:RELIANCE"]`) and `get_quotes` give live prices; Claude writes them to
`trader-in/prices.json` (`{"as_of": "...Z", "prices": {"RELIANCE": 1228.1},
"detail": {"RELIANCE": {"time": "...Z", "prev_close": 1184.5}}}`) or passes
`--price` to the Python tools. The scripts never call Kite themselves.

**No bracket orders at Zerodha.** Bracket orders (BO) were discontinued and
cover orders are not used here. So an entry goes in alone, and after it fills
the exit legs go in:
- **MIS**: two `place_order` calls, an SL-M SELL at the stop (`trigger_price`)
  and a LIMIT SELL at the target. They do not cancel each other: when one
  fills, cancel the other at once (`get_orders`, then `cancel_order`).
  india-alerts watches both and the flat-by time.
- **CNC**: one `place_gtt_order` with `trigger_type` "two-leg": upper leg =
  target, lower leg = stop, `last_price` = the fill price or `get_ltp`.
  Whichever triggers first cancels the other. A GTT places a LIMIT order when
  triggered, so the stop leg's limit sits 1% under its trigger.
- MARKET orders through the API may need market protection on some Kite
  accounts; if Kite rejects one, make it a LIMIT (india-budget `--limit`) and
  check it again.

## Steps: the entry

1. `ticket.py show`. Only READY tickets go to the user. A ticket that is NOT
   READY or BLOCKED goes back to `/india-risk`.
2. Show ONE ticket and ask: "Place this order? Reply yes or no." One order
   per question. Never offer "yes to all". The yes covers this one trade: its
   entry and its own stop and target legs, nothing else.
3. Run `ticket.py approve --index N --reply "<the user's reply, word for word>"`.
   If it prints "Not recorded", the reply was not an explicit yes for this
   order (for example "sure", "haan", "yes to all", or a yes that names a
   different stock or quantity). Ask again. Never turn a reply into a yes yourself.
4. **Paper**: approve prints the simulated fill (at `--fill-price`, the limit, a Kite price from
   `--kite-ltp` / `--price`, else the plan's entry, and it says which). Stop. No Kite call.
   **Live**: before asking for the yes, fetch `get_ltp` (read only) for the symbol, save it verbatim to
   `trader-in/kite/<SYMBOL>-ltp-<date>.json` and show the ticket with `--kite-ltp FILE`: the ticket then
   reads "last price 1190.00 (Kite, live, get_ltp)". Without it the ticket says NOT FETCHED. Only after
   "Recorded your yes", call `place_order` with exactly the JSON the ticket printed. Claude's card appears: Allow once or Deny.
5. Record what happened: `ticket.py result --index N --status placed
   --broker-id <order_id> --filled-qty Q --filled-price P` (from
   `get_order_history`), or `--status denied`, or `--status failed` with
   `--note`. A failed order is not retried without a new check and a new yes.
6. After the fill, `result` (or `ticket.py exits --index N --last-price P`)
   prints the exit-leg calls. Make each one exactly as printed; each gets its
   own Allow once card.

## Steps: while it is open, and out by the close

- india-alerts watches the stop, the target and the flat-by time
  (`alerts.py add --from-plan`).
- When the stop or the target fills, record it:
  `ticket.py fill --symbol RELIANCE --side sell --qty 30 --price 1215.30`,
  and (MIS, live) cancel the other leg with `cancel_order`.
- By `flat_by`, anything still open gets closed. "auto" is 15:00 for a closing-auction (CAS, F&O)
  stock and 15:10 for the rest. Zerodha squares off MIS itself at **15:12 for CAS stocks** (their
  continuous trading ends at 15:15) and **15:25 for the rest**, and charges ₹50 + GST for each order it
  closes. `flatten` reads the stock's status from the plan, `--fo-list trader-in/fo_list.txt` or
  `--cas yes|no`; unknown means the stricter 15:12, and it says so.
  1. Ask Kite how many shares are held: `get_positions` (read only).
  2. `ticket.py flatten --symbol RELIANCE --qty 30` writes a MARKET SELL for
     those shares to `trader-in/orders.json`.
  3. `/india-risk`. A sell that only closes what the user holds is never
     blocked by the daily limits or the entry window.
  4. `ticket.py show`, then the user's yes for that one order, as above.
  5. Live: first cancel the open stop and target legs (`get_orders`, then
     `cancel_order` each, Needs approval), or for CNC delete the GTT
     (`get_gtts`, `delete_gtt_order`), so they cannot sell the same shares
     again. Then `place_order` with the ticket, then `result`.

## Example: RELIANCE on Monday 2026-10-19

```
ORDER 1 of 1, PAPER (simulated here, nothing goes to Zerodha), Zerodha Kite
  BUY 30 NSE:RELIANCE, market, intraday (MIS), good for today
  stop-loss 1215.30 (SL-M, placed after the fill)
  target 1240.30 (LIMIT, placed after the fill)
  max loss about ₹413.82 incl. charges if the stop fills at 1215.30 (it can fill worse in a fast market)
  why: opening-range breakout on RELIANCE: 30 shares, bound by your position cap (30 x 12.50 + ₹38.82 charges = ₹413.82 at the stop)
  last price 1227.80 (Kite, live, get_ltp)
  READY: india-risk PASS, waiting for your yes
  Paper: after your yes, ticket.py records a simulated fill. No Kite call is made.
  These are the calls a live account would make:
    place_order with exactly:
      {"variety": "regular", "exchange": "NSE", "tradingsymbol": "RELIANCE", "transaction_type": "BUY",
       "quantity": 30, "product": "MIS", "order_type": "MARKET", "validity": "DAY", "tag": "indiabudget"}
  Step 2, after the entry fills, the exit legs (same yes, each its own Allow once card):
    place_order with exactly: {... "transaction_type": "SELL", "quantity": 30, "order_type": "SL-M", "trigger_price": 1215.3 ...}
    place_order with exactly: {... "transaction_type": "SELL", "quantity": 30, "order_type": "LIMIT", "price": 1240.3 ...}
```

The user replies "yes". In paper mode ticket.py records BUY 30 RELIANCE at
1227.80: cash ₹50,000 becomes ₹13,166, and the log keeps the ₹413.82 max
loss for india-risk's daily limit. In live mode Claude calls `place_order`
and the card reads "Claude wants to use place_order" with that JSON and two
buttons, Allow once and Deny. The `why`, the `plan` and the `exit_plan` stay
in the file; they are never part of the entry call. RELIANCE is a CAS stock: Zerodha would square off
its MIS at 15:12, so at 15:10 (the plan's flat-by) the flatten
ticket SELL 30 RELIANCE MARKET MIS passes india-risk as an exit.

## What it never does

- Never places an order without all three gates, and never two trades on one yes.
- Never calls a Kite tool that places or changes anything in paper mode.
- Never places orders from a scheduled or background task: nobody is there to say yes.
- Never asks for, reads, stores or repeats passwords, TOTP codes, API keys or tokens.
- Never switches `"paper"` to false. The user does that themselves.
- Never calls `modify_order`, `cancel_order` or `delete_gtt_order` except on this pack's own legs, as above.
- Never trades F&O, MTF or NRML, and never promises a fill price: a market order and a stop fill
  at whatever the market gives, and at a circuit they may not fill at all.

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
