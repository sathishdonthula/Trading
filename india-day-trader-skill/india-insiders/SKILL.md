---
name: india-insiders
description: >-
  The Insider and Big-Investor Tracker for Indian stocks. India has no STOCK
  Act for politicians, so this skill follows the two public records that do
  exist: SEBI PIT (Prohibition of Insider Trading) disclosures, where
  promoters, directors, KMP and their relatives report buying, selling or
  pledging their own company's shares, and NSE bulk and block deals, where
  "big bull" investors like Ashish Kacholia, Vijay Kedia, Mukul Agrawal,
  Rekha Jhunjhunwala, Dolly Khanna, mutual funds and FIIs show up by name.
  Alerts once per new filing or deal, turns each into clean rows (who,
  category, buy/sell/pledge, shares, value in ₹, holding before and after,
  mode, trade and disclosure dates, the delay), matches investor names
  loosely, keeps a saved list of investors and flags trades in stocks you
  hold. Use when the user says "what is Kacholia buying", "promoter buying",
  "insider trades in RELIANCE", "bulk deals today", "block deals", "did any
  big investor buy X", "track Vijay Kedia", "alert me when promoters sell",
  or "run the insiders skill".
argument-hint: "[SYMBOL | investor name] [--from YYYY-MM-DD]"
---

# india-insiders

Two kinds of public filings tell you who traded what in Indian stocks:

- **SEBI PIT disclosures.** Under the PIT Regulations 2015, a promoter,
  director, key managerial person (KMP), designated employee or one of their
  immediate relatives who trades more than ₹10 lakh in a quarter must tell the
  company within 2 trading days, and the company must tell the exchange
  within 2 trading days after that (Reg 7(2)). NSE publishes every one.
- **Bulk and block deals.** A bulk deal is one client trading 0.5% or more of
  a company's listed shares in a day. A block deal is one trade of ₹10 crore
  or more in the exchange's block window. NSE publishes both after the close,
  with the client's name. This is where people "follow" big investors.

This skill reads both fast. It is not early and it is not inside
information: a PIT row is up to about 4 trading days old, and a deal is
published after the day is over. Say that every time.

Tool in this folder (standard library Python 3.9+):

```bash
python3 "${CLAUDE_SKILL_DIR}/insiders.py" --help
```

## What it reads and writes

| file / URL | what |
| --- | --- |
| `https://www.nseindia.com/api/corporates-pit?index=equities&symbol=X` (+ `&from_date=DD-MM-YYYY&to_date=DD-MM-YYYY`) | PIT rows for one stock (NSE cookie session) |
| `https://nsearchives.nseindia.com/content/equities/bulk.csv`, `block.csv` | the last trading day's deals (public, no cookie) |
| `https://www.nseindia.com/api/historicalOR/bulk-block-short-deals?optionType=bulk_deals&from=..&to=..[&symbol=X]` | past deals (cookie session) |
| `trader-in/state/insiders.json` | what has been shown, so each filing or deal alerts once |
| `trader-in/investors.json` | your saved investor names (`{"investors": [...]}`) |
| `trader-in/portfolio.json` | what you hold (`positions[].symbol`), to flag and to check for PIT filings |
| `trader-in/watchlist.json` | the scanner's hits (`hits[].symbol`), also checked for PIT filings |

The tool waits at least a second between NSE calls, retries once, and says
clearly when NSE is blocking scripts (it happens: wait a few minutes).

## The rules

- **Quote the filing, not a tracker.** Every PIT row has NSE's disclosure id
  and the XBRL link; every deal row is from NSE's own file. Websites and
  Telegram channels that "track Kacholia" are fine for browsing but rename,
  merge and mis-date. The exchange record wins.
- **Names are as the broker reported them.** NSE prints the client name it
  was given: `ASHISH RAMESHCHANDRA KACHOLIA`, a family member, or a company
  the investor controls. `--investor` matches loosely (every word you type
  must match a word of the name, by prefix or close spelling, so `agarwal`
  finds `AGRAWAL`), and the full name is always printed. Check it is the
  person you mean: `kedia` will also match every other Kedia.
- **Not every big buyer is an investor.** A client that bought AND sold the
  same stock the same day (Jump Trading, Graviton, HRTI and other trading
  firms) is market-making or arbitrage, not building a position. The tool
  marks those rows "bought and sold the same day: trading, not a position".
- **Not every insider sale is a signal.** Gifts, pledges, off-market family
  transfers and ESOP sales are filed too, often with value ₹0. The tool shows
  the mode (market, off-market, gift, pledge, ESOP) and leaves value blank
  when NSE has 0. Pledges (created, revoked, invoked) are about loans, not a
  view on the price, though an invoked pledge means a lender sold.
- **Say the delay.** Each PIT row shows the calendar and trading days from
  the trade to NSE's broadcast; over 4 trading days is marked LATE.

## Step 1: insider trades in a stock

```bash
python3 "${CLAUDE_SKILL_DIR}/insiders.py" pit RELIANCE
python3 "${CLAUDE_SKILL_DIR}/insiders.py" pit RELIANCE --from 2025-01-01 --category promoter
python3 "${CLAUDE_SKILL_DIR}/insiders.py" pit HDFCBANK --action sell --json
```

`--category` takes promoter, director, kmp, relative, employee or other.
`--action` buy, sell or pledge. `--who` matches the insider's name.

## Step 2: bulk and block deals, and big investors

```bash
python3 "${CLAUDE_SKILL_DIR}/insiders.py" deals                              # last trading day
python3 "${CLAUDE_SKILL_DIR}/insiders.py" deals --investor kacholia --investor "vijay kedia"
python3 "${CLAUDE_SKILL_DIR}/insiders.py" deals --kind block --side buy
python3 "${CLAUDE_SKILL_DIR}/insiders.py" deals --from 2026-10-01 --to 2026-10-22 --symbol RAVITA
python3 "${CLAUDE_SKILL_DIR}/insiders.py" investors add "Ashish Kacholia" "Vijay Kedia" "Mukul Agrawal"
python3 "${CLAUDE_SKILL_DIR}/insiders.py" investors add "Rekha Jhunjhunwala" "Dolly Khanna"
python3 "${CLAUDE_SKILL_DIR}/insiders.py" deals --saved --from 2026-09-01
```

Without dates, `deals` reads today's or the last trading day's files. With
`--date` or `--from`, it asks NSE's history one day at a time (see the limits
below). Sorted newest first, then by value.

## Step 3: watch for new ones

```bash
python3 "${CLAUDE_SKILL_DIR}/insiders.py" new
```

Checks PIT filings for every stock you hold and every watchlist hit, and the
last trading day's deals for your saved investors and your stocks
(`--all-deals` for everything). Shows only what was not seen at the last
check (the first run shows PIT filings from the last 30 days), and records it
in the state file. Exit code 1 means nothing new. Deals land once a day after
the close (the 23 Oct 2026 file was up by the evening), PIT filings through
the day and evening, so checking every hour on weekdays is plenty: `/loop 1h`
or a scheduled task.

## Example: real rows (read 2026-10-23)

```
NSE bulk/block deals, 2026-10-23:
  - bulk deal SWIGGY: GRAVITON RESEARCH CAPITAL LLP sell 1,44,04,126 shares at ₹184.21
    = ₹2,65,33,84,050 (₹265.34 Cr) (bought and sold the same day: trading, not a position)
  - bulk deal TONBO: YALI DEEPTECH FUND I sell 24,00,000 shares at ₹1069.22
    = ₹2,56,61,28,000 (₹256.61 Cr)
SEBI PIT disclosures for RELIANCE:
  - Shaila Narayan (immediate relative), buy 3,120 shares, ₹44,30,088, off-market,
    traded 2026-02-13, on NSE 2026-02-18 (5 days, 3 trading days later)
```

Then: "These are public disclosures. Graviton is a trading firm that bought
and sold the same day; the RELIANCE row is a relative's off-market transfer
from February. None of it says what anyone will do next."

If the user wants to act on it, it goes the long way: a disclosure is a
reason to look at a chart, not a setup. `/india-scanner` and `/india-entry`
decide if there is a trade today; then cash equity only, sized by
`/india-budget`, checked by `/india-risk`, placed by `/india-broker` only
after their yes. Bulk-deal stocks are often small, illiquid or in the BE
(trade-to-trade) series, where intraday is not allowed; the risk check flags
it.

## Limits found on 2026-10-23

- **Whole-market PIT by date returns nothing.** `corporates-pit` with
  `from_date`/`to_date` and no symbol answered scripts with an empty list
  (the NSE web page shows rows, probably with cookies set by its own
  scripts). So PIT is per stock: give a symbol, or let `new` walk your
  holdings and watchlist. For a market-wide view, the user can open
  `https://www.nseindia.com/companies-listing/corporate-filings-insider-trading`
  in their browser and paste or save the table.
- **PIT by symbol without dates returns about 20 rows, not the newest 20**
  (RELIANCE gave rows from 2021 to 2026). Add `--from` to get a period.
- **Deal history is capped at 70 rows a call**, sorted by symbol, so on a busy
  day the rest of the alphabet is cut off. The tool asks one day at a time and
  prints a warning when a day hits 70; `--symbol X` (one stock) gets them all.
- **The NSE home page sometimes returns 403** to scripts while the API works;
  the tool warms its cookie session on the insider-trading page instead.
- BSE publishes its own bulk/block deals and PIT filings, but BSE's APIs
  refuse scripts. BSE-only deals are not covered; the user can check
  `bseindia.com` in their browser.
- SAST (takeover code) disclosures, shareholding patterns and mutual fund
  monthly portfolios also show big holders, with longer delays. Not read here.

## What it never does

- Never says or implies an insider or an investor has secret information, or
  that copying a "big bull" beats the market. No return claims.
- Never presents a fuzzy name match as certain: it prints the full name NSE
  filed.
- Never places an order. Nothing here is a reason to buy on its own.

## Safety

- Education and tooling, not financial advice. Nothing in this pack is a
  recommendation to buy or sell anything. Not tax advice.
- Paper trading by default (`"paper": true` in `trader-in/config.json`).
- Nothing places an order except `/india-broker`, and only after
  `/india-risk` passes AND the user types an explicit yes for that order in
  the chat, AND the Kite tool is set to "Needs approval" in Claude
  (Customize > Connectors > tool permissions) so Claude's own "Allow once /
  Deny" card appears too. Never "Always allow" an order tool. No order
  without your yes.
- Long only, cash only: no shorting, margin, F&O or leverage unless the
  config allows it and the user confirms.
- PIT disclosures and bulk/block deals are PUBLIC records, published days
  (PIT) or hours (deals) after the trade. This skill reads them fast; it has
  no inside information, and it says so.
