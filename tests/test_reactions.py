from benji_api.agents.reactions import direct_imessage_reaction_target
from benji_api.models.channel import Conversation, ConversationChannel, ConversationKind, Message


def _target(*, service: str, kind: str = "direct", source_channel: str = "linq") -> str | None:
    conversation = Conversation(kind=kind)
    channel = ConversationChannel(
        id=__import__("uuid").uuid4(),
        provider="linq",
        external_id="chat-1",
        service=service,
    )
    trigger = Message(
        source_channel=source_channel,
        source_binding_id=channel.id,
        source_external_id="inbound-1",
    )
    return direct_imessage_reaction_target(
        conversation=conversation,
        channel=channel,
        trigger=trigger,
    )


def test_reactions_are_only_eligible_for_direct_linq_imessage() -> None:
    assert _target(service="iMessage") == "inbound-1"
    assert _target(service="SMS") is None
    assert _target(service="RCS") is None
    assert _target(service="iMessage", kind=ConversationKind.GROUP.value) is None
    assert _target(service="iMessage", source_channel="web") is None
