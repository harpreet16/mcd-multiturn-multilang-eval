"""One place that wires the stages together. The CLI and the web server both
call these functions, so they behave identically.
"""
import json
import os

from . import analysis, grader, ingest, remixer, runner
from .backends import make_backend
from .jobs import Progress
from .rulebook import Rulebook, rule_hash
from .workspace import Workspace, latest_responses, read_jsonl

DEFAULT_DATASET = "tests/assets/Lost in the Switch — Simple Conversations (1).xlsx"


def do_ingest(ws, xlsx_path=DEFAULT_DATASET):
    conversations, warnings = ingest.ingest(xlsx_path)
    ws.save_conversations(conversations)
    added = Rulebook(ws.rulebook).add_missing_from_seed()   # seeds, or adds new seed rules
    do_remix(ws)
    return {"conversations": len(conversations), "warnings": warnings, "rules_added": added}


def rule_intro_turns(ws):
    """{conv_id: {turns where an enabled rule starts}} from the current rulebook."""
    out = {}
    for r in Rulebook(ws.rulebook).rules:
        if r.get("enabled", True):
            out.setdefault(r["conv_id"], set()).add(r["from_turn"])
    return out


def do_remix(ws, conditions=None):
    variants = remixer.remix(ws.load_conversations(), conditions, rule_intro_turns(ws))
    ws.save_variants(variants)
    return {"variants": len(variants)}


def do_run(ws, model, base_url=None, api_key=None, max_tokens=4096,
           conv_ids=None, conditions=None, progress=None, workers=4, extra_body=None):
    backend = make_backend(model, base_url, api_key, max_tokens, extra_body)
    return runner.run(ws, backend, conv_ids, conditions, progress, workers)


def do_grade(ws, judge_model, base_url=None, api_key=None, progress=None, workers=4, extra_body=None):
    # judge answers are short, but reasoning models need room to think first
    backend = None if judge_model == "manual" else make_backend(judge_model, base_url, api_key, 2048, extra_body)
    return grader.grade(ws, backend, progress, workers)


def judge_apply(ws, verdicts_path, judge_name="claude (in-session)"):
    """Merge manual verdicts [{key, verdict: YES|NO, reason}] into the judge cache, and log them."""
    import time
    from .workspace import append_jsonl
    cache = json.load(open(ws.judge_cache)) if os.path.exists(ws.judge_cache) else {}
    pending = {p["key"]: p for p in json.load(open(ws.judge_pending))} if os.path.exists(ws.judge_pending) else {}
    n = 0
    for v in json.load(open(verdicts_path, encoding="utf-8")):
        if v["key"] not in pending and v["key"] not in cache:
            raise ValueError(f"unknown question key {v['key']}")
        if str(v["verdict"]).strip().upper() not in ("YES", "NO"):
            raise ValueError(f"verdict must be YES or NO, got {v['verdict']!r}")
        cache[v["key"]] = str(v["verdict"]).strip().upper() == "YES"
        item = pending.get(v["key"], {})
        append_jsonl(ws.judge_log, {"key": v["key"], "judge": judge_name, "verdict": v["verdict"].upper(),
                                    "reason": v.get("reason", ""), "question": item.get("question"),
                                    "at": time.strftime("%Y-%m-%d %H:%M:%S")})
        n += 1
    json.dump(cache, open(ws.judge_cache, "w"))
    return {"applied": n}


def preflight(ws, model, base_url=None, api_key=None, max_tokens=4096, extra_body=None, judge_model=None):
    """Quick end-to-end check before a long run. Returns [(step, ok, detail, seconds)]."""
    import tempfile, time
    from .checks import run_check
    from .grader import Judge
    steps = []

    def step(name, fn):
        t0 = time.time()
        try:
            ok, detail = fn()
        except Exception as e:
            ok, detail = False, f"{type(e).__name__}: {e}"
        steps.append((name, ok, detail, round(time.time() - t0, 1)))
        return ok

    backend = make_backend(model, base_url, api_key, max_tokens, extra_body)

    def connect():
        r = backend.chat([{"role": "user", "content": "Reply with the single word OK."}])
        return (bool(r.text.strip()) and not r.truncated), f"reply={r.text.strip()[:40]!r} cut_off={r.truncated}"
    if not step("1. connect and get a non-empty reply", connect):
        return steps

    def conversation():
        variant = next(v for v in ws.load_variants() if (v.conv_id, v.condition) == ("LIS-X01", "EN"))
        replies, cut = runner.play(variant, backend)
        rule = next(r for r in Rulebook(ws.rulebook).rules if r["id"] == "LIS-X01-R1")
        followed = [run_check(rule["check"], replies[i]) for i, t in enumerate(variant.turns) if not t.distractor]
        empty = sum(1 for x in replies if not x.strip())
        return (empty == 0 and not any(cut)), f"{len(replies)} replies, empty={empty}, cut_off={sum(cut)}, rule followed on {sum(followed)}/{len(followed)} graded turns"
    step("2. run a 5-turn conversation with no empty/cut-off replies", conversation)

    def judge():
        jb = make_backend(judge_model or model, base_url, api_key, 2048, extra_body)
        j = Judge(jb, tempfile.mktemp())
        yes = j.ask("Is Paris mentioned?", "Paris is the capital of France.")
        no = j.ask("Is Berlin mentioned?", "Paris is the capital of France.")
        return (yes is True and no is False), f"answered {yes} / {no} (expected True / False)"
    step("3. judge answers a yes/no question correctly", judge)
    return steps


def results(ws, group="all"):
    """group: 'all', 'S' (original dataset) or 'X' (curated conversations)."""
    grades = all_grades = read_jsonl(ws.grades)
    if group in ("S", "X"):
        grades = [g for g in grades if g["conv_id"].startswith(f"LIS-{group}")]
    return {"summary": analysis.summarize(grades), "grid": analysis.rule_grid(grades),
            "headroom": analysis.headroom(all_grades), "quality": analysis.quality(all_grades)}


def rulebook_view(ws):
    """Rules plus what each did in the last graded run, and which are stale."""
    rb = Rulebook(ws.rulebook)
    last = analysis.rule_last_run(read_jsonl(ws.grades))
    stale = [r["id"] for r in rb.rules
             if any(v["rule_hash"] != rule_hash(r) for v in last.get(r["id"], {}).values())]
    return {"version": rb.data["version"], "updated_at": rb.data["updated_at"],
            "rules": rb.rules, "last_run": last, "stale": stale}


def status(ws):
    """Progress per (model, condition) from the saved files; safe to call while a run is going."""
    from collections import defaultdict
    expected = defaultdict(int)
    for v in ws.load_variants():
        expected[v.condition] += 1
    done = defaultdict(lambda: {"saved": 0, "cut_off": 0, "last": ""})
    for r in latest_responses(ws.responses):
        d = done[(r["model"], r["condition"])]
        d["saved"] += 1
        d["cut_off"] += any(r["truncated"])
        d["last"] = max(d["last"], r["finished_at"])
    return [{"model": m, "condition": c, "expected": expected.get(c, 0), **d}
            for (m, c), d in sorted(done.items())]


def state(ws):
    responses = latest_responses(ws.responses)
    return {"workspace": str(ws.root),
            "conversations": len(read_jsonl(ws.conversations)),
            "variants": len(read_jsonl(ws.variants)),
            "responses": len(responses),
            "models": sorted({r["model"] for r in responses}),
            "grades": len(read_jsonl(ws.grades)),
            "conditions": {k: v[0] for k, v in remixer.CONDITIONS.items()}}


def transcript(ws, conv_id, condition, model):
    variant = next((v for v in ws.load_variants()
                    if v.conv_id == conv_id and v.condition == condition), None)
    resp = next((r for r in latest_responses(ws.responses)
                 if (r["conv_id"], r["condition"], r["model"]) == (conv_id, condition, model)), None)
    if not variant:
        return None
    grades = [g for g in read_jsonl(ws.grades)
              if (g["conv_id"], g["condition"], g["model"]) == (conv_id, condition, model)]
    turns = []
    for i, t in enumerate(variant.turns):
        turns.append({"n": t.n, "lang": t.lang, "text": t.text, "distractor": t.distractor,
                      "reply": resp["replies"][i] if resp else None,
                      "truncated": resp["truncated"][i] if resp else False,
                      "grades": [{"rule_id": g["rule_id"], "passed": g["passed"]}
                                 for g in grades if g["turn"] == t.n]})
    return {"conv_id": conv_id, "condition": condition, "model": model, "turns": turns}
