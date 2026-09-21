from pydantic_settings import BaseSettings, SettingsConfigDict

class Settings(BaseSettings):
    app_name: str = "MyTradingBot"
    environment: str = "paper"
    bybit_testnet: bool = False
    bybit_api_key: str = ""
    bybit_api_secret: str = ""
    bybit_category: str = "linear"
    scan_interval_seconds: int = 5
    min_24h_turnover_usdt: float = 5_000_000
    max_spread_bps: float = 12
    max_simultaneous_positions: int = 3
    default_leverage: int = 3
    risk_per_trade: float = 0.005
    db_path: str = "data/tradingbot.db"
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

settings = Settings()
