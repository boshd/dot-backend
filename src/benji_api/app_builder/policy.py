from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from pathlib import PurePosixPath
from typing import Any

from benji_api.app_builder.types import GeneratedSource, ValidationIssue

ALLOWED_SOURCE_SUFFIXES = frozenset({".ts", ".tsx", ".css"})
ALLOWED_IMPORTS = frozenset(
    {
        "@dot/app-runtime",
        "@dot/ui",
        "date-fns",
        "lucide-react",
        "motion/react",
        "react",
        "recharts",
    }
)
ALLOWED_RENDER_NODE_TYPES = frozenset(
    {
        "page",
        "hero",
        "section",
        "stack",
        "cluster",
        "grid",
        "card",
        "heading",
        "text",
        "badge",
        "button",
        "metric",
        "progress",
        "callout",
        "divider",
        "list",
        "table",
        "timeline",
        "kanban",
        "form",
        "empty",
        "sparkline",
    }
)

_RENDER_NODE_KEYS = frozenset(
    {
        "id",
        "type",
        "children",
        "title",
        "subtitle",
        "body",
        "value",
        "label",
        "overline",
        "source",
        "tone",
        "size",
        "align",
        "gap",
        "columns",
        "variant",
        "action",
        "format",
        "currency",
        "min",
        "max",
        "items",
        "item_title",
        "item_detail",
        "item_meta",
        "table_columns",
        "lanes",
        "fields",
        "submit_label",
        "points",
    }
)

_IMPORT_PATTERN = re.compile(
    r"(?:import\s+(?:[^;]+?\s+from\s+)?|export\s+[^;]+?\s+from\s+)[\"']([^\"']+)[\"']"
)
_NODE_IDENTIFIER = re.compile(r"^[a-z][a-z0-9_]{0,47}$")
_FORBIDDEN_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("network_access", re.compile(r"\b(?:fetch|XMLHttpRequest|WebSocket|EventSource)\s*\(")),
    ("dynamic_code", re.compile(r"\b(?:eval|Function)\s*\(")),
    ("dynamic_import", re.compile(r"\b(?:import|require)\s*\(")),
    ("unsafe_html", re.compile(r"\bdangerouslySetInnerHTML\b")),
    ("browser_storage", re.compile(r"\b(?:localStorage|sessionStorage|indexedDB)\b")),
    ("cookie_access", re.compile(r"\bdocument\s*\.\s*cookie\b")),
    ("service_worker", re.compile(r"\bserviceWorker\b")),
    ("direct_host_bridge", re.compile(r"\bpostMessage\s*\(")),
    ("beacon_access", re.compile(r"\bsendBeacon\s*\(")),
    ("location_change", re.compile(r"\b(?:window\s*\.\s*)?location\s*[.=]")),
    ("node_runtime", re.compile(r"\b(?:process|Deno|Bun)\s*\.")),
    # Match actual URL literals, not ordinary TypeScript `//` comments. Build isolation and the
    # product runtime's CSP remain the authoritative network boundaries.
    ("external_url", re.compile(r"\bhttps?://", re.IGNORECASE)),
    ("css_import", re.compile(r"@import\b", re.IGNORECASE)),
    ("css_url", re.compile(r"\burl\s*\(", re.IGNORECASE)),
)

# Generated apps get their visual vocabulary from @dot/ui. These checks are deliberately
# narrower than a generic HTML/CSS sanitizer: they make the branded boundary enforceable before
# compilation instead of relying on a model to remember a style-guide prompt.
_DESIGN_CONTRACT_PATTERNS: tuple[tuple[str, re.Pattern[str], str], ...] = (
    (
        "reserved_primary_action_marker",
        re.compile(r"\bdata-dot-primary-action\b"),
        "data-dot-primary-action is SDK-reserved; use PrimaryWorkflowTrigger",
    ),
    (
        "custom_class_name",
        re.compile(r"<[^>]*\bclassName\s*=", re.MULTILINE),
        "className is not part of the generated-app design contract; use @dot/ui semantic props",
    ),
    (
        "inline_style",
        re.compile(r"<[^>]*\bstyle\s*=", re.MULTILINE),
        "inline styles are not allowed; use @dot/ui semantic props",
    ),
    (
        "embedded_style_element",
        re.compile(r"<\s*style\b", re.IGNORECASE),
        "embedded style elements are not allowed; use @dot/ui",
    ),
    (
        "unbranded_interactive_control",
        re.compile(r"<\s*(?:button|input|textarea|select)\b"),
        "native interactive controls must use the matching @dot/ui component",
    ),
    (
        "imperative_element_creation",
        re.compile(r"\b(?:React\s*\.\s*)?createElement\s*\("),
        "imperative element creation can bypass the generated-app design contract",
    ),
    (
        "raw_color_value",
        re.compile(
            r"(?<![\w-])#[0-9a-f]{3,8}\b|\b(?:rgb|rgba|hsl|hsla|hwb|lab|lch|oklab|oklch)\s*\(",
            re.IGNORECASE,
        ),
        "raw color values are not allowed; use semantic accents or chartTokens",
    ),
    (
        "raw_color_attribute",
        re.compile(
            r"\b(?:color|fill|stroke|stopColor|floodColor)\s*=\s*[\"']"
            r"(?!(?:none|currentColor|transparent|var\(--dot-[a-z0-9-]+\))[\"'])",
            re.IGNORECASE,
        ),
        "visual color attributes must use currentColor, a Dot token, or chartTokens",
    ),
)


def _is_allowed_import(specifier: str) -> bool:
    if specifier.startswith("./") or specifier.startswith("../"):
        return True
    return specifier in ALLOWED_IMPORTS


def _source_location(path: str, contents: str, match: re.Match[str]) -> str:
    """Give repairs an exact source position without changing the issue schema."""

    line = contents.count("\n", 0, match.start()) + 1
    line_start = contents.rfind("\n", 0, match.start()) + 1
    column = match.start() - line_start + 1
    return f"{path}:{line}:{column}"


def _source_issues(source: GeneratedSource) -> list[ValidationIssue]:
    issues: list[ValidationIssue] = []
    if not source.files:
        issues.append(ValidationIssue("missing_source", "build produced no source files"))
        return issues
    if len(source.files) > 32:
        issues.append(ValidationIssue("too_many_files", "build may contain at most 32 files"))
    total_bytes = sum(len(item.contents.encode()) for item in source.files)
    if total_bytes > 512_000:
        issues.append(ValidationIssue("source_too_large", "source may be at most 512 KB"))

    paths: set[str] = set()
    for item in source.files:
        path = PurePosixPath(item.path)
        if (
            path.is_absolute()
            or ".." in path.parts
            or not item.path.startswith("src/")
            or path.suffix not in ALLOWED_SOURCE_SUFFIXES
        ):
            issues.append(
                ValidationIssue(
                    "invalid_source_path",
                    "source files must be .ts, .tsx, or .css files below src/",
                    item.path,
                )
            )
        if item.path in paths:
            issues.append(
                ValidationIssue("duplicate_source_path", "file path is duplicated", item.path)
            )
        paths.add(item.path)
        if len(item.contents.encode()) > 256_000:
            issues.append(
                ValidationIssue(
                    "file_too_large",
                    "individual source files may be at most 256 KB",
                    item.path,
                )
            )
        if path.suffix == ".css":
            issues.append(
                ValidationIssue(
                    "generated_css_not_allowed",
                    "generated CSS is not allowed; compose the branded @dot/ui primitives",
                    item.path,
                )
            )
        for specifier in _IMPORT_PATTERN.findall(item.contents):
            if not _is_allowed_import(specifier):
                issues.append(
                    ValidationIssue(
                        "dependency_not_allowed",
                        f"dependency {specifier!r} is not in the approved catalog",
                        item.path,
                    )
                )
        for code, pattern in _FORBIDDEN_PATTERNS:
            if match := pattern.search(item.contents):
                issues.append(
                    ValidationIssue(
                        code,
                        f"source contains forbidden capability: {code}",
                        _source_location(item.path, item.contents, match),
                    )
                )
        if path.suffix in {".ts", ".tsx"}:
            for code, pattern, message in _DESIGN_CONTRACT_PATTERNS:
                if match := pattern.search(item.contents):
                    issues.append(
                        ValidationIssue(
                            code,
                            message,
                            _source_location(item.path, item.contents, match),
                        )
                    )

    if source.entrypoint not in paths:
        issues.append(
            ValidationIssue(
                "missing_entrypoint",
                "entrypoint is not present in source files",
                source.entrypoint,
            )
        )
    else:
        entrypoint = next(item.contents for item in source.files if item.path == source.entrypoint)
        if not re.search(r"\bexport\s+default\b", entrypoint):
            issues.append(
                ValidationIssue(
                    "missing_default_export",
                    "entrypoint must have a default export",
                    source.entrypoint,
                )
            )
        issues.extend(_balanced_delimiter_issues(entrypoint, path=source.entrypoint))
    return issues


def _balanced_delimiter_issues(value: str, *, path: str) -> list[ValidationIssue]:
    """Cheap structural gate; a sandbox provider adds a real TypeScript compiler gate later."""

    pairs = {")": "(", "]": "[", "}": "{"}
    stack: list[str] = []
    quote: str | None = None
    escaped = False
    for character in value:
        if quote is not None:
            if escaped:
                escaped = False
            elif character == "\\":
                escaped = True
            elif character == quote:
                quote = None
            continue
        if character in {"\"", "'", "`"}:
            quote = character
        elif character in pairs.values():
            stack.append(character)
        elif character in pairs and (not stack or stack.pop() != pairs[character]):
            return [ValidationIssue("invalid_typescript", "unbalanced delimiter", path)]
    if quote is not None or stack:
        return [ValidationIssue("invalid_typescript", "unterminated string or delimiter", path)]
    return []


def _render_document_issues(
    document: Mapping[str, Any],
    *,
    allowed_capabilities: frozenset[str],
    manifest: Mapping[str, Any] | None = None,
) -> list[ValidationIssue]:
    issues: list[ValidationIssue] = []
    node_ids: set[str] = set()
    visited = 0

    if set(document) - {"schema_version", "theme", "data", "root"}:
        issues.append(
            ValidationIssue(
                "unknown_render_document_property",
                "render document contains unknown top-level properties",
                "render_document",
            )
        )
    if document.get("schema_version") != 1:
        issues.append(
            ValidationIssue(
                "unsupported_render_schema",
                "render document schema_version must be 1",
                "render_document.schema_version",
            )
        )
    theme = document.get("theme", {})
    if not isinstance(theme, Mapping):
        issues.append(
            ValidationIssue(
                "invalid_render_theme",
                "theme must be an object",
                "render_document.theme",
            )
        )
    else:
        theme_options = {
            "accent": {"coral", "sage", "ocean", "plum", "sky"},
            "density": {"compact", "comfortable", "spacious"},
            "radius": {"soft", "round", "sharp"},
        }
        if set(theme) - set(theme_options):
            issues.append(
                ValidationIssue(
                    "unknown_render_theme_property",
                    "theme contains unknown properties",
                    "render_document.theme",
                )
            )
        for key, allowed in theme_options.items():
            if key in theme and theme[key] not in allowed:
                issues.append(
                    ValidationIssue(
                        "invalid_render_theme_value",
                        f"unsupported {key} value",
                        f"render_document.theme.{key}",
                    )
                )
    data = document.get("data", {})
    if not isinstance(data, Mapping):
        issues.append(
            ValidationIssue("invalid_render_data", "data must be an object", "render_document.data")
        )
    try:
        import json

        document_bytes = len(json.dumps(dict(document), ensure_ascii=False).encode())
    except (TypeError, ValueError):
        issues.append(
            ValidationIssue(
                "invalid_render_document",
                "render document must contain JSON values",
                "render_document",
            )
        )
        document_bytes = 0
    if document_bytes > 256_000:
        issues.append(
            ValidationIssue(
                "render_document_too_large",
                "render document may be at most 256 KB",
                "render_document",
            )
        )

    def visit(node: object, *, path: str, depth: int) -> None:
        nonlocal visited
        visited += 1
        if visited > 250:
            if not any(issue.code == "render_document_too_large" for issue in issues):
                issues.append(
                    ValidationIssue(
                        "render_document_too_large",
                        "render document may contain at most 250 nodes",
                        path,
                    )
                )
            return
        if depth > 12:
            issues.append(
                ValidationIssue(
                    "render_document_too_deep",
                    "render document may be at most 12 nodes deep",
                    path,
                )
            )
            return
        if not isinstance(node, Mapping):
            issues.append(
                ValidationIssue("invalid_render_node", "render node must be an object", path)
            )
            return
        node_type = node.get("type")
        if node_type not in ALLOWED_RENDER_NODE_TYPES:
            issues.append(
                ValidationIssue(
                    "unsupported_render_node",
                    f"unsupported render node type: {node_type!r}",
                    path,
                )
            )
        unknown = set(node) - _RENDER_NODE_KEYS
        if unknown:
            issues.append(
                ValidationIssue(
                    "unknown_render_node_property",
                    f"render node has unknown properties: {', '.join(sorted(unknown))}",
                    path,
                )
            )
        node_id = node.get("id")
        if not isinstance(node_id, str) or not _NODE_IDENTIFIER.fullmatch(node_id):
            issues.append(
                ValidationIssue("invalid_render_node_id", "render node must have a safe id", path)
            )
        elif node_id in node_ids:
            issues.append(
                ValidationIssue(
                    "duplicate_render_node_id",
                    f"render node id is duplicated: {node_id}",
                    f"{path}.id",
                )
            )
        else:
            node_ids.add(node_id)
        for key, value in node.items():
            if key in {"id", "type", "children"}:
                continue
            if key == "action":
                _validate_action(
                    value,
                    path=f"{path}.action",
                    issues=issues,
                    allowed_capabilities=allowed_capabilities,
                    manifest=manifest,
                )
            elif isinstance(value, Mapping) and "bind" in value:
                binding = value.get("bind")
                if not isinstance(binding, str) or not re.fullmatch(
                    r"[a-zA-Z0-9_]+(?:\.[a-zA-Z0-9_]+)*", binding
                ):
                    issues.append(
                        ValidationIssue(
                            "invalid_render_binding",
                            "binding must be a safe dotted data path",
                            f"{path}.{key}",
                        )
                    )
        children = node.get("children", [])
        if not isinstance(children, Sequence) or isinstance(children, (str, bytes)):
            issues.append(
                ValidationIssue("invalid_render_children", "children must be an array", path)
            )
            return
        for index, child in enumerate(children):
            visit(child, path=f"{path}.children[{index}]", depth=depth + 1)

    root = document.get("root")
    if root is None:
        issues.append(
            ValidationIssue(
                "missing_render_root", "render document must have a root", "render_document.root"
            )
        )
    else:
        visit(root, path="render_document.root", depth=0)
    return issues


def _validate_action(
    value: object,
    *,
    path: str,
    issues: list[ValidationIssue],
    allowed_capabilities: frozenset[str],
    manifest: Mapping[str, Any] | None,
) -> None:
    if not isinstance(value, Mapping):
        issues.append(ValidationIssue("invalid_render_action", "action must be an object", path))
        return
    if set(value) - {"operation", "payload", "confirm"}:
        issues.append(
            ValidationIssue("unknown_render_action_property", "action has unknown properties", path)
        )
    operation = value.get("operation")
    allowed_operations = {
        "records.create",
        "records.update",
        "records.delete",
    }
    allowed_operations.update(allowed_capabilities)
    if operation not in allowed_operations:
        issues.append(
            ValidationIssue(
                "invalid_render_action_operation",
                "action operation must be a declared persistent mutation or capability",
                f"{path}.operation",
            )
        )
    payload = value.get("payload")
    if payload is not None and not isinstance(payload, Mapping):
        issues.append(
            ValidationIssue(
                "invalid_render_action_payload",
                "action payload must be an object",
                f"{path}.payload",
            )
        )
    confirm = value.get("confirm")
    if confirm is not None and (
        not isinstance(confirm, Mapping) or not isinstance(confirm.get("title"), str)
    ):
        issues.append(
            ValidationIssue(
                "invalid_render_action_confirmation",
                "confirmation must have a title",
                f"{path}.confirm",
            )
        )
    if operation == "dot.reminder.create" and confirm is None:
        issues.append(
            ValidationIssue(
                "reminder_confirmation_required",
                "safe reminder actions require an explicit confirmation",
                f"{path}.confirm",
            )
        )
    if operation in {"records.create", "records.update"}:
        _validate_record_action_payload(
            operation=operation,
            payload=payload,
            manifest=manifest,
            path=f"{path}.payload",
            issues=issues,
        )
    elif operation == "records.delete" and (
        not isinstance(payload, Mapping) or not isinstance(payload.get("record_id"), str)
    ):
        issues.append(
            ValidationIssue(
                "invalid_record_action_payload",
                "delete actions require a record_id binding or value",
                f"{path}.payload.record_id",
            )
        )


def _validate_record_action_payload(
    *,
    operation: object,
    payload: object,
    manifest: Mapping[str, Any] | None,
    path: str,
    issues: list[ValidationIssue],
) -> None:
    if not isinstance(payload, Mapping):
        issues.append(
            ValidationIssue(
                "invalid_record_action_payload",
                "record actions require an object payload",
                path,
            )
        )
        return
    entity = payload.get("entity")
    if not isinstance(entity, str):
        issues.append(
            ValidationIssue(
                "invalid_record_action_entity",
                "record actions require a declared entity",
                f"{path}.entity",
            )
        )
        return
    definitions = (
        manifest.get("entities", []) if isinstance(manifest, Mapping) else []
    )
    definition = next(
        (
            item
            for item in definitions
            if isinstance(item, Mapping) and item.get("name") == entity
        ),
        None,
    )
    if definition is None:
        issues.append(
            ValidationIssue(
                "undeclared_record_action_entity",
                f"record action uses undeclared entity {entity!r}",
                f"{path}.entity",
            )
        )
        return
    data = payload.get("data")
    if not isinstance(data, Mapping):
        issues.append(
            ValidationIssue(
                "invalid_record_action_data",
                "create and update actions require an object data payload",
                f"{path}.data",
            )
        )
        return
    fields = definition.get("fields", {})
    declared = (
        set(fields)
        if isinstance(fields, Mapping)
        else {
            item.get("name")
            for item in fields
            if isinstance(item, Mapping) and isinstance(item.get("name"), str)
        }
        if isinstance(fields, Sequence) and not isinstance(fields, (str, bytes))
        else set()
    )
    unknown = set(data) - declared
    if unknown:
        issues.append(
            ValidationIssue(
                "undeclared_record_action_field",
                f"record action uses undeclared fields: {', '.join(sorted(unknown))}",
                f"{path}.data",
            )
        )
    if operation == "records.update" and not isinstance(payload.get("record_id"), str):
        issues.append(
            ValidationIssue(
                "invalid_record_action_payload",
                "update actions require a record_id binding or value",
                f"{path}.record_id",
            )
        )


def inspect_generated_source(
    source: GeneratedSource,
    *,
    allowed_capabilities: frozenset[str] = frozenset(),
    manifest: Mapping[str, Any] | None = None,
) -> tuple[ValidationIssue, ...]:
    """Apply gates available without npm, Docker, or provider credentials."""

    return tuple(
        [
            *_source_issues(source),
            *_render_document_issues(
                source.render_document,
                allowed_capabilities=allowed_capabilities,
                manifest=manifest,
            ),
        ]
    )
