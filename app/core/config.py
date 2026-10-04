from functools import lru_cache
from typing import Annotated

from pydantic import field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str = "postgresql+asyncpg://chargehub:chargehub@localhost:5432/chargehub"

    secret_key: str = "dev-only-change-me"
    algorithm: str = "HS256"
    access_token_expire_minutes: int = 60

    environment: str = "development"

    #: Fabricate energy readings for active sessions, so the Active Session and
    #: receipt screens have moving data before any firmware exists. Development
    #: only — `get_settings()` refuses to start with this on in production,
    #: because an invented kWh figure on a real receipt is a billing lie.
    simulate_telemetry: bool = True

    #: How much faster than real time the simulated energy accrues. Real
    #: charging is slow — a 48 W draw needs 75 seconds to register its first
    #: whole watt-hour, so an honest 1× simulation looks broken on a demo.
    #: Energy is still clamped to the plan's cap, so this changes how quickly
    #: the cap is reached, never how much is delivered. Only ever applies when
    #: simulate_telemetry is on.
    simulate_speedup: int = 60

    # NoDecode stops pydantic-settings from trying to JSON-parse the env var
    # before our validator runs, which is what lets us accept a plain
    # comma-separated list instead of requiring '["http://..."]'.
    cors_origins: Annotated[list[str], NoDecode] = ["http://localhost:5173"]

    @field_validator("cors_origins", mode="before")
    @classmethod
    def _split_origins(cls, v: str | list[str]) -> list[str]:
        if isinstance(v, str):
            return [o.strip() for o in v.split(",") if o.strip()]
        return v

    @property
    def is_production(self) -> bool:
        return self.environment == "production"


@lru_cache
def get_settings() -> Settings:
    settings = Settings()
    if settings.is_production and settings.secret_key == "dev-only-change-me":
        raise RuntimeError("SECRET_KEY must be set to a real value in production")
    if settings.is_production and settings.simulate_telemetry:
        raise RuntimeError(
            "SIMULATE_TELEMETRY must be off in production — it invents energy "
            "readings, which would put fabricated figures on customer receipts."
        )
    return settings


settings = get_settings()
