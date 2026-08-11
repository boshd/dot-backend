import asyncio
import logging
import signal
from contextlib import suppress

from benji_api.agents.dependencies import build_model_provider
from benji_api.agents.tools import build_default_tool_registry
from benji_api.config import get_settings
from benji_api.db.session import close_database
from benji_api.integrations.linq.client import LinqClient
from benji_api.memory.embeddings import build_embedding_provider
from benji_api.services.agent_followups import dispatch_due_follow_up
from benji_api.services.schedules import dispatch_due_scheduled_task
from benji_api.services.user_events import dispatch_user_event

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


async def run() -> None:
    settings = get_settings()
    shutdown_requested = asyncio.Event()
    loop = asyncio.get_running_loop()

    def request_shutdown(shutdown_signal: signal.Signals) -> None:
        logger.info("Agent wake worker received %s; finishing current work", shutdown_signal.name)
        shutdown_requested.set()

    for shutdown_signal in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(
                shutdown_signal,
                request_shutdown,
                shutdown_signal,
            )
        except NotImplementedError:
            signal.signal(
                shutdown_signal,
                lambda *_args, selected=shutdown_signal: request_shutdown(selected),
            )
    linq_client = (
        LinqClient(
            api_key=settings.linq_api_key,
            base_url=settings.linq_api_base_url,
            timeout_seconds=settings.linq_request_timeout_seconds,
        )
        if settings.linq_api_key
        else None
    )
    provider = build_model_provider(settings)
    tools = build_default_tool_registry(settings)
    embedding_provider = build_embedding_provider(settings)
    logger.info("Agent wake worker started")
    try:
        while not shutdown_requested.is_set():
            handled_schedule = await dispatch_due_scheduled_task(settings=settings)
            if shutdown_requested.is_set():
                continue
            handled_event = await dispatch_user_event(
                settings=settings,
                provider=provider,
                tools=tools,
                linq_client=linq_client,
                embedding_provider=embedding_provider,
            )
            if shutdown_requested.is_set():
                continue
            handled_follow_up = await dispatch_due_follow_up(
                settings=settings,
                provider=provider,
                tools=tools,
                linq_client=linq_client,
                embedding_provider=embedding_provider,
            )
            if not handled_schedule and not handled_event and not handled_follow_up:
                with suppress(TimeoutError):
                    await asyncio.wait_for(
                        shutdown_requested.wait(),
                        timeout=settings.user_event_poll_interval_seconds,
                    )
    finally:
        await close_database()
        logger.info("Agent wake worker stopped")


def main() -> None:
    asyncio.run(run())


if __name__ == "__main__":
    main()
