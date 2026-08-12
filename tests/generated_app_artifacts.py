from __future__ import annotations

import hashlib


def compiled_artifact(*, sdk_version: str = "1.0.0") -> dict[str, object]:
    """Small valid compiled artifact for service-layer promotion tests."""

    javascript = "globalThis.__dotGeneratedApp = true;"
    css = ""
    digest = hashlib.sha256(javascript.encode() + b"\0" + css.encode()).hexdigest()
    return {
        "format_version": 1,
        "sdk_version": sdk_version,
        "browser_bundle": {
            "format": "iife",
            "javascript": javascript,
            "css": css,
            "sha256": digest,
            "sdk_version": sdk_version,
            "static_html": "<main>ready</main>",
            "compiler": {"name": "test", "version": "1"},
        },
        "test_results": {
            "policy": {"status": "passed"},
            "typescript_compile": {"status": "passed"},
            "browser_smoke": {
                "status": "passed",
                "real_browser": {"ready": True, "runtime": "chromium"},
            },
        },
    }
