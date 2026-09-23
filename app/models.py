from dataclasses import dataclass, field
from datetime import datetime

@dataclass
class MarketSnapshot:
    symbol: str
    last: float
    bid: float
    ask: float
    turnover_24h: float
    change_24h: float
    volume_24h: float
    funding_rate: float = 0.0
    open_interest: float = 0.0
    spread_bps: float = 0.0

@dataclass
class Candle:
    timestamp: int
    open: float
    high: float
    low: float
    close: float
    volume: float

@dataclass
class Opportunity:
    symbol: str
    side: str
    regime: str
    setup: str
    confidence: float
    expected_move: float
    entry: float
    stop_loss: float
    take_profits: list[float]
    reasons: list[str] = field(default_factory=list)
    score_10: float = 0.0
    score_components: dict = field(default_factory=dict)
    decision: str = "WAIT"

@dataclass
class Position:
    id: str
    symbol: str
    side: str
    entry: float
    quantity: float
    stop_loss: float
    take_profits: list[float]
    opened_at: datetime
    leverage: int
    pnl: float = 0.0
    status: str = "OPEN"
    initial_quantity: float = 0.0
    realized_pnl: float = 0.0
    tp_index: int = 0
    last_price: float = 0.0
    initial_stop_loss: float = 0.0
    entry_fee: float = 0.0
    fees: float = 0.0
