"""Stage 4: grade every reply against the rulebook.

For each response and each enabled rule, the rule is checked on every task turn
(not distractors) between from_turn and to_turn. Output: one row per
(response, rule, turn) with passed = True/False, or None when it could not be
graded (judge call failed, or an english_only rule met a Hindi reply; see skip_reason).

Judge rules ask a model a yes/no question. Answers are cached by
(question, text), so editing one rule only re-judges that rule.
"""
import hashlib
import json
import os
import re
import threading
from concurrent.futures import ThreadPoolExecutor

from .checks import reply_lang, run_check
from .jobs import Progress
from .rulebook import Rulebook, rule_hash
from .workspace import latest_responses, read_jsonl, write_jsonl


class PendingVerdict(Exception):
    """Raised in manual-judge mode when a question has no verdict yet."""


class Judge:
    """Yes/no questions to a model, with an on-disk cache.

    backend=None is manual mode: unanswered questions are collected in self.pending
    (to be answered by a person, or by Claude in a session) instead of being sent anywhere.
    """

    def __init__(self, backend, cache_path):
        self.backend, self.cache_path = backend, cache_path
        self.pending = {}
        self.cache = json.load(open(cache_path)) if os.path.exists(cache_path) else {}

    def ask(self, question, reply, transcript=None):
        material = transcript or reply
        key = hashlib.sha1(f"{question}\x00{material}".encode()).hexdigest()
        if key not in self.cache and self.backend is None:
            self.pending[key] = {"key": key, "question": question,
                                 "material": material, "kind": "conversation" if transcript else "reply"}
            raise PendingVerdict(key)
        if key not in self.cache:
            prompt = (f"{'Conversation so far:' if transcript else 'Assistant reply:'}\n"
                      f"{material}\n\nQuestion: {question}\n"
                      f"Answer YES or NO on the first line, then one short reason.")
            answer = self.backend.chat([{"role": "user", "content": prompt}])
            if answer.truncated or not answer.text.strip():
                # never read an empty/cut-off answer as "NO": leave the rule ungraded
                raise ValueError("judge returned an empty or cut-off answer; raise its max tokens or switch thinking off")
            self.cache[key] = bool(re.match(r"\W*yes", answer.text, re.IGNORECASE))
        return self.cache[key]

    def save(self):
        with open(self.cache_path, "w") as f:
            json.dump(self.cache, f)


def _applicable_turns(rule, variant):
    last = rule["to_turn"] if rule.get("to_turn") is not None else variant.turns[-1].n
    return [i for i, t in enumerate(variant.turns)
            if not t.distractor and rule["from_turn"] <= t.n <= last]


def _transcript(variant, replies, upto_index):
    lines = []
    for i in range(upto_index + 1):
        lines.append(f"User: {variant.turns[i].text}")
        lines.append(f"Assistant: {replies[i]}")
    return "\n".join(lines)


def grade(workspace, judge_backend, progress=None, workers=4):
    progress = progress or Progress("grade")
    rules = [r for r in Rulebook(workspace.rulebook).rules if r.get("enabled", True)]
    rules_by_conv = {}
    for r in rules:
        rules_by_conv.setdefault(r["conv_id"], []).append(r)
    variants = {(v.conv_id, v.condition): v for v in workspace.load_variants()}
    responses = latest_responses(workspace.responses)
    judge = Judge(judge_backend, workspace.judge_cache)

    progress.total, progress.done, progress.state = len(responses), 0, "running"
    lock = threading.Lock()

    def grade_response(resp):
        if progress.should_stop:
            return []
        variant = variants.get((resp["conv_id"], resp["condition"]))
        out = []
        for rule in rules_by_conv.get(resp["conv_id"], []):
            if variant is not None:
                for i in _applicable_turns(rule, variant):
                    out.append(_grade_one(rule, variant, resp, i, judge, progress))
        with lock:
            progress.done += 1
            progress.message = f"{resp['conv_id']} {resp['condition']} ({resp['model']})"
        return out

    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        rows = [row for batch in pool.map(grade_response, responses) for row in batch]
    judge.save()
    if judge.pending:
        import random
        items = list(judge.pending.values())
        random.Random(0).shuffle(items)          # shuffled, no condition labels: the judge cannot tell EN from MIX
        with open(workspace.judge_pending, "w", encoding="utf-8") as f:
            json.dump(items, f, ensure_ascii=False, indent=1)
        progress.message = f"{len(items)} question(s) await a manual verdict: see {workspace.judge_pending}"
    write_jsonl(workspace.grades, rows)
    progress.state = "stopped" if progress.should_stop else "finished"
    return progress


def _grade_one(rule, variant, resp, i, judge, progress):
    reply, check = resp["replies"][i], rule["check"]
    lang, skip, passed = reply_lang(reply), None, None
    if not reply.strip():
        skip = "empty_reply"                         # nothing to grade: drop, do not count as a fail
    elif resp["truncated"][i]:
        skip = "cut_off_by_max_tokens"               # answer incomplete: drop, do not guess
    elif rule.get("language_scope") == "english_only" and lang == "hi":
        skip = "english_only_rule_hindi_reply"       # not gradable: drop, do not guess
    else:
        try:
            if check["type"] == "judge":
                transcript = (_transcript(variant, resp["replies"], i)
                              if check.get("context") == "full" else None)
                passed = judge.ask(check["question"], reply, transcript)
            else:
                passed = run_check(check, reply)
        except PendingVerdict:
            skip = "awaiting_manual_judgement"
        except Exception as e:
            progress.errors.append(f"{rule['id']} turn {variant.turns[i].n}: {e}")
    return {"conv_id": resp["conv_id"], "condition": resp["condition"],
            "model": resp["model"], "rule_id": rule["id"], "polarity": rule["polarity"],
            "turn": variant.turns[i].n, "passed": passed, "skip_reason": skip,
            "reply_lang": lang, "truncated": resp["truncated"][i],
            "rule_hash": rule_hash(rule)}
