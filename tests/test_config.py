import pytest

from benji_api.config import Settings


@pytest.mark.parametrize(
    ("provider_url", "expected_url"),
    (
        (
            "postgres://dot:secret@postgres.railway.internal:5432/dot",
            "postgresql+asyncpg://dot:secret@postgres.railway.internal:5432/dot",
        ),
        (
            "postgresql://dot:secret@postgres.railway.internal:5432/dot",
            "postgresql+asyncpg://dot:secret@postgres.railway.internal:5432/dot",
        ),
        (
            "postgresql+asyncpg://dot:secret@postgres.railway.internal:5432/dot",
            "postgresql+asyncpg://dot:secret@postgres.railway.internal:5432/dot",
        ),
    ),
)
def test_normalizes_provider_postgres_urls(
    monkeypatch: pytest.MonkeyPatch,
    provider_url: str,
    expected_url: str,
) -> None:
    monkeypatch.setenv("DATABASE_URL", provider_url)

    assert Settings().database_url == expected_url


def test_database_pool_settings_are_configurable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DB_POOL_SIZE", "3")
    monkeypatch.setenv("DB_MAX_OVERFLOW", "2")
    monkeypatch.setenv("DB_POOL_TIMEOUT_SECONDS", "7.5")
    monkeypatch.setenv("DB_POOL_RECYCLE_SECONDS", "240")

    settings = Settings()

    assert settings.database_pool_size == 3
    assert settings.database_max_overflow == 2
    assert settings.database_pool_timeout_seconds == 7.5
    assert settings.database_pool_recycle_seconds == 240


def test_firebase_auth_settings_are_configurable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FIREBASE_PROJECT_ID", "dot-production")
    monkeypatch.setenv("FIREBASE_SERVICE_ACCOUNT_JSON", '{"type":"service_account"}')
    monkeypatch.setenv("FIREBASE_CHECK_REVOKED", "false")

    settings = Settings()

    assert settings.firebase_project_id == "dot-production"
    assert settings.firebase_service_account_json == '{"type":"service_account"}'
    assert settings.firebase_check_revoked is False
