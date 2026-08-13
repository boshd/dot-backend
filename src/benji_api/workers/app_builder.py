import asyncio
import logging
import os
import signal
import socket
from contextlib import suppress

from benji_api.app_builder.browser_smoke import (
    ChromiumAppAcceptanceRunner,
    QuickJSAppSmokeRunner,
    VerifiedAppSmokeRunner,
)
from benji_api.app_builder.pipeline import AppBuildPipeline, process_next_build
from benji_api.app_builder.providers import DeterministicLocalProvider, OpenAIAppSourceProvider
from benji_api.app_builder.service_hooks import GeneratedAppBuildServiceHooks
from benji_api.app_builder.visual_review import OpenAIVisualReviewer
from benji_api.db.session import close_database

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


def _positive_float(name: str, default: float) -> float:
    raw = os.getenv(name)
    value = float(raw) if raw else default
    if value <= 0:
        raise ValueError(f"{name} must be positive")
    return value


def _bounded_int(name: str, default: int, *, minimum: int, maximum: int) -> int:
    raw = os.getenv(name)
    value = int(raw) if raw else default
    if not minimum <= value <= maximum:
        raise ValueError(f"{name} must be between {minimum} and {maximum}")
    return value


def _chromium_sandbox_required_from_environment() -> bool:
    """Return whether Chromium's native sandbox must be used.

    The only production escape hatch is an explicit Railway-only fallback. Keeping the
    opt-in here, rather than in the browser runner, prevents a generic environment flag
    from silently weakening local or another provider's deployment.
    """

    name = "APP_BUILDER_ALLOW_UNSANDBOXED_CHROMIUM_ON_RAILWAY"
    raw = (os.getenv(name) or "false").strip().lower()
    if raw in {"false", "0", "no", "off"}:
        return True
    if raw not in {"true", "1", "yes", "on"}:
        raise ValueError(f"{name} must be true or false")
    if not (os.getenv("RAILWAY_PROJECT_ID") and os.getenv("RAILWAY_SERVICE_ID")):
        raise RuntimeError(f"{name}=true is only permitted in a Railway service")
    return False


def _source_provider_from_environment(
    *,
    timeout_seconds: float,
) -> DeterministicLocalProvider | OpenAIAppSourceProvider:
    environment = (os.getenv("APP_ENV") or "development").strip().lower()
    api_key = os.getenv("OPENAI_API_KEY")
    configured_provider = (os.getenv("APP_BUILDER_PROVIDER") or "").strip().lower()
    if not configured_provider:
        if environment == "production":
            raise RuntimeError("APP_BUILDER_PROVIDER must be explicit in production")
        configured_provider = "openai" if api_key else "local"
    if configured_provider == "local":
        if environment == "production":
            raise RuntimeError("The deterministic app builder is disabled in production")
        return DeterministicLocalProvider()
    if configured_provider == "openai":
        if not api_key:
            raise RuntimeError("OPENAI_API_KEY is required for the OpenAI app builder")
        return OpenAIAppSourceProvider(
            api_key=api_key,
            model=os.getenv("APP_BUILDER_MODEL", "gpt-5.6-terra"),
            reasoning_effort=os.getenv("APP_BUILDER_REASONING_EFFORT", "medium"),
            # One model call may legitimately consume most of the user-visible build window.
            # Keep the pipeline's global deadline authoritative instead of imposing a hidden
            # 50-second per-call cutoff that makes complex apps fail prematurely.
            timeout_seconds=min(timeout_seconds, 300.0),
        )
    raise RuntimeError(f"Unsupported app builder provider: {configured_provider}")


def _visual_reviewer_from_environment() -> OpenAIVisualReviewer | None:
    """Blocking-but-fail-open design review over the at-rest Chromium screenshot."""

    raw = (os.getenv("APP_BUILDER_VISUAL_REVIEW") or "true").strip().lower()
    if raw in {"false", "0", "no", "off"}:
        return None
    if raw not in {"true", "1", "yes", "on"}:
        raise ValueError("APP_BUILDER_VISUAL_REVIEW must be true or false")
    api_key = os.getenv("OPENAI_API_KEY")
    if not api_key:
        return None
    return OpenAIVisualReviewer(
        api_key=api_key,
        model=os.getenv("APP_BUILDER_VISUAL_REVIEW_MODEL", "gpt-5.6-terra"),
    )


async def run() -> None:
    shutdown_requested = asyncio.Event()
    loop = asyncio.get_running_loop()

    def request_shutdown(shutdown_signal: signal.Signals) -> None:
        logger.info("App builder received %s; finishing current build", shutdown_signal.name)
        shutdown_requested.set()

    for shutdown_signal in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(shutdown_signal, request_shutdown, shutdown_signal)
        except NotImplementedError:
            signal.signal(
                shutdown_signal,
                lambda *_args, selected=shutdown_signal: request_shutdown(selected),
            )

    poll_seconds = _positive_float("APP_BUILDER_POLL_INTERVAL_SECONDS", 0.5)
    timeout_seconds = _positive_float("APP_BUILDER_TIMEOUT_SECONDS", 420.0)
    repair_attempts = _bounded_int(
        "APP_BUILDER_MAX_REPAIR_ATTEMPTS",
        4,
        minimum=0,
        maximum=4,
    )
    lease_seconds = _bounded_int(
        "APP_BUILDER_LEASE_SECONDS",
        600,
        minimum=60,
        maximum=600,
    )
    worker_id = f"{socket.gethostname()}:{os.getpid()}"
    hooks = GeneratedAppBuildServiceHooks(worker_id=worker_id, lease_seconds=lease_seconds)
    provider = _source_provider_from_environment(timeout_seconds=timeout_seconds)
    browser_timeout_seconds = _positive_float(
        "APP_BUILDER_BROWSER_TIMEOUT_SECONDS",
        12.0,
    )
    chromium_sandbox_required = _chromium_sandbox_required_from_environment()
    if not chromium_sandbox_required:
        logger.warning(
            "Chromium native sandbox is disabled by the explicit Railway fallback; "
            "generated-app acceptance remains network-blocked but has weaker process isolation"
        )
    smoke_runner = VerifiedAppSmokeRunner(
        QuickJSAppSmokeRunner(
            timeout_seconds=min(10.0, max(1.0, timeout_seconds / 4)),
            simulate_first_click=True,
        ),
        ChromiumAppAcceptanceRunner(
            timeout_seconds=min(browser_timeout_seconds, max(2.0, timeout_seconds / 4)),
            sandbox_required=chromium_sandbox_required,
        ),
    )
    if not smoke_runner.available:
        raise RuntimeError("The app builder requires both QuickJS and real Chromium acceptance")
    visual_reviewer = _visual_reviewer_from_environment()
    pipeline = AppBuildPipeline(
        provider,
        max_repair_attempts=repair_attempts,
        timeout_seconds=timeout_seconds,
        smoke_runner=smoke_runner,
        require_browser_smoke=True,
        visual_reviewer=visual_reviewer,
    )
    logger.info(
        "App builder started with provider=%s visual_review=%s",
        pipeline.provider.name,
        visual_reviewer.name if visual_reviewer is not None else "disabled",
    )
    try:
        while not shutdown_requested.is_set():
            handled = await process_next_build(hooks, pipeline)
            if not handled:
                with suppress(TimeoutError):
                    await asyncio.wait_for(shutdown_requested.wait(), timeout=poll_seconds)
    finally:
        await close_database()
        logger.info("App builder stopped")


def main() -> None:
    asyncio.run(run())


if __name__ == "__main__":
    main()
