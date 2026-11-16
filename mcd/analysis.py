"""Stage 5: turn grade rows into the numbers shown in the UI.

final pass     rule followed on its LAST applicable turn (the retention test)
first pass     rule followed on its FIRST applicable turn (was it ever followed?)
forgetting     among rules followed on the first turn, share broken on the last
switch cost    final pass (EN) - final pass (other condition), paired by
               (conversation, rule), with a cluster bootstrap that resamples
               whole conversations

Memory gap: a rule that is stated and tested in the same turn cannot show forgetting. The PRIMARY switch
cost therefore uses only rules with a memory gap of at least one turn (last tested turn minus first
turn); the all-rules version is kept as a secondary result.

Ungradable cells (passed = None) are never guessed. For the switch cost the whole
PAIR is dropped when either side is ungradable, so EN and the other condition are
compared on exactly the same rules. Sensitivity bounds then re-count the dropped
pairs as all-pass and all-fail to show how much the dropping could matter.
"""
import random
from collections import defaultdict

from .checks import reply_lang


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


def _permutation_p(pairs, n_perm, seed):
    """Two-sided p for 'no difference', flipping the sign of whole conversations (paired, clustered)."""
    sums = [sum(int(a) - int(b) for a, b in flat) for flat in pairs.values()]
    n = sum(len(flat) for flat in pairs.values())
    observed = abs(sum(sums)) / n
    rng, hits = random.Random(seed), 0
    for _ in range(n_perm):
        hits += abs(sum(x if rng.random() < .5 else -x for x in sums)) / n >= observed - 1e-12
    return (hits + 1) / (n_perm + 1)


def hindi_exposure(grades, rules, variants):
    """{(conv, condition, rule): number of Hindi user turns BETWEEN the turn the rule was stated and the last turn it was tested}.

    A rule with exposure 0 cannot show an effect of the language switch: nothing in Hindi came between the
    statement and the test. rules: rulebook rules (stated_turn, else from_turn); variants: {(conv, cond): [turn dicts]}.
    """
    stated = {r["id"]: r.get("stated_turn", r["from_turn"]) for r in rules}
    last = {}
    for g in grades:
        key = (g["conv_id"], g["condition"], g["rule_id"])
        last[key] = max(last.get(key, 0), g["turn"])
    out = {}
    for (conv, cond, rule), test_turn in last.items():
        turns = variants.get((conv, cond), [])
        out[(conv, cond, rule)] = sum(1 for t in turns
                                      if stated.get(rule, 0) < t["n"] < test_turn and t["lang"] == "Hindi")
    return out


def switch_cost(final, model, cond_a="EN", cond_b="MIX", fill=None, n_boot=2000, seed=0,
                min_gap=0, n_perm=20000, exposure=None, min_exposure=0):
    """final-pass(cond_a) - final-pass(cond_b) over paired rules.

    fill=None drops pairs with an ungradable side; fill=True/False counts the
    ungradable side as pass/fail (sensitivity). n_boot=0 skips the interval.
    min_gap=1 keeps only rules whose first and last graded turns differ (older, weaker filter).
    exposure/min_exposure keep only rules with at least that many Hindi turns between the statement and the
    test in cond_b (see hindi_exposure): the right filter for a language-switch test. The p-value comes from a
    sign-flip permutation test over whole conversations (only when nothing is filled in).
    """
    pairs, dropped = defaultdict(list), 0
    for (m, cond, conv, rule), (first_a, last_a) in final.items():
        other = final.get((m, cond_b, conv, rule))
        if m != model or cond != cond_a or not other:
            continue
        if last_a["turn"] - first_a["turn"] < min_gap:
            continue
        if exposure is not None and exposure.get((conv, cond_b, rule), 0) < min_exposure:
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

    out = {"diff": diff(convs), "lo": None, "hi": None, "p": None, "dropped_pairs": dropped,
           "pairs": sum(len(p) for p in pairs.values()), "conversations": len(convs),
           "too_few": len(convs) < MIN_CONVERSATIONS_FOR_INTERVAL, "min_gap": min_gap,
           "min_exposure": min_exposure}
    if n_boot:
        rng = random.Random(seed)
        boots = sorted(diff([rng.choice(convs) for _ in convs]) for _ in range(n_boot))
        out["lo"], out["hi"] = boots[int(.025 * n_boot)], boots[int(.975 * n_boot) - 1]
    if n_perm and fill is None:
        out["p"] = _permutation_p(pairs, n_perm, seed)
    return out


def _paired_stat(pairs, n_boot=2000, n_perm=20000, seed=0):
    """pairs: {conv: [(a, b), ...]} -> diff (mean a - mean b), 95% interval and p, resampling whole conversations."""
    convs = list(pairs)
    if not convs:
        return None
    flat = lambda sample: [p for c in sample for p in pairs[c]]
    diff = lambda sample: _mean([int(a) for a, _ in flat(sample)]) - _mean([int(b) for _, b in flat(sample)])
    rng = random.Random(seed)
    boots = sorted(diff([rng.choice(convs) for _ in convs]) for _ in range(n_boot))
    return {"diff": diff(convs), "lo": boots[int(.025 * n_boot)], "hi": boots[int(.975 * n_boot) - 1],
            "p": _permutation_p(pairs, n_perm, seed), "pairs": len(flat(convs)), "conversations": len(convs)}


def turn_level_cost(grades, exposure_by_turn, model, cond_a="EN", cond_b="MIX_CLEAN", min_exposure=1):
    """Forgetting at ANY turn: pass rate in cond_a minus cond_b over every graded (conversation, rule, turn).

    A rule broken at an in-between turn is forgetting, not only a rule broken at the last turn. Only turns with
    at least `min_exposure` Hindi turns between the statement and that turn (in cond_b) are kept.
    exposure_by_turn: {(conv, cond, rule, turn): Hindi turns between the rule's statement and that turn}.
    """
    rows = defaultdict(dict)
    for g in grades:
        if g["model"] == model and g["condition"] in (cond_a, cond_b) and g["passed"] is not None:
            rows[(g["conv_id"], g["rule_id"], g["turn"])][g["condition"]] = g["passed"]
    pairs = defaultdict(list)
    for (conv, rule, turn), both in rows.items():
        if cond_a in both and cond_b in both and exposure_by_turn.get((conv, cond_b, rule, turn), 0) >= min_exposure:
            pairs[conv].append((both[cond_a], both[cond_b]))
    return _paired_stat(pairs)


def lapse_rate(grades, model, cond):
    """Share of rules followed at their first graded turn that were broken at ANY later graded turn."""
    series = defaultdict(list)
    for g in grades:
        if g["model"] == model and g["condition"] == cond and g["passed"] is not None:
            series[(g["conv_id"], g["rule_id"])].append((g["turn"], g["passed"]))
    start = lapsed = 0
    for rows in series.values():
        rows.sort()
        if len(rows) > 1 and rows[0][1]:
            start += 1
            lapsed += any(not passed for _, passed in rows[1:])
    return {"followed_at_start": start, "lapsed": lapsed, "rate": lapsed / start if start else None}


def hindi_exposure_by_turn(grades, rules, variants):
    """{(conv, condition, rule, turn): Hindi user turns between the rule's statement and that turn}."""
    stated = {r["id"]: r.get("stated_turn", r["from_turn"]) for r in rules}
    out = {}
    for g in grades:
        key = (g["conv_id"], g["condition"], g["rule_id"], g["turn"])
        if key not in out:
            out[key] = sum(1 for t in variants.get((g["conv_id"], g["condition"]), [])
                           if stated.get(g["rule_id"], 0) < t["n"] < g["turn"] and t["lang"] == "Hindi")
    return out


def switch_cost_with_sensitivity(final, model, cond, min_gap=0, exposure=None, min_exposure=0):
    main = switch_cost(final, model, "EN", cond, min_gap=min_gap, exposure=exposure, min_exposure=min_exposure)
    if main:
        for name, fill in (("if_dropped_pass", True), ("if_dropped_fail", False)):
            alt = switch_cost(final, model, "EN", cond, fill=fill, n_boot=0, min_gap=min_gap,
                              exposure=exposure, min_exposure=min_exposure)
            main[name] = alt["diff"] if alt else None
    return main


def summarize(grades, exposure=None):
    final = first_and_last(grades)
    out = {}
    for model in sorted({k[0] for k in final}):
        conds = sorted({k[1] for k in final if k[0] == model})
        out[model] = {
            "conditions": {c: condition_stats([v for k, v in final.items()
                                               if k[0] == model and k[1] == c]) for c in conds},
            # PRIMARY: rules with at least one Hindi turn between statement and test (when exposure is known),
            # else rules whose first and last graded turns differ. SECONDARY: all rules.
            "switch_costs": {c: (switch_cost_with_sensitivity(final, model, c, exposure=exposure, min_exposure=1)
                                 if exposure is not None else switch_cost_with_sensitivity(final, model, c, min_gap=1))
                             for c in conds if c != "EN"},
            "switch_costs_all": {c: switch_cost_with_sensitivity(final, model, c)
                                 for c in conds if c != "EN"},
        }
    return out


def leakage(variants, responses):
    """How often the reply language differs from the language of the question it answers.

    variants: {(conv_id, condition): [Turn dicts]}; responses: latest response records.
    Returns {model: {condition: {en_turns, en_turns_hindi_reply, hi_turns, hi_turns_english_reply}}}.
    A reply counts as Hindi when it has more Devanagari than Latin letters (checks.reply_lang).
    """
    out = defaultdict(lambda: defaultdict(lambda: {"en_turns": 0, "en_turns_hindi_reply": 0,
                                                   "hi_turns": 0, "hi_turns_english_reply": 0}))
    for r in responses:
        turns = variants.get((r["conv_id"], r["condition"]))
        if not turns:
            continue
        for turn, reply in zip(turns, r["replies"]):
            if not reply.strip():
                continue
            cell = out[r["model"]][r["condition"]]
            hindi_reply = reply_lang(reply) == "hi"
            if turn["lang"] == "English":
                cell["en_turns"] += 1
                cell["en_turns_hindi_reply"] += hindi_reply
            else:
                cell["hi_turns"] += 1
                cell["hi_turns_english_reply"] += not hindi_reply
    return {m: dict(c) for m, c in out.items()}


def headroom(grades, low=0.40, high=0.90):
    """English-only baseline per model (the original LIS-S conversations).

    Uses ONLY the EN condition, so it can guide which models/groups to study without
    looking at the switch result. A pass rate above `high` leaves no room to observe
    forgetting (ceiling); below `low`, the model rarely follows the rules even in
    English (floor). Between them there is headroom.
    """
    passes = defaultdict(lambda: defaultdict(list))
    for (model, cond, conv, _), (_, last) in first_and_last(grades).items():
        if cond == "EN" and last["passed"] is not None:
            passes[model]["S"].append(last["passed"])
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
