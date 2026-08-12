from __future__ import annotations

import json
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from hashlib import sha256
from types import MappingProxyType
from typing import Any, Literal, Protocol

from benji_api.generated_app_contract import (
    GeneratedAppCapabilityError,
    parse_generated_app_capabilities,
)

BuildStage = Literal[
    "validate",
    "generate",
    "repair",
    "inspect",
    "compile",
    "smoke",
    "package",
]
AppAccent = Literal["coral", "sage", "ocean", "plum", "sky"]

# The static render is an untrusted build artifact that is embedded in the persisted
# revision and later sanitized by the web runtime. Keep it small enough for the build
# worker response and the database-backed MVP artifact envelope.
MAX_STATIC_HTML_BYTES = 256_000

_IDENTIFIER = re.compile(r"^[a-z][a-z0-9_]{0,47}$")
_ACCENTS = frozenset({"coral", "sage", "ocean", "plum", "sky"})
_LEGACY_ACCENTS = {
    "gold": "coral",
    "#e65f45": "coral",
    "#e7654b": "coral",
    "#5f8067": "sage",
    "#497a9d": "ocean",
    "#765779": "plum",
    "#5b88a8": "sky",
    "#a97824": "coral",
}


class BlueprintValidationError(ValueError):
    """Raised before model or sandbox work when the app contract is invalid."""


class BuildCompletionHookError(RuntimeError):
    """Normalized durable-settlement error raised by a build service hook."""

    def __init__(self, *, code: str, message: str, retryable: bool) -> None:
        self.code = code
        self.retryable = retryable
        super().__init__(message)


def canonical_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def content_hash(value: object) -> str:
    return sha256(canonical_json(value).encode()).hexdigest()


def _required_text(value: object, *, field_name: str, max_length: int) -> str:
    if not isinstance(value, str) or not value.strip():
        raise BlueprintValidationError(f"{field_name} must be a non-empty string")
    normalized = " ".join(value.split())
    if len(normalized) > max_length:
        raise BlueprintValidationError(f"{field_name} must be at most {max_length} characters")
    return normalized


def _optional_text(
    value: object,
    *,
    field_name: str,
    max_length: int,
    default: str,
) -> str:
    if value is None:
        return default
    return _required_text(value, field_name=field_name, max_length=max_length)


def _json_copy(value: Mapping[str, Any]) -> dict[str, Any]:
    try:
        copied = json.loads(canonical_json(value))
    except (TypeError, ValueError) as exc:
        raise BlueprintValidationError("blueprint must contain only JSON values") from exc
    if not isinstance(copied, dict):
        raise BlueprintValidationError("blueprint must be a JSON object")
    return copied


@dataclass(frozen=True, slots=True)
class AppBlueprint:
    """Validated product contract consumed by every source-generation provider.

    UI source is intentionally not part of this contract. The generator owns composition and
    interaction, while the manifest remains the authority boundary for data and capabilities.
    """

    title: str
    description: str
    purpose: str
    layout: str = "workspace"
    accent: AppAccent = "coral"
    product_brief: str = ""
    visual_direction: str = ""
    revision_request: str = ""
    base_revision: Mapping[str, Any] = field(default_factory=lambda: MappingProxyType({}))
    manifest: Mapping[str, Any] = field(default_factory=lambda: MappingProxyType({}))
    seed_data: Mapping[str, Any] = field(default_factory=lambda: MappingProxyType({}))

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any]) -> AppBlueprint:
        data = _json_copy(raw)
        title = _required_text(data.get("title"), field_name="title", max_length=120)
        description = _optional_text(
            data.get("description"),
            field_name="description",
            max_length=500,
            default=title,
        )
        purpose = _optional_text(
            data.get("purpose"),
            field_name="purpose",
            max_length=500,
            default=description,
        )
        layout = (
            _optional_text(
                data.get("layout"),
                field_name="layout",
                max_length=48,
                default="workspace",
            )
            .lower()
            .replace("-", "_")
            .replace(" ", "_")
        )
        if not _IDENTIFIER.fullmatch(layout):
            raise BlueprintValidationError("layout must be a lowercase identifier")
        accent = data.get("accent", "coral")
        if isinstance(accent, str):
            accent = _LEGACY_ACCENTS.get(accent.casefold(), accent.casefold())
        if accent not in _ACCENTS:
            raise BlueprintValidationError("accent must be a supported Dot semantic accent")
        manifest = data.get("manifest", {})
        seed_data = data.get("seed_data", {})
        if not isinstance(manifest, dict):
            raise BlueprintValidationError("manifest must be an object")
        try:
            parse_generated_app_capabilities(manifest)
        except GeneratedAppCapabilityError as exc:
            raise BlueprintValidationError(str(exc)) from exc
        if not isinstance(seed_data, dict):
            raise BlueprintValidationError("seed_data must be an object")
        product_brief = _optional_text(
            data.get("product_brief"),
            field_name="product_brief",
            max_length=4_000,
            default=purpose,
        )
        visual_direction = _optional_text(
            data.get("visual_direction"),
            field_name="visual_direction",
            max_length=1_000,
            default="purpose-native, mobile-first, and composed within Dot's design system",
        )
        revision_request = _optional_text(
            data.get("revision_request"),
            field_name="revision_request",
            max_length=4_000,
            default="",
        )
        base_revision = data.get("base_revision", {})
        if not isinstance(base_revision, dict):
            raise BlueprintValidationError("base_revision must be an object")
        if len(canonical_json(manifest)) > 64_000:
            raise BlueprintValidationError("manifest is too large")
        if len(canonical_json(seed_data)) > 128_000:
            raise BlueprintValidationError("seed_data is too large")
        if len(canonical_json(base_revision)) > 768_000:
            raise BlueprintValidationError("base_revision is too large")
        return cls(
            title=title,
            description=description,
            purpose=purpose,
            layout=layout,
            accent=accent,
            product_brief=product_brief,
            visual_direction=visual_direction,
            revision_request=revision_request,
            base_revision=MappingProxyType(base_revision),
            manifest=MappingProxyType(manifest),
            seed_data=MappingProxyType(seed_data),
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "title": self.title,
            "description": self.description,
            "purpose": self.purpose,
            "layout": self.layout,
            "accent": self.accent,
            "product_brief": self.product_brief,
            "visual_direction": self.visual_direction,
            "revision_request": self.revision_request,
            "base_revision": dict(self.base_revision),
            "manifest": dict(self.manifest),
            "seed_data": dict(self.seed_data),
        }


@dataclass(frozen=True, slots=True)
class SourceFile:
    path: str
    contents: str

    def as_dict(self) -> dict[str, str]:
        return {"path": self.path, "contents": self.contents}


@dataclass(frozen=True, slots=True)
class GeneratedSource:
    files: tuple[SourceFile, ...]
    entrypoint: str
    render_document: Mapping[str, Any]
    provider_metadata: Mapping[str, Any] = field(default_factory=lambda: MappingProxyType({}))


@dataclass(frozen=True, slots=True)
class BuildClaim:
    job_id: str
    app_id: str
    revision_id: str | None
    blueprint: Mapping[str, Any]
    attempt: int = 1


@dataclass(frozen=True, slots=True)
class ValidationIssue:
    code: str
    message: str
    path: str | None = None

    def as_dict(self) -> dict[str, str]:
        value = {"code": self.code, "message": self.message}
        if self.path is not None:
            value["path"] = self.path
        return value


@dataclass(frozen=True, slots=True)
class BuildMetrics:
    started_at: str
    completed_at: str
    duration_ms: int
    target_duration_ms: int
    within_target: bool
    generation_attempts: int
    repair_attempts: int
    source_files: int
    source_bytes: int
    stage_duration_ms: Mapping[str, int]

    def as_dict(self) -> dict[str, Any]:
        return {
            "started_at": self.started_at,
            "completed_at": self.completed_at,
            "duration_ms": self.duration_ms,
            "target_duration_ms": self.target_duration_ms,
            "within_target": self.within_target,
            "generation_attempts": self.generation_attempts,
            "repair_attempts": self.repair_attempts,
            "source_files": self.source_files,
            "source_bytes": self.source_bytes,
            "stage_duration_ms": dict(self.stage_duration_ms),
        }


@dataclass(frozen=True, slots=True)
class BrowserBundle:
    format: Literal["iife"]
    javascript: str
    css: str
    sha256: str
    sdk_version: str
    static_html: str = ""
    compiler: Mapping[str, str] = field(default_factory=lambda: MappingProxyType({}))

    def as_dict(self) -> dict[str, Any]:
        return {
            "format": self.format,
            "javascript": self.javascript,
            "css": self.css,
            "sha256": self.sha256,
            "sdk_version": self.sdk_version,
            "static_html": self.static_html,
            "compiler": dict(self.compiler),
        }


@dataclass(frozen=True, slots=True)
class BuildArtifact:
    format_version: int
    provider: str
    provider_version: str
    sdk_version: str
    entrypoint: str
    files: tuple[SourceFile, ...]
    manifest: Mapping[str, Any]
    render_document: Mapping[str, Any]
    dependency_lock: Mapping[str, str]
    test_results: Mapping[str, Any]
    source_hash: str
    content_hash: str
    provider_metadata: Mapping[str, Any]
    browser_bundle: BrowserBundle

    def as_dict(self) -> dict[str, Any]:
        return {
            "format_version": self.format_version,
            "provider": self.provider,
            "provider_version": self.provider_version,
            "sdk_version": self.sdk_version,
            "entrypoint": self.entrypoint,
            "files": [source_file.as_dict() for source_file in self.files],
            "manifest": dict(self.manifest),
            "render_document": dict(self.render_document),
            "dependency_lock": dict(self.dependency_lock),
            "test_results": dict(self.test_results),
            "source_hash": self.source_hash,
            "content_hash": self.content_hash,
            "provider_metadata": dict(self.provider_metadata),
            "browser_bundle": self.browser_bundle.as_dict(),
        }


@dataclass(frozen=True, slots=True)
class BuildCompletion:
    job_id: str
    app_id: str
    revision_id: str | None
    artifact: BuildArtifact
    metrics: BuildMetrics


@dataclass(frozen=True, slots=True)
class BuildFailure:
    job_id: str
    app_id: str
    revision_id: str | None
    code: str
    message: str
    retryable: bool
    issues: tuple[ValidationIssue, ...] = ()
    duration_ms: int = 0


class AppSourceProvider(Protocol):
    @property
    def name(self) -> str: ...

    @property
    def version(self) -> str: ...

    async def generate(self, blueprint: AppBlueprint) -> GeneratedSource: ...

    async def repair(
        self,
        blueprint: AppBlueprint,
        previous: GeneratedSource,
        issues: tuple[ValidationIssue, ...],
        *,
        attempt: int,
    ) -> GeneratedSource: ...


class AppCompiler(Protocol):
    @property
    def name(self) -> str: ...

    @property
    def version(self) -> str: ...

    async def compile(self, source: GeneratedSource) -> BrowserBundle: ...


class AppBrowserSmokeRunner(Protocol):
    @property
    def name(self) -> str: ...

    @property
    def version(self) -> str: ...

    async def smoke(
        self,
        bundle: BrowserBundle,
        *,
        acceptance_plan: tuple[Mapping[str, Any], ...] = (),
    ) -> Mapping[str, Any]: ...


class BuildJobHooks(Protocol):
    async def claim_next_build(self) -> BuildClaim | None: ...

    async def complete_build(self, claim: BuildClaim, completion: BuildCompletion) -> None: ...

    async def fail_build(self, claim: BuildClaim, failure: BuildFailure) -> None: ...
