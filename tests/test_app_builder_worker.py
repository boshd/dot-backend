import pytest

from benji_api.app_builder.providers import DeterministicLocalProvider, OpenAIAppSourceProvider
from benji_api.workers.app_builder import (
    _chromium_sandbox_required_from_environment,
    _source_provider_from_environment,
)


def test_chromium_sandbox_is_required_by_default(monkeypatch) -> None:
    monkeypatch.delenv(
        "APP_BUILDER_ALLOW_UNSANDBOXED_CHROMIUM_ON_RAILWAY",
        raising=False,
    )

    assert _chromium_sandbox_required_from_environment() is True


def test_unsandboxed_chromium_fallback_is_rejected_outside_railway(monkeypatch) -> None:
    monkeypatch.setenv(
        "APP_BUILDER_ALLOW_UNSANDBOXED_CHROMIUM_ON_RAILWAY",
        "true",
    )
    monkeypatch.delenv("RAILWAY_PROJECT_ID", raising=False)
    monkeypatch.delenv("RAILWAY_SERVICE_ID", raising=False)

    with pytest.raises(RuntimeError, match="only permitted in a Railway service"):
        _chromium_sandbox_required_from_environment()


def test_unsandboxed_chromium_fallback_requires_explicit_railway_context(
    monkeypatch,
) -> None:
    monkeypatch.setenv(
        "APP_BUILDER_ALLOW_UNSANDBOXED_CHROMIUM_ON_RAILWAY",
        "true",
    )
    monkeypatch.setenv("RAILWAY_PROJECT_ID", "project-id")
    monkeypatch.setenv("RAILWAY_SERVICE_ID", "service-id")

    assert _chromium_sandbox_required_from_environment() is False


def test_development_defaults_to_local_provider_without_credentials(monkeypatch) -> None:
    monkeypatch.setenv("APP_ENV", "development")
    monkeypatch.delenv("APP_BUILDER_PROVIDER", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)

    provider = _source_provider_from_environment(timeout_seconds=55)

    assert isinstance(provider, DeterministicLocalProvider)


@pytest.mark.parametrize("configured_provider", [None, "local"])
def test_production_never_uses_implicit_or_local_provider(
    monkeypatch,
    configured_provider: str | None,
) -> None:
    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    if configured_provider is None:
        monkeypatch.delenv("APP_BUILDER_PROVIDER", raising=False)
    else:
        monkeypatch.setenv("APP_BUILDER_PROVIDER", configured_provider)

    with pytest.raises(RuntimeError, match="production"):
        _source_provider_from_environment(timeout_seconds=55)


def test_openai_provider_requires_a_key(monkeypatch) -> None:
    monkeypatch.setenv("APP_ENV", "development")
    monkeypatch.setenv("APP_BUILDER_PROVIDER", "openai")
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)

    with pytest.raises(RuntimeError, match="OPENAI_API_KEY"):
        _source_provider_from_environment(timeout_seconds=55)


def test_openai_app_builder_defaults_to_balanced_model(monkeypatch) -> None:
    monkeypatch.setenv("APP_ENV", "development")
    monkeypatch.setenv("APP_BUILDER_PROVIDER", "openai")
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    monkeypatch.delenv("APP_BUILDER_MODEL", raising=False)

    provider = _source_provider_from_environment(timeout_seconds=55)

    assert isinstance(provider, OpenAIAppSourceProvider)
    assert provider.model == "gpt-5.6-terra"
    assert provider.reasoning_effort == "medium"
