from benji_api.models.channel import Conversation, ConversationChannel, ConversationKind, Message


def direct_imessage_reaction_target(
    *,
    conversation: Conversation,
    channel: ConversationChannel | None,
    trigger: Message | None,
) -> str | None:
    """Return the exact provider message ID only for direct Linq iMessage turns."""
    if (
        conversation.kind != ConversationKind.DIRECT.value
        or channel is None
        or channel.provider != "linq"
        or (channel.service or "").casefold() != "imessage"
        or trigger is None
        or trigger.source_channel != "linq"
        or trigger.source_binding_id != channel.id
        or not trigger.source_external_id
    ):
        return None
    return trigger.source_external_id
