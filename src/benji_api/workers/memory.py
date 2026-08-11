import asyncio
import logging
import signal
from contextlib import suppress

from benji_api.config import get_settings
from benji_api.db.session import close_database
from benji_api.memory.embeddings import build_embedding_provider
from benji_api.memory.providers import build_memory_model_provider
from benji_api.memory.service import consolidate_next_memory_job

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


async def run() -> None:
    settings = get_settings()
    shutdown_requested = asyncio.Event()
    loop = asyncio.get_running_loop()

    def request_shutdown(shutdown_signal: signal.Signals) -> None:
        logger.info("Memory worker received %s; finishing current work", shutdown_signal.name)
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
    if not settings.memory_enabled:
        raise RuntimeError("Memory worker started while memory is disabled")
    model_provider = build_memory_model_provider(settings)
    embedding_provider = build_embedding_provider(settings)
    if model_provider is None or embedding_provider is None:
        raise RuntimeError("Memory worker requires configured model and embedding providers")
    logger.info("Memory worker started")
    try:
        while not shutdown_requested.is_set():
            handled = await consolidate_next_memory_job(
                settings=settings,
                model_provider=model_provider,
                embedding_provider=embedding_provider,
            )
            if not handled:
                with suppress(TimeoutError):
                    await asyncio.wait_for(
                        shutdown_requested.wait(),
                        timeout=settings.memory_worker_poll_interval_seconds,
                    )
    finally:
        await close_database()
        logger.info("Memory worker stopped")


def main() -> None:
    asyncio.run(run())


if __name__ == "__main__":
    main()
