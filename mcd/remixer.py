"""Stage 2: build runnable conversations ("variants") for each condition.

Every condition is a function (conversation, intro_turns) -> list of Turns.
English and Hindi texts are aligned by turn number, so a condition simply decides,
turn by turn, which language to use. Hindi is only used where the dataset has a
human-written Hindi turn; nothing is machine-translated.

intro_turns is the set of turn numbers where some rule of that conversation
starts (from the rulebook). MIX_CLEAN uses it to keep rule-introducing turns in
English, so a failure there cannot be blamed on not understanding Hindi.

To add a condition, write a function and add it to CONDITIONS.
"""
from .types import Variant


def _hindi_turns(conv):
    return {t.n: t for t in conv.mix if t.lang == "Hindi"}


def _pick(conv, use_hindi):
    """English turns, swapping in the authored Hindi turn where use_hindi(n)."""
    hindi = _hindi_turns(conv)
    return [hindi[t.n] if use_hindi(t.n) and t.n in hindi else t for t in conv.english]


def all_english(conv, intro_turns):
    return _pick(conv, lambda n: False)


def authored_mix(conv, intro_turns):
    return list(conv.mix)


def mix_clean(conv, intro_turns):
    """Authored mix, but turns that introduce a rule stay English."""
    return _pick(conv, lambda n: n not in intro_turns)


def hindi_early(conv, intro_turns):
    half = len(conv.english) / 2
    return _pick(conv, lambda n: n <= half)


def hindi_late(conv, intro_turns):
    half = len(conv.english) / 2
    return _pick(conv, lambda n: n > half)


def hindi_all(conv, intro_turns):
    """Control: every turn in Hindi. Only for conversations with a full Hindi version."""
    return list(conv.hindi) or None


def hindi_en_hi(conv, intro_turns):
    """Reverse direction: Hindi, English in the middle, Hindi at the end."""
    if not conv.hindi:
        return None
    n = len(conv.english)
    return [conv.english[i] if 0 < i < n - 1 else conv.hindi[i] for i in range(n)]


CONDITIONS = {
    "EN":        ("All English baseline", all_english),
    "EN_REP":    ("Replicate of EN (noise control: same prompts, run again)", all_english),
    "MIX":       ("Authored English + Hindi (some rules introduced in Hindi)", authored_mix),
    "MIX_CLEAN": ("Authored mix, rule-introducing turns kept in English (primary memory test)", mix_clean),
    "HI_EARLY":  ("Hindi only in the first half", hindi_early),
    "HI_LATE":   ("Hindi only in the second half", hindi_late),
    "HI_ALL":    ("Control: all turns Hindi (curated conversations only)", hindi_all),
    "HI_EN_HI":  ("Reverse: Hindi, English middle, Hindi (curated conversations only)", hindi_en_hi),
}
DEFAULT_CONDITIONS = ["EN", "MIX", "MIX_CLEAN"]


def remix(conversations, condition_names=None, rule_turns=None):
    """rule_turns: {conv_id: set of turn numbers where a rule starts}."""
    rule_turns = rule_turns or {}
    names = condition_names or list(CONDITIONS)
    variants = []
    for c in conversations:
        for name in names:
            turns = CONDITIONS[name][1](c, rule_turns.get(c.id, set()))
            if turns:                      # None = condition not available for this conversation
                variants.append(Variant(c.id, name, turns))
    return variants
