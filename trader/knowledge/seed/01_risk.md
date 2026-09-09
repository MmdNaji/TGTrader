# Risk management - the rules that keep an account alive

## risk
- **Fixed fractional risk**: Risk a fixed small fraction of equity per trade, 0.5-1% while a system is unproven and never more than 2%. Position size is derived from the distance to the stop, never from conviction.
- **Stop before entry**: Every entry has a stop-loss price decided before the order is sent. A trade without a predefined invalidation point is a guess, not a trade.
- **Daily loss limit**: After losing 3% of the capital limit in one day, stop trading for the day. Losses cluster; the next trade after a bad streak is usually taken with worse judgment.
- **Minimum reward-to-risk**: Do not take a trade whose realistic target is less than 1.5 times the stop distance; prefer 2 or more. With a 2R target you can be wrong 60% of the time and still profit.
- **Correlated exposure counts once**: BTC and ETH, or EURUSD and GBPUSD, move together. Two positions in correlated markets are one position with double risk; cap total correlated exposure as if it were one trade.
- **Never add to a loser**: Averaging down turns a small planned loss into a large unplanned one. Only add to positions that are already in profit and only if the stop on the whole position can be moved to break-even.
- **Volatility sizing**: Size positions by ATR so a normal bar cannot hit the stop. A 2-ATR stop is a sensible default; a stop tighter than 1 ATR is noise-fishing.
- **Expectancy over win rate**: Judge a method by average R per trade (win% x avg win - loss% x avg loss), not by how often it wins. A 35% win rate with 3R winners beats an 80% win rate with 0.3R winners.
- **Drawdown reduces size**: After a 10% drawdown from the equity peak, halve the risk per trade until a new equity high. Digging out of a hole with bigger bets is how accounts die.
- **Leverage is not an edge**: Leverage multiplies the outcome of a decision, not its quality. Keep effective leverage at or under 2x in crypto and 5x in forex for a discretionary system.

## exit
- **Break-even after 1R**: Once a trade is one stop-distance in profit, move the stop to the entry price. A winner should not be allowed to turn into a full loser.
- **Trail in trends, target in ranges**: In a trending regime, trail the stop (for example under the last swing low or 2 ATR from the high) and let it run. In a range, take profit at the opposite side of the range.
- **Time stop**: If a trade has gone nowhere after the number of bars the setup usually needs (about 10 bars on the entry timeframe), close it. Dead trades tie up risk and attention.
- **Exit on invalidation, not on pain**: Close when the reason for the trade is gone (a trend setup where price closes back below the trend average), not because the P&L is uncomfortable.
