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


class LinqInboundMessage(BaseModel):
    external_message_id: str
    external_chat_id: str
    sender_handle: str
    service: str | None = None
    text: str = ""
    is_group: bool = False
    reply_to_message_id: str | None = None

    @classmethod
    def from_envelope(cls, envelope: LinqWebhookEnvelope) -> "LinqInboundMessage":
        data = envelope.data
        chat = data.get("chat")
        sender = data.get("sender_handle")
        parts = data.get("parts", [])

        if not isinstance(chat, dict) or not isinstance(sender, dict):
            raise ValueError("message.received payload is missing chat or sender_handle")

        text_parts = [
            part.get("value", "")
            for part in parts
            if isinstance(part, dict) and part.get("type") == "text"
        ]
        return cls(
            external_message_id=str(data["id"]),
            external_chat_id=str(chat["id"]),
            sender_handle=str(sender["handle"]),
            service=data.get("service") or sender.get("service"),
            text="\n".join(value for value in text_parts if value),
            is_group=bool(chat.get("is_group", False)),
            reply_to_message_id=(
                str(data["reply_to"]["message_id"])
                if isinstance(data.get("reply_to"), dict) and data["reply_to"].get("message_id")
                else None
            ),
        )
