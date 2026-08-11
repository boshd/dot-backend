import os
from dataclasses import dataclass, field
from functools import lru_cache


def _env(name: str, default: str) -> str:
    return os.getenv(name, default)


def _optional_env(name: str) -> str | None:
    value = os.getenv(name)
    return value if value else None


def _bool_env(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if not value:
        return default
    return value.lower() in {"1", "true", "yes", "on"}


def _database_url(value: str) -> str:
    """Use SQLAlchemy's asyncpg dialect for provider-supplied Postgres URLs."""
    if value.startswith("postgres://"):
        return f"postgresql+asyncpg://{value.removeprefix('postgres://')}"
    if value.startswith("postgresql://"):
        return f"postgresql+asyncpg://{value.removeprefix('postgresql://')}"
    return value


@dataclass(frozen=True, slots=True)
class Settings:
    app_name: str = "Dot API"
    app_version: str = "0.1.0"
    environment: str = field(default_factory=lambda: _env("APP_ENV", "development"))
    database_url: str = field(
        default_factory=lambda: _database_url(
            _env(
                "DATABASE_URL",
                "postgresql+asyncpg://benji:benji_dev@localhost:5432/benji",
            )
        )
    )
    database_pool_size: int = field(default_factory=lambda: max(1, int(_env("DB_POOL_SIZE", "5"))))
    database_max_overflow: int = field(
        default_factory=lambda: max(0, int(_env("DB_MAX_OVERFLOW", "5")))
    )
    database_pool_timeout_seconds: float = field(
        default_factory=lambda: max(1.0, float(_env("DB_POOL_TIMEOUT_SECONDS", "10")))
    )
    database_pool_recycle_seconds: int = field(
        default_factory=lambda: max(1, int(_env("DB_POOL_RECYCLE_SECONDS", "300")))
    )
    cors_origins: tuple[str, ...] = field(
        default_factory=lambda: tuple(
            origin.strip()
            for origin in _env(
                "CORS_ORIGINS",
                "http://localhost:3000,http://localhost:3001",
            ).split(",")
            if origin.strip()
        )
    )
    web_chat_dev_identity_enabled: bool = field(
        default_factory=lambda: _bool_env(
            "WEB_CHAT_DEV_IDENTITY_ENABLED",
            _env("APP_ENV", "development") != "production",
        )
    )
    web_app_url: str = field(
        default_factory=lambda: _env("WEB_APP_URL", "http://localhost:3000").rstrip("/")
    )
    generated_app_public_url: str = field(
        default_factory=lambda: _env(
            "GENERATED_APP_PUBLIC_URL",
            _env("WEB_APP_URL", "http://localhost:3000"),
        ).rstrip("/")
    )
    public_api_url: str = field(
        default_factory=lambda: _env("BENJI_PUBLIC_API_URL", "http://localhost:8000").rstrip("/")
    )
    integration_token_encryption_key: str | None = field(
        default_factory=lambda: _optional_env("INTEGRATION_TOKEN_ENCRYPTION_KEY")
    )
    integration_connect_link_ttl_minutes: int = field(
        default_factory=lambda: int(_env("INTEGRATION_CONNECT_LINK_TTL_MINUTES", "15"))
    )
    integration_oauth_state_ttl_minutes: int = field(
        default_factory=lambda: int(_env("INTEGRATION_OAUTH_STATE_TTL_MINUTES", "10"))
    )
    user_event_poll_interval_seconds: float = field(
        default_factory=lambda: float(_env("USER_EVENT_POLL_INTERVAL_SECONDS", "1"))
    )
    user_event_max_attempts: int = field(
        default_factory=lambda: int(_env("USER_EVENT_MAX_ATTEMPTS", "8"))
    )
    scheduled_task_max_attempts: int = field(
        default_factory=lambda: int(_env("SCHEDULED_TASK_MAX_ATTEMPTS", "8"))
    )
    google_oauth_client_id: str | None = field(
        default_factory=lambda: _optional_env("GOOGLE_OAUTH_CLIENT_ID")
    )
    google_oauth_client_secret: str | None = field(
        default_factory=lambda: _optional_env("GOOGLE_OAUTH_CLIENT_SECRET")
    )
    google_oauth_redirect_uri: str = field(
        default_factory=lambda: _env(
            "GOOGLE_OAUTH_REDIRECT_URI",
            "http://localhost:8000/api/v1/integrations/google/callback",
        )
    )
    google_calendar_webhook_url: str | None = field(
        default_factory=lambda: _optional_env("GOOGLE_CALENDAR_WEBHOOK_URL")
    )
    google_gmail_pubsub_topic: str | None = field(
        default_factory=lambda: _optional_env("GOOGLE_GMAIL_PUBSUB_TOPIC")
    )
    google_pubsub_push_verification_token: str | None = field(
        default_factory=lambda: _optional_env("GOOGLE_PUBSUB_PUSH_VERIFICATION_TOKEN")
    )
    plaid_client_id: str | None = field(default_factory=lambda: _optional_env("PLAID_CLIENT_ID"))
    plaid_secret: str | None = field(default_factory=lambda: _optional_env("PLAID_SECRET"))
    plaid_environment: str = field(default_factory=lambda: _env("PLAID_ENV", "sandbox"))
    plaid_country_codes: tuple[str, ...] = field(
        default_factory=lambda: tuple(
            code.strip().upper()
            for code in _env("PLAID_COUNTRY_CODES", "US,CA").split(",")
            if code.strip()
        )
    )
    plaid_webhook_url: str | None = field(
        default_factory=lambda: _optional_env("PLAID_WEBHOOK_URL")
    )
    plaid_redirect_uri: str | None = field(
        default_factory=lambda: _optional_env("PLAID_REDIRECT_URI")
    )
    plaid_request_timeout_seconds: float = field(
        default_factory=lambda: float(_env("PLAID_REQUEST_TIMEOUT_SECONDS", "15"))
    )
    firebase_project_id: str | None = field(
        default_factory=lambda: _optional_env("FIREBASE_PROJECT_ID")
    )
    firebase_service_account_json: str | None = field(
        default_factory=lambda: _optional_env("FIREBASE_SERVICE_ACCOUNT_JSON")
    )
    firebase_check_revoked: bool = field(
        default_factory=lambda: _bool_env("FIREBASE_CHECK_REVOKED", True)
    )
    auth_eligibility_ip_limit_per_minute: int = field(
        default_factory=lambda: max(1, int(_env("AUTH_ELIGIBILITY_IP_LIMIT_PER_MINUTE", "10")))
    )
    auth_eligibility_ip_limit_per_hour: int = field(
        default_factory=lambda: max(1, int(_env("AUTH_ELIGIBILITY_IP_LIMIT_PER_HOUR", "60")))
    )
    auth_eligibility_identifier_limit_per_hour: int = field(
        default_factory=lambda: max(
            1, int(_env("AUTH_ELIGIBILITY_IDENTIFIER_LIMIT_PER_HOUR", "10"))
        )
    )
    linq_api_key: str | None = field(default_factory=lambda: _optional_env("LINQ_API_KEY"))
    linq_webhook_secret: str | None = field(
        default_factory=lambda: _optional_env("LINQ_WEBHOOK_SECRET")
    )
    linq_phone_number: str = field(
        default_factory=lambda: _env("LINQ_PHONE_NUMBER", "+16463038325")
    )
    linq_api_base_url: str = field(
        default_factory=lambda: _env(
            "LINQ_API_BASE_URL", "https://api.linqapp.com/api/partner/v3"
        ).rstrip("/")
    )
    linq_request_timeout_seconds: float = field(
        default_factory=lambda: float(_env("LINQ_REQUEST_TIMEOUT_SECONDS", "8"))
    )
    linq_automated_replies_enabled: bool = field(
        default_factory=lambda: _bool_env("LINQ_AUTOMATED_REPLIES_ENABLED", True)
    )
    linq_share_contact_card_enabled: bool = field(
        default_factory=lambda: _bool_env("LINQ_SHARE_CONTACT_CARD_ENABLED", False)
    )
    agent_model_provider: str = field(
        default_factory=lambda: _env("AGENT_MODEL_PROVIDER", "openai")
    )
    agent_context_message_limit: int = field(
        default_factory=lambda: int(_env("AGENT_CONTEXT_MESSAGE_LIMIT", "40"))
    )
    agent_max_tool_rounds: int = field(
        default_factory=lambda: int(_env("AGENT_MAX_TOOL_ROUNDS", "5"))
    )
    agent_inter_bubble_delay_seconds: float = field(
        default_factory=lambda: float(_env("AGENT_INTER_BUBBLE_DELAY_SECONDS", "0.65"))
    )
    agent_typing_seconds_per_character: float = field(
        default_factory=lambda: float(_env("AGENT_TYPING_SECONDS_PER_CHARACTER", "0.022"))
    )
    agent_typing_max_delay_seconds: float = field(
        default_factory=lambda: float(_env("AGENT_TYPING_MAX_DELAY_SECONDS", "3.2"))
    )
    agent_group_ack_settle_seconds: float = field(
        default_factory=lambda: float(_env("AGENT_GROUP_ACK_SETTLE_SECONDS", "1.75"))
    )
    agent_follow_ups_enabled: bool = field(
        default_factory=lambda: _bool_env("AGENT_FOLLOW_UPS_ENABLED", True)
    )
    agent_follow_up_min_delay_seconds: int = field(
        default_factory=lambda: int(_env("AGENT_FOLLOW_UP_MIN_DELAY_SECONDS", "90"))
    )
    agent_follow_up_max_delay_seconds: int = field(
        default_factory=lambda: int(_env("AGENT_FOLLOW_UP_MAX_DELAY_SECONDS", "86400"))
    )
    agent_follow_up_max_chain_depth: int = field(
        default_factory=lambda: int(_env("AGENT_FOLLOW_UP_MAX_CHAIN_DEPTH", "1"))
    )
    agent_follow_up_max_attempts: int = field(
        default_factory=lambda: int(_env("AGENT_FOLLOW_UP_MAX_ATTEMPTS", "5"))
    )
    memory_enabled: bool = field(default_factory=lambda: _bool_env("MEMORY_ENABLED", False))
    memory_context_limit: int = field(
        default_factory=lambda: int(_env("MEMORY_CONTEXT_LIMIT", "12"))
    )
    memory_candidate_limit: int = field(
        default_factory=lambda: int(_env("MEMORY_CANDIDATE_LIMIT", "100"))
    )
    memory_worker_poll_interval_seconds: float = field(
        default_factory=lambda: float(_env("MEMORY_WORKER_POLL_INTERVAL_SECONDS", "1"))
    )
    memory_worker_max_attempts: int = field(
        default_factory=lambda: int(_env("MEMORY_WORKER_MAX_ATTEMPTS", "8"))
    )
    memory_embedding_provider: str = field(
        default_factory=lambda: _env("MEMORY_EMBEDDING_PROVIDER", "openai")
    )
    memory_model_provider: str = field(
        default_factory=lambda: _env("MEMORY_MODEL_PROVIDER", "openai")
    )
    memory_model: str = field(default_factory=lambda: _env("MEMORY_MODEL", "gpt-5.6-luna"))
    memory_reasoning_effort: str = field(
        default_factory=lambda: _env("MEMORY_REASONING_EFFORT", "low")
    )
    memory_embedding_model: str = field(
        default_factory=lambda: _env("MEMORY_EMBEDDING_MODEL", "text-embedding-3-small")
    )
    memory_embedding_dimensions: int = field(
        default_factory=lambda: int(_env("MEMORY_EMBEDDING_DIMENSIONS", "1536"))
    )
    openai_api_key: str | None = field(default_factory=lambda: _optional_env("OPENAI_API_KEY"))
    openai_model: str = field(default_factory=lambda: _env("OPENAI_MODEL", "gpt-5.6-terra"))
    openai_reasoning_effort: str = field(
        default_factory=lambda: _env("OPENAI_REASONING_EFFORT", "low")
    )
    web_search_enabled: bool = field(default_factory=lambda: _bool_env("WEB_SEARCH_ENABLED", True))
    web_search_provider: str = field(default_factory=lambda: _env("WEB_SEARCH_PROVIDER", "openai"))
    web_search_model: str = field(
        default_factory=lambda: _env(
            "WEB_SEARCH_MODEL",
            _env("OPENAI_MODEL", "gpt-5.6-luna"),
        )
    )
    web_search_reasoning_effort: str = field(
        default_factory=lambda: _env("WEB_SEARCH_REASONING_EFFORT", "low")
    )
    web_search_max_sources: int = field(
        default_factory=lambda: int(_env("WEB_SEARCH_MAX_SOURCES", "5"))
    )


@lru_cache
def get_settings() -> Settings:
    return Settings()
