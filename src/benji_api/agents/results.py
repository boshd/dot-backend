from dataclasses import dataclass
from uuid import UUID


@dataclass(frozen=True, slots=True)
class PersistedReply:
    message_id: UUID
    text: str


@dataclass(frozen=True, slots=True)
class PersistedTurn:
    replies: tuple[PersistedReply, ...]
    onboarding_completed: bool = False

    @property
    def message_id(self) -> UUID:
        return self.replies[0].message_id

    @property
    def text(self) -> str:
        return self.replies[0].text
