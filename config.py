from functools import lru_cache
from pathlib import Path

from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

ROOT = Path(__file__).resolve().parent


class Settings(BaseSettings):
    anthropic_api_key: SecretStr
    github_token: SecretStr
    model: str = "claude-haiku-4-5-20251001"
    max_tokens: int = 4096
    max_iterations: int = 10
    max_wall_clock_s: int = 300
    max_tokens_total: int = 200_000
    max_diff_bytes: int = 400_000
    db_path: str = "auto_pr.db"

    # Record/replay. live = call the API. record = call it and save the
    # responses. replay = read them off disk and never touch the network.
    model_mode: str = "live"          # live | record | replay
    cassette: str = ""                # cassette name under evals/cassettes/

    # Context budget. Caps cumulative tool output fed back into the loop,
    # independent of any single tool's own truncation.
    max_tool_bytes_total: int = 120_000

    model_config = SettingsConfigDict(
        env_file=str(ROOT / ".env"),
        extra="ignore",
        case_sensitive=False,
    )


@lru_cache
def get_settings() -> Settings:
    return Settings()
