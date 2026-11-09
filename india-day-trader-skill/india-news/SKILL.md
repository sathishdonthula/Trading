---
name: india-news
description: >-
  News Monitor for Indian stocks. Tracks breaking news on every NSE / BSE stock
  you watch and flags what moves it: each company's NSE corporate
  announcements (financial results, board meetings, dividends, bonus and
  splits, acquisitions, credit ratings, resignations of key managers, SEBI
  orders), Google News headlines from the India edition, and how far each
  stock is from yesterday's close. Shows only what is new since the last
  check, newest first, in India time (IST), tags each item (results, board
  meeting, dividend/bonus/split, deal, rating, management, regulatory, block/
  bulk deal, macro/RBI...) and never repeats one. Use when the user says "any
  news on RELIANCE", "why is HDFC Bank falling", "what's the news on my
  watchlist", "morning briefing", "pre-market news", "monitor the news", "did
  anything break", "check the announcements", "any board meeting", or after
  /india-scanner finds setups.
argument-hint: "[NSE symbols]"
---

# india-news

A chart can look perfect and still be about to gap on a results day, a SEBI
order or a credit downgrade. Before a setup becomes a trade, this skill reads
the two feeds that carry those first, for every stock on the watchlist, and
tells you only what is new. It also prints how far each stock has moved
today, so a headline and a move sit side by side.

In India the company's own word comes through the exchange: every listed
company must tell NSE and BSE about anything price-sensitive (SEBI LODR
Regulation 30), usually within 30 minutes of a board meeting ending. These
corporate announcements are the Indian version of a US 8-K, and this skill
reads them first.

Tool in this folder (standard library Python, no keys, no login):

```bash
python3 "${CLAUDE_SKILL_DIR}/news.py"                               # watchlist + plan + portfolio
python3 "${CLAUDE_SKILL_DIR}/news.py" --symbols RELIANCE HDFCBANK --hours 12
python3 "${CLAUDE_SKILL_DIR}/news.py" --no-headlines --json          # announcements only
python3 "${CLAUDE_SKILL_DIR}/news.py" --rss                          # no NSE cookie: today's public RSS
```

## What it reads and writes

| file / feed | what |
| --- | --- |
| `--symbols`, else `trader-in/watchlist.json` | the scanner's hits (india-scanner); `name` helps the search |
| `trader-in/plan.json` | the trade you are planning (india-exit) |
| `trader-in/portfolio.json` | what you hold (india-broker records fills there) |
| NSE corporate announcements | `https://www.nseindia.com/api/corporate-announcements?index=equities&symbol=RELIANCE&from_date=..&to_date=..` (JSON, needs NSE's cookies: the tool visits the home page first, like a browser) |
| NSE announcements RSS | `https://nsearchives.nseindia.com/content/RSS/Online_announcements.xml`: public, no cookie, today only, matched on the company name. Used when NSE blocks the API, or with `--rss` |
| Google News RSS (India) | `https://news.google.com/rss/search?q="Reliance Industries" when:2d&hl=en-IN&gl=IN&ceid=IN:en` |
| Yahoo Finance chart endpoint | the last price against yesterday's close for `RELIANCE.NS` (or `.BO`); unofficial, can lag |
| `trader-in/state/news.json` | what has been shown (kept 30 days) and each symbol's company name |

Symbols are NSE symbols as you type them (`RELIANCE`, `M&M`, `BAJAJ-AUTO`).
`RELIANCE.BO` or `BSE:RELIANCE` takes the price from BSE; announcements come
from NSE either way (a company listed on both files the same text on both).
BSE's own API refuses scripts, so the tool does not use it. A bare BSE code
(`500325`) has no NSE symbol: the tool says so.

If the Zerodha Kite MCP server is connected in Claude, you may use its
`get_ltp` / `get_quotes` for a live price instead of the delayed Yahoo line.
The script itself never calls Kite.

## The rules

- **Headlines are leads, not facts.** Before telling the user something
  happened, open the filing PDF or the story and read it. Quote the source
  and link it. Never summarise a paywalled story you could not read: say so.
- **Filings beat headlines.** If an NSE announcement and a headline disagree,
  the announcement is what the company told the exchange. When NSE asks a
  company to clarify a news item ("News Verification"), the reply is in the
  announcements: read it.
- **Say what it touches.** Every item lists the symbols it touches and its
  tags: results, board meeting, dividend/bonus/split, buyback, deal, rating,
  management, regulatory, capital, insider/SAST, clarification, meeting/call,
  risk for filings; results, analyst, deal, dividend/bonus/split, rating,
  management, regulatory, block/bulk deal, capital, risk, circuit and macro
  (RBI, repo, FII flows, rupee, crude) for headlines. Filings marked `!` are
  the kind that gap a stock: results, dividend/bonus/split, buyback, a deal,
  a credit rating, a regulatory order, or a default/insolvency/fraud item.
- **Routine filings stay out.** Lost share certificates, newspaper copies,
  trading-window closures and NAV declarations are skipped unless you pass
  `--routine`.
- **MOVING** marks a stock 3 % or more from yesterday's close (`--move-pct`).
  A move with no news is worth saying out loud too. A stock near its price
  band (circuit) cannot be exited easily: see /india-risk.
- **No predictions.** Report what was published and how far the price is from
  yesterday's close. Never say what the stock will do next.

## Steps

1. Run the check: `python3 "${CLAUDE_SKILL_DIR}/news.py"`. Exit code 1 means
   nothing new. `--all` shows items already seen, `--json` gives everything
   machine readable, `--dry-run` leaves the state alone.
2. Read the items that matter: every `!` filing, anything tagged results,
   board meeting, regulatory, rating or risk, and every MOVING symbol first.
   Open the PDF link of a filing before describing it.
3. Write a short brief, per symbol: what happened, the source and link, the
   move, and one line on why it matters to the setup (for example "the board
   meets on Friday to approve results: an overnight hold carries the gap").
4. If a story changes the picture, say so before `/india-entry` and
   `/india-exit`: results or a board meeting inside the trade, a SEBI order, a
   rating change, a block deal. Never act on a headline alone.

If the tool notes that NSE refused the request, NSE is blocking scripts for a
while (it does that). The public RSS still covers today's announcements; say
that older announcements were not checked, and try again in a few minutes.

During the session: `/loop 15m /india-news`, or a scheduled task each morning
before the 09:15 open (results usually come out after 15:30 or on weekends,
so the morning check catches them).

## Example: Friday 2026-10-23

`news.py --symbols RELIANCE HDFCBANK` at 11:30 IST: the price lines read
`RELIANCE ₹1,169.90 -1.23% vs yesterday's close ₹1,184.50` and `HDFCBANK
₹690.05 -0.58% vs yesterday's close ₹694.05`. NSE announcements: `!
2026-10-21 20:43 IST RELIANCE NSE Reliance Industries: Credit Rating`
(tag: rating) and HDFC Bank's allotment of 91,38,522 ESOP shares on 22 Oct
(capital). Headlines: "Jio Platforms IPO: Here's how to apply under Reliance
Industries shareholders' quota" (Upstox, capital) and "HDFC Bank Shares Fall
2.3% After Q2 Results; Jefferies, PL, JM, Emkay See Up To 44% Upside"
(Goodreturns, results, analyst), plus reports of an internal probe into leaks
at HDFC Bank (regulatory, risk). The brief: "HDFC Bank reported Q2 on 17 Oct
and is under a news-driven cloud (internal probe reports); Reliance is moving
with the Jio IPO. Neither has a board meeting on the calendar this week. No
stock is MOVING."

## What it never does

- Never invents or embellishes a story, a quote or a number.
- Never trades on news, and never tells the user to buy or sell because of a
  headline.
- Never hammers a server: at most one request a second, one request per feed
  per symbol, a single retry, and it backs off when NSE refuses.

## Safety

Education and tooling, not financial advice. This skill only reads and
reports. Nothing places an order except `/india-broker`, after `/india-risk`
passes and the user types an explicit yes for that order.
