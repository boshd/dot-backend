from benji_api.agents.text_style import (
    DELIVERY_BUBBLE_SAFETY_LIMIT,
    plain_text_bubble,
    prepare_text_bubbles,
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
