def position_size(balance, risk_fraction, entry, stop, max_leverage, fee_rate=0.0, stop_slippage_rate=0.0):
    """Size a position so stop loss, estimated fees and stop slippage fit the risk budget."""
    if balance <= 0 or risk_fraction <= 0 or entry <= 0 or max_leverage <= 0:
        return 0.0
    distance = abs(entry - stop)
    if distance <= 0:
        return 0.0

    risk_cash = balance * risk_fraction
    fee_rate = max(0.0, float(fee_rate))
    stop_slippage_rate = max(0.0, float(stop_slippage_rate))
    estimated_stop_slippage = entry * stop_slippage_rate
    per_unit_risk = (
        distance
        + (entry + abs(stop)) * fee_rate
        + estimated_stop_slippage
    )
    qty = risk_cash / per_unit_risk
    notional_cap_qty = balance * max_leverage / entry
    return min(qty, notional_cap_qty)
