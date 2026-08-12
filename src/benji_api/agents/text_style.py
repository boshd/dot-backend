import re

MARKDOWN_LINK = re.compile(r"\[([^\]\n]+)\]\((https?://[^)\s]+)\)")
STRONG_ASTERISK = re.compile(r"\*\*([^*\n]+)\*\*")
STRONG_UNDERSCORE = re.compile(r"__([^_\n]+)__")
STRIKETHROUGH = re.compile(r"~~([^~\n]+)~~")
INLINE_CODE = re.compile(r"`([^`\n]+)`")
EMPHASIS_ASTERISK = re.compile(r"(?<!\*)\*([^*\n]+)\*(?!\*)")
HEADING = re.compile(r"(?m)^#{1,6}\s+")
BLOCKQUOTE = re.compile(r"(?m)^>\s?")
PARAGRAPH_BREAK = re.compile(r"\n\s*\n+")
HTTP_URL = re.compile(r"https?://\S+", re.IGNORECASE)

# This is deliberately not part of the model contract or prompt. It only prevents a malformed
# provider response from turning into an unbounded sequence of paid outbound messages.
DELIVERY_BUBBLE_SAFETY_LIMIT = 12


def plain_text_bubble(text: str) -> str:
    """Remove common model-authored Markdown that messaging clients display literally."""
    clean = MARKDOWN_LINK.sub(r"\1: \2", text.strip())
    for pattern in (
        STRONG_ASTERISK,
        STRONG_UNDERSCORE,
        STRIKETHROUGH,
        INLINE_CODE,
        EMPHASIS_ASTERISK,
    ):
        clean = pattern.sub(r"\1", clean)
    clean = HEADING.sub("", clean)
    clean = re.sub(r"\s+—\s+", ", ", clean)
    return BLOCKQUOTE.sub("", clean).strip()


def prepare_text_bubbles(messages: tuple[str, ...] | list[str]) -> tuple[str, ...]:
    """Clean a natural model-authored turn and apply only the delivery safety ceiling."""
    bubbles = tuple(
        clean
        for message in messages
        for paragraph in PARAGRAPH_BREAK.split(message)
        if (clean := plain_text_bubble(paragraph))
    )
    return bubbles[:DELIVERY_BUBBLE_SAFETY_LIMIT]


def prepare_app_completion_bubbles(
    messages: tuple[str, ...] | list[str],
    *,
    app_url: str,
) -> tuple[str, ...]:
    """Keep model-authored prose while making every authored URL the trusted app URL."""
    bubbles = tuple(HTTP_URL.sub(app_url, bubble) for bubble in prepare_text_bubbles(messages))
    if any(app_url in bubble for bubble in bubbles):
        return bubbles
    return (*bubbles[: DELIVERY_BUBBLE_SAFETY_LIMIT - 1], app_url)
