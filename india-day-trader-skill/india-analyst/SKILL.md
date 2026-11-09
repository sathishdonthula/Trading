---
name: india-analyst
description: >-
  The Results Analyst for Indian stocks. Reads a company's quarterly results
  straight from the XBRL files it filed with NSE (Ind-AS, and the banking
  format for banks) and sums up what changed: revenue, EBITDA, operating and
  net margins, net profit and diluted EPS, quarter by quarter, each against
  the same quarter a year earlier, with the last four quarters, in rupee
  crore. Consolidated by default, standalone on request; restates EPS across
  a bonus or split; flags exceptional items and big other income; says when
  the next results are due. Reads a concall transcript when you paste one,
  and finds the company's own transcript filings. Use when the user says "how
  were Reliance's results", "analyse the Q2 numbers", "what changed this
  quarter", "is revenue growing", "compare margins", "HDFC Bank NII", "sum up
  the concall", "when are the results", "does it report this week", or before
  trading a stock around its results.
argument-hint: "[NSE symbols]"
---

# india-analyst

Results move Indian stocks more than anything else on the calendar, and the
press release and the investor presentation put the best number first. This
skill reads the numbers the company actually filed with the exchange, lines
them up quarter by quarter, and says plainly what changed. For a day trader it
answers two questions before a trade: what did the last results say, and are
the next ones inside this trade?

Tool in this folder (standard library Python, no keys, no login):

```bash
python3 "${CLAUDE_SKILL_DIR}/results.py" --help
```

## What it reads and writes

- NSE's results lists, through a cookie session like a browser:
  `api/integrated-filing-results?...&type=Integrated Filing- Financials`
  (filings from January 2025, SEBI's "integrated filing") and
  `api/corporates-financial-results?...&period=Quarterly` (older filings).
  Each entry links one XBRL file on `nsearchives.nseindia.com`, which the
  tool reads, one file per quarter, at most one request a second.
- NSE's event calendar (`api/event-calendar`) for the next board meeting.
- With `--transcripts`, the company's announcements of the last 120 days, to
  link the concall transcript, audio recording and investor presentation
  (SEBI makes companies file the recording within 24 hours and the
  transcript within five working days).
- Writes `trader-in/state/results-cache.json` (parsed filings, so a second
  run asks NSE only for the lists) and, with `--metrics`, one row per company
  in `trader-in/metrics.json`. `metrics.example.json` in this folder is a real
  run for RELIANCE and HDFCBANK on 2026-10-23.
- Earnings calls: if the user pastes a transcript (or you open the PDF that
  `--transcripts` links), read it and sum up what management said changed:
  guidance, margins, volumes, capex, debt, anything they declined to answer.

## The rules

- **Filed numbers first.** The table comes from the XBRL the company filed.
  Use the press release, presentation and call for context and say which
  numbers came from where.
- **Consolidated by default.** It is the group the market trades. A company
  without subsidiaries files standalone only, and the tool says so.
  `--standalone` shows the parent alone (for Reliance, mostly oil-to-chemicals;
  Jio and Retail are subsidiaries). Never compare a consolidated number with a
  standalone one.
- **Compare like with like.** Growth is against the same quarter a year
  earlier. Quarter-on-quarter changes are shown but many businesses are
  seasonal (Q3, October to December, carries the festive season).
- **Say what is derived.** India files no gross profit or EBITDA line.
  EBITDA here is profit before exceptional items and tax + finance costs +
  depreciation - other income; operating profit (EBIT) leaves depreciation
  in. Both leave other income out, so they can differ from the company's own
  EBITDA (Reliance includes other income in its own).
- **Bonus and splits.** EPS before a bonus issue or a split is restated to
  today's share count for the growth figure and marked `adj`. The EPS column
  itself is as filed.
- **Flag one-offs.** Exceptional items, and other income that is a quarter or
  more of profit before tax, this quarter or a year earlier, are called out:
  the growth figure may say more about the base than the business.
- **Banks are different.** Banks file the banking format: the table shows
  total income, net interest income (interest earned - interest expended),
  operating profit before provisions as a share of total income, net margin
  and EPS. NBFCs file Ind-AS: for them finance costs are the cost of the
  business, so EBITDA margins are not comparable with a manufacturer's; say
  so. Asset quality (GNPA, NNPA) is in the results PDF, not reliably in the
  XBRL: read it there.
- **Say when results are due.** Quarterly results are due within 45 days of
  the quarter's end, 60 for the March quarter (SEBI LODR Regulation 33). The
  tool prints the board meeting date from NSE's calendar, or the deadline when
  none is announced. When a board met but NSE's list does not have the
  numbers yet, it says so: read the "Outcome of Board Meeting" announcement.

## Steps

1. Run it for the companies in question (or the symbols on
   `trader-in/watchlist.json`):

   ```bash
   python3 "${CLAUDE_SKILL_DIR}/results.py" RELIANCE
   python3 "${CLAUDE_SKILL_DIR}/results.py" RELIANCE TCS HDFCBANK --metrics trader-in/metrics.json
   python3 "${CLAUDE_SKILL_DIR}/results.py" RELIANCE --transcripts
   ```

   `--quarters 8` shows two years, `--json` everything machine-readable,
   `--standalone` the parent company. If NSE refuses (it blocks scripts at
   times), wait a few minutes and run again; the cache keeps what was read.
2. Open the latest filing (the iXBRL link prints as a readable page) and the
   presentation or transcript when the numbers need context, and read them.
3. Write the summary: one line on the quarter (revenue, profit, EPS, growth),
   one on margins, one on anything unusual (exceptional items, other income,
   a bonus restatement, standalone only), the next results date, and the
   links. Plain words, real numbers in ₹ crore, no adjectives the numbers do
   not support.
4. If the next results fall inside the planned trade (an overnight or CNC
   hold, or results announced during market hours), say it before
   `/india-exit` sets the stop: a stop does not protect against a gap, and a
   stock can open past it or hit its price band.

## Example: the reference stock

`results.py RELIANCE` (run 2026-10-23): quarter ending 2026-06-30 (Q1 FY27,
consolidated, unaudited), revenue ₹3,11,850 cr, +25.4% against a year
earlier; net profit (owners' share) ₹20,946 cr, -22.4%; diluted EPS ₹15.48,
-22.4%. EBITDA margin 15.2%, operating margin 10.4%, net margin 6.7%. Last
four quarters: revenue ₹11,38,865 cr, net profit ₹74,727 cr, EPS ₹55.22. The
tool flags that a year earlier other income was ₹15,119 cr, 41% of profit
before tax, so the fall in profit is against a swollen base, and that EPS
growth for Q4 FY25 to Q2 FY26 is against EPS restated for the October 2024 1:1
bonus. Q2
FY27 (September quarter) results are not filed yet: due by 2026-11-14, no
board meeting on NSE's calendar on 23 October. The summary ends: "Results
are due within three weeks. A trade closed the same day is not exposed to
them; a CNC hold is."

`results.py HDFCBANK` (bank format): Q2 FY27 total income ₹1,29,359 cr
(+9.1%), NII ₹44,464 cr (+10.5%), operating profit before provisions ₹32,872
cr, provisions ₹4,209 cr, net profit ₹20,700 cr (+5.6%), diluted EPS ₹13.41
(+5.5%; the June 2025 EPS of
₹21.12 and older are restated for the 2025 1:1 bonus), exceptional items of
-₹837 cr inside the quarter.

## What it never does

- Never says a stock is cheap, expensive, a buy or a sell. It reports what
  the company filed.
- Never invents a number the filings do not have, or mixes consolidated and
  standalone figures.
- Never hammers NSE: one request a second, one retry, cached filings, and a
  plain message when NSE blocks.

## Safety

Education and tooling, not financial advice. This skill only reads filings.
Nothing places an order except `/india-broker`, after `/india-risk` passes
and the user types an explicit yes for that order.
