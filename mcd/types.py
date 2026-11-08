"""Plain data containers shared by every stage. No logic lives here."""
from dataclasses import dataclass, field


@dataclass
class Turn:
    """One user message. n is the 1-based turn number from the dataset."""
    n: int
    lang: str          # "English" or "Hindi"
    text: str
    distractor: bool   # unrelated side question added for the experiment


@dataclass
class Conversation:
    """One conversation in both authored versions, aligned by turn number."""
    id: str
    english: list      # list[Turn]  ("All English" version)
    mix: list          # list[Turn]  ("English + Hindi" version)
    hindi: list = field(default_factory=list)   # all-Hindi version, if authored


@dataclass
class Variant:
    """A runnable conversation for one condition (produced by the remixer)."""
    conv_id: str
    condition: str
    turns: list        # list[Turn]
