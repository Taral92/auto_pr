from functools import lru_cache
from pathlib import Path

from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

ROOT = Path(__file__).resolve().parent


class Settings(BaseSettings):
    # Which provider `agent/model_client.py` talks to. Both are kept live so
    # the same fixtures can be scored against each: Phases 1-4 produced
    # projections, never measurements, and deleting a provider before
    # measuring would destroy the only baseline there is.
    provider: str = "openai"          # openai | anthropic

    anthropic_api_key: SecretStr = SecretStr("")
    openai_api_key: SecretStr = SecretStr("")
    github_token: SecretStr
    # Cost is the primary constraint, so the default is the cheapest tier that
    # can run this agent: $0.20/$0.02/$1.20 per 1M in/cached/out, a tenth of
    # gpt-5.6-terra. Move up a tier only if the evals show Luna missing
    # defects - the grounding gate already drops hallucinated findings
    # mechanically, so precision is defended without paying for reasoning.
    model: str = "gpt-5.6-luna"
    max_tokens: int = 4096            # anthropic path only

    # OpenAI path. Every candidate model is a REASONING model, and reasoning
    # tokens are billed as output AND counted against max_output_tokens - they
    # can exhaust it before a single visible token is produced, which arrives
    # as status="incomplete". At the Anthropic path's 4096 that would starve
    # the submit_findings call and degrade every run, so this is deliberately
    # large. `low` effort is OpenAI's own recommendation for tool-use loops.
    max_output_tokens: int = 32_000
    reasoning_effort: str = "low"     # none | low | medium | high | xhigh | max
    # Fuses, not the work allowance. What a review costs is measured in tokens,
    # wall clock and tool bytes; these two only catch a loop that has stopped
    # getting anywhere, which no cost dimension can see because going nowhere
    # is cheap. Raised from the old max_iterations=10, which was doing both
    # jobs badly - see agent/budget.py.
    max_turns: int = 25
    max_unproductive_turns: int = 3
    max_wall_clock_s: int = 300
    max_tokens_total: int = 200_000
    max_diff_bytes: int = 400_000
    # Postgres
    database_url: str = "postgresql://autopr:autopr@localhost:5432/autopr"
    db_pool_size: int = 5

    # GitHub App
    github_app_id: str = ""
    github_app_private_key: SecretStr = SecretStr("")   # PEM or base64 PEM
    github_webhook_secret: SecretStr = SecretStr("")

    # Worker
    lease_s: int = 900                # MUST exceed max_wall_clock_s, or a slow
                                      # run gets reclaimed and reviewed twice
    max_attempts: int = 3
    poll_interval_s: float = 2.0

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
