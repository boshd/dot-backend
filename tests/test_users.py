import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from benji_api.db.base import Base
from benji_api.models.user import OnboardingStep, User
from benji_api.services.users import resolve_user_from_phone


@pytest.mark.anyio
async def test_resolves_the_same_user_for_the_same_phone_number() -> None:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    session_factory = async_sessionmaker(engine, expire_on_commit=False)

    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)

    async with session_factory() as session:
        first = await resolve_user_from_phone(session, "+1 (415) 555-2671")
        await session.commit()
        second = await resolve_user_from_phone(session, "+14155552671")
        await session.commit()

        user_count = await session.scalar(select(func.count()).select_from(User))

    await engine.dispose()

    assert first.created is True
    assert second.created is False
    assert first.user.id == second.user.id
    assert first.user.phone_number == "+14155552671"
    assert first.user.onboarding_step == OnboardingStep.NAME.value
    assert user_count == 1
