"""Deterministic rule checks. Each takes the model reply and the rule's params
and returns True when the reply FOLLOWS the rule.

To add a check type: write a function and register it in CHECKS. The rulebook
editor offers every key in CHECKS.
"""
import re

DEVANAGARI = "\u0900-\u097F"
WORD = rf"[\w{DEVANAGARI}]+"          # \w alone splits Hindi words at vowel marks
SENTENCE_END = r"[.!?\u0964\u0965]+"   # includes the Hindi danda and double danda


def reply_lang(text):
    """'hi' if most letters are Devanagari, else 'en'."""
    hindi = len(re.findall(f"[{DEVANAGARI}]", text))
    latin = len(re.findall(r"[A-Za-z]", text))
    return "hi" if hindi > latin else "en"


def _in_range(value, lo=None, hi=None):
    return (lo is None or value >= lo) and (hi is None or value <= hi)


def contains(text, terms, mode="all", **_):
    found = [t.lower() in text.lower() for t in terms]
    return all(found) if mode == "all" else any(found)


def regex(text, pattern, expect=True, **_):
    return bool(re.search(pattern, text, re.IGNORECASE | re.MULTILINE)) == expect


def regex_count(text, pattern, min=None, max=None, **_):
    return _in_range(len(re.findall(pattern, text, re.MULTILINE)), min, max)


def word_count(text, min=None, max=None, **_):
    return _in_range(len(re.findall(WORD, text)), min, max)


def sentence_count(text, min=None, max=None, **_):
    return _in_range(len([s for s in re.split(SENTENCE_END, text) if s.strip()]), min, max)


def _paragraphs(text):
    return [p for p in re.split(r"\n\s*\n", text.strip()) if p.strip()]


def paragraph_count(text, min=None, max=None, min_words=0, min_lines=1, **_):
    """Blank-line separated blocks. min_words / min_lines ignore short blocks such as
    a salutation, sign-off or a one-line title, so only real paragraphs / stanzas count."""
    real = [p for p in _paragraphs(text)
            if len(re.findall(WORD, p)) >= min_words and len([l for l in p.splitlines() if l.strip()]) >= min_lines]
    return _in_range(len(real), min, max)


def max_words_per_paragraph(text, max, **_):
    return all(len(re.findall(WORD, p)) <= max for p in _paragraphs(text))


def reply_language(text, expect="en", **_):
    return reply_lang(text) == expect


def lowercase_only(text, **_):
    return text == text.lower()


def ends_with(text, phrase, **_):
    return text.strip().strip('"').lower().endswith(phrase.strip().lower())


CHECKS = {
    "contains": contains,
    "regex": regex,
    "regex_count": regex_count,
    "word_count": word_count,
    "sentence_count": sentence_count,
    "paragraph_count": paragraph_count,
    "max_words_per_paragraph": max_words_per_paragraph,
    "lowercase_only": lowercase_only,
    "ends_with": ends_with,
    "reply_language": reply_language,
}
# "judge" is the one non-deterministic type: a yes/no question put to a model.
CHECK_TYPES = sorted(CHECKS) + ["judge"]


def run_check(check, reply):
    params = {k: v for k, v in check.items() if k != "type"}
    return CHECKS[check["type"]](reply, **params)
