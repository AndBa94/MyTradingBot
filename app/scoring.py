"""Explainable multi-factor trade evaluator.

The evaluator is deliberately separate from signal generation. Strategies propose
setups; this module scores the evidence and the execution economics. It does not
pretend the score is a probability of profit.
"""

def _value(opportunity, key, default=0.0):
    prefix = f"{key}="
    for reason in getattr(opportunity, "reasons", []):
        if str(reason).startswith(prefix):
            try:
                return float(str(reason).split("=", 1)[1])
            except (TypeError, ValueError):
                return default
    return default


def _trend_score(opportunity):
    trend5 = _value(opportunity, "trend5", 0)
    trend15 = _value(opportunity, "trend15", 0)
    side = opportunity.side
    if side == "LONG":
        if trend5 == 1 and trend15 == 1: return 10.0
        if trend5 == 1 and trend15 == 0: return 8.5
        if trend5 == 0 and trend15 == 0: return 6.0
        if trend5 == 0 and trend15 == 1: return 7.5
        return 2.0
    if trend5 == -1 and trend15 == -1: return 10.0
    if trend5 == -1 and trend15 == 0: return 8.5
    if trend5 == 0 and trend15 == 0: return 6.0
    if trend5 == 0 and trend15 == -1: return 7.5
    return 2.0


def _volume_score(ratio):
    if ratio >= 1.80: return 10.0
    if ratio >= 1.50: return 9.0
    if ratio >= 1.30: return 8.0
    if ratio >= 1.15: return 7.0
    if ratio >= 1.00: return 5.0
    return 2.0


def _book_score(opportunity):
    imbalance = _value(opportunity, "book_imbalance", 0.5)
    flow = _value(opportunity, "flow_delta", 0.0)
    if opportunity.side == "LONG":
        edge = imbalance
        flow_edge = flow
    else:
        edge = 1.0 - imbalance
        flow_edge = -flow
    base = max(0.0, min(10.0, 2.0 + (edge - 0.50) * 40.0))
    if flow_edge >= 0.03: base += 1.0
    elif flow_edge >= 0.01: base += 0.5
    elif flow_edge < -0.03: base -= 1.0
    return max(0.0, min(10.0, base))


def _momentum_score(opportunity):
    body = _value(opportunity, "body", 0.0)
    close_loc = _value(opportunity, "close_loc", 0.5)
    if opportunity.side == "SHORT":
        close_loc = 1.0 - close_loc
    return max(0.0, min(10.0, body * 5.0 + close_loc * 5.0))


def _volatility_score(opportunity):
    atr_pct = _value(opportunity, "atr_pct", 0.0)
    if 0.15 <= atr_pct <= 0.80: return 10.0
    if 0.10 <= atr_pct < 0.15 or 0.80 < atr_pct <= 1.20: return 7.5
    if 0.05 <= atr_pct < 0.10 or 1.20 < atr_pct <= 1.60: return 5.0
    return 3.0


def _rr_score(opportunity):
    rr = _value(opportunity, "tp1_r", 0.0)
    if rr >= 3.0: return 10.0
    if rr >= 2.5: return 9.0
    if rr >= 2.0: return 8.0
    if rr >= 1.5: return 7.0
    if rr >= 1.2: return 6.0
    return 2.0


def _cost_score(opportunity):
    move = _value(opportunity, "tp1_move_pct", 0.0)
    cost = _value(opportunity, "net_cost_est_pct", 0.0)
    if cost <= 0: return 0.0
    ratio = move / cost
    if ratio >= 3.0: return 10.0
    if ratio >= 2.5: return 9.0
    if ratio >= 2.0: return 8.0
    if ratio >= 1.5: return 6.5
    if ratio >= 1.2: return 5.0
    return 2.0


def evaluate_opportunity(opportunity):
    """Return a transparent 0-10 evidence score and component breakdown."""
    components = {
        "trend": round(_trend_score(opportunity), 2),
        "volume": round(_volume_score(_value(opportunity, "volume_x", 0.0)), 2),
        "order_book": round(_book_score(opportunity), 2),
        "momentum": round(_momentum_score(opportunity), 2),
        "volatility": round(_volatility_score(opportunity), 2),
        "risk_reward": round(_rr_score(opportunity), 2),
        "cost": round(_cost_score(opportunity), 2),
    }
    weights = {
        "trend": 0.18,
        "volume": 0.14,
        "order_book": 0.18,
        "momentum": 0.15,
        "volatility": 0.10,
        "risk_reward": 0.15,
        "cost": 0.10,
    }
    score = sum(components[k] * weights[k] for k in components)
    return round(max(0.0, min(10.0, score)), 2), components
