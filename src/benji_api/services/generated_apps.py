import re
import secrets
from dataclasses import dataclass
from datetime import date
from typing import Any
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from benji_api.models.channel import Conversation, ConversationKind
from benji_api.models.generated_app import (
    GeneratedApp,
    GeneratedAppAccessMode,
    GeneratedAppRecord,
    GeneratedAppStatus,
    GeneratedAppVersion,
)
from benji_api.models.user import utc_now
from benji_api.services.groups import group_app_participant_names, list_conversation_members

APP_TEMPLATES = {"budget", "expense_splitter", "metric_tracker", "checklist"}
APP_THEMES = {"coral", "sage", "ocean", "plum", "gold"}
APP_ACCESS_MODES = {
    GeneratedAppAccessMode.PRIVATE_LINK.value,
    GeneratedAppAccessMode.COLLABORATIVE_LINK.value,
}
HANDLE_LIKE_PARTICIPANT = re.compile(
    r"(?:^\+?\d{7,}$|@|^[0-9a-f]{8}-[0-9a-f-]{27,}$|^member ending\s+\d+$)",
    re.IGNORECASE,
)


class GeneratedAppNotFoundError(LookupError):
    pass


class GeneratedAppValidationError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class GeneratedAppBundle:
    app: GeneratedApp
    version: GeneratedAppVersion
    records: tuple[GeneratedAppRecord, ...]


async def create_generated_app(
    session: AsyncSession,
    *,
    user_id: UUID,
    conversation_id: UUID,
    title: str,
    description: str,
    template: str,
    theme: str,
    access_mode: str,
    currency: str | None,
    unit: str | None,
    target_number: float | int | None,
    target_direction: str | None,
    participants: list[str],
) -> GeneratedAppBundle:
    conversation = await session.get(Conversation, conversation_id)
    if conversation is None or conversation.user_id != user_id:
        raise GeneratedAppValidationError("Conversation does not belong to this user")
    if template not in APP_TEMPLATES:
        raise GeneratedAppValidationError("Unsupported app template")
    if theme not in APP_THEMES:
        raise GeneratedAppValidationError("Unsupported app theme")
    if access_mode not in APP_ACCESS_MODES:
        raise GeneratedAppValidationError("Unsupported app access mode")
    if conversation.kind == ConversationKind.GROUP.value:
        access_mode = GeneratedAppAccessMode.COLLABORATIVE_LINK.value
        if template == "expense_splitter" and (
            not participants
            or any(
                HANDLE_LIKE_PARTICIPANT.search(participant.strip())
                or participant.strip().casefold() in {"group member", "everyone"}
                for participant in participants
            )
        ):
            members = await list_conversation_members(session, conversation_id=conversation.id)
            participants = group_app_participant_names(members)

    clean_title = _text(title, "title", max_length=120)
    clean_description = _optional_text(description, max_length=500) or ""
    settings = _template_settings(
        template=template,
        currency=currency,
        unit=unit,
        target_number=target_number,
        target_direction=target_direction,
    )
    specification = {
        "schema_version": 1,
        "template": template,
        "theme": theme,
        "settings": settings,
        "capabilities": _template_capabilities(template),
    }
    app = GeneratedApp(
        user_id=user_id,
        conversation_id=conversation_id,
        public_id=secrets.token_urlsafe(24),
        title=clean_title,
        description=clean_description,
        template=template,
        theme=theme,
        access_mode=access_mode,
    )
    session.add(app)
    await session.flush()
    version = GeneratedAppVersion(app_id=app.id, version=1, specification=specification)
    session.add(version)

    records: list[GeneratedAppRecord] = []
    if template == "expense_splitter":
        seen: set[str] = set()
        for participant in participants[:20]:
            name = _text(participant, "participant", max_length=80)
            key = name.casefold()
            if key in seen:
                continue
            seen.add(key)
            record = GeneratedAppRecord(
                app_id=app.id,
                kind="participant",
                data={"name": name},
            )
            session.add(record)
            records.append(record)
    await session.commit()
    return GeneratedAppBundle(app=app, version=version, records=tuple(records))


async def list_generated_apps(
    session: AsyncSession,
    *,
    user_id: UUID,
) -> list[GeneratedApp]:
    return list(
        (
            await session.scalars(
                select(GeneratedApp)
                .where(
                    GeneratedApp.user_id == user_id,
                    GeneratedApp.status == GeneratedAppStatus.ACTIVE.value,
                )
                .order_by(GeneratedApp.updated_at.desc(), GeneratedApp.created_at.desc())
            )
        ).all()
    )


async def get_generated_app_by_public_id(
    session: AsyncSession,
    *,
    public_id: str,
) -> GeneratedAppBundle:
    app = await session.scalar(
        select(GeneratedApp).where(
            GeneratedApp.public_id == public_id,
            GeneratedApp.status == GeneratedAppStatus.ACTIVE.value,
        )
    )
    if app is None:
        raise GeneratedAppNotFoundError("App was not found")
    version = await session.scalar(
        select(GeneratedAppVersion).where(
            GeneratedAppVersion.app_id == app.id,
            GeneratedAppVersion.version == app.current_version,
        )
    )
    if version is None:
        raise RuntimeError("Generated app version is missing")
    records = tuple(
        (
            await session.scalars(
                select(GeneratedAppRecord)
                .where(GeneratedAppRecord.app_id == app.id)
                .order_by(GeneratedAppRecord.created_at, GeneratedAppRecord.id)
            )
        ).all()
    )
    return GeneratedAppBundle(app=app, version=version, records=records)


async def create_generated_app_record(
    session: AsyncSession,
    *,
    public_id: str,
    kind: str,
    data: dict[str, Any],
    actor_name: str | None,
) -> GeneratedAppBundle:
    bundle = await get_generated_app_by_public_id(session, public_id=public_id)
    count = await session.scalar(
        select(func.count())
        .select_from(GeneratedAppRecord)
        .where(GeneratedAppRecord.app_id == bundle.app.id)
    )
    if (count or 0) >= 10_000:
        raise GeneratedAppValidationError("This app has reached its record limit")
    clean_data = await _validate_record(
        session,
        app=bundle.app,
        kind=kind,
        data=data,
    )
    record = GeneratedAppRecord(
        app_id=bundle.app.id,
        kind=kind,
        actor_name=_optional_text(actor_name, max_length=120),
        data=clean_data,
    )
    session.add(record)
    bundle.app.updated_at = utc_now()
    await session.commit()
    return await get_generated_app_by_public_id(session, public_id=public_id)


async def update_generated_app_record(
    session: AsyncSession,
    *,
    public_id: str,
    record_id: UUID,
    data: dict[str, Any],
) -> GeneratedAppBundle:
    bundle = await get_generated_app_by_public_id(session, public_id=public_id)
    record = await session.get(GeneratedAppRecord, record_id)
    if record is None or record.app_id != bundle.app.id:
        raise GeneratedAppNotFoundError("App record was not found")
    if bundle.app.template != "checklist" or record.kind != "item":
        raise GeneratedAppValidationError("This record type cannot be edited")
    merged = {**record.data, **data}
    record.data = await _validate_record(
        session,
        app=bundle.app,
        kind=record.kind,
        data=merged,
    )
    bundle.app.updated_at = utc_now()
    await session.commit()
    return await get_generated_app_by_public_id(session, public_id=public_id)


async def delete_generated_app_record(
    session: AsyncSession,
    *,
    public_id: str,
    record_id: UUID,
) -> GeneratedAppBundle:
    bundle = await get_generated_app_by_public_id(session, public_id=public_id)
    record = await session.get(GeneratedAppRecord, record_id)
    if record is None or record.app_id != bundle.app.id:
        raise GeneratedAppNotFoundError("App record was not found")
    if bundle.app.template == "expense_splitter" and record.kind == "participant":
        participant_name = record.data.get("name")
        if any(
            participant_name in expense.data.get("split_between", [])
            or expense.data.get("paid_by") == participant_name
            for expense in bundle.records
            if expense.kind == "expense"
        ):
            raise GeneratedAppValidationError("Remove expenses involving this participant first")
    bundle.app.updated_at = utc_now()
    await session.delete(record)
    await session.commit()
    return await get_generated_app_by_public_id(session, public_id=public_id)


def generated_app_url(*, base_url: str, public_id: str) -> str:
    return f"{base_url.rstrip('/')}/apps/{public_id}"


def _template_settings(
    *,
    template: str,
    currency: str | None,
    unit: str | None,
    target_number: float | int | None,
    target_direction: str | None,
) -> dict[str, Any]:
    if template in {"budget", "expense_splitter"}:
        clean_currency = (_optional_text(currency, max_length=12) or "USD").upper()
        settings: dict[str, Any] = {"currency": clean_currency}
        if template == "budget":
            settings["monthly_budget"] = (
                _number(target_number, "target_number") if target_number is not None else None
            )
        return settings
    if template == "metric_tracker":
        direction = target_direction or "decrease"
        if direction not in {"increase", "decrease"}:
            raise GeneratedAppValidationError(
                "Metric target direction must be increase or decrease"
            )
        return {
            "unit": _optional_text(unit, max_length=30) or "units",
            "target": (
                _number(target_number, "target_number") if target_number is not None else None
            ),
            "direction": direction,
        }
    return {}


def _template_capabilities(template: str) -> list[str]:
    return {
        "budget": ["expense_log", "category_totals", "budget_progress"],
        "expense_splitter": ["participants", "shared_expenses", "settlement_balances"],
        "metric_tracker": ["measurement_log", "goal_progress", "trend"],
        "checklist": ["items", "completion_progress"],
    }[template]


async def _validate_record(
    session: AsyncSession,
    *,
    app: GeneratedApp,
    kind: str,
    data: dict[str, Any],
) -> dict[str, Any]:
    if app.template == "budget" and kind == "expense":
        return {
            "amount": _positive_number(data.get("amount"), "amount"),
            "category": _text(data.get("category"), "category", max_length=60),
            "note": _optional_text(data.get("note"), max_length=200) or "",
            "date": _date(data.get("date")),
        }
    if app.template == "expense_splitter" and kind == "participant":
        name = _text(data.get("name"), "name", max_length=80)
        participant_records = (
            await session.scalars(
                select(GeneratedAppRecord).where(
                    GeneratedAppRecord.app_id == app.id,
                    GeneratedAppRecord.kind == "participant",
                )
            )
        ).all()
        if any(
            str(record.data.get("name", "")).casefold() == name.casefold()
            for record in participant_records
        ):
            raise GeneratedAppValidationError("That participant already exists")
        return {"name": name}
    if app.template == "expense_splitter" and kind == "expense":
        participant_names = {
            str(record.data.get("name"))
            for record in (
                await session.scalars(
                    select(GeneratedAppRecord).where(
                        GeneratedAppRecord.app_id == app.id,
                        GeneratedAppRecord.kind == "participant",
                    )
                )
            ).all()
        }
        paid_by = _text(data.get("paid_by"), "paid_by", max_length=80)
        raw_split = data.get("split_between")
        if not isinstance(raw_split, list):
            raise GeneratedAppValidationError("split_between must be a list")
        split_between = list(
            dict.fromkeys(
                _text(name, "split participant", max_length=80) for name in raw_split[:20]
            )
        )
        if not split_between:
            raise GeneratedAppValidationError("Choose at least one person for the split")
        if paid_by not in participant_names or any(
            name not in participant_names for name in split_between
        ):
            raise GeneratedAppValidationError("Expense participants must already exist")
        return {
            "amount": _positive_number(data.get("amount"), "amount"),
            "description": _text(data.get("description"), "description", max_length=160),
            "paid_by": paid_by,
            "split_between": split_between,
            "date": _date(data.get("date")),
        }
    if app.template == "metric_tracker" and kind == "measurement":
        return {
            "value": _number(data.get("value"), "value"),
            "note": _optional_text(data.get("note"), max_length=200) or "",
            "date": _date(data.get("date")),
        }
    if app.template == "checklist" and kind == "item":
        completed = data.get("completed", False)
        if not isinstance(completed, bool):
            raise GeneratedAppValidationError("completed must be true or false")
        return {
            "text": _text(data.get("text"), "text", max_length=240),
            "completed": completed,
        }
    raise GeneratedAppValidationError("Record type does not match this app")


def _text(value: Any, field_name: str, *, max_length: int) -> str:
    if not isinstance(value, str):
        raise GeneratedAppValidationError(f"{field_name} must be text")
    clean = " ".join(value.strip().split())
    if not clean or len(clean) > max_length:
        raise GeneratedAppValidationError(
            f"{field_name} must be between 1 and {max_length} characters"
        )
    return clean


def _optional_text(value: Any, *, max_length: int) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise GeneratedAppValidationError("Optional value must be text or null")
    clean = " ".join(value.strip().split())
    if not clean:
        return None
    if len(clean) > max_length:
        raise GeneratedAppValidationError(f"Text cannot exceed {max_length} characters")
    return clean


def _number(value: Any, field_name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise GeneratedAppValidationError(f"{field_name} must be a number")
    number = float(value)
    if not -1_000_000_000 <= number <= 1_000_000_000:
        raise GeneratedAppValidationError(f"{field_name} is outside the supported range")
    return round(number, 4)


def _positive_number(value: Any, field_name: str) -> float:
    number = _number(value, field_name)
    if number <= 0:
        raise GeneratedAppValidationError(f"{field_name} must be greater than zero")
    return number


def _date(value: Any) -> str:
    if not isinstance(value, str):
        raise GeneratedAppValidationError("date must use YYYY-MM-DD")
    try:
        return date.fromisoformat(value).isoformat()
    except ValueError as error:
        raise GeneratedAppValidationError("date must use YYYY-MM-DD") from error
