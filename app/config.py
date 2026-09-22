from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    app_name: str = "MyTradingBot"
    environment: str = "paper"

    bybit_testnet: bool = False
    bybit_api_key: str = ""
    bybit_api_secret: str = ""
    bybit_category: str = "linear"

    # Five seconds keeps PAPER scanning responsive without turning the REST
    # scanner into an unnecessarily aggressive request loop.
    scan_interval_seconds: int = 5

    # Keep a liquidity floor so the order-book logic is not applied to thin
    # contracts where spread/slippage can dominate a short trade.
    min_24h_turnover_usdt: float = 5_000_000
    max_spread_bps: float = 12

    max_simultaneous_positions: int = 3
    default_leverage: int = 3
    default_budget: float = 100.0

    # 0.5% of PAPER equity is the maximum planned loss per position.
    risk_per_trade: float = 0.005
    paper_taker_fee_rate: float = 0.00055

    # Automatic entries require a stronger setup score after the anti-repaint,
    # trend and signal-persistence filters are applied.
    auto_min_confidence: float = 0.68
    auto_cooldown_seconds: int = 90
    max_hold_minutes: int = 30

    db_path: str = "data/tradingbot.db"

    model_config = SettingsConfigDict(
        env_file=".env",
        extra="ignore",
    )


settings = Settings()
