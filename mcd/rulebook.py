"""The rulebook: an editable list of rules, per conversation.

A rule says "from turn X to turn Y, the reply must satisfy <check>".
  polarity   "must" (include/do) or "must_not" (avoid/never). A label used for
             analysis; the check itself is always written so passing = followed.
  from_turn  turn where the rule is introduced
  to_turn    last turn it applies (null = through the end of the conversation)
  language_scope  "any" (graded whatever language the reply is in) or
             "english_only" (depends on English letters/words, e.g. "no letter c";
             not graded when the reply is in Hindi, and the matching EN/MIX pair
             is dropped from the switch-cost comparison, not just one side)
Rules are only graded on task turns, never on distractor side-questions.

Every save bumps `version`. Grades remember each rule's hash, so the UI can
tell when a rule was edited after the last grading.
"""
import hashlib
import json
import os
import shutil
import time

from .checks import CHECK_TYPES
from .workspace import SEED_RULEBOOK

REQUIRED_FIELDS = ["id", "conv_id", "name", "polarity", "check", "from_turn"]


def rule_hash(rule):
    """Fingerprint of everything that affects grading."""
    relevant = {k: rule.get(k) for k in ("check", "from_turn", "to_turn", "polarity", "language_scope")}
    return hashlib.sha1(json.dumps(relevant, sort_keys=True).encode()).hexdigest()[:10]


def validate(rule):
    missing = [f for f in REQUIRED_FIELDS if f not in rule]
    if missing:
        raise ValueError(f"rule is missing fields: {missing}")
    if rule["polarity"] not in ("must", "must_not"):
        raise ValueError("polarity must be 'must' or 'must_not'")
    if rule["check"].get("type") not in CHECK_TYPES:
        raise ValueError(f"check.type must be one of {CHECK_TYPES}")
    if rule["check"]["type"] == "judge" and not rule["check"].get("question"):
        raise ValueError("a judge check needs a 'question'")
    if rule.get("language_scope", "any") not in ("any", "english_only"):
        raise ValueError("language_scope must be 'any' or 'english_only'")
    if rule.get("to_turn") is not None and rule["to_turn"] < rule["from_turn"]:
        raise ValueError("to_turn is before from_turn")


class Rulebook:
    def __init__(self, path):
        self.path = path
        if not os.path.exists(path):
            shutil.copy(SEED_RULEBOOK, path)       # start from the shipped seed
        with open(path, encoding="utf-8") as f:
            self.data = json.load(f)

    @property
    def rules(self):
        return self.data["rules"]

    def save(self):
        self.data["version"] += 1
        self.data["updated_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
        with open(self.path, "w", encoding="utf-8") as f:
            json.dump(self.data, f, indent=1, ensure_ascii=False)

    def upsert(self, rule):
        validate(rule)
        rule.setdefault("enabled", True)
        rule.setdefault("source", "user")
        rule.setdefault("to_turn", None)
        rule.setdefault("language_scope", "any")
        for i, existing in enumerate(self.rules):
            if existing["id"] == rule["id"]:
                self.rules[i] = rule
                break
        else:
            self.rules.append(rule)
        self.save()
        return rule

    def delete(self, rule_id):
        self.data["rules"] = [r for r in self.rules if r["id"] != rule_id]
        self.save()

    def add_missing_from_seed(self):
        """Add seed rules whose id is absent. Never overwrites existing (possibly edited) rules."""
        with open(SEED_RULEBOOK, encoding="utf-8") as f:
            seed = json.load(f)["rules"]
        have = {r["id"] for r in self.rules}
        new = [r for r in seed if r["id"] not in have]
        if new:
            self.rules.extend(new)
            self.save()
        return len(new)

    def reset_to_seed(self):
        shutil.copy(SEED_RULEBOOK, self.path)
        self.__init__(self.path)
