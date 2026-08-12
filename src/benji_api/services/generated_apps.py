import re
import secrets
from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import Any
from uuid import UUID

from sqlalchemy import func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from benji_api.models.channel import (
    Conversation,
    ConversationKind,
    ConversationMember,
    ConversationMemberRole,
    ConversationMemberStatus,
)
from benji_api.models.generated_app import (
    GeneratedApp,
    GeneratedAppAccessMode,
    GeneratedAppRecord,
    GeneratedAppStatus,
    GeneratedAppVersion,
)
from benji_api.models.generated_app_v2 import (
    GeneratedAppAccessTicket,
    GeneratedAppBuildJob,
    GeneratedAppBuildStatus,
    GeneratedAppSession,
)
from benji_api.models.user import utc_now
from benji_api.services.generated_app_specs import (
    APP_THEMES,
    GeneratedAppValidationError,
    build_composable_specification,
    modules_from_specification,
    resolve_record_module,
    validate_module_record,
)
from benji_api.services.generated_apps_v2 import (
    CodeAppAuthorizationError,
    CodeAppNotFoundError,
    authorize_user,
)
from benji_api.services.groups import group_app_participant_names, list_conversation_members

APP_TEMPLATES = {"budget", "expense_splitter", "metric_tracker", "checklist"}
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


@dataclass(frozen=True, slots=True)
class GeneratedAppBundle:
    app: GeneratedApp
    version: GeneratedAppVersion
    records: tuple[GeneratedAppRecord, ...]


async def _locked_owned_conversation(
    session: AsyncSession,
    *,
    conversation_id: UUID,
    user_id: UUID,
) -> Conversation:
    statement = select(Conversation).where(Conversation.id == conversation_id)
    if session.bind is not None and session.bind.dialect.name == "postgresql":
        statement = statement.with_for_update()
    conversation = await session.scalar(statement.execution_options(populate_existing=True))
    if conversation is None or conversation.user_id != user_id:
        raise GeneratedAppValidationError("Conversation does not belong to this user")
    if conversation.kind != ConversationKind.GROUP.value:
        return conversation
    if conversation.group_owner_source == "unclaimed":
        raise GeneratedAppValidationError("This group does not currently have an app owner")
    membership_statement = select(ConversationMember).where(
        ConversationMember.conversation_id == conversation.id,
        ConversationMember.user_id == user_id,
        ConversationMember.status == ConversationMemberStatus.ACTIVE.value,
        ConversationMember.role == ConversationMemberRole.OWNER.value,
    )
    if session.bind is not None and session.bind.dialect.name == "postgresql":
        membership_statement = membership_statement.with_for_update()
    membership = await session.scalar(
        membership_statement.execution_options(populate_existing=True)
    )
    if membership is None:
        raise GeneratedAppValidationError("Only the active group owner can create an app")
    return conversation


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
    conversation = await _locked_owned_conversation(
        session,
        conversation_id=conversation_id,
        user_id=user_id,
    )
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
            or any(_unsafe_participant_name(participant) for participant in participants)
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
    specification: dict[str, Any] = {
        "schema_version": 2,
        "template": template,
        "theme": theme,
        "settings": settings,
        "capabilities": _template_capabilities(template),
    }
    specification["modules"] = modules_from_specification(
        {**specification, "schema_version": 1},
        legacy_template=template,
    )
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
                module_id="expenses",
                kind="participant",
                data={"name": name},
            )
            session.add(record)
            records.append(record)
    await session.commit()
    return GeneratedAppBundle(app=app, version=version, records=tuple(records))


async def create_composable_generated_app(
    session: AsyncSession,
    *,
    user_id: UUID,
    conversation_id: UUID,
    title: str,
    description: str,
    theme: str,
    access_mode: str,
    modules: list[dict[str, Any]],
    initial_records: list[dict[str, Any]],
) -> GeneratedAppBundle:
    conversation = await _locked_owned_conversation(
        session,
        conversation_id=conversation_id,
        user_id=user_id,
    )
    if access_mode not in APP_ACCESS_MODES:
        raise GeneratedAppValidationError("Unsupported app access mode")
    if conversation.kind == ConversationKind.GROUP.value:
        access_mode = GeneratedAppAccessMode.COLLABORATIVE_LINK.value

    clean_title = _text(title, "title", max_length=120)
    clean_description = _optional_text(description, max_length=500) or ""
    specification = build_composable_specification(theme=theme, modules=modules)
    clean_seeds = _initial_records(initial_records)
    if conversation.kind == ConversationKind.GROUP.value:
        clean_seeds = await _replace_group_seed_handles(
            session,
            conversation=conversation,
            specification=specification,
            seeds=clean_seeds,
        )

    app = GeneratedApp(
        user_id=user_id,
        conversation_id=conversation_id,
        public_id=secrets.token_urlsafe(24),
        title=clean_title,
        description=clean_description,
        template="workspace",
        theme=theme,
        access_mode=access_mode,
    )
    session.add(app)
    await session.flush()
    version = GeneratedAppVersion(app_id=app.id, version=1, specification=specification)
    session.add(version)
    await session.flush()
    bundle = GeneratedAppBundle(app=app, version=version, records=())

    records: list[GeneratedAppRecord] = []
    for seed in sorted(clean_seeds, key=lambda value: value["kind"] != "participant"):
        module_id, clean_data = await _validate_and_resolve_record(
            session,
            bundle=bundle,
            module_id=seed["module_id"],
            kind=seed["kind"],
            data=seed["data"],
        )
        record = GeneratedAppRecord(
            app_id=app.id,
            module_id=module_id,
            kind=seed["kind"],
            actor_name=seed["actor_name"],
            data=clean_data,
        )
        session.add(record)
        records.append(record)
        await session.flush()
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
                .join(Conversation, Conversation.id == GeneratedApp.conversation_id)
                .where(
                    GeneratedApp.user_id == user_id,
                    Conversation.user_id == user_id,
                    or_(
                        Conversation.kind != ConversationKind.GROUP.value,
                        Conversation.group_owner_source.is_(None),
                        Conversation.group_owner_source != "unclaimed",
                    ),
                    GeneratedApp.status == GeneratedAppStatus.ACTIVE.value,
                )
                .order_by(GeneratedApp.updated_at.desc(), GeneratedApp.created_at.desc())
            )
        ).all()
    )


async def archive_generated_app(
    session: AsyncSession,
    *,
    user_id: UUID,
    app_id: UUID,
) -> GeneratedApp | None:
    """Disable an owned app and its public link without destroying its records."""
    try:
        actor = await authorize_user(session, app_id=app_id, user_id=user_id)
    except (CodeAppAuthorizationError, CodeAppNotFoundError):
        return None
    app = await session.get(GeneratedApp, app_id)
    if (
        app is None
        or actor.role != "owner"
        or app.user_id != user_id
        or app.status != GeneratedAppStatus.ACTIVE.value
    ):
        return None
    now = datetime.now(UTC)
    app.status = GeneratedAppStatus.ARCHIVED.value
    app.updated_at = now
    # Archiving disables every old bearer, not only discovery by the public URL. Otherwise a
    # previously redeemed session can continue mutating data through the app-id action route.
    await session.execute(
        update(GeneratedAppSession)
        .where(
            GeneratedAppSession.app_id == app.id,
            GeneratedAppSession.revoked_at.is_(None),
        )
        .values(revoked_at=now)
    )
    await session.execute(
        update(GeneratedAppAccessTicket)
        .where(
            GeneratedAppAccessTicket.app_id == app.id,
            GeneratedAppAccessTicket.expires_at > now,
        )
        .values(expires_at=now)
    )
    # A queued or leased build must not later reactivate or announce the archived app.
    await session.execute(
        update(GeneratedAppBuildJob)
        .where(
            GeneratedAppBuildJob.app_id == app.id,
            GeneratedAppBuildJob.status.in_(
                {
                    GeneratedAppBuildStatus.QUEUED.value,
                    GeneratedAppBuildStatus.CLAIMED.value,
                }
            ),
        )
        .values(
            status=GeneratedAppBuildStatus.FAILED.value,
            error="App was archived before its build completed",
            claimed_by=None,
            lease_expires_at=None,
            completed_at=now,
            updated_at=now,
        )
    )
    await session.flush()
    return app


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
    return await _load_generated_app_bundle(session, app=app)


async def get_owned_generated_app(
    session: AsyncSession,
    *,
    user_id: UUID,
    app_id: UUID,
) -> GeneratedAppBundle:
    try:
        actor = await authorize_user(session, app_id=app_id, user_id=user_id)
    except (CodeAppAuthorizationError, CodeAppNotFoundError) as error:
        raise GeneratedAppNotFoundError("App was not found") from error
    app = await session.get(GeneratedApp, app_id)
    if app is None or actor.role != "owner" or app.user_id != user_id:
        raise GeneratedAppNotFoundError("App was not found")
    return await _load_generated_app_bundle(session, app=app)


async def _load_generated_app_bundle(
    session: AsyncSession,
    *,
    app: GeneratedApp,
) -> GeneratedAppBundle:
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
    module_id: str | None,
    kind: str,
    data: dict[str, Any],
    actor_name: str | None,
) -> GeneratedAppBundle:
    bundle = await get_generated_app_by_public_id(session, public_id=public_id)
    updated, _ = await _create_generated_app_record(
        session,
        bundle=bundle,
        module_id=module_id,
        kind=kind,
        data=data,
        actor_name=actor_name,
    )
    return updated


async def create_owned_generated_app_record(
    session: AsyncSession,
    *,
    user_id: UUID,
    app_id: UUID,
    module_id: str | None,
    kind: str,
    data: dict[str, Any],
    actor_name: str | None,
) -> tuple[GeneratedAppBundle, GeneratedAppRecord]:
    bundle = await get_owned_generated_app(session, user_id=user_id, app_id=app_id)
    return await _create_generated_app_record(
        session,
        bundle=bundle,
        module_id=module_id,
        kind=kind,
        data=data,
        actor_name=actor_name,
    )


async def _create_generated_app_record(
    session: AsyncSession,
    *,
    bundle: GeneratedAppBundle,
    module_id: str | None,
    kind: str,
    data: dict[str, Any],
    actor_name: str | None,
) -> tuple[GeneratedAppBundle, GeneratedAppRecord]:
    count = await session.scalar(
        select(func.count())
        .select_from(GeneratedAppRecord)
        .where(GeneratedAppRecord.app_id == bundle.app.id)
    )
    if (count or 0) >= 10_000:
        raise GeneratedAppValidationError("This app has reached its record limit")
    resolved_module_id, clean_data = await _validate_and_resolve_record(
        session,
        bundle=bundle,
        module_id=module_id,
        kind=kind,
        data=data,
    )
    record = GeneratedAppRecord(
        app_id=bundle.app.id,
        module_id=resolved_module_id,
        kind=kind,
        actor_name=_optional_text(actor_name, max_length=120),
        data=clean_data,
    )
    session.add(record)
    bundle.app.updated_at = utc_now()
    await session.commit()
    return await _load_generated_app_bundle(session, app=bundle.app), record


async def update_generated_app_record(
    session: AsyncSession,
    *,
    public_id: str,
    record_id: UUID,
    data: dict[str, Any],
) -> GeneratedAppBundle:
    bundle = await get_generated_app_by_public_id(session, public_id=public_id)
    return await _update_generated_app_record(
        session,
        bundle=bundle,
        record_id=record_id,
        data=data,
    )


async def update_owned_generated_app_record(
    session: AsyncSession,
    *,
    user_id: UUID,
    app_id: UUID,
    record_id: UUID,
    data: dict[str, Any],
) -> GeneratedAppBundle:
    bundle = await get_owned_generated_app(session, user_id=user_id, app_id=app_id)
    return await _update_generated_app_record(
        session,
        bundle=bundle,
        record_id=record_id,
        data=data,
    )


async def _update_generated_app_record(
    session: AsyncSession,
    *,
    bundle: GeneratedAppBundle,
    record_id: UUID,
    data: dict[str, Any],
) -> GeneratedAppBundle:
    record = await session.get(GeneratedAppRecord, record_id)
    if record is None or record.app_id != bundle.app.id:
        raise GeneratedAppNotFoundError("App record was not found")
    if bundle.app.template != "workspace" and (
        bundle.app.template != "checklist" or record.kind != "item"
    ):
        raise GeneratedAppValidationError("This record type cannot be edited")
    merged = {**record.data, **data}
    _, clean_data = await _validate_and_resolve_record(
        session,
        bundle=bundle,
        module_id=record.module_id,
        kind=record.kind,
        data=merged,
        exclude_record_id=record.id,
    )
    if (
        record.kind == "participant"
        and clean_data.get("name") != record.data.get("name")
        and _participant_is_referenced(bundle, record)
    ):
        raise GeneratedAppValidationError(
            "Remove expenses involving this participant before renaming them"
        )
    record.data = clean_data
    bundle.app.updated_at = utc_now()
    await session.commit()
    return await _load_generated_app_bundle(session, app=bundle.app)


async def delete_generated_app_record(
    session: AsyncSession,
    *,
    public_id: str,
    record_id: UUID,
) -> GeneratedAppBundle:
    bundle = await get_generated_app_by_public_id(session, public_id=public_id)
    return await _delete_generated_app_record(
        session,
        bundle=bundle,
        record_id=record_id,
    )


async def delete_owned_generated_app_record(
    session: AsyncSession,
    *,
    user_id: UUID,
    app_id: UUID,
    record_id: UUID,
) -> GeneratedAppBundle:
    bundle = await get_owned_generated_app(session, user_id=user_id, app_id=app_id)
    return await _delete_generated_app_record(
        session,
        bundle=bundle,
        record_id=record_id,
    )


async def _delete_generated_app_record(
    session: AsyncSession,
    *,
    bundle: GeneratedAppBundle,
    record_id: UUID,
) -> GeneratedAppBundle:
    record = await session.get(GeneratedAppRecord, record_id)
    if record is None or record.app_id != bundle.app.id:
        raise GeneratedAppNotFoundError("App record was not found")
    if record.kind == "participant" and _participant_is_referenced(bundle, record):
        raise GeneratedAppValidationError("Remove expenses involving this participant first")
    bundle.app.updated_at = utc_now()
    await session.delete(record)
    await session.commit()
    return await _load_generated_app_bundle(session, app=bundle.app)


def generated_app_url(*, base_url: str, public_id: str) -> str:
    return f"{base_url.rstrip('/')}/apps/{public_id}"


def _participant_is_referenced(
    bundle: GeneratedAppBundle,
    participant: GeneratedAppRecord,
) -> bool:
    participant_name = participant.data.get("name")
    return any(
        participant_name in expense.data.get("split_between", [])
        or expense.data.get("paid_by") == participant_name
        for expense in bundle.records
        if expense.kind == "expense" and expense.module_id == participant.module_id
    )


def _initial_records(raw_records: Any) -> list[dict[str, Any]]:
    if not isinstance(raw_records, list) or len(raw_records) > 200:
        raise GeneratedAppValidationError("initial_records must be a list of at most 200 records")
    records: list[dict[str, Any]] = []
    for raw_record in raw_records:
        if not isinstance(raw_record, dict) or set(raw_record) != {
            "module_id",
            "kind",
            "actor_name",
            "data",
        }:
            raise GeneratedAppValidationError(
                "initial records require module_id, kind, actor_name, and data"
            )
        module_id = raw_record.get("module_id")
        kind = raw_record.get("kind")
        data = raw_record.get("data")
        if not isinstance(module_id, str) or not isinstance(kind, str):
            raise GeneratedAppValidationError("initial record module_id and kind must be text")
        if not isinstance(data, dict):
            raise GeneratedAppValidationError("initial record data must be an object")
        records.append(
            {
                "module_id": module_id,
                "kind": kind,
                "actor_name": _optional_text(raw_record.get("actor_name"), max_length=120),
                "data": data,
            }
        )
    return records


async def _replace_group_seed_handles(
    session: AsyncSession,
    *,
    conversation: Conversation,
    specification: dict[str, Any],
    seeds: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    members = await list_conversation_members(session, conversation_id=conversation.id)
    names = group_app_participant_names(members)
    aliases: dict[str, str] = {}
    for (member, user), name in zip(members, names, strict=True):
        raw_aliases = [member.external_handle, member.external_id, str(member.id)]
        if user is not None:
            raw_aliases.extend([str(user.id), user.phone_number])
        for alias in raw_aliases:
            if alias:
                aliases[alias.strip().casefold()] = name
    result = list(seeds)
    for module in specification["modules"]:
        if module["type"] == "expenses" and module["settings"]["mode"] == "split":
            kind = "participant"
            make_data = lambda name: {"name": name}  # noqa: E731
        elif module["type"] == "guest_list":
            kind = "guest"
            make_data = lambda name: {  # noqa: E731
                "name": name,
                "status": "invited",
                "party_size": 1,
                "note": "",
            }
        else:
            continue
        matching = [
            seed for seed in result if seed["module_id"] == module["id"] and seed["kind"] == kind
        ]
        has_unsafe_name = any(
            _unsafe_participant_name(str(seed["data"].get("name", ""))) for seed in matching
        )
        replacements = dict(aliases)
        for index, seed in enumerate(matching):
            raw_name = str(seed["data"].get("name", "")).strip()
            if HANDLE_LIKE_PARTICIPANT.search(raw_name) and index < len(names):
                replacements.setdefault(raw_name.casefold(), names[index])
        if module["type"] == "expenses":
            result = [
                _rewrite_group_expense_seed(
                    seed,
                    module_id=module["id"],
                    names=names,
                    replacements=replacements,
                )
                for seed in result
            ]
        if matching and not has_unsafe_name:
            continue
        result = [
            seed
            for seed in result
            if not (seed["module_id"] == module["id"] and seed["kind"] == kind)
        ]
        result.extend(
            {
                "module_id": module["id"],
                "kind": kind,
                "actor_name": None,
                "data": make_data(name),
            }
            for name in names
        )
    return result


def _unsafe_participant_name(value: str) -> bool:
    clean = value.strip()
    return bool(
        HANDLE_LIKE_PARTICIPANT.search(clean) or clean.casefold() in {"group member", "everyone"}
    )


def _rewrite_group_expense_seed(
    seed: dict[str, Any],
    *,
    module_id: str,
    names: list[str],
    replacements: dict[str, str],
) -> dict[str, Any]:
    if seed["module_id"] != module_id or seed["kind"] != "expense":
        return seed
    rewritten = {**seed, "data": dict(seed["data"])}
    data = rewritten["data"]

    paid_by = data.get("paid_by")
    if isinstance(paid_by, str):
        data["paid_by"] = replacements.get(paid_by.strip().casefold(), paid_by)

    split_between = data.get("split_between")
    if isinstance(split_between, list):
        rewritten_split: list[str] = []
        for raw_name in split_between:
            if not isinstance(raw_name, str):
                continue
            key = raw_name.strip().casefold()
            if key in {"everyone", "group member"}:
                rewritten_split.extend(names)
            else:
                rewritten_split.append(replacements.get(key, raw_name))
        data["split_between"] = list(dict.fromkeys(rewritten_split))

    actor_name = rewritten.get("actor_name")
    if isinstance(actor_name, str):
        rewritten["actor_name"] = replacements.get(actor_name.strip().casefold(), actor_name)
    return rewritten


async def _validate_and_resolve_record(
    session: AsyncSession,
    *,
    bundle: GeneratedAppBundle,
    module_id: str | None,
    kind: str,
    data: dict[str, Any],
    exclude_record_id: UUID | None = None,
) -> tuple[str, dict[str, Any]]:
    if bundle.app.template != "workspace":
        legacy_module_id = {
            "budget": "expenses",
            "expense_splitter": "expenses",
            "metric_tracker": "metric",
            "checklist": "todos",
        }.get(bundle.app.template)
        if legacy_module_id is None:
            raise GeneratedAppValidationError("Unsupported legacy app template")
        if module_id is not None and module_id != legacy_module_id:
            raise GeneratedAppValidationError("module_id does not belong to this app")
        return legacy_module_id, await _validate_record(
            session,
            app=bundle.app,
            kind=kind,
            data=data,
        )

    module = resolve_record_module(
        bundle.version.specification,
        legacy_template=bundle.app.template,
        module_id=module_id,
        kind=kind,
    )
    clean_data = validate_module_record(module, kind=kind, data=data)
    statement = select(GeneratedAppRecord).where(
        GeneratedAppRecord.app_id == bundle.app.id,
        GeneratedAppRecord.module_id == module["id"],
    )
    if exclude_record_id is not None:
        statement = statement.where(GeneratedAppRecord.id != exclude_record_id)
    module_records = list((await session.scalars(statement)).all())

    if kind in {"participant", "guest"}:
        name = str(clean_data["name"]).casefold()
        if any(
            record.kind == kind and str(record.data.get("name", "")).casefold() == name
            for record in module_records
        ):
            raise GeneratedAppValidationError(f"That {kind.replace('_', ' ')} already exists")
    if kind == "expense" and module["settings"]["mode"] == "split":
        participant_names = {
            str(record.data.get("name"))
            for record in module_records
            if record.kind == "participant"
        }
        if clean_data["paid_by"] not in participant_names or any(
            name not in participant_names for name in clean_data["split_between"]
        ):
            raise GeneratedAppValidationError("Expense participants must already exist")
    return module["id"], clean_data


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
