from __future__ import annotations

import re
from pathlib import PurePosixPath

from benji_api.app_builder.types import GeneratedSource, ValidationIssue

ALLOWED_SOURCE_SUFFIXES = frozenset({".ts", ".tsx"})
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
_IMPORT_PATTERN = re.compile(
    r"(?:import\s+(?:[^;]+?\s+from\s+)?|export\s+[^;]+?\s+from\s+)[\"']([^\"']+)[\"']"
)
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
        "reserved_workflow_marker",
        re.compile(r"\bdata-dot-(?:operation|entity)\b"),
        "data-dot workflow markers are SDK-reserved; use WorkflowForm or PrimaryWorkflowTrigger",
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
                    "source files must be .ts or .tsx files below src/",
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


def inspect_generated_source(
    source: GeneratedSource,
) -> tuple[ValidationIssue, ...]:
    """Apply gates available without npm, Docker, or provider credentials."""

    return tuple(_source_issues(source))
