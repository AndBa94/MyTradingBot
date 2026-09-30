def risk_budget(balance, risk_fraction):
    """Return the maximum cash loss allowed for one position."""
    return max(0.0, float(balance)) * max(0.0, float(risk_fraction))


def position_risk_per_unit(entry, stop, fee_rate=0.0, stop_slippage_rate=0.0):
    """Conservative per-unit loss estimate used by the sizing formula."""
    distance = abs(float(entry) - float(stop))
    if distance <= 0 or float(entry) <= 0:
        return 0.0
    fee_rate = max(0.0, float(fee_rate))
    stop_slippage_rate = max(0.0, float(stop_slippage_rate))
    return (
        distance
        + (float(entry) + abs(float(stop))) * fee_rate
        + float(entry) * stop_slippage_rate
    )


def position_size(balance, risk_fraction, entry, stop, max_leverage, fee_rate=0.0, stop_slippage_rate=0.0):
    """Size a position so stop loss, estimated fees and stop slippage fit the risk budget."""
    if balance <= 0 or risk_fraction <= 0 or entry <= 0 or max_leverage <= 0:
        return 0.0
    distance = abs(entry - stop)
    if distance <= 0:
        return 0.0

    risk_cash = risk_budget(balance, risk_fraction)
    per_unit_risk = position_risk_per_unit(
        entry, stop, fee_rate=fee_rate, stop_slippage_rate=stop_slippage_rate
    )
    qty = risk_cash / per_unit_risk
    notional_cap_qty = balance * max_leverage / entry
    return min(qty, notional_cap_qty)

def audit_position_size(balance, risk_fraction, entry, stop, leverage, fee_rate=0.0,
                        stop_slippage_rate=0.0, quantity=0.0, tolerance=1e-10):
    """Independent deterministic audit of a PAPER position's risk math.

    Reference cases are cross-checked with Wolfram during development tests.
    """
    balance = float(balance)
    entry = float(entry)
    stop = float(stop)
    leverage = float(leverage)
    quantity = float(quantity)
    risk_cash = risk_budget(balance, risk_fraction)
    per_unit = position_risk_per_unit(entry, stop, fee_rate, stop_slippage_rate)
    expected_qty = (risk_cash / per_unit) if per_unit > 0 else 0.0
    leverage_qty_cap = (balance * leverage / entry) if entry > 0 and leverage > 0 else 0.0
    expected_qty = min(expected_qty, leverage_qty_cap)
    margin = quantity * entry / leverage if leverage > 0 else float("inf")
    estimated_loss = quantity * per_unit
    return {
        "pass": (
            balance > 0 and entry > 0 and stop > 0 and leverage > 0
            and quantity >= -tolerance
            and quantity <= expected_qty + tolerance
            and estimated_loss <= risk_cash + tolerance
            and margin <= balance + tolerance
        ),
        "risk_cash": risk_cash,
        "per_unit_risk": per_unit,
        "expected_quantity": expected_qty,
        "quantity": quantity,
        "estimated_max_loss": estimated_loss,
        "margin": margin,
        "leverage": leverage,
    }
