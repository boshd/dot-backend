from __future__ import annotations

import asyncio
import json
import os
from collections.abc import Mapping
from pathlib import Path
from types import MappingProxyType
from typing import Any

from benji_api.app_builder.types import BrowserBundle, GeneratedSource, ValidationIssue

_DEFAULT_TIMEOUT_SECONDS = 20.0
_MAX_COMPILER_RESPONSE_BYTES = 3_500_000


class AppCompilationError(RuntimeError):
    """A generated source tree failed the controlled compiler gate."""

    def __init__(self, issues: tuple[ValidationIssue, ...]) -> None:
        self.issues = issues
        super().__init__("generated source did not compile")


class EsbuildAppCompiler:
    """Compile source through the fixed Dot SDK without executing generated code."""

    name = "esbuild"
    version = "1"

    def __init__(
        self,
        *,
        node_binary: str | None = None,
        harness_path: str | Path | None = None,
        timeout_seconds: float = _DEFAULT_TIMEOUT_SECONDS,
    ) -> None:
        if timeout_seconds <= 0:
            raise ValueError("compiler timeout_seconds must be positive")
        self.node_binary = node_binary or os.getenv("DOT_APP_COMPILER_NODE", "node")
        configured_harness = harness_path or os.getenv("DOT_APP_COMPILER_HARNESS")
        self.harness_path = (
            Path(configured_harness)
            if configured_harness
            else Path(__file__).with_name("harness.mjs")
        )
        self.timeout_seconds = timeout_seconds

    async def compile(self, source: GeneratedSource) -> BrowserBundle:
        request = {
            "protocol_version": 1,
            "entrypoint": source.entrypoint,
            "files": [item.as_dict() for item in source.files],
        }
        try:
            process = await asyncio.create_subprocess_exec(
                self.node_binary,
                str(self.harness_path),
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except (FileNotFoundError, OSError) as exc:
            raise RuntimeError("Dot app compiler runtime is unavailable") from exc
        try:
            async with asyncio.timeout(self.timeout_seconds):
                stdout, stderr = await process.communicate(
                    json.dumps(request, ensure_ascii=False, separators=(",", ":")).encode()
                )
        except TimeoutError:
            process.kill()
            await process.wait()
            raise AppCompilationError(
                (
                    ValidationIssue(
                        "compiler_timeout",
                        f"browser bundle compilation exceeded {self.timeout_seconds:g} seconds",
                    ),
                )
            ) from None

        if len(stdout) > _MAX_COMPILER_RESPONSE_BYTES:
            raise AppCompilationError(
                (
                    ValidationIssue(
                        "browser_bundle_too_large",
                        "compiled browser bundle exceeds the 3.5 MB compiler envelope",
                    ),
                )
            )
        result = _decode_response(stdout)
        if process.returncode != 0 or not result.get("ok"):
            issues = _response_issues(result)
            if not issues:
                detail = stderr.decode(errors="replace").strip()[:600]
                issues = (
                    ValidationIssue(
                        "compiler_failed",
                        detail or "controlled browser compilation failed",
                    ),
                )
            raise AppCompilationError(issues)

        bundle = result.get("bundle")
        if not isinstance(bundle, Mapping):
            raise RuntimeError("Dot app compiler returned no browser bundle")
        javascript = bundle.get("javascript")
        css = bundle.get("css")
        digest = bundle.get("sha256")
        static_html = bundle.get("static_html", "")
        if (
            bundle.get("format") != "iife"
            or not isinstance(javascript, str)
            or not isinstance(css, str)
            or not isinstance(digest, str)
            or not isinstance(static_html, str)
            or len(digest) != 64
        ):
            raise RuntimeError("Dot app compiler returned an invalid browser bundle")
        return BrowserBundle(
            format="iife",
            javascript=javascript,
            css=css,
            sha256=digest,
            sdk_version=str(bundle.get("sdk_version", "2")),
            static_html=static_html,
            compiler=MappingProxyType(
                {
                    "name": str(bundle.get("compiler", "esbuild")),
                    "version": str(bundle.get("compiler_version", "unknown")),
                }
            ),
        )


def _decode_response(stdout: bytes) -> dict[str, Any]:
    if not stdout:
        return {}
    try:
        value = json.loads(stdout)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RuntimeError("Dot app compiler returned malformed output") from exc
    if not isinstance(value, dict):
        raise RuntimeError("Dot app compiler response must be an object")
    return value


def _response_issues(result: Mapping[str, Any]) -> tuple[ValidationIssue, ...]:
    raw_issues = result.get("issues")
    if not isinstance(raw_issues, list):
        return ()
    issues: list[ValidationIssue] = []
    for item in raw_issues[:25]:
        if not isinstance(item, Mapping):
            continue
        code = item.get("code")
        message = item.get("message")
        path = item.get("path")
        if not isinstance(code, str) or not isinstance(message, str):
            continue
        issues.append(
            ValidationIssue(
                code=code[:80],
                message=message[:800],
                path=path[:240] if isinstance(path, str) else None,
            )
        )
    return tuple(issues)
