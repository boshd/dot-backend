from __future__ import annotations

import asyncio
import json
import os
from collections.abc import Mapping, Sequence
from pathlib import Path

from benji_api.app_builder.types import SourceFile

_NORMALIZER_TIMEOUT_SECONDS = 10.0
_MAX_NORMALIZER_RESPONSE_BYTES = 1_500_000


async def normalize_generated_source(
    files: Sequence[SourceFile],
    *,
    node_binary: str | None = None,
    script_path: str | Path | None = None,
) -> tuple[list[SourceFile], dict[str, int]]:
    """Remove visual escape hatches with TypeScript syntax positions, never text regexes."""

    if not any(item.path.endswith((".ts", ".tsx")) for item in files):
        return list(files), {}
    configured_script = script_path or os.getenv("DOT_APP_SOURCE_NORMALIZER")
    normalizer = (
        Path(configured_script)
        if configured_script
        else Path(__file__).with_name("normalize_source.mjs")
    )
    try:
        process = await asyncio.create_subprocess_exec(
            node_binary or os.getenv("DOT_APP_COMPILER_NODE", "node"),
            str(normalizer),
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except (FileNotFoundError, OSError) as exc:
        raise RuntimeError("Dot app source normalizer is unavailable") from exc
    request = {
        "files": [item.as_dict() for item in files],
    }
    try:
        async with asyncio.timeout(_NORMALIZER_TIMEOUT_SECONDS):
            stdout, stderr = await process.communicate(
                json.dumps(request, ensure_ascii=False, separators=(",", ":")).encode()
            )
    except TimeoutError:
        process.kill()
        await process.wait()
        raise RuntimeError("Dot app source normalization timed out") from None
    if len(stdout) > _MAX_NORMALIZER_RESPONSE_BYTES:
        raise RuntimeError("Dot app source normalizer returned too much data")
    try:
        result = json.loads(stdout)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RuntimeError("Dot app source normalizer returned malformed output") from exc
    if process.returncode != 0 or not isinstance(result, Mapping) or result.get("ok") is not True:
        detail = result.get("error") if isinstance(result, Mapping) else None
        if not isinstance(detail, str):
            detail = stderr.decode(errors="replace").strip()
        raise RuntimeError(f"Dot app source normalization failed: {detail[:500]}")

    raw_files = result.get("files")
    if not isinstance(raw_files, list) or len(raw_files) != len(files):
        raise RuntimeError("Dot app source normalizer returned an invalid source tree")
    normalized: list[SourceFile] = []
    for expected, value in zip(files, raw_files, strict=True):
        if (
            not isinstance(value, Mapping)
            or value.get("path") != expected.path
            or not isinstance(value.get("contents"), str)
        ):
            raise RuntimeError("Dot app source normalizer changed the source tree shape")
        normalized.append(SourceFile(path=expected.path, contents=value["contents"]))

    raw_counts = result.get("counts", {})
    if not isinstance(raw_counts, Mapping):
        raw_counts = {}
    counts = {
        str(key): value
        for key, value in raw_counts.items()
        if isinstance(key, str)
        and isinstance(value, int)
        and not isinstance(value, bool)
        and value >= 0
    }
    return normalized, counts
