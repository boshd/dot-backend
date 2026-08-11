import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from benji_api.db.base import Base
from benji_api.models.user import OnboardingStep, User, UserIdentifier, UserIdentifierKind
from benji_api.services.users import (
    UserIdentifierConflictError,
    UserIdentifierRevokedError,
    find_user_by_identifier,
    link_user_identifier,
    normalize_email_address,
    resolve_user_from_identifier,
    resolve_user_from_phone,
)


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
        identifier_count = await session.scalar(select(func.count()).select_from(UserIdentifier))

    await engine.dispose()

    assert first.created is True
    assert second.created is False
    assert first.user.id == second.user.id
    assert first.user.phone_number == "+14155552671"
    assert first.user.onboarding_step == OnboardingStep.NAME.value
    assert user_count == 1
    assert identifier_count == 1


@pytest.mark.anyio
async def test_email_handle_creates_one_email_only_canonical_user() -> None:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    session_factory = async_sessionmaker(engine, expire_on_commit=False)

    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)

    async with session_factory() as session:
        first = await resolve_user_from_identifier(
            session,
            "  Kareem@Example.COM ",
            source="linq",
        )
        await session.commit()
        second = await resolve_user_from_identifier(
            session,
            "kareem@example.com",
            source="linq",
        )
        await session.commit()
        identifiers = (await session.scalars(select(UserIdentifier))).all()

    await engine.dispose()

    assert normalize_email_address("Kareem@Example.COM") == "kareem@example.com"
    assert first.created is True
    assert second.created is False
    assert first.user.id == second.user.id
    assert first.user.phone_number is None
    assert len(identifiers) == 1
    assert identifiers[0].kind == UserIdentifierKind.EMAIL.value
    assert identifiers[0].normalized_value == "kareem@example.com"
    assert identifiers[0].verified_at is not None


@pytest.mark.anyio
async def test_user_can_link_phone_and_email_without_silent_account_merge() -> None:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    session_factory = async_sessionmaker(engine, expire_on_commit=False)

    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)

    async with session_factory() as session:
        phone_user = (await resolve_user_from_phone(session, "+14155552671")).user
        email_user = (
            await resolve_user_from_identifier(
                session,
                "other@example.com",
                source="linq",
            )
        ).user
        await link_user_identifier(
            session,
            user=phone_user,
            value="kareem@example.com",
            expected_kind=UserIdentifierKind.EMAIL,
            source="firebase",
            verified_at=phone_user.phone_verified_at,
        )
        assert await find_user_by_identifier(session, "KAREEM@example.com") == phone_user
        with pytest.raises(UserIdentifierConflictError):
            await link_user_identifier(
                session,
                user=phone_user,
                value="other@example.com",
                expected_kind=UserIdentifierKind.EMAIL,
                source="firebase",
                verified_at=phone_user.phone_verified_at,
            )
    assert email_user.id != phone_user.id

    await engine.dispose()


@pytest.mark.anyio
async def test_inbound_message_does_not_reactivate_revoked_identifier() -> None:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    session_factory = async_sessionmaker(engine, expire_on_commit=False)

    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)

    async with session_factory() as session:
        resolution = await resolve_user_from_identifier(
            session,
            "former@example.com",
            source="linq",
        )
        resolution.identifier.status = "revoked"
        await session.commit()

        with pytest.raises(UserIdentifierRevokedError):
            await resolve_user_from_identifier(
                session,
                "FORMER@example.com",
                source="linq",
            )
        await session.refresh(resolution.identifier)
        assert resolution.identifier.status == "revoked"

    await engine.dispose()
