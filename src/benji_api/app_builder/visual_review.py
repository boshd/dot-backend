from __future__ import annotations

import base64
import json
import logging
from time import monotonic
from typing import Any, Protocol

from openai import AsyncOpenAI

from benji_api.app_builder.types import AppBlueprint, ValidationIssue

logger = logging.getLogger(__name__)

VISUAL_QUALITY_ISSUE_CODE = "visual_quality"
_REVIEW_AXES = ("hierarchy", "density", "alignment", "redundancy", "copy")

_REVIEW_INSTRUCTIONS = """You are the design review gate for Dot's generated mobile apps.
You receive the at-rest first screen of a generated app rendered in a 390x844 mobile viewport,
plus the app's purpose. Dot apps should feel like products from Cursor, OpenAI, or natural.com:
one typeface, one restrained accent, generous whitespace, one obvious job.

Complexity must match the purpose, so judge against what was asked for. A rich multi-area app
(tabs, several interactive sections) is correct when the purpose names those areas; blandness is
not the goal and a purposeful, delightful screen passes. Fail an axis only for a defect its user
would actually notice.

Judge design, not functional coverage: whether required fields, validation, or persistence work
is verified by a separate behavioral gate. Do not fail an axis because a workflow looks
incomplete or a field seems missing.

The screen is composed from Dot's own design system, whose standard patterns are correct by
definition: full-width tab and segmented rows, right-aligned status or meta text in list rows,
quiet borders instead of shadows, and a single accent. Never fail these patterns themselves;
fail only genuine defects. Borderline calls pass — fail an axis only when you are confident its
user would notice the problem.

Rubric, one verdict per axis:
- hierarchy: the first screen opens on the job the purpose names, with one clear primary action;
  it is not an overview, metric grid, or dashboard nobody asked for.
- density: comfortable mobile spacing; no crammed rows, wall-of-controls, or stacked cards of
  competing tones; whitespace does the styling.
- alignment: nothing clipped, overlapping, truncated mid-word, or visibly uneven across repeated
  elements of the same kind.
- redundancy: no section, stat, filter, badge, or decoration that repeats information or exists
  only to fill space.
- copy: quiet sentence case; no exclamation marks; the app never explains itself to its user.

For every failed axis, write one actionable sentence a code generator can apply directly."""

_REVIEW_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["axes"],
    "properties": {
        "axes": {
            "type": "array",
            "minItems": 5,
            "maxItems": 5,
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["axis", "passed", "note"],
                "properties": {
                    "axis": {"type": "string", "enum": list(_REVIEW_AXES)},
                    "passed": {"type": "boolean"},
                    "note": {"type": "string", "maxLength": 300},
                },
            },
        },
    },
}


class AppVisualReviewer(Protocol):
    @property
    def name(self) -> str: ...

    async def review(
        self,
        screenshot_jpeg: bytes,
        *,
        blueprint: AppBlueprint,
    ) -> tuple[ValidationIssue, ...]: ...


class OpenAIVisualReviewer:
    """Vision rubric over the at-rest screenshot; fails an app only on actionable defects.

    This gate is fail-open by design: a reviewer outage, timeout, or malformed response
    accepts the candidate with a logged warning. Behavioral acceptance already ran; the
    review only guards aesthetics and must never silently block shippable apps.
    """

    name = "openai-vision"

    def __init__(
        self,
        *,
        api_key: str | None = None,
        model: str = "gpt-5.6-terra",
        timeout_seconds: float = 25.0,
        client: Any | None = None,
    ) -> None:
        if client is None and not api_key:
            raise ValueError("OpenAI visual review requires an API key")
        self.model = model
        self._client = client or AsyncOpenAI(api_key=api_key, timeout=timeout_seconds)

    async def review(
        self,
        screenshot_jpeg: bytes,
        *,
        blueprint: AppBlueprint,
    ) -> tuple[ValidationIssue, ...]:
        encoded = base64.b64encode(screenshot_jpeg).decode()
        prompt = (
            f"App title: {blueprint.title}\n"
            f"App purpose: {blueprint.purpose}\n"
            "Review the attached at-rest first screen against the rubric."
        )
        started = monotonic()
        try:
            response = await self._client.responses.create(
                model=self.model,
                instructions=_REVIEW_INSTRUCTIONS,
                input=[
                    {
                        "role": "user",
                        "content": [
                            {"type": "input_text", "text": prompt},
                            {
                                "type": "input_image",
                                "image_url": f"data:image/jpeg;base64,{encoded}",
                            },
                        ],
                    }
                ],
                text={
                    "format": {
                        "type": "json_schema",
                        "name": "dot_app_visual_review",
                        "description": "Per-axis design review verdicts",
                        "schema": _REVIEW_SCHEMA,
                        "strict": True,
                    }
                },
                reasoning={"effort": "low"},
                max_output_tokens=1_000,
                store=False,
            )
            payload = json.loads(response.output_text)
            axes = payload["axes"]
        except Exception:
            logger.warning(
                "Visual review is unavailable; accepting the candidate without a score",
                exc_info=True,
            )
            return ()
        latency_ms = max(0, round((monotonic() - started) * 1000))
        issues: list[ValidationIssue] = []
        for verdict in axes:
            if not isinstance(verdict, dict) or verdict.get("passed") is True:
                continue
            axis = verdict.get("axis")
            note = verdict.get("note")
            if axis in _REVIEW_AXES and isinstance(note, str) and note.strip():
                issues.append(
                    ValidationIssue(VISUAL_QUALITY_ISSUE_CODE, f"{axis}: {note.strip()[:300]}")
                )
        usage = getattr(response, "usage", None)
        logger.info(
            "Visual review completed model=%s latency_ms=%s failed_axes=%s tokens=%s",
            getattr(response, "model", self.model),
            latency_ms,
            len(issues),
            getattr(usage, "total_tokens", None),
        )
        return tuple(issues)
