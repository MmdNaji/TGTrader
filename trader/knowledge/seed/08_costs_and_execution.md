# Costs and execution: the edge that is lost before the analysis even starts

Most losing bots are not wrong about direction. They are right about direction and still lose,
because every trade pays a spread, two fees and some slippage, and nobody subtracted them.

## risk
- **Price every trade against its round trip before taking it**: The cost of a trade is entry fee + exit fee + spread + slippage on both sides. At a 0.1% taker fee that is roughly 0.2% before the market moves at all. If the target is not several times that, the trade is a losing trade with a good chart.
- **Require the target to be at least 3x the round-trip cost**: A target of 0.3% on a 0.2% round trip keeps a third of the win and all of the loss. Demand the reward be worth taking after costs, not before.
- **Count costs in R, not in percent**: If the round trip is 0.2% and the stop is 1%, costs are 0.2R. A system with a 0.3R average edge loses two-thirds of it to fees. Add the cost to the stop distance when judging whether a setup is worth it.
- **A tighter stop is not free**: Halving the stop doubles the position for the same risk, which doubles the fees paid in currency. Very tight stops move the fee burden up until the strategy cannot win.
- **Never let the number of trades be the goal**: Costs scale linearly with trade count and edge does not. Doubling activity doubles the fees and rarely doubles the profit.
- **Slippage is worst exactly when the trade matters**: Fills are worst in fast moves, at news, and at the open of a session - the same moments a breakout looks most attractive. Assume a worse fill when the candle is unusually large.

## entry
- **Wait for the bar to close before acting on it**: A signal read from a bar still forming can disappear before that bar closes. Entering on an unclosed bar is entering on a signal that may never have existed.
- **Enter with a limit order at the level, not with a market order after the move**: Chasing a move that has already run pays the spread and buys the worst price of the swing. If price has already travelled most of the way to the target, the trade is gone; let it go.
- **Skip the trade when the spread is wide**: A spread that is a meaningful share of the stop distance means the market is illiquid right now. That is a reason not to trade, not a cost to absorb.
- **Trade the liquid hours**: Thin books widen spreads and exaggerate wicks. For crypto that is the weekend and the hours between the US close and the Asia open; for forex it is the late New York session.

## exit
- **Do not take a profit smaller than the cost of taking it**: Closing a winner that has not covered the round trip converts a small win into a small loss. Either the target has been reached or the exit reason is the stop, not impatience.
