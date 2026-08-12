from benji_api.agents.providers.openai import _openai_message_input, _openai_messages_input
from benji_api.agents.types import AgentAttachment, AgentMessage


def test_openai_input_uses_low_detail_images_and_hides_unusable_urls() -> None:
    safe_url = "https://cdn.linqapp.com/attachments/partners/p/a/photo.jpg"
    unsafe_url = "https://example.com/private.mov?token=secret"
    result = _openai_message_input(
        AgentMessage(
            role="user",
            content="what is this?",
            attachments=(
                AgentAttachment(
                    kind="image",
                    mime_type="image/jpeg",
                    filename="photo.jpg",
                    url=safe_url,
                    provider="linq",
                ),
                AgentAttachment(
                    kind="media",
                    mime_type="video/quicktime",
                    filename="clip.mov",
                    url=unsafe_url,
                    provider="linq",
                ),
            ),
        )
    )

    assert result["role"] == "user"
    assert {"type": "input_image", "image_url": safe_url, "detail": "low"} in result["content"]
    serialized = str(result)
    assert unsafe_url not in serialized
    assert "untrusted user-provided content" in serialized


def test_openai_text_only_input_shape_is_unchanged() -> None:
    assert _openai_message_input(AgentMessage(role="assistant", content="hey")) == {
        "role": "assistant",
        "content": "hey",
    }


def test_openai_remote_media_is_bounded_per_request() -> None:
    attachments = tuple(
        AgentAttachment(
            kind="image",
            mime_type="image/jpeg",
            filename=None,
            url=f"https://cdn.linqapp.com/attachments/partners/p/{index}/photo.jpg",
            provider="linq",
        )
        for index in range(3)
    )

    result = _openai_messages_input(
        [AgentMessage(role="user", content="three photos", attachments=attachments)]
    )[0]

    assert sum(part["type"] == "input_image" for part in result["content"]) == 2
    assert sum("unavailable" in part.get("text", "") for part in result["content"]) == 1
