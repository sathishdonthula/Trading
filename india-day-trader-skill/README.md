# The Day Trader skill, India edition

11 Claude skills for day trading Indian stocks on NSE and BSE: scan thousands of charts, track corporate announcements, time every entry and exit, and replay every outcome before you risk a rupee.

A port of [Jake Schincariol's day-trader-skill](https://github.com/Jakeschincariol/day-trader-skill)
(MIT) to the Indian market: IST and NSE holidays, NSE tick sizes and circuit
limits, the closing-auction session times, Indian charges in every rupee of risk, Zerodha's Kite MCP for live
prices and orders, and SEBI insider trades and bulk deals in place of the
Congress tracker.

Free, MIT, no API key for the data. The tools are plain Python that read free
public data: Yahoo Finance charts (`.NS` / `.BO`), NSE's archive files, NSE's
corporate filings and Google News. Nothing in this pack places an order on its
own. Paper trading is the default.

## Install

Copy the folders into your skills:

```bash
cp -r india-day-trader-skill/india-* ~/.claude/skills/
```

Or project-local: copy them into your repo's `.claude/skills/`. Or as a plugin
from a local clone: `/plugin marketplace add ./india-day-trader-skill`, then
`/plugin install india-day-trader-skill@india-day-trader-skill`.

Python 3.8 or newer. Nothing to pip install.

### Zerodha Kite (optional, recommended)

The skills work on free data alone. Connect Zerodha's official Kite MCP
server and they prefer your broker's own data for the trade itself: live
prices (`get_ltp`, `get_quotes`), the exchange's 5-minute candles
(`get_historical_data`), and the day's official tick size and circuit limits
(`search_instruments`, which also covers F&O stocks that NSE's band file lists
as "No Band"). Through `/india-broker` only, it can also place real orders.

```bash
claude mcp add --transport http --scope user kite https://mcp.kite.trade/mcp
```

Or in Claude: Settings, Connectors, Add custom connector,
`https://mcp.kite.trade/mcp`. Then say "login to kite" and sign in on Zerodha's
own page (never type your password or TOTP into the chat). The login lasts the
trading day. Claude saves each Kite answer as-is under `trader-in/kite/`
and passes it to the Python tools (`--kite-bars`, `--kite-ltp`,
`--kite-instrument`); every price they print says where it came from (Kite or
Yahoo). The full-market scan and the Time Machine's 60-day history stay on
Yahoo: thousands of bars are too many to pass through the chat.

Kite and Yahoo bars differ a little. On October 19, RELIANCE's opening range
was 1,207.10 to 1,223.60 on Kite and 1,207.30 to 1,223.40 on Yahoo, and
Yahoo's 09:15 bar showed no volume at all. Prefer Kite for the trade.

Set every Kite order tool (`place_order`, `modify_order`,
`cancel_order`, `place_gtt_order`...) to "Needs approval", never "Always
allow".

## The eleven

| command | job | what it does |
| --- | --- | --- |
| `/india-scanner` | Chart Scanner | Scans every NSE EQ stock (or NIFTY 50 / 500) for setups: opening-range breakouts, flags, oversold bounces, with circuit bands. |
| `/india-news` | News Monitor | NSE corporate announcements (results, board meetings, ratings, deals, SEBI orders) and Indian headlines for every stock you watch. |
| `/india-entry` | Entry Timer | Watches a setup bar by bar and tells you the moment to get in. |
| `/india-exit` | Exit Planner | Sets your stop and target before you enter, on NSE's tick sizes, inside the circuit limits. |
| `/india-timemachine` | Time Machine | Replays the rest of the session 10,000 times plus every past time the setup fired, gross and net of charges. |
| `/india-risk` | Risk Manager | Checks every order against your limits, circuits, trade-to-trade stocks, NSE hours and Zerodha's square-off (15:12 for closing-auction stocks, 15:25 for the rest). |
| `/india-budget` | Budget Manager | Sizes every position to your budget and your max loss in rupees, charges included. |
| `/india-broker` | Order Executor | Sends approved orders to Zerodha through the Kite MCP, paper by default, never without your yes. |
| `/india-alerts` | Price Alerts | Rings on the stop, the target, flat-by, the 09:15 bell and circuit levels. |
| `/india-analyst` | Results Analyst | Reads quarterly results straight from the XBRL files filed with NSE and sums up what changed. |
| `/india-insiders` | Insider Tracker | SEBI PIT trades by promoters and directors, and NSE bulk and block deals by big investors (Kacholia, Kedia, funds). |

## How to use it

Everything lands in a `trader-in/` folder in your project, and each skill
reads what the last one wrote:

```
scanner -> news -> entry -> exit -> timemachine -> budget -> risk -> broker   (one trade)
alerts: stop, target, flat by 15:00 or 15:10                                  (while it is open)
news / analyst / insiders                                                      (on a schedule)
```

An example on real data: RELIANCE, Monday October 19 2026. An example on Yahoo's free bars (Kite's numbers are in each skill's SKILL.md).

1. **`/india-scanner`** at 10:15 IST: 8 of the NIFTY 50 had closed above their
   15-minute opening range (SBIN, ULTRACEMCO, HCLTECH, MARUTI, TCS, RELIANCE,
   TECHM, TITAN). RELIANCE's range: 1,207.30 to 1,223.40.
2. **`/india-entry`**: triggered at 9:50 AM, the 9:45 bar closed at 1,227.80,
   0.36 % over the range high.
3. **`/india-exit`**: stop at the range midpoint, 1,215.35, rounded down to the
   ₹0.10 tick: 1,215.30. ₹12.50 a share at risk. Target 1R: 1,240.30. Flat by
   15:00 either way: RELIANCE has F&O contracts, so it is a closing-auction
   (CAS) stock and Zerodha squares off its MIS positions at 15:12.
4. **`/india-timemachine`** replayed the rest of the day 10,000 times from
   RELIANCE's own 5-minute bars over the 56 sessions before (seed 19): target
   first 19.2 %, stop first 13.9 %, neither by 15:00 67.0 %. Average R +0.047
   before charges, -0.058 after them. The same rule fired 27 times in those
   sessions: 14 target, 10 stop, 3 neither. Charges turn a thin edge negative.
5. **`/india-budget`** with ₹50,000 and a ₹500 max loss: the position cap
   decides it, 30 shares (₹36,834). Charges ₹38.82. Max loss at the stop
   ₹413.82. One entry (BUY 30 NSE:RELIANCE, MARKET, MIS) plus an exit plan:
   SL-M SELL at 1,215.30 and LIMIT SELL at 1,240.30, because Zerodha has no
   bracket orders.
6. **`/india-risk`**: PASS. Max loss ₹413.82 of ₹500, reward:risk 1.0, 0 of 3
   trades today.
7. **`/india-broker`** shows the ticket in plain words and the exact Kite
   `place_order` JSON. You type yes for that exact order. On paper it records a
   simulated fill; live, Claude calls Kite and Claude's own "Allow once / Deny"
   card asks you once more.
8. **`/india-alerts`** watches the stop, the target and 15:00. That day the
   12:00 bar fell to 1,214.10: the stop rang at the 12:05 check. A loss of
   about ₹414, exactly the planned one.

## The tools

All standard-library Python.

```bash
python3 india-scanner/scan.py universe --index nifty50               # or nifty500, or all NSE EQ
python3 india-scanner/scan.py orb --at 10:15                         # opening-range breakouts so far
python3 india-news/news.py --symbols RELIANCE HDFCBANK --hours 12    # announcements + headlines
python3 india-entry/entry.py check RELIANCE                          # waiting / triggered / expired
python3 india-exit/exit.py plan --stop or-mid --target 1R --write    # -> trader-in/plan.json
python3 india-timemachine/timemachine.py run --write                 # 10,000 replays + the history
python3 india-budget/size.py --write                                 # -> trader-in/orders.json
python3 india-risk/check.py                                          # PASS / BLOCK per order
python3 india-broker/ticket.py show                                  # the tickets, READY or not
python3 india-alerts/alerts.py add --from-plan                       # stop, target, flat by
python3 india-alerts/alerts.py check --price RELIANCE=1227.8         # a Kite get_ltp price, or Yahoo
python3 india-analyst/results.py RELIANCE                            # quarters, growth, EPS
python3 india-insiders/insiders.py deals --investor kacholia         # bulk and block deals
```

```bash
python3 -m unittest discover -s tests -v
```

The tests run offline, on fixtures cut from real data taken October 23 2026:
RELIANCE's 5-minute bars for 60 sessions, the NIFTY 50 on October 19, NSE's
holiday list, price bands, announcements, XBRL results, PIT filings and deals.

## India notes

- **Hours**: pre-open order entry 09:00 to 09:10 (market orders only until
  09:05, since September 7 2026), continuous trading from 09:15.
- **Closing auction (CAS)**: since August 3 2026, stocks with F&O contracts
  (about 213, from NSE's `fo_mktlots.csv`) stop continuous trading at 15:15 and
  close in an auction from 15:15 to 15:35; Zerodha squares off their MIS
  positions at 15:12. Every other stock trades to 15:30, square-off 15:25. The
  tools mark each stock, and when they cannot tell they use the stricter CAS
  times and say so. Defaults: no entries before 09:30 or after 14:45; flat by
  "auto" = 15:00 for CAS stocks, 15:10 for the rest.
- **Holidays** come live from NSE, with the 2026 list built in. NSE has not
  published 2027 yet.
- **Tick sizes** follow NSE's table (₹0.01 under ₹250, ₹0.05 to ₹1,000, ₹0.10 to
  ₹5,000, then ₹0.50, ₹1 and ₹5). NSE reviews it monthly.
- **Circuits**: each stock's price band (2 to 20 %) comes from NSE's
  `sec_list.csv`. Entries near the upper circuit are blocked. F&O stocks have no
  fixed band.
- **Trade-to-trade** (series BE / BZ) stocks cannot be traded intraday and are
  left out of the scan and blocked for MIS.
- **Charges** (Zerodha, as of October 2026): brokerage, STT, exchange fees, SEBI
  fee, stamp duty and GST, counted in every max loss. Check your broker's list.
- **Long only.** Intraday shorting is legal in India, but this pack does not do
  it. No MIS leverage unless you turn on `allow_margin`.

## Fine print

**Not financial advice.** This is software that reads public data and does
arithmetic. It does not know your situation and it cannot see the future. SEBI's
own study found that 7 in 10 intraday traders in the cash segment lost money in
FY 2022-23. You can lose money. Decide for yourself, and talk to a
SEBI-registered adviser if you need advice.

**No trade goes through until you say yes.** Only `/india-broker` can lead to
an order, and only after `/india-risk` passes that exact order and you type yes
for it in the chat. Set Kite's order tools to "Needs approval", so Claude's own
"Allow once / Deny" card is a second check.

**Paper first.** Zerodha has no paper account, so `"paper": true` (the default)
means the broker skill records a simulated fill locally and never calls Kite.

**The Time Machine is a simulation, not a prediction.** It is built from the
stock's own last 60 days of 5-minute bars. Results, news, circuits and the
market can do things those days never did.

**A stop is a plan, not a guarantee.** In a fast market, across a gap or at a
circuit, a stop can fill well past its price, or not at all.

**The data is free and unofficial.** Yahoo's chart endpoint and NSE's website
APIs are not official APIs: they can lag, change, block scripts or rate-limit
you. Yahoo's bars can differ from the exchange's by a tick or two, so prefer
Kite for the trade. Check anything that matters against Kite before you
act on it.

**Insider trades and bulk deals are public disclosures, not inside
information,** and not a reason to buy on their own.

**Taxes.** Intraday equity gains are speculative business income; delivery
gains under 12 months are short-term capital gains. Nothing here is tax advice.

## Files

```
india-scanner/     SKILL.md, scan.py
india-news/        SKILL.md, news.py
india-entry/       SKILL.md, entry.py
india-exit/        SKILL.md, exit.py
india-timemachine/ SKILL.md, timemachine.py
india-risk/        SKILL.md, check.py
india-budget/      SKILL.md, size.py, config.example.json
india-broker/      SKILL.md, ticket.py, claude_desktop_config.example.json
india-alerts/      SKILL.md, alerts.py, alerts.example.json
india-analyst/     SKILL.md, results.py, metrics.example.json
india-insiders/    SKILL.md, insiders.py
tests/             unittest suite + offline fixtures
```

MIT. Original pack by [Jake Schincariol](https://opusjake.ai); India port by
its contributors.
