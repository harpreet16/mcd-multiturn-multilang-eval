"""Stage 5: turn grade rows into the numbers shown in the UI.

final pass     rule followed on its LAST applicable turn (the retention test)
first pass     rule followed on its FIRST applicable turn (was it ever followed?)
forgetting     among rules followed on the first turn, share broken on the last
switch cost    final pass (EN) - final pass (other condition), paired by
               (conversation, rule), with a cluster bootstrap that resamples
               whole conversations

Ungradable cells (passed = None) are never guessed. For the switch cost the whole
PAIR is dropped when either side is ungradable, so EN and the other condition are
compared on exactly the same rules. Sensitivity bounds then re-count the dropped
pairs as all-pass and all-fail to show how much the dropping could matter.
"""
import random
from collections import defaultdict


MIN_CONVERSATIONS_FOR_INTERVAL = 10   # below this, a bootstrap interval over whole conversations is not trustworthy


def _mean(values):
    return sum(values) / len(values) if values else None


def first_and_last(grades):
    """{(model, condition, conv, rule): (first_row, last_row)} by turn number."""
    by_key = defaultdict(list)
    for g in grades:
        by_key[(g["model"], g["condition"], g["conv_id"], g["rule_id"])].append(g)
    return {k: (min(v, key=lambda g: g["turn"]), max(v, key=lambda g: g["turn"]))
            for k, v in by_key.items()}


def condition_stats(pairs):
    """Stats for a list of (first_row, last_row)."""
    usable = [(f, l) for f, l in pairs if l["passed"] is not None]
    followed_first = [(f, l) for f, l in usable if f["passed"] and f["turn"] != l["turn"]]
    dropped_lang = [l for _, l in pairs if l.get("skip_reason")]
    return {
        "rules": len(pairs),
        "graded": len(usable),
        "dropped_skipped": len(dropped_lang),          # empty, cut off, or English-only rule on a Hindi reply
        "dropped_error": len([1 for _, l in pairs if l["passed"] is None and not l.get("skip_reason")]),
        "final_pass": _mean([l["passed"] for _, l in usable]),
        "first_pass": _mean([f["passed"] for f, _ in pairs if f["passed"] is not None]),
        "forgetting": _mean([not l["passed"] for _, l in followed_first]),
        "forgetting_n": len(followed_first),
        "must_final": _mean([l["passed"] for _, l in usable if l["polarity"] == "must"]),
        "must_not_final": _mean([l["passed"] for _, l in usable if l["polarity"] == "must_not"]),
        "truncated": _mean([l["truncated"] for _, l in pairs]),
        "hindi_reply_share": _mean([l.get("reply_lang") == "hi" for _, l in pairs]),
    }


def switch_cost(final, model, cond_a="EN", cond_b="MIX", fill=None, n_boot=2000, seed=0):
    """final-pass(cond_a) - final-pass(cond_b) over paired rules.

    fill=None drops pairs with an ungradable side; fill=True/False counts the
    ungradable side as pass/fail (sensitivity). n_boot=0 skips the interval.
    """
    pairs, dropped = defaultdict(list), 0
    for (m, cond, conv, rule), (_, last_a) in final.items():
        other = final.get((m, cond_b, conv, rule))
        if m != model or cond != cond_a or not other:
            continue
        a, b = last_a["passed"], other[1]["passed"]
        a, b = (fill if a is None else a), (fill if b is None else b)
        if a is None or b is None:
            dropped += 1
        else:
            pairs[conv].append((a, b))
    convs = list(pairs)
    if not convs:
        return None

    def diff(sample):
        flat = [p for c in sample for p in pairs[c]]
        return _mean([a for a, _ in flat]) - _mean([b for _, b in flat])

    out = {"diff": diff(convs), "lo": None, "hi": None, "dropped_pairs": dropped,
           "pairs": sum(len(p) for p in pairs.values()), "conversations": len(convs),
           "too_few": len(convs) < MIN_CONVERSATIONS_FOR_INTERVAL}
    if n_boot:
        rng = random.Random(seed)
        boots = sorted(diff([rng.choice(convs) for _ in convs]) for _ in range(n_boot))
        out["lo"], out["hi"] = boots[int(.025 * n_boot)], boots[int(.975 * n_boot) - 1]
    return out


def switch_cost_with_sensitivity(final, model, cond):
    main = switch_cost(final, model, "EN", cond)
    if main:
        for name, fill in (("if_dropped_pass", True), ("if_dropped_fail", False)):
            alt = switch_cost(final, model, "EN", cond, fill=fill, n_boot=0)
            main[name] = alt["diff"] if alt else None
    return main


def summarize(grades):
    final = first_and_last(grades)
    out = {}
    for model in sorted({k[0] for k in final}):
        conds = sorted({k[1] for k in final if k[0] == model})
        out[model] = {
            "conditions": {c: condition_stats([v for k, v in final.items()
                                               if k[0] == model and k[1] == c]) for c in conds},
            "switch_costs": {c: switch_cost_with_sensitivity(final, model, c)
                             for c in conds if c != "EN"},
        }
    return out


def headroom(grades, low=0.40, high=0.90):
    """English-only baseline per model and conversation group (S original, X curated).

    Uses ONLY the EN condition, so it can guide which models/groups to study without
    looking at the switch result. A pass rate above `high` leaves no room to observe
    forgetting (ceiling); below `low`, the model rarely follows the rules even in
    English (floor). Between them there is headroom.
    """
    passes = defaultdict(lambda: defaultdict(list))
    for (model, cond, conv, _), (_, last) in first_and_last(grades).items():
        if cond == "EN" and last["passed"] is not None:
            passes[model]["X" if conv.startswith("LIS-X") else "S"].append(last["passed"])
    out = {}
    for model, groups in passes.items():
        out[model] = {}
        for group, values in groups.items():
            rate = _mean(values)
            out[model][group] = {"rules": len(values), "pass": rate,
                                 "status": "ceiling" if rate > high else "floor" if rate < low else "headroom"}
    return out


def quality(grades, warn=0.05, stop=0.15):
    """How much of each condition could NOT be graded for avoidable reasons.

    Avoidable = empty reply, cut-off reply, or a failed judge call. English-only rules on
    Hindi replies are an expected, reported drop and are not counted here.
    verdict: ok (<= warn), warning (<= stop), do_not_interpret (> stop).
    """
    out = {}
    for (model, cond, _, _), (_, last) in first_and_last(grades).items():
        q = out.setdefault(model, {}).setdefault(cond, {"rules": 0, "avoidable": 0, "reasons": defaultdict(int)})
        q["rules"] += 1
        if last["passed"] is None and last.get("skip_reason") != "english_only_rule_hindi_reply":
            q["avoidable"] += 1
            q["reasons"][last.get("skip_reason") or "judge_error"] += 1
    for conds in out.values():
        for q in conds.values():
            share = q["avoidable"] / q["rules"]
            q["share"], q["reasons"] = share, dict(q["reasons"])
            q["verdict"] = "ok" if share <= warn else "warning" if share <= stop else "do_not_interpret"
    return out


def rule_grid(grades):
    """{rule_id: {"model|condition": [{turn, passed}, ...]}} for the heatmap."""
    grid = defaultdict(lambda: defaultdict(list))
    for g in grades:
        grid[g["rule_id"]][f"{g['model']}|{g['condition']}"].append(
            {"turn": g["turn"], "passed": g["passed"]})
    for rule in grid.values():
        for cells in rule.values():
            cells.sort(key=lambda c: c["turn"])
    return grid


def rule_last_run(grades):
    """Latest-turn result per rule, per model|condition, plus the rule hash graded."""
    out = defaultdict(dict)
    for (model, cond, _, rule_id), (first, last) in first_and_last(grades).items():
        out[rule_id][f"{model}|{cond}"] = {"passed": last["passed"], "turn": last["turn"],
                                           "first_passed": first["passed"],
                                           "skip_reason": last.get("skip_reason"),
                                           "rule_hash": last["rule_hash"]}
    return out
