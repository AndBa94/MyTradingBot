def position_size(balance,risk_fraction,entry,stop,max_leverage):
    risk_cash=balance*risk_fraction
    distance=abs(entry-stop)
    if distance<=0: return 0
    qty=risk_cash/distance
    return min(qty,balance*max_leverage/entry)
