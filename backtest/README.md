# NIFTY intraday backtests

Data: `nifty_5m.csv`, NIFTY 50 5-minute candles from Zerodha Kite, 2015-01-09 to 2026-11-06 (~219k bars).

| script | what |
| --- | --- |
| `orb_nifty.py` | 15-min opening-range breakout, calls + puts, SL 60 / target 120, exit 15:00. P&L for 1 month, 6 months, 1 year, 5 years. `python3 orb_nifty.py nifty_5m.csv --trades` |
| `research.py` | Tests 1,080 rule variants, train up to 2024 / test 2025+ |
| `kite_to_csv.py` | Merges saved Kite `get_historical_data` JSON files into the CSV |

## Findings (after ~2 index points cost per trade)

- Original 15-min ORB (both sides, 60/120): no edge. 5 years to Nov 2026 about +1,745 pts gross, roughly break-even net; loses on an ATM option.
- **Best rule ("Strategy A")**: 09:15-09:45 range, skip days that open >0.5% from the previous close,
  buy a put on the first 5-min close below the range low, stop = range high, target = 3R, exit 15:00.
  - Blind check 2015 to Oct 2021: 825 trades, +1,907 pts (+2.3/trade)
  - Nov 2021 to 2026: 588 trades, +3,230 pts (+5.5/trade)
  - Positive in 11 of 12 years (2020: -398)
  - The same rule on calls has no edge.
- Edge is thin: about 3.6 index points a trade. Prefer ITM puts (higher delta, less theta). Paper trade first.

Not financial advice. Past results do not predict future results.
