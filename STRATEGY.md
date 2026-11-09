# NIFTY "Calm-Range Breakdown" put strategy

Found by testing 990 rule variants on NIFTY 5-minute data (Jan 2015 to Nov 2026).
Rules chosen on 2015-2020 and confirmed on 2021-2026. Script: `backtest/research2.py`.

## Rules (one trade a day, puts only)

1. **Range:** the high and low of 09:15-10:00 (the first 45 minutes).
2. **Skip the day** if
   - NIFTY opened more than 0.5% above or below the previous close, or
   - the range is wider than 0.6% of the price (about 150 points at NIFTY 25,000).
3. **Entry:** buy an in-the-money weekly put (strike 100-150 points above spot, not expiring today)
   when a 5-minute candle **closes below the range low**. No new entry after **12:00**.
4. **Stop:** NIFTY **40 points** above the entry price.
5. **No target. Exit at 15:00.**

## Backtest (NIFTY points, after 2 points a trade for costs)

- 741 trades (~63 a year), win rate 45%, average win +54.7, average loss -33.0
- Total +5,104 points, +6.9 a trade, **positive in all 12 years**
- Worst drawdown -465 points; worst losing streak 12 trades
- One lot (65) of an ITM put (delta ~0.6) is roughly ₹10,000-38,000 a year, before theta

Not financial advice. Past results do not predict future results. Paper trade first.
