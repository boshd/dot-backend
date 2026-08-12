from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class LinqWebhookEnvelope(BaseModel):
    model_config = ConfigDict(extra="allow")

    api_version: str
    webhook_version: str
    event_type: str = Field(min_length=1, max_length=128)
    event_id: str = Field(min_length=1, max_length=255)
    created_at: datetime
    trace_id: str | None = None
    partner_id: str | None = None
    data: dict[str, Any]


class LinqInboundAttachment(BaseModel):
    part_index: int = Field(ge=0)
    provider_attachment_id: str | None = Field(default=None, max_length=255)
    filename: str | None = Field(default=None, max_length=512)
    mime_type: str | None = Field(default=None, max_length=255)
    size_bytes: int | None = Field(default=None, ge=0)
    url: str | None = Field(default=None, max_length=8_192)
    raw_payload: dict[str, Any]


class LinqInboundMessage(BaseModel):
    external_message_id: str
    external_chat_id: str
    sender_handle: str
    service: str | None = None
    text: str = ""
    is_group: bool = False
    reply_to_message_id: str | None = None
    attachments: tuple[LinqInboundAttachment, ...] = ()

    @classmethod
    def from_envelope(cls, envelope: LinqWebhookEnvelope) -> "LinqInboundMessage":
        data = envelope.data
        chat = data.get("chat")
        message = data.get("message")
        message_data = message if isinstance(message, dict) else data
        sender = data.get("sender_handle") or data.get("from_handle")
        parts = message_data.get("parts", [])

        if not isinstance(sender, dict):
            raise ValueError("message.received payload is missing sender_handle")
        if isinstance(chat, dict):
            external_chat_id = chat.get("id")
            is_group = chat.get("is_group", data.get("is_group", False))
        else:
            external_chat_id = data.get("chat_id")
            is_group = data.get("is_group", False)
        if not external_chat_id:
            raise ValueError("message.received payload is missing chat id")
        if not isinstance(parts, list):
            raise ValueError("message.received payload has invalid parts")

        text_parts = [
            part.get("value", "")
            for part in parts
            if (
                isinstance(part, dict)
                and part.get("type") == "text"
                and isinstance(part.get("value"), str)
            )
        ]
        attachments = tuple(
            LinqInboundAttachment(
                part_index=index,
                provider_attachment_id=(str(part["id"]) if part.get("id") is not None else None),
                filename=(part.get("filename") if isinstance(part.get("filename"), str) else None),
                mime_type=(
                    part.get("mime_type")
                    if isinstance(part.get("mime_type"), str)
                    else (
                        part.get("content_type")
                        if isinstance(part.get("content_type"), str)
                        else None
                    )
                ),
                size_bytes=(
                    part.get("size_bytes")
                    if isinstance(part.get("size_bytes"), int)
                    and not isinstance(part.get("size_bytes"), bool)
                    else None
                ),
                url=part.get("url") if isinstance(part.get("url"), str) else None,
                raw_payload=part,
            )
            for index, part in enumerate(parts)
            if isinstance(part, dict) and part.get("type") == "media"
        )
        text = "\n".join(value for value in text_parts if value)
        if not text and attachments:
            image_count = sum(
                attachment.mime_type is not None
                and attachment.mime_type.casefold().startswith("image/")
                for attachment in attachments
            )
            if image_count == len(attachments):
                text = "[sent an image]" if image_count == 1 else "[sent images]"
            else:
                text = "[sent an attachment]" if len(attachments) == 1 else "[sent attachments]"

        external_message_id = message_data.get("id") or data.get("message_id")
        if not external_message_id:
            raise ValueError("message.received payload is missing message id")
        reply_to = message_data.get("reply_to") or data.get("reply_to")
        return cls(
            external_message_id=str(external_message_id),
            external_chat_id=str(external_chat_id),
            sender_handle=str(sender["handle"]),
            service=data.get("service") or message_data.get("service") or sender.get("service"),
            text=text,
            is_group=bool(is_group),
            reply_to_message_id=(
                str(reply_to["message_id"])
                if isinstance(reply_to, dict) and reply_to.get("message_id")
                else None
            ),
            attachments=attachments,
        )
