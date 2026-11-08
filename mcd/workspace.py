"""All file locations and JSONL helpers. Every stage reads/writes through here.

A workspace is one folder holding the whole pipeline state:

    conversations.jsonl   ingest   output (both versions of each conversation)
    variants.jsonl        remixer  output (runnable conversation per condition)
    rulebook.json         editable rules (seeded from data/seed_rulebook.json)
    responses.jsonl       runner   output (model replies)
    grades.jsonl          grader   output (one row per rule x turn)
    judge_cache.json      cached judge answers, so re-grading is cheap
"""
import json
import os
from dataclasses import asdict
from pathlib import Path

from .types import Conversation, Turn, Variant

SEED_RULEBOOK = Path(__file__).resolve().parent.parent / "data" / "seed_rulebook.json"


def read_jsonl(path):
    if not os.path.exists(path):
        return []
    with open(path, encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]


def write_jsonl(path, rows):
    with open(path, "w", encoding="utf-8") as f:
        for row in rows:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")


def append_jsonl(path, row):
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(row, ensure_ascii=False) + "\n")
        f.flush()


def latest_responses(path):
    """Saved replies, keeping only the newest record per (conversation, condition, model).

    A re-run after a cut-off or a settings change appends a new record; the old one is ignored.
    """
    newest = {}
    for r in read_jsonl(path):
        newest[(r["conv_id"], r["condition"], r["model"])] = r
    return list(newest.values())


class Workspace:
    def __init__(self, root):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        for name in ("conversations", "variants", "responses", "grades"):
            setattr(self, name, str(self.root / f"{name}.jsonl"))
        self.rulebook = str(self.root / "rulebook.json")
        self.judge_cache = str(self.root / "judge_cache.json")
        self.judge_pending = str(self.root / "judge_pending.json")   # questions awaiting a manual verdict
        self.partial = str(self.root / "partial.json")               # turns finished in conversations still in progress
        self.judge_log = str(self.root / "judge_log.jsonl")          # every manual verdict with its reason

    # --- conversations & variants -------------------------------------
    def save_conversations(self, conversations):
        write_jsonl(self.conversations, [asdict(c) for c in conversations])

    def load_conversations(self):
        return [Conversation(r["id"], [Turn(**t) for t in r["english"]],
                             [Turn(**t) for t in r["mix"]],
                             [Turn(**t) for t in r.get("hindi", [])])
                for r in read_jsonl(self.conversations)]

    def save_variants(self, variants):
        write_jsonl(self.variants, [asdict(v) for v in variants])

    def load_variants(self):
        return [Variant(r["conv_id"], r["condition"], [Turn(**t) for t in r["turns"]])
                for r in read_jsonl(self.variants)]

    def dataset_hash(self):
        """Short fingerprint of the ingested data, stored with every response."""
        import hashlib
        if not os.path.exists(self.conversations):
            return ""
        return hashlib.sha256(open(self.conversations, "rb").read()).hexdigest()[:12]
