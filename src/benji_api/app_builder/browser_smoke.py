from __future__ import annotations

import asyncio
import json
import os
import shutil
import signal
import tempfile
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from benji_api.app_builder.types import (
    MAX_STATIC_HTML_BYTES,
    BrowserBundle,
    ValidationIssue,
)

_DEFAULT_TIMEOUT_SECONDS = 10.0
# JSON escaping can expand the otherwise bounded static HTML. The guest rejects a
# render above MAX_STATIC_HTML_BYTES before serializing it; this is a second envelope
# around a malformed or substituted harness.
_MAX_RESPONSE_BYTES = 1_600_000
_MAX_REAL_BROWSER_RESPONSE_BYTES = 512_000


class AppBrowserSmokeError(RuntimeError):
    """The compiled app failed to become ready in the isolated smoke guest."""

    def __init__(self, issues: tuple[ValidationIssue, ...]) -> None:
        self.issues = issues
        detail = "; ".join(f"{item.code}: {item.message}" for item in issues[:3])
        super().__init__(detail or "generated app failed its browser smoke test")


class QuickJSAppSmokeRunner:
    """Render generated browser code inside a bounded QuickJS-WebAssembly guest.

    This gate deliberately does not emulate product behavior. It proves that startup renders,
    remains inside resource limits, and cannot use host/network primitives or mutate without a
    gesture. Real Chromium is the sole authority for keyboard, form, and persistence behavior.
    The outer subprocess deadline remains a second hard stop around QuickJS's interrupt, memory,
    and stack bounds.
    """

    name = "quickjs-wasm"
    version = "1"

    def __init__(
        self,
        *,
        harness_path: str | Path | None = None,
        node_binary: str | None = None,
        timeout_seconds: float = _DEFAULT_TIMEOUT_SECONDS,
        simulate_first_click: bool = False,
    ) -> None:
        if timeout_seconds <= 0:
            raise ValueError("app smoke timeout_seconds must be positive")
        self.node_binary = node_binary or os.getenv("DOT_APP_COMPILER_NODE", "node")
        configured_harness = harness_path or os.getenv("DOT_APP_BROWSER_HARNESS")
        self.harness_path = (
            Path(configured_harness)
            if configured_harness
            else Path(__file__).with_name("compiler") / "browser_smoke.mjs"
        )
        self.timeout_seconds = timeout_seconds
        self.simulate_first_click = simulate_first_click

    @property
    def available(self) -> bool:
        return bool(shutil.which(self.node_binary) and self.harness_path.is_file())

    async def smoke(
        self,
        bundle: BrowserBundle,
        *,
        acceptance_plan: tuple[Mapping[str, Any], ...] = (),
    ) -> Mapping[str, Any]:
        if not self.available:
            raise RuntimeError("Dot app QuickJS smoke runtime is unavailable")
        request = {
            "protocol_version": 1,
            "timeout_ms": max(1_000, round(self.timeout_seconds * 1_000)),
            "bundle": bundle.as_dict(),
        }
        # Keep the protocol-compatible parameter while callers migrate, but never send an
        # interaction plan into the synthetic DOM. Synthetic interaction was rejecting valid
        # browser apps because it cannot faithfully model native controls or React focus.
        del acceptance_plan
        encoded_request = json.dumps(
            request,
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode()
        if len(encoded_request) > 3_600_000:
            raise AppBrowserSmokeError(
                (
                    ValidationIssue(
                        "browser_smoke_input_too_large",
                        "browser smoke input exceeds 3.6 MB",
                    ),
                )
            )
        scrubbed_env = {
            "HOME": tempfile.gettempdir(),
            "LANG": "C.UTF-8",
            "PATH": "/usr/local/bin:/usr/bin:/bin",
            "TMPDIR": tempfile.gettempdir(),
        }
        try:
            process = await asyncio.create_subprocess_exec(
                self.node_binary,
                str(self.harness_path),
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=scrubbed_env,
                start_new_session=True,
            )
        except (FileNotFoundError, OSError) as exc:
            raise RuntimeError("Dot app QuickJS smoke harness is unavailable") from exc
        try:
            async with asyncio.timeout(self.timeout_seconds + 3):
                stdout, stderr = await process.communicate(
                    encoded_request
                )
        except TimeoutError:
            _terminate_process_group(process)
            await process.wait()
            raise AppBrowserSmokeError(
                (
                    ValidationIssue(
                        "browser_smoke_timeout",
                        f"app did not become ready within {self.timeout_seconds:g} seconds",
                    ),
                )
            ) from None
        if len(stdout) > _MAX_RESPONSE_BYTES:
            raise AppBrowserSmokeError(
                (ValidationIssue("browser_smoke_output_too_large", "browser output was too large"),)
            )
        if not stdout:
            detail = stderr.decode(errors="replace").strip()[:600]
            raise RuntimeError(detail or "Dot app QuickJS harness returned no output")
        response = _decode(stdout)
        if process.returncode != 0 or not response.get("ok"):
            issues = _issues(response)
            if not issues:
                detail = stderr.decode(errors="replace").strip()[:600]
                issues = (
                    ValidationIssue(
                        "browser_smoke_failed",
                        detail or "app did not complete its browser smoke test",
                    ),
                )
            raise AppBrowserSmokeError(issues)
        result = response.get("result")
        if not isinstance(result, dict) or result.get("ready") is not True:
            raise RuntimeError("Dot app QuickJS smoke harness returned an invalid result")
        static_html = result.get("static_html")
        if not isinstance(static_html, str) or not static_html.strip():
            raise AppBrowserSmokeError(
                (
                    ValidationIssue(
                        "missing_static_render",
                        "app did not produce usable isolated HTML",
                    ),
                )
            )
        if len(static_html.encode()) > MAX_STATIC_HTML_BYTES:
            raise AppBrowserSmokeError(
                (
                    ValidationIssue(
                        "static_render_too_large",
                        "isolated app HTML exceeds 256 KB",
                    ),
                )
            )
        return result


class ChromiumAppAcceptanceRunner:
    """Exercise a generated SDK v2 bundle through real trusted browser interactions.

    Chromium runs only after the bounded QuickJS guest has accepted the bundle. This is a
    behavioral gate, not the primary build-time security boundary. Chromium's native process
    sandbox must remain enabled, and deployments must additionally isolate this short-lived
    acceptance process from credentials and the network. Page-level request interception catches
    attempted network use; it is not a kernel network boundary. The subprocess deadline is the
    hard stop around Chromium and all of its children.
    """

    name = "chromium"
    version = "1"

    def __init__(
        self,
        *,
        harness_path: str | Path | None = None,
        node_binary: str | None = None,
        chromium_binary: str | None = None,
        timeout_seconds: float = 12.0,
        sandbox_required: bool = True,
    ) -> None:
        if timeout_seconds <= 0:
            raise ValueError("app acceptance timeout_seconds must be positive")
        self.node_binary = node_binary or os.getenv("DOT_APP_COMPILER_NODE", "node")
        configured_harness = harness_path or os.getenv("DOT_APP_CHROMIUM_HARNESS")
        self.harness_path = (
            Path(configured_harness)
            if configured_harness
            else Path(__file__).with_name("compiler") / "chromium_acceptance.mjs"
        )
        self.chromium_binary = chromium_binary or _chromium_binary()
        self.timeout_seconds = timeout_seconds
        self.sandbox_required = sandbox_required

    @property
    def available(self) -> bool:
        return bool(
            shutil.which(self.node_binary)
            and self.harness_path.is_file()
            and self.chromium_binary
            and Path(self.chromium_binary).is_file()
        )

    async def smoke(
        self,
        bundle: BrowserBundle,
        *,
        acceptance_plan: tuple[Mapping[str, Any], ...] = (),
        records: Mapping[str, Any] | None = None,
    ) -> Mapping[str, Any]:
        if not self.available:
            raise RuntimeError("Dot app real Chromium acceptance runtime is unavailable")
        request = {
            "protocol_version": 1,
            "timeout_ms": max(2_000, round(self.timeout_seconds * 1_000)),
            "acceptance_plan": [dict(step) for step in acceptance_plan],
            "context": {},
            "bundle": bundle.as_dict(),
        }
        if records is not None:
            request["records"] = dict(records)
        encoded_request = json.dumps(
            request,
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode()
        if len(encoded_request) > 3_600_000:
            raise AppBrowserSmokeError(
                (
                    ValidationIssue(
                        "real_browser_input_too_large",
                        "real browser acceptance input exceeds 3.6 MB",
                    ),
                )
            )
        scrubbed_env = {
            "HOME": tempfile.gettempdir(),
            "LANG": "C.UTF-8",
            "PATH": "/usr/local/bin:/usr/bin:/bin",
            "TMPDIR": tempfile.gettempdir(),
            "DOT_APP_CHROMIUM_PATH": str(self.chromium_binary),
            "DOT_APP_CHROMIUM_REQUIRE_SANDBOX": "true" if self.sandbox_required else "false",
        }
        try:
            process = await asyncio.create_subprocess_exec(
                self.node_binary,
                str(self.harness_path),
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=scrubbed_env,
                start_new_session=True,
            )
        except (FileNotFoundError, OSError) as exc:
            raise RuntimeError("Dot app real Chromium harness is unavailable") from exc
        try:
            async with asyncio.timeout(self.timeout_seconds + 5):
                stdout, stderr = await process.communicate(encoded_request)
        except TimeoutError:
            _terminate_process_group(process)
            await process.wait()
            raise AppBrowserSmokeError(
                (
                    ValidationIssue(
                        "real_browser_timeout",
                        f"real browser acceptance exceeded {self.timeout_seconds:g} seconds",
                    ),
                )
            ) from None
        if len(stdout) > _MAX_REAL_BROWSER_RESPONSE_BYTES:
            raise AppBrowserSmokeError(
                (
                    ValidationIssue(
                        "real_browser_output_too_large",
                        "real browser acceptance output exceeded 512 KB",
                    ),
                )
            )
        if not stdout:
            detail = stderr.decode(errors="replace").strip()[:600]
            raise RuntimeError(detail or "Dot app Chromium harness returned no output")
        response = _decode(stdout)
        if process.returncode != 0 or not response.get("ok"):
            issues = _issues(response)
            infrastructure_codes = {"real_browser_unavailable", "real_browser_invalid_input"}
            if any(item.code in infrastructure_codes for item in issues):
                detail = issues[0].message if issues else "Chromium acceptance runtime unavailable"
                raise RuntimeError(detail)
            if not issues:
                detail = stderr.decode(errors="replace").strip()[:600]
                issues = (
                    ValidationIssue(
                        "real_browser_runtime_error",
                        detail or "app did not complete real browser acceptance",
                    ),
                )
            raise AppBrowserSmokeError(issues)
        result = response.get("result")
        if not isinstance(result, dict) or result.get("ready") is not True:
            raise RuntimeError("Dot app Chromium harness returned an invalid result")
        return result


class VerifiedAppSmokeRunner:
    """Require both bounded guest safety checks and real-browser product behavior."""

    name = "quickjs-wasm+chromium"
    version = "1"

    def __init__(
        self,
        quickjs: QuickJSAppSmokeRunner,
        chromium: ChromiumAppAcceptanceRunner,
    ) -> None:
        self.quickjs = quickjs
        self.chromium = chromium

    @property
    def available(self) -> bool:
        return self.quickjs.available and self.chromium.available

    async def smoke(
        self,
        bundle: BrowserBundle,
        *,
        acceptance_plan: tuple[Mapping[str, Any], ...] = (),
    ) -> Mapping[str, Any]:
        quickjs_result = dict(await self.quickjs.smoke(bundle))
        chromium_result = dict(
            await self.chromium.smoke(bundle, acceptance_plan=acceptance_plan)
        )
        return {
            **quickjs_result,
            "runtime": self.name,
            "real_browser": chromium_result,
        }


def _terminate_process_group(process: asyncio.subprocess.Process) -> None:
    if process.returncode is not None:
        return
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        process.kill()


def _chromium_binary() -> str | None:
    configured = os.getenv("DOT_APP_CHROMIUM_PATH")
    if configured:
        return configured
    for executable in ("chromium", "chromium-browser", "google-chrome"):
        resolved = shutil.which(executable)
        if resolved:
            return resolved
    for candidate in (
        "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
        "/Applications/Chromium.app/Contents/MacOS/Chromium",
    ):
        if Path(candidate).is_file():
            return candidate
    return None


def _decode(stdout: bytes) -> dict[str, Any]:
    try:
        value = json.loads(stdout)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RuntimeError("Dot app browser harness returned malformed output") from exc
    if not isinstance(value, dict):
        raise RuntimeError("Dot app browser response must be an object")
    return value


def _issues(response: Mapping[str, Any]) -> tuple[ValidationIssue, ...]:
    raw = response.get("issues")
    if not isinstance(raw, list):
        return ()
    issues: list[ValidationIssue] = []
    for item in raw[:20]:
        if not isinstance(item, Mapping):
            continue
        code = item.get("code")
        message = item.get("message")
        if isinstance(code, str) and isinstance(message, str):
            issues.append(ValidationIssue(code[:80], message[:800]))
    return tuple(issues)
