import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, time
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

APP_SCHEMA_VERSION = 2
APP_THEMES = {"coral", "sage", "ocean", "plum", "gold"}
MODULE_ID_PATTERN = re.compile(r"^[a-z][a-z0-9_]{0,39}$")
FIELD_ID_PATTERN = re.compile(r"^[a-z][a-z0-9_]{0,39}$")
COLLECTION_FIELD_TYPES = {
    "text",
    "number",
    "currency",
    "date",
    "time",
    "select",
    "boolean",
}


class GeneratedAppValidationError(ValueError):
    pass


RecordValidator = Callable[[dict[str, Any], dict[str, Any]], dict[str, Any]]
SettingsValidator = Callable[[dict[str, Any]], dict[str, Any]]


@dataclass(frozen=True, slots=True)
class AppModuleDefinition:
    type: str
    capabilities: tuple[str, ...]
    record_validators: dict[str, RecordValidator]
    settings_validator: SettingsValidator


def _empty_settings(settings: dict[str, Any]) -> dict[str, Any]:
    _exact_keys(settings, set(), "module settings")
    return {}


def _overview_settings(settings: dict[str, Any]) -> dict[str, Any]:
    _exact_keys(settings, {"body", "facts"}, "overview settings")
    raw_facts = settings.get("facts")
    if not isinstance(raw_facts, list) or len(raw_facts) > 8:
        raise GeneratedAppValidationError("overview facts must be a list of at most 8 items")
    facts: list[dict[str, str]] = []
    for raw_fact in raw_facts:
        if not isinstance(raw_fact, dict):
            raise GeneratedAppValidationError("overview facts must be objects")
        _exact_keys(raw_fact, {"label", "value"}, "overview fact")
        facts.append(
            {
                "label": _text(raw_fact.get("label"), "fact label", max_length=60),
                "value": _text(raw_fact.get("value"), "fact value", max_length=160),
            }
        )
    return {
        "body": _text_or_empty(settings.get("body"), max_length=1_000),
        "facts": facts,
    }


def _todos_settings(settings: dict[str, Any]) -> dict[str, Any]:
    _exact_keys(settings, {"show_completed"}, "todos settings")
    return {"show_completed": _boolean(settings.get("show_completed"), "show_completed")}


def _guest_settings(settings: dict[str, Any]) -> dict[str, Any]:
    _exact_keys(settings, {"allow_plus_ones"}, "guest list settings")
    return {
        "allow_plus_ones": _boolean(settings.get("allow_plus_ones"), "allow_plus_ones")
    }


def _itinerary_settings(settings: dict[str, Any]) -> dict[str, Any]:
    _exact_keys(settings, {"timezone"}, "itinerary settings")
    timezone = _optional_text(settings.get("timezone"), max_length=64)
    if timezone is not None:
        try:
            ZoneInfo(timezone)
        except ZoneInfoNotFoundError as error:
            raise GeneratedAppValidationError("timezone must be a valid IANA timezone") from error
    return {"timezone": timezone}


def _expenses_settings(settings: dict[str, Any]) -> dict[str, Any]:
    _exact_keys(settings, {"currency", "budget", "mode"}, "expenses settings")
    currency = _text(settings.get("currency"), "currency", max_length=12).upper()
    if not currency.isalnum():
        raise GeneratedAppValidationError("currency must contain only letters or numbers")
    mode = settings.get("mode")
    if mode not in {"personal", "split"}:
        raise GeneratedAppValidationError("expense mode must be personal or split")
    budget = settings.get("budget")
    return {
        "currency": currency,
        "budget": _positive_number(budget, "budget") if budget is not None else None,
        "mode": mode,
    }


def _metric_settings(settings: dict[str, Any]) -> dict[str, Any]:
    _exact_keys(settings, {"unit", "target", "direction"}, "metric settings")
    direction = settings.get("direction")
    if direction not in {"increase", "decrease"}:
        raise GeneratedAppValidationError("metric direction must be increase or decrease")
    target = settings.get("target")
    return {
        "unit": _text(settings.get("unit"), "unit", max_length=30),
        "target": _number(target, "target") if target is not None else None,
        "direction": direction,
    }


def _collection_settings(settings: dict[str, Any]) -> dict[str, Any]:
    _exact_keys(
        settings,
        {"display", "fields", "primary_field", "currency"},
        "collection settings",
    )
    display = settings.get("display")
    if display not in {"list", "table", "cards"}:
        raise GeneratedAppValidationError("collection display must be list, table, or cards")
    raw_fields = settings.get("fields")
    if not isinstance(raw_fields, list) or not 1 <= len(raw_fields) <= 12:
        raise GeneratedAppValidationError("collection fields must contain 1 to 12 fields")
    fields: list[dict[str, Any]] = []
    seen: set[str] = set()
    for raw_field in raw_fields:
        if not isinstance(raw_field, dict):
            raise GeneratedAppValidationError("collection fields must be objects")
        _exact_keys(raw_field, {"key", "label", "type", "required", "options"}, "field")
        field_key = _identifier(raw_field.get("key"), "field key", FIELD_ID_PATTERN)
        if field_key in seen:
            raise GeneratedAppValidationError("collection field keys must be unique")
        seen.add(field_key)
        field_type = raw_field.get("type")
        if field_type not in COLLECTION_FIELD_TYPES:
            raise GeneratedAppValidationError("unsupported collection field type")
        raw_options = raw_field.get("options")
        if not isinstance(raw_options, list):
            raise GeneratedAppValidationError("field options must be a list")
        options = _unique_texts(raw_options, field_name="option", max_items=20, max_length=60)
        if field_type == "select" and not options:
            raise GeneratedAppValidationError("select fields require at least one option")
        if field_type != "select" and options:
            raise GeneratedAppValidationError("only select fields may define options")
        fields.append(
            {
                "key": field_key,
                "label": _text(raw_field.get("label"), "field label", max_length=60),
                "type": field_type,
                "required": _boolean(raw_field.get("required"), "field required"),
                "options": options,
            }
        )
    primary_field = settings.get("primary_field")
    if primary_field is not None and primary_field not in seen:
        raise GeneratedAppValidationError("primary_field must reference a configured field")
    currency = _optional_text(settings.get("currency"), max_length=12)
    if currency is not None and not currency.isalnum():
        raise GeneratedAppValidationError("currency must contain only letters or numbers")
    return {
        "display": display,
        "fields": fields,
        "primary_field": primary_field,
        "currency": currency.upper() if currency else None,
    }


def _todo_record(data: dict[str, Any], settings: dict[str, Any]) -> dict[str, Any]:
    del settings
    _exact_keys(
        data,
        {"text", "completed", "due_date", "assignee", "priority"},
        "todo",
    )
    priority = data.get("priority")
    if priority not in {"low", "normal", "high"}:
        raise GeneratedAppValidationError("todo priority must be low, normal, or high")
    return {
        "text": _text(data.get("text"), "text", max_length=240),
        "completed": _boolean(data.get("completed"), "completed"),
        "due_date": _optional_date(data.get("due_date")),
        "assignee": _optional_text(data.get("assignee"), max_length=80),
        "priority": priority,
    }


def _guest_record(data: dict[str, Any], settings: dict[str, Any]) -> dict[str, Any]:
    _exact_keys(data, {"name", "status", "party_size", "note"}, "guest")
    status = data.get("status")
    if status not in {"invited", "going", "maybe", "declined"}:
        raise GeneratedAppValidationError("guest status is invalid")
    party_size = data.get("party_size")
    if isinstance(party_size, bool) or not isinstance(party_size, int):
        raise GeneratedAppValidationError("party_size must be an integer")
    maximum = 20 if settings["allow_plus_ones"] else 1
    if not 1 <= party_size <= maximum:
        raise GeneratedAppValidationError(f"party_size must be between 1 and {maximum}")
    return {
        "name": _text(data.get("name"), "name", max_length=80),
        "status": status,
        "party_size": party_size,
        "note": _text_or_empty(data.get("note"), max_length=240),
    }


def _itinerary_record(data: dict[str, Any], settings: dict[str, Any]) -> dict[str, Any]:
    del settings
    _exact_keys(
        data,
        {"title", "date", "start_time", "end_time", "location", "note", "completed"},
        "itinerary item",
    )
    start_time = _optional_time(data.get("start_time"))
    end_time = _optional_time(data.get("end_time"))
    if start_time and end_time and end_time <= start_time:
        raise GeneratedAppValidationError("end_time must be after start_time")
    return {
        "title": _text(data.get("title"), "title", max_length=160),
        "date": _optional_date(data.get("date")),
        "start_time": start_time,
        "end_time": end_time,
        "location": _text_or_empty(data.get("location"), max_length=160),
        "note": _text_or_empty(data.get("note"), max_length=500),
        "completed": _boolean(data.get("completed"), "completed"),
    }


def _participant_record(data: dict[str, Any], settings: dict[str, Any]) -> dict[str, Any]:
    if settings["mode"] != "split":
        raise GeneratedAppValidationError("participants require split expense mode")
    _exact_keys(data, {"name"}, "participant")
    return {"name": _text(data.get("name"), "name", max_length=80)}


def _expense_record(data: dict[str, Any], settings: dict[str, Any]) -> dict[str, Any]:
    _exact_keys(
        data,
        {"amount", "category", "note", "date", "paid_by", "split_between"},
        "expense",
    )
    paid_by = _optional_text(data.get("paid_by"), max_length=80)
    raw_split = data.get("split_between")
    if not isinstance(raw_split, list):
        raise GeneratedAppValidationError("split_between must be a list")
    split_between = _unique_texts(
        raw_split,
        field_name="split participant",
        max_items=20,
        max_length=80,
    )
    if settings["mode"] == "split" and (paid_by is None or not split_between):
        raise GeneratedAppValidationError(
            "split expenses require paid_by and at least one split participant"
        )
    if settings["mode"] == "personal" and (paid_by is not None or split_between):
        raise GeneratedAppValidationError("personal expenses cannot include split participants")
    return {
        "amount": _positive_number(data.get("amount"), "amount"),
        "category": _text(data.get("category"), "category", max_length=60),
        "note": _text_or_empty(data.get("note"), max_length=240),
        "date": _required_date(data.get("date")),
        "paid_by": paid_by,
        "split_between": split_between,
    }


def _measurement_record(data: dict[str, Any], settings: dict[str, Any]) -> dict[str, Any]:
    del settings
    _exact_keys(data, {"value", "note", "date"}, "measurement")
    return {
        "value": _number(data.get("value"), "value"),
        "note": _text_or_empty(data.get("note"), max_length=240),
        "date": _required_date(data.get("date")),
    }


def _note_record(data: dict[str, Any], settings: dict[str, Any]) -> dict[str, Any]:
    del settings
    _exact_keys(data, {"title", "body", "pinned"}, "note")
    return {
        "title": _text(data.get("title"), "title", max_length=120),
        "body": _text(data.get("body"), "body", max_length=5_000),
        "pinned": _boolean(data.get("pinned"), "pinned"),
    }


def _collection_entry_record(data: dict[str, Any], settings: dict[str, Any]) -> dict[str, Any]:
    fields = {field["key"]: field for field in settings["fields"]}
    unknown = set(data) - set(fields)
    if unknown:
        raise GeneratedAppValidationError("collection entry contains unknown fields")
    values = {
        field_key: _collection_value(value, fields[field_key])
        for field_key, value in data.items()
    }
    missing = [
        field["label"]
        for field in fields.values()
        if field["required"] and field["key"] not in values
    ]
    if missing:
        raise GeneratedAppValidationError(
            f"Missing required collection fields: {', '.join(missing)}"
        )
    return values


APP_MODULES: dict[str, AppModuleDefinition] = {
    "overview": AppModuleDefinition("overview", ("overview",), {}, _overview_settings),
    "todos": AppModuleDefinition(
        "todos", ("todo_items", "completion_progress"), {"todo": _todo_record}, _todos_settings
    ),
    "guest_list": AppModuleDefinition(
        "guest_list", ("guests", "rsvp_totals"), {"guest": _guest_record}, _guest_settings
    ),
    "itinerary": AppModuleDefinition(
        "itinerary",
        ("schedule", "timeline"),
        {"itinerary_item": _itinerary_record},
        _itinerary_settings,
    ),
    "expenses": AppModuleDefinition(
        "expenses",
        ("expenses", "category_totals", "budget_progress", "settlement_balances"),
        {"participant": _participant_record, "expense": _expense_record},
        _expenses_settings,
    ),
    "metric": AppModuleDefinition(
        "metric",
        ("measurements", "goal_progress", "trend"),
        {"measurement": _measurement_record},
        _metric_settings,
    ),
    "notes": AppModuleDefinition(
        "notes", ("notes", "pinned_notes"), {"note": _note_record}, _empty_settings
    ),
    "collection": AppModuleDefinition(
        "collection",
        ("custom_collection",),
        {"entry": _collection_entry_record},
        _collection_settings,
    ),
}


def _closed_schema(properties: dict[str, Any]) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": properties,
        "required": list(properties),
        "additionalProperties": False,
    }


def _module_tool_variant(module_type: str, settings: dict[str, Any]) -> dict[str, Any]:
    return _closed_schema(
        {
            "id": {"type": "string", "pattern": MODULE_ID_PATTERN.pattern},
            "type": {"type": "string", "enum": [module_type]},
            "title": {"type": "string", "minLength": 1, "maxLength": 80},
            "description": {"type": "string", "maxLength": 240},
            "settings": _closed_schema(settings),
        }
    )


GENERATED_APP_MODULES_TOOL_SCHEMA: dict[str, Any] = {
    "type": "array",
    "minItems": 1,
    "maxItems": 12,
    "items": {
        "anyOf": [
            _module_tool_variant(
                "overview",
                {
                    "body": {"type": "string", "maxLength": 1_000},
                    "facts": {
                        "type": "array",
                        "maxItems": 8,
                        "items": _closed_schema(
                            {
                                "label": {"type": "string", "maxLength": 60},
                                "value": {"type": "string", "maxLength": 160},
                            }
                        ),
                    },
                },
            ),
            _module_tool_variant("todos", {"show_completed": {"type": "boolean"}}),
            _module_tool_variant(
                "guest_list",
                {"allow_plus_ones": {"type": "boolean"}},
            ),
            _module_tool_variant(
                "itinerary",
                {"timezone": {"type": ["string", "null"]}},
            ),
            _module_tool_variant(
                "expenses",
                {
                    "currency": {"type": "string"},
                    "budget": {"type": ["number", "null"]},
                    "mode": {"type": "string", "enum": ["personal", "split"]},
                },
            ),
            _module_tool_variant(
                "metric",
                {
                    "unit": {"type": "string"},
                    "target": {"type": ["number", "null"]},
                    "direction": {
                        "type": "string",
                        "enum": ["increase", "decrease"],
                    },
                },
            ),
            _module_tool_variant("notes", {}),
            _module_tool_variant(
                "collection",
                {
                    "display": {
                        "type": "string",
                        "enum": ["list", "table", "cards"],
                    },
                    "primary_field": {"type": ["string", "null"]},
                    "currency": {"type": ["string", "null"]},
                    "fields": {
                        "type": "array",
                        "minItems": 1,
                        "maxItems": 12,
                        "items": _closed_schema(
                            {
                                "key": {
                                    "type": "string",
                                    "pattern": FIELD_ID_PATTERN.pattern,
                                },
                                "label": {
                                    "type": "string",
                                    "minLength": 1,
                                    "maxLength": 60,
                                },
                                "type": {
                                    "type": "string",
                                    "enum": sorted(COLLECTION_FIELD_TYPES),
                                },
                                "required": {"type": "boolean"},
                                "options": {
                                    "type": "array",
                                    "maxItems": 20,
                                    "items": {"type": "string", "maxLength": 60},
                                },
                            }
                        ),
                    },
                },
            ),
        ]
    },
}


def _initial_record_variant(kind: str, data: dict[str, Any]) -> dict[str, Any]:
    return _closed_schema(
        {
            "module_id": {"type": "string", "pattern": MODULE_ID_PATTERN.pattern},
            "kind": {"type": "string", "enum": [kind]},
            "actor_name": {"type": ["string", "null"]},
            "data": _closed_schema(data),
        }
    )


GENERATED_APP_INITIAL_RECORDS_TOOL_SCHEMA: dict[str, Any] = {
    "type": "array",
    "maxItems": 200,
    "items": {
        "anyOf": [
            _initial_record_variant(
                "todo",
                {
                    "text": {"type": "string"},
                    "completed": {"type": "boolean"},
                    "due_date": {"type": ["string", "null"]},
                    "assignee": {"type": ["string", "null"]},
                    "priority": {
                        "type": "string",
                        "enum": ["low", "normal", "high"],
                    },
                },
            ),
            _initial_record_variant(
                "guest",
                {
                    "name": {"type": "string"},
                    "status": {
                        "type": "string",
                        "enum": ["invited", "going", "maybe", "declined"],
                    },
                    "party_size": {"type": "integer", "minimum": 1, "maximum": 20},
                    "note": {"type": "string"},
                },
            ),
            _initial_record_variant(
                "itinerary_item",
                {
                    "title": {"type": "string"},
                    "date": {"type": ["string", "null"]},
                    "start_time": {"type": ["string", "null"]},
                    "end_time": {"type": ["string", "null"]},
                    "location": {"type": "string"},
                    "note": {"type": "string"},
                    "completed": {"type": "boolean"},
                },
            ),
            _initial_record_variant("participant", {"name": {"type": "string"}}),
            _initial_record_variant(
                "expense",
                {
                    "amount": {"type": "number"},
                    "category": {"type": "string"},
                    "note": {"type": "string"},
                    "date": {"type": "string"},
                    "paid_by": {"type": ["string", "null"]},
                    "split_between": {
                        "type": "array",
                        "items": {"type": "string"},
                        "maxItems": 20,
                    },
                },
            ),
            _initial_record_variant(
                "measurement",
                {
                    "value": {"type": "number"},
                    "note": {"type": "string"},
                    "date": {"type": "string"},
                },
            ),
            _initial_record_variant(
                "note",
                {
                    "title": {"type": "string"},
                    "body": {"type": "string"},
                    "pinned": {"type": "boolean"},
                },
            ),
            _initial_record_variant(
                "entry",
                {
                    "values": {
                        "type": "array",
                        "maxItems": 12,
                        "items": _closed_schema(
                            {
                                "field_key": {"type": "string"},
                                "text_value": {"type": ["string", "null"]},
                                "number_value": {"type": ["number", "null"]},
                                "boolean_value": {"type": ["boolean", "null"]},
                            }
                        ),
                    }
                },
            ),
        ]
    },
}


def normalize_tool_initial_records(raw_records: Any) -> list[dict[str, Any]]:
    if not isinstance(raw_records, list):
        raise GeneratedAppValidationError("initial_records must be a list")
    normalized: list[dict[str, Any]] = []
    for raw_record in raw_records:
        if not isinstance(raw_record, dict):
            raise GeneratedAppValidationError("initial records must be objects")
        record = dict(raw_record)
        if record.get("kind") == "entry":
            data = record.get("data")
            raw_values = data.get("values") if isinstance(data, dict) else None
            if not isinstance(raw_values, list):
                raise GeneratedAppValidationError("collection seed values must be a list")
            values: dict[str, Any] = {}
            for raw_value in raw_values:
                if not isinstance(raw_value, dict):
                    raise GeneratedAppValidationError("collection seed values must be objects")
                key = raw_value.get("field_key")
                if not isinstance(key, str) or key in values:
                    raise GeneratedAppValidationError("collection seed field keys must be unique")
                candidates = [
                    raw_value.get("text_value"),
                    raw_value.get("number_value"),
                    raw_value.get("boolean_value"),
                ]
                present = [value for value in candidates if value is not None]
                if len(present) > 1:
                    raise GeneratedAppValidationError(
                        "collection seed values must use one typed value"
                    )
                values[key] = present[0] if present else None
            record["data"] = values
        normalized.append(record)
    return normalized


def validate_modules(raw_modules: Any) -> list[dict[str, Any]]:
    if not isinstance(raw_modules, list) or not 1 <= len(raw_modules) <= 12:
        raise GeneratedAppValidationError("modules must contain 1 to 12 modules")
    modules: list[dict[str, Any]] = []
    seen: set[str] = set()
    for raw_module in raw_modules:
        if not isinstance(raw_module, dict):
            raise GeneratedAppValidationError("modules must be objects")
        _exact_keys(raw_module, {"id", "type", "title", "description", "settings"}, "module")
        module_id = _identifier(raw_module.get("id"), "module id", MODULE_ID_PATTERN)
        if module_id in seen:
            raise GeneratedAppValidationError("module ids must be unique")
        seen.add(module_id)
        module_type = raw_module.get("type")
        definition = APP_MODULES.get(str(module_type))
        if definition is None:
            raise GeneratedAppValidationError("unsupported app module type")
        settings = raw_module.get("settings")
        if not isinstance(settings, dict):
            raise GeneratedAppValidationError("module settings must be an object")
        modules.append(
            {
                "id": module_id,
                "type": definition.type,
                "title": _text(raw_module.get("title"), "module title", max_length=80),
                "description": _text_or_empty(raw_module.get("description"), max_length=240),
                "settings": definition.settings_validator(settings),
            }
        )
    return modules


def build_composable_specification(
    *,
    theme: str,
    modules: Any,
) -> dict[str, Any]:
    if theme not in APP_THEMES:
        raise GeneratedAppValidationError("Unsupported app theme")
    clean_modules = validate_modules(modules)
    capabilities = list(
        dict.fromkeys(
            capability
            for module in clean_modules
            for capability in APP_MODULES[module["type"]].capabilities
        )
    )
    return {
        "schema_version": APP_SCHEMA_VERSION,
        "template": "workspace",
        "theme": theme,
        "settings": {},
        "capabilities": capabilities,
        "modules": clean_modules,
    }


def modules_from_specification(
    specification: dict[str, Any],
    *,
    legacy_template: str,
) -> list[dict[str, Any]]:
    raw_modules = specification.get("modules")
    if specification.get("schema_version") == APP_SCHEMA_VERSION and isinstance(
        raw_modules, list
    ):
        return validate_modules(raw_modules)
    return [_legacy_module(legacy_template, dict(specification.get("settings") or {}))]


def resolve_record_module(
    specification: dict[str, Any],
    *,
    legacy_template: str,
    module_id: str | None,
    kind: str,
) -> dict[str, Any]:
    modules = modules_from_specification(specification, legacy_template=legacy_template)
    if module_id is not None:
        clean_id = _identifier(module_id, "module id", MODULE_ID_PATTERN)
        matches = [module for module in modules if module["id"] == clean_id]
        if not matches:
            raise GeneratedAppValidationError("module_id does not belong to this app")
        module = matches[0]
        if kind not in APP_MODULES[module["type"]].record_validators:
            raise GeneratedAppValidationError("Record type does not match this module")
        return module
    matches = [
        module
        for module in modules
        if kind in APP_MODULES[module["type"]].record_validators
    ]
    if len(matches) != 1:
        raise GeneratedAppValidationError("module_id is required for this record type")
    return matches[0]


def validate_module_record(
    module: dict[str, Any],
    *,
    kind: str,
    data: dict[str, Any],
) -> dict[str, Any]:
    definition = APP_MODULES[module["type"]]
    validator = definition.record_validators.get(kind)
    if validator is None:
        raise GeneratedAppValidationError("Record type does not match this module")
    if not isinstance(data, dict):
        raise GeneratedAppValidationError("record data must be an object")
    return validator(data, module["settings"])


def _legacy_module(template: str, settings: dict[str, Any]) -> dict[str, Any]:
    if template == "budget":
        return {
            "id": "expenses",
            "type": "expenses",
            "title": "Expenses",
            "description": "",
            "settings": {
                "currency": settings.get("currency") or "USD",
                "budget": settings.get("monthly_budget"),
                "mode": "personal",
            },
        }
    if template == "expense_splitter":
        return {
            "id": "expenses",
            "type": "expenses",
            "title": "Shared expenses",
            "description": "",
            "settings": {
                "currency": settings.get("currency") or "USD",
                "budget": None,
                "mode": "split",
            },
        }
    if template == "metric_tracker":
        return {
            "id": "metric",
            "type": "metric",
            "title": "Progress",
            "description": "",
            "settings": {
                "unit": settings.get("unit") or "units",
                "target": settings.get("target"),
                "direction": settings.get("direction") or "decrease",
            },
        }
    if template == "checklist":
        return {
            "id": "todos",
            "type": "todos",
            "title": "To dos",
            "description": "",
            "settings": {"show_completed": True},
        }
    raise GeneratedAppValidationError("Unsupported legacy app template")


def _collection_value(value: Any, field: dict[str, Any]) -> Any:
    if value is None:
        if field["required"]:
            raise GeneratedAppValidationError(f"{field['label']} is required")
        return None
    field_type = field["type"]
    if field_type in {"number", "currency"}:
        return _number(value, field["label"])
    if field_type == "boolean":
        return _boolean(value, field["label"])
    if field_type == "date":
        return _required_date(value)
    if field_type == "time":
        parsed = _optional_time(value)
        if parsed is None:
            raise GeneratedAppValidationError(f"{field['label']} is required")
        return parsed
    clean = _text(value, field["label"], max_length=500)
    if field_type == "select" and clean not in field["options"]:
        raise GeneratedAppValidationError(f"{field['label']} must use a configured option")
    return clean


def _exact_keys(value: dict[str, Any], expected: set[str], label: str) -> None:
    extras = set(value) - expected
    missing = expected - set(value)
    if extras or missing:
        raise GeneratedAppValidationError(
            f"{label} fields must be exactly: {', '.join(sorted(expected)) or '(none)'}"
        )


def _identifier(value: Any, label: str, pattern: re.Pattern[str]) -> str:
    if not isinstance(value, str) or not pattern.fullmatch(value):
        raise GeneratedAppValidationError(
            f"{label} must start with a letter and contain lowercase letters, "
            "numbers, or underscores"
        )
    return value


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


def _text_or_empty(value: Any, *, max_length: int) -> str:
    if value == "":
        return ""
    return _optional_text(value, max_length=max_length) or ""


def _boolean(value: Any, field_name: str) -> bool:
    if not isinstance(value, bool):
        raise GeneratedAppValidationError(f"{field_name} must be true or false")
    return value


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


def _required_date(value: Any) -> str:
    if not isinstance(value, str):
        raise GeneratedAppValidationError("date must use YYYY-MM-DD")
    try:
        return date.fromisoformat(value).isoformat()
    except ValueError as error:
        raise GeneratedAppValidationError("date must use YYYY-MM-DD") from error


def _optional_date(value: Any) -> str | None:
    return None if value is None else _required_date(value)


def _optional_time(value: Any) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise GeneratedAppValidationError("time must use HH:MM")
    try:
        return time.fromisoformat(value).isoformat(timespec="minutes")
    except ValueError as error:
        raise GeneratedAppValidationError("time must use HH:MM") from error


def _unique_texts(
    values: list[Any],
    *,
    field_name: str,
    max_items: int,
    max_length: int,
) -> list[str]:
    if len(values) > max_items:
        raise GeneratedAppValidationError(f"{field_name} has too many values")
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        clean = _text(value, field_name, max_length=max_length)
        key = clean.casefold()
        if key not in seen:
            seen.add(key)
            result.append(clean)
    return result
