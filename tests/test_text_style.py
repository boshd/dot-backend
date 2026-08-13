from benji_api.agents.text_style import (
    DELIVERY_BUBBLE_SAFETY_LIMIT,
    plain_text_bubble,
    prepare_app_completion_bubbles,
    prepare_text_bubbles,
    prepare_trusted_link_bubbles,
    trusted_urls_from_tool_outputs,
)


def test_plain_text_bubble_removes_messaging_markdown() -> None:
    assert plain_text_bubble(
        "for a group, i'd go **time out market montréal** — everyone wins. "
        "see [the list](https://example.com/list) or `ask me`"
    ) == (
        "for a group, i'd go time out market montréal, everyone wins. "
        "see the list: https://example.com/list or ask me"
    )


def test_prepare_text_bubbles_has_only_a_backend_delivery_safety_ceiling() -> None:
    messages = [f"message {index}" for index in range(DELIVERY_BUBBLE_SAFETY_LIMIT + 3)]

    bubbles = prepare_text_bubbles(messages)

    assert len(bubbles) == DELIVERY_BUBBLE_SAFETY_LIMIT
    assert bubbles[-1] == f"message {DELIVERY_BUBBLE_SAFETY_LIMIT - 1}"


def test_prepare_text_bubbles_turns_accidental_paragraphs_into_real_bubbles() -> None:
    bubbles = prepare_text_bubbles(
        ["i'm an ai you text.\n\ni can also build little apps.", "one more beat"]
    )

    assert bubbles == (
        "i'm an ai you text.",
        "i can also build little apps.",
        "one more beat",
    )


def test_app_completion_bubbles_always_include_only_the_exact_trusted_url() -> None:
    trusted_url = "https://app.example/a/demo#handoff=trusted"

    assert prepare_app_completion_bubbles(
        ["it's ready"], app_url=trusted_url
    ) == ("it's ready", trusted_url)
    assert prepare_app_completion_bubbles(
        ["it's ready: https://wrong.example/a/nope"], app_url=trusted_url
    ) == (f"it's ready: {trusted_url}",)


def test_trusted_link_bubbles_append_a_missing_connect_or_app_url() -> None:
    connect_url = "https://api.example/api/v1/integrations/connect/token"
    app_url = "https://app.example/a/demo#handoff=ticket"

    assert prepare_trusted_link_bubbles(
        ["use this one:"],
        urls=(connect_url,),
    ) == ("use this one:", connect_url)
    assert prepare_trusted_link_bubbles(
        ["this one, sorry lol"],
        urls=(app_url,),
    ) == ("this one, sorry lol", app_url)
    assert prepare_trusted_link_bubbles(
        ["open this", app_url],
        urls=(app_url,),
    ) == ("open this", app_url)


def test_trusted_urls_come_from_connect_and_fresh_app_link_tools() -> None:
    connect_url = "https://api.example/api/v1/integrations/connect/token"
    app_url = "https://app.example/a/demo#handoff=ticket"

    assert trusted_urls_from_tool_outputs(
        (
            {
                "name": "create_integration_connect_link",
                "succeeded": True,
                "output": {"ok": True, "result": {"connect_url": connect_url}},
            },
            {
                "name": "create_custom_app_link",
                "succeeded": True,
                "output": {"ok": True, "result": {"app_url": app_url}},
            },
            {
                "name": "list_personal_apps",
                "succeeded": True,
                "output": {"ok": True, "result": {"apps": [{"app_url": "https://app.example/a/nope"}]}},
            },
            {
                "name": "create_personal_app",
                "succeeded": True,
                "output": {"ok": True, "result": {"status": "queued"}},
            },
        )
    ) == (connect_url, app_url)
