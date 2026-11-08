"""Stage 1: read the dataset spreadsheet into Conversation objects.

Expected columns (long format, one row per turn):
    Conversation ID | Version | Turn | Language | Turn text | Source | Source link
Versions: "All English" and "English + Hindi". Turns are aligned by number.

Returns (conversations, warnings). Warnings are things a human should look at,
such as an English turn whose text differs between the two versions.
"""
import json
import os
from collections import defaultdict
from pathlib import Path

from .types import Conversation, Turn
from .xlsx import read_sheet

VERSION_EN = "All English"
VERSION_MIX = "English + Hindi"
DISTRACTOR_SOURCE = "Added for this experiment"
# The sheet labels every added turn "Added for this experiment", but some added turns are
# retention probes (S19/S20 turn 4: "write the full resume following all the requirements").
# Only the unrelated side-questions are distractors, so they are recognised by their wording.
DISTRACTOR_PREFIXES = ("Quick side question", "A small unrelated question", "One more quick detour")
FIXES_PATH = Path(__file__).resolve().parent.parent / "data" / "translation_fixes.json"
EXTRA_PATH = Path(__file__).resolve().parent.parent / "data" / "extra_conversations.json"
REQUIRED = ["Conversation ID", "Version", "Turn", "Language", "Turn text", "Source"]


def _header_index(header):
    missing = [c for c in REQUIRED if c not in header]
    if missing:
        raise ValueError(f"Spreadsheet is missing columns: {missing}. Found: {header}")
    return {name: header.index(name) for name in REQUIRED}


def load_fixes(path=FIXES_PATH):
    """Reviewed corrections to specific turns, kept outside the source spreadsheet."""
    return json.load(open(path, encoding="utf-8")) if os.path.exists(path) else []


def _apply_fixes(versions, fixes):
    """Replace a turn's text only if it still equals the recorded 'old' text."""
    warnings = []
    for fix in fixes:
        turns = versions.get(fix["conv_id"], {}).get(VERSION_MIX, [])
        match = [t for t in turns if t.n == fix["turn"] and t.lang == fix["lang"]]
        if match and match[0].text == fix["old"]:
            match[0].text = fix["new"]
        else:
            warnings.append(f"{fix['conv_id']} turn {fix['turn']}: fix not applied "
                            f"(turn text no longer matches the recorded original)")
    return warnings


def load_extra(path=EXTRA_PATH):
    """Curated conversations written for this study (English + a full Hindi version).

    The authored mix is English at the first and last turn and Hindi in between,
    so the rule (given in turn 1) must survive an English -> Hindi -> English switch.
    """
    if not os.path.exists(path):
        return []
    out = []
    for c in json.load(open(path, encoding="utf-8"))["conversations"]:
        en = [Turn(t["n"], "English", t["en"], t["distractor"]) for t in c["turns"]]
        hi = [Turn(t["n"], "Hindi", t["hi"], t["distractor"]) for t in c["turns"]]
        hindi_at = set(c["mix_hindi_turns"])
        mix = [hi[i] if t.n in hindi_at else en[i] for i, t in enumerate(en)]
        out.append(Conversation(c["id"], en, mix, hi))
    return out


def ingest(xlsx_path, fixes_path=FIXES_PATH, extra_path=EXTRA_PATH):
    rows = read_sheet(xlsx_path)
    col = _header_index(rows[0])

    versions = defaultdict(lambda: defaultdict(list))   # conv -> version -> [Turn]
    for r in rows[1:]:
        r = r + [""] * (len(rows[0]) - len(r))
        turn = Turn(n=int(float(r[col["Turn"]])),
                    lang=r[col["Language"]].strip(),
                    text=r[col["Turn text"]],
                    distractor=r[col["Source"]].startswith(DISTRACTOR_SOURCE))
        versions[r[col["Conversation ID"]]][r[col["Version"]]].append(turn)

    fixes = load_fixes(fixes_path)
    conversations, warnings = [], _apply_fixes(versions, fixes)
    for conv_id, by_version in versions.items():
        missing = {VERSION_EN, VERSION_MIX} - set(by_version)
        if missing:
            warnings.append(f"{conv_id}: missing version {sorted(missing)}; skipped")
            continue
        english = sorted(by_version[VERSION_EN], key=lambda t: t.n)
        mix = sorted(by_version[VERSION_MIX], key=lambda t: t.n)
        if [t.n for t in english] == [t.n for t in mix]:
            for en, mx in zip(english, mix):          # decide from the English text, apply to both versions
                en.distractor = mx.distractor = en.distractor and en.text.startswith(DISTRACTOR_PREFIXES)
        conversations.append(Conversation(conv_id, english, mix))
        warnings += _lint(conv_id, english, mix)
    for conv in load_extra(extra_path):
        conversations.append(conv)
        warnings += _lint(conv.id, conv.english, conv.mix)
    return conversations, warnings


def _lint(conv_id, english, mix):
    """Checks that make the EN/MIX comparison fair."""
    out = []
    if [t.n for t in english] != [t.n for t in mix]:
        out.append(f"{conv_id}: turn numbers differ between versions")
        return out
    for en, mx in zip(english, mix):
        if mx.lang == "English" and mx.text != en.text:
            out.append(f"{conv_id} turn {en.n}: English text differs between versions")
        if en.lang != "English":
            out.append(f"{conv_id} turn {en.n}: 'All English' version contains {en.lang}")
        if en.distractor != mx.distractor:
            out.append(f"{conv_id} turn {en.n}: distractor flag differs between versions")
    if mix[-1].lang != "English":
        out.append(f"{conv_id}: final turn is {mix[-1].lang}, not English")
    return out
