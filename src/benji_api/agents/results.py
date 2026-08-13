from dataclasses import dataclass
from uuid import UUID


@dataclass(frozen=True, slots=True)
class PersistedReply:
    message_id: UUID
    text: str


@dataclass(frozen=True, slots=True)
class PersistedReaction:
    message_id: UUID
    target_external_id: str
    reaction_type: str


@dataclass(frozen=True, slots=True)
class PersistedTurn:
    replies: tuple[PersistedReply, ...]
    onboarding_completed: bool = False
    reaction: PersistedReaction | None = None

    @property
    def message_id(self) -> UUID:
        if self.replies:
            return self.replies[0].message_id
        if self.reaction is not None:
            return self.reaction.message_id
        raise IndexError("Persisted turn has no reply or reaction")

    @property
    def text(self) -> str:
        return self.replies[0].text if self.replies else ""
