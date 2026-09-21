def position_size(balance, risk_fraction, entry, stop, max_leverage, fee_rate=0.0):
    """Size a position so stop loss plus estimated entry/exit fees fit the risk budget."""
    if balance <= 0 or risk_fraction <= 0 or entry <= 0 or max_leverage <= 0:
        return 0.0
    distance = abs(entry - stop)
    if distance <= 0:
        return 0.0

    risk_cash = balance * risk_fraction
    fee_rate = max(0.0, float(fee_rate))
    per_unit_risk = distance + (entry + abs(stop)) * fee_rate
    qty = risk_cash / per_unit_risk
    notional_cap_qty = balance * max_leverage / entry
    return min(qty, notional_cap_qty)
