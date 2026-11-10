import json
import os
import tempfile
import threading
import unittest
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

from mcd import analysis, checks, ingest, remixer, service
from mcd.backends import MockChat, Reply
from mcd.grader import grade
from mcd.rulebook import Rulebook
from mcd.runner import run
from mcd.server import App, make_handler
from mcd.workspace import Workspace, read_jsonl

DATASET = Path(__file__).parent / "assets" / "Lost in the Switch — Simple Conversations (1).xlsx"


class ChecksTests(unittest.TestCase):
    def test_banned_words_use_word_boundaries(self):
        c = {"type": "regex", "pattern": r"\b(mindful|heal)\b", "expect": False}
        self.assertTrue(checks.run_check(c, "good for your health"))
        self.assertFalse(checks.run_check(c, "a mindful retreat"))

    def test_counts(self):
        self.assertTrue(checks.run_check({"type": "word_count", "min": 3}, "one two three"))
        self.assertFalse(checks.run_check({"type": "sentence_count", "max": 1}, "A. B."))
        self.assertTrue(checks.run_check({"type": "ends_with", "phrase": "the end."}, 'Story. "The end."'))


class CodeFirstRuleTests(unittest.TestCase):
    """Rules that code can decide must not go to the judge; each converted rule is proven on a pass and a fail."""
    @classmethod
    def setUpClass(cls):
        cls.rules = {r["id"]: r for r in Rulebook(tempfile.mktemp()).rules}

    def check(self, rule_id, text):
        rule = self.rules[rule_id]
        self.assertNotEqual(rule["check"]["type"], "judge", rule_id)
        return checks.run_check(rule["check"], text)

    def test_converted_rules_pass_and_fail_correctly(self):
        cases = [
            ("LIS-S01-R4", "He has a 32 inch vertical.", "He jumps high."),
            ("LIS-S01-R4", "उसकी 32 इंच की वर्टिकल जंप है।", "वह ऊँचा कूदता है।"),
            ("LIS-S01-R6", "## About Me\nHi\n## Achievements\nMVP", "Dear Coach,\nHello."),
            ("LIS-S01-R7", "A consistent fade-away shot", "A good shot"),
            ("LIS-S03-R2", "| Title | Author | Date | Copies |\n|---|---|---|---|\n| A | B | 2000 | 5 |", "1. A book by B"),
            ("LIS-S03-R3", "Use jump rope, plyometric drills and dynamic stretching.", "Just run daily."),
            ("LIS-S07-R1", "Dear Mr. Rao,\n\n" + "word " * 20 + "\n\n" + "word " * 20 + "\n\nSincerely,\nAsha", "\n\n".join(["word " * 20] * 5)),
            ("LIS-S07-R3", "Dear Mr. Rao,\nThank you.", "March 5, 2025\nDear Mr. Rao,"),
            ("LIS-S08-R5", "**Historical Significance and Authenticity**\ntext", "Historical Significance\ntext"),
            ("LIS-S11-R3", "\n".join(f"{i}. Song {i}" for i in range(1, 13)), "\n".join(f"{i}. Song {i}" for i in range(1, 9))),
            ("LIS-S12-R3", "Quiet Autumn Light Falls Down\n\nline one\nline two\n\nline three\nline four\n\nline five\nline six",
                           "line one\nline two\n\nline three\nline four"),
            ("LIS-S13-R1", "Grades\n1. Band: 100\n2. Reading: 91", "Grades are Band 100 then Reading 91."),
            ("LIS-S13-R3", "## Top scores\n1. Band: 100", "## 100-90\n1. Band: 100"),
            ("LIS-S13-R4", "ELA 81, FACS 90, Math 83, PE 86, Reading 85, Science 94, Social Studies 91, Elementary Band 50", "ELA 81"),
            ("LIS-S14-R4", 'def calculate_final(number, method: str = "recursive"):', "def calculate_final(number):"),
        ]
        for rule_id, good, bad in cases:
            self.assertTrue(self.check(rule_id, good), f"{rule_id} should pass: {good[:40]!r}")
            self.assertFalse(self.check(rule_id, bad), f"{rule_id} should fail: {bad[:40]!r}")

    def test_judge_is_reserved_for_meaning_rules(self):
        judged = sorted(r for r, v in self.rules.items() if v["check"]["type"] == "judge")
        # these are the ones that genuinely need understanding of the conversation or of meaning
        self.assertEqual(len(judged), 30)
        for rid in judged:
            self.assertTrue(self.rules[rid]["check"].get("question"), rid)


class HindiCheckTests(unittest.TestCase):
    def test_hindi_words_and_sentences_are_counted_properly(self):
        self.assertTrue(checks.run_check({"type": "word_count", "min": 4, "max": 4}, "नमस्ते दुनिया कैसे हो"))
        two = "यह पहला वाक्य है। यह दूसरा वाक्य है।"
        self.assertTrue(checks.run_check({"type": "sentence_count", "min": 2, "max": 2}, two))

    def test_reply_language(self):
        self.assertEqual(checks.reply_lang("यह हिंदी है"), "hi")
        self.assertEqual(checks.reply_lang("This is English"), "en")
        self.assertFalse(checks.run_check({"type": "reply_language", "expect": "en"}, "यह हिंदी है"))


class CuratedDataTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        data = json.load(open(Path(__file__).parent.parent / "data" / "extra_conversations.json", encoding="utf-8"))
        cls.convs = data["conversations"]
        cls.rules = {r["conv_id"]: r for r in Rulebook(tempfile.mktemp()).rules}

    def test_twenty_conversations_in_ten_matched_pairs(self):
        self.assertEqual(len(self.convs), 20)
        pairs = {}
        for c in self.convs:
            pairs.setdefault(c["pair_id"], []).append(c)
        self.assertEqual(len(pairs), 10)
        for pid, (a, b) in pairs.items():
            self.assertEqual({a["polarity"], b["polarity"]}, {"must", "must_not"}, pid)
            for ta, tb in list(zip(a["turns"], b["turns"]))[1:]:      # turns 2-5 identical
                self.assertEqual((ta["en"], ta["hi"]), (tb["en"], tb["hi"]), pid)
            self.assertNotEqual(a["turns"][0]["en"], b["turns"][0]["en"])  # only the rule differs

    def test_shape_and_en_hi_en_pattern(self):
        for c in self.convs:
            self.assertEqual([t["n"] for t in c["turns"]], [1, 2, 3, 4, 5])
            self.assertEqual(c["mix_hindi_turns"], [2, 3, 4])             # English at both ends
            self.assertEqual([t["distractor"] for t in c["turns"]], [False, False, True, False, False])
            self.assertTrue(all(t["hi"] and any("\u0900" <= ch <= "\u097f" for ch in t["hi"]) for t in c["turns"]))

    def test_scoring_targets_survive_translation(self):
        import re
        literal = ["✅", "(!)", "**", "'# '", "'- '", "Let me know if you need anything else."]
        for c in self.convs:
            en, hi = c["turns"][0]["en"], c["turns"][0]["hi"]
            for token in literal:
                if token in en:
                    self.assertIn(token, hi, f"{c['id']} lost {token!r} in Hindi")
            for t in c["turns"]:
                self.assertEqual(sorted(re.findall(r"\d+", t["en"])), sorted(re.findall(r"\d+", t["hi"])), c["id"])

    def test_each_has_one_rule_starting_at_turn_1(self):
        for c in self.convs:
            r = self.rules[c["id"]]
            self.assertEqual((r["from_turn"], r["polarity"]), (1, c["polarity"]))
            self.assertEqual(r["language_scope"], "any")


class DatasetTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.convs, cls.warnings = ingest.ingest(DATASET)

    def test_ingest_shape(self):
        self.assertEqual(len(self.convs), 40)            # 20 from the spreadsheet + 20 curated
        self.assertEqual(sum(c.id.startswith("LIS-S") for c in self.convs), 20)
        self.assertEqual(self.warnings, [])

    def test_only_real_side_questions_are_distractors(self):
        for conv in self.convs:
            for turn in conv.english:
                if turn.distractor:
                    self.assertTrue(turn.text.startswith(ingest.DISTRACTOR_PREFIXES), (conv.id, turn.n))
        for cid in ("LIS-S19", "LIS-S20"):                 # the final "follow all requirements" turn is graded
            conv = next(c for c in self.convs if c.id == cid)
            self.assertEqual([t.distractor for t in conv.english], [True, False, True, False])

    def test_replicate_condition_is_identical_to_en(self):
        conv = self.convs[0]
        self.assertEqual([x.text for x in remixer.CONDITIONS["EN_REP"][1](conv, set())],
                         [x.text for x in remixer.all_english(conv, set())])

    def test_reviewed_translation_fix_is_applied(self):
        s13 = next(c for c in self.convs if c.id == "LIS-S13")
        text = next(t.text for t in s13.mix if t.n == 3)
        self.assertEqual(text, "उन सभी की माध्यिका क्या है?")
        self.assertNotIn("दोनों", text)

    def test_en_and_mix_differ_only_in_hindi_turns(self):
        for conv in self.convs:
            en, mix = remixer.all_english(conv, set()), remixer.authored_mix(conv, set())
            for a, b in zip(en, mix):
                if b.lang == "English":
                    self.assertEqual(a.text, b.text)
                self.assertEqual(a.distractor, b.distractor)

    def test_every_condition_keeps_turn_count_and_final_english(self):
        for conv in self.convs:
            for name, (_, fn) in remixer.CONDITIONS.items():
                turns = fn(conv, {1})
                if name in ("HI_ALL", "HI_EN_HI"):           # controls: only curated conversations
                    self.assertEqual(turns is not None, bool(conv.hindi), conv.id)
                    continue
                self.assertEqual(len(turns), len(conv.english), name)
                self.assertEqual(turns[-1].lang, "English")

    def test_mix_clean_keeps_rule_introducing_turns_english(self):
        by_conv = {}
        for r in Rulebook(tempfile.mktemp()).rules:
            by_conv.setdefault(r["conv_id"], set()).add(r["from_turn"])
        for conv in self.convs:
            intro = by_conv.get(conv.id, set())
            clean = remixer.mix_clean(conv, intro)
            mix = remixer.authored_mix(conv, intro)
            for c, m in zip(clean, mix):
                if c.n in intro:
                    self.assertEqual(c.lang, "English", f"{conv.id} turn {c.n}")
                else:
                    self.assertEqual(c.text, m.text)      # unchanged elsewhere

    def test_seed_rules_point_at_real_turns(self):
        by_id = {c.id: len(c.english) for c in self.convs}
        for r in Rulebook(tempfile.mktemp()).rules:
            self.assertIn(r["conv_id"], by_id)
            self.assertLessEqual(r["from_turn"], by_id[r["conv_id"]], r["id"])


class PipelineTests(unittest.TestCase):
    def setUp(self):
        self.ws = Workspace(tempfile.mkdtemp())
        service.do_ingest(self.ws, str(DATASET))

    def test_run_is_resumable_and_records_metadata(self):
        run(self.ws, MockChat(), conditions=["EN"])
        first = read_jsonl(self.ws.responses)
        run(self.ws, MockChat(), conditions=["EN"])
        self.assertEqual(len(read_jsonl(self.ws.responses)), len(first))
        self.assertEqual(first[0]["dataset_hash"], self.ws.dataset_hash())

    def test_distractor_turns_are_never_graded(self):
        run(self.ws, MockChat(), conditions=["EN"])
        grade(self.ws, MockChat())
        distractors = {(c.id, t.n) for c in self.ws.load_conversations() for t in c.english if t.distractor}
        graded = {(g["conv_id"], g["turn"]) for g in read_jsonl(self.ws.grades)}
        self.assertFalse(distractors & graded)

    def test_editing_a_rule_marks_it_stale_and_regrade_clears_it(self):
        run(self.ws, MockChat(), conditions=["EN"])
        grade(self.ws, MockChat())
        self.assertEqual(service.rulebook_view(self.ws)["stale"], [])
        rb = Rulebook(self.ws.rulebook)
        rule = next(r for r in rb.rules if r["id"] == "LIS-S20-R1")
        rule["check"] = {"type": "regex", "pattern": ";", "expect": False}
        rb.upsert(rule)
        self.assertEqual(service.rulebook_view(self.ws)["stale"], ["LIS-S20-R1"])
        grade(self.ws, MockChat())
        self.assertEqual(service.rulebook_view(self.ws)["stale"], [])

    def test_planted_effect_is_detected(self):
        """A model that obeys 'no commas' in English but breaks it after Hindi."""
        class Forgetful:
            def chat(self, messages):
                hindi = any(any("ऀ" <= ch <= "ॿ" for ch in m["content"])
                            for m in messages if m["role"] == "user")
                return Reply("Yes, with a comma." if hindi else "No comma here.")
            def settings(self):
                return {"model": "forgetful"}
        run(self.ws, Forgetful())
        grade(self.ws, MockChat())
        grades = [g for g in read_jsonl(self.ws.grades)
                  if g["rule_id"] == "LIS-S20-R1"]          # no-commas rule
        final = analysis.first_and_last(grades)
        by_cond = {k[1]: v[1]["passed"] for k, v in final.items()}
        self.assertTrue(by_cond["EN"])
        self.assertFalse(by_cond["MIX"])

    def test_failed_calls_are_recorded_not_fatal(self):
        class Broken:
            def chat(self, messages):
                raise RuntimeError("boom")
            def settings(self):
                return {"model": "broken"}
        progress = run(self.ws, Broken(), conditions=["EN"])
        self.assertEqual(progress.state, "finished")
        self.assertEqual(progress.snapshot()["error_count"], 40)
        self.assertEqual(read_jsonl(self.ws.responses), [])


class CutOffTests(unittest.TestCase):
    """A reasoning model can burn its token budget thinking and return nothing."""
    def setUp(self):
        self.ws = Workspace(tempfile.mkdtemp())
        service.do_ingest(self.ws, str(DATASET))

    class Budget:
        """Cuts the LAST reply off (empty) unless given a large budget."""
        def __init__(self, ok, extra=None):
            self.ok, self.extra = ok, extra or {}
        def chat(self, messages):
            n = sum(m["role"] == "user" for m in messages)
            return Reply("fine reply" if self.ok or n < 5 else "", truncated=not self.ok and n == 5)
        def settings(self):
            return {"model": "reasoner", "max_tokens": 1024 if not self.ok else 4096, "extra_body": self.extra}

    def test_cut_off_runs_are_redone_and_old_record_is_ignored(self):
        p1 = run(self.ws, self.Budget(ok=False), conv_ids=["LIS-X01"], conditions=["EN"])
        self.assertIn("cut off", p1.message)
        run(self.ws, self.Budget(ok=False), conv_ids=["LIS-X01"], conditions=["EN"])   # still cut off: redone again
        self.assertEqual(len(read_jsonl(self.ws.responses)), 2)
        run(self.ws, self.Budget(ok=True), conv_ids=["LIS-X01"], conditions=["EN"])    # fixed: redone once more
        self.assertEqual(len(read_jsonl(self.ws.responses)), 3)
        from mcd.workspace import latest_responses
        latest = latest_responses(self.ws.responses)
        self.assertEqual(len(latest), 1)
        self.assertFalse(any(latest[0]["truncated"]))
        run(self.ws, self.Budget(ok=True), conv_ids=["LIS-X01"], conditions=["EN"])    # complete: skipped
        self.assertEqual(len(read_jsonl(self.ws.responses)), 3)

    def test_empty_or_cut_off_reply_is_dropped_not_failed(self):
        run(self.ws, self.Budget(ok=False), conv_ids=["LIS-X01"], conditions=["EN"])
        grade(self.ws, MockChat())
        last = [g for g in read_jsonl(self.ws.grades) if g["turn"] == 5]
        self.assertTrue(last and all(g["passed"] is None and g["skip_reason"] for g in last))

    def test_changing_request_fields_triggers_a_rerun(self):
        run(self.ws, self.Budget(ok=True, extra={}), conv_ids=["LIS-X01"], conditions=["EN"])
        run(self.ws, self.Budget(ok=True, extra={"chat_template_kwargs": {"enable_thinking": False}}),
            conv_ids=["LIS-X01"], conditions=["EN"])
        self.assertEqual(len(read_jsonl(self.ws.responses)), 2)

    def test_extra_body_is_sent_in_the_request(self):
        from mcd.backends import OpenAIChat
        import threading
        from http.server import BaseHTTPRequestHandler, HTTPServer
        seen = {}
        class H(BaseHTTPRequestHandler):
            def do_POST(self):
                seen["body"] = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                out = json.dumps({"choices": [{"message": {"content": "hi"}, "finish_reason": "length"}]}).encode()
                self.send_response(200); self.send_header("Content-Length", str(len(out))); self.end_headers(); self.wfile.write(out)
            def log_message(self, *a): pass
        srv = HTTPServer(("127.0.0.1", 0), H); threading.Thread(target=srv.serve_forever, daemon=True).start()
        backend = OpenAIChat("m", f"http://127.0.0.1:{srv.server_address[1]}/v1",
                             extra_body={"chat_template_kwargs": {"enable_thinking": False}})
        reply = backend.chat([{"role": "user", "content": "x"}])
        srv.shutdown()
        self.assertEqual(seen["body"]["chat_template_kwargs"], {"enable_thinking": False})
        self.assertTrue(reply.truncated)                       # finish_reason == "length"


class JudgeSafetyTests(unittest.TestCase):
    def test_empty_judge_answer_is_not_read_as_no(self):
        class Empty:
            def chat(self, messages):
                return Reply("", truncated=True)
            def settings(self):
                return {"model": "j"}
        ws = Workspace(tempfile.mkdtemp()); service.do_ingest(ws, str(DATASET))
        run(ws, MockChat(), conv_ids=["LIS-S11"], conditions=["EN"])
        progress = grade(ws, Empty())
        judged = [g for g in read_jsonl(ws.grades) if g["rule_id"] == "LIS-S11-R1"]   # a judge rule
        self.assertTrue(judged and all(g["passed"] is None for g in judged))
        self.assertGreater(progress.snapshot()["error_count"], 0)


class EscalationAndQualityTests(unittest.TestCase):
    def test_cut_off_reply_is_retried_with_a_bigger_budget(self):
        import threading
        from http.server import BaseHTTPRequestHandler, HTTPServer
        from mcd.backends import OpenAIChat
        budgets = []
        class H(BaseHTTPRequestHandler):
            def do_POST(self):
                budget = json.loads(self.rfile.read(int(self.headers["Content-Length"])))["max_tokens"]
                budgets.append(budget)
                ok = budget >= 4000        # a reasoning model needs about 4000 tokens here
                out = json.dumps({"choices": [{"message": {"content": "full answer" if ok else ""},
                                               "finish_reason": "stop" if ok else "length"}]}).encode()
                self.send_response(200); self.send_header("Content-Length", str(len(out))); self.end_headers(); self.wfile.write(out)
            def log_message(self, *a): pass
        srv = HTTPServer(("127.0.0.1", 0), H); threading.Thread(target=srv.serve_forever, daemon=True).start()
        reply = OpenAIChat("m", f"http://127.0.0.1:{srv.server_address[1]}/v1", max_tokens=1024).chat([{"role": "user", "content": "x"}])
        srv.shutdown()
        self.assertEqual((reply.text, reply.truncated), ("full answer", False))
        self.assertEqual(budgets, [1024, 2048, 4096])

    def test_escalation_never_goes_above_the_cap(self):
        import threading
        from http.server import BaseHTTPRequestHandler, HTTPServer
        from mcd.backends import OpenAIChat
        budgets = []
        class H(BaseHTTPRequestHandler):
            def do_POST(self):
                budgets.append(json.loads(self.rfile.read(int(self.headers["Content-Length"])))["max_tokens"])
                out = json.dumps({"choices": [{"message": {"content": "looping..."}, "finish_reason": "length"}]}).encode()
                self.send_response(200); self.send_header("Content-Length", str(len(out))); self.end_headers(); self.wfile.write(out)
            def log_message(self, *a): pass
        srv = HTTPServer(("127.0.0.1", 0), H); threading.Thread(target=srv.serve_forever, daemon=True).start()
        reply = OpenAIChat("m", f"http://127.0.0.1:{srv.server_address[1]}/v1", max_tokens=1024, max_tokens_cap=4096).chat([{"role": "user", "content": "x"}])
        srv.shutdown()
        self.assertEqual(budgets, [1024, 2048, 4096])      # stops at the cap
        self.assertTrue(reply.truncated)                   # still reported as cut off, so it is dropped, not graded

    def test_quality_gate_flags_avoidable_drops_but_not_english_only_drops(self):
        def row(cond, i, passed, skip=None):
            return {"model": "m", "condition": cond, "conv_id": f"c{i}", "rule_id": "r", "turn": 5,
                    "passed": passed, "skip_reason": skip, "polarity": "must", "truncated": False, "rule_hash": "x"}
        grades = ([row("EN", i, True) for i in range(10)]
                  + [row("MIX", i, None, "english_only_rule_hindi_reply") for i in range(3)] + [row("MIX", i, True) for i in range(3, 10)]
                  + [row("BAD", i, None, "empty_reply") for i in range(4)] + [row("BAD", i, True) for i in range(4, 10)])
        q = analysis.quality(grades)["m"]
        self.assertEqual(q["EN"]["verdict"], "ok")
        self.assertEqual(q["MIX"]["verdict"], "ok")                 # expected English-only drops are not penalised
        self.assertEqual(q["BAD"]["verdict"], "do_not_interpret")   # 40% empty
        self.assertEqual(q["BAD"]["reasons"], {"empty_reply": 4})

    def test_preflight_passes_with_mock_and_reports_failures(self):
        ws = Workspace(tempfile.mkdtemp()); service.do_ingest(ws, str(DATASET))
        steps = service.preflight(ws, "mock")
        self.assertEqual([ok for _, ok, _, _ in steps], [True, True, False])   # a yes-to-everything judge must be rejected
        broken = service.preflight(ws, "m", base_url="http://127.0.0.1:1/v1")      # nothing listening
        self.assertFalse(broken[0][1])
        self.assertEqual(len(broken), 1)                                           # stops at the first failure


class StatusTests(unittest.TestCase):
    def test_status_counts_saved_vs_expected_and_cut_offs(self):
        ws = Workspace(tempfile.mkdtemp()); service.do_ingest(ws, str(DATASET))
        self.assertEqual(service.status(ws), [])
        run(ws, MockChat(), conv_ids=["LIS-X01", "LIS-X02"], conditions=["EN"])
        rows = service.status(ws)
        self.assertEqual([(r["condition"], r["saved"], r["expected"], r["cut_off"]) for r in rows], [("EN", 2, 40, 0)])

    def test_cli_prints_live_progress_without_breaking_output(self):
        import contextlib, io
        from mcd import cli
        ws_dir = tempfile.mkdtemp(); out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            cli.main(["--workspace", ws_dir, "ingest", str(DATASET)])
            cli.main(["--workspace", ws_dir, "run", "--model", "mock", "--conv-ids", "LIS-X01", "--conditions", "EN"])
            cli.main(["--workspace", ws_dir, "status"])
        self.assertIn("1/40", out.getvalue())


class ManualJudgeTests(unittest.TestCase):
    def test_manual_flow_exports_blind_questions_then_uses_verdicts(self):
        ws = Workspace(tempfile.mkdtemp()); service.do_ingest(ws, str(DATASET))
        run(ws, MockChat(), conv_ids=["LIS-S11"], conditions=["EN", "MIX_CLEAN"])
        service.do_grade(ws, "manual")
        pending = json.load(open(ws.judge_pending))
        self.assertTrue(pending)
        self.assertTrue(all(set(p) == {"key", "question", "material", "kind"} for p in pending))   # no condition/conv labels
        pending_rows = [g for g in read_jsonl(ws.grades) if g["skip_reason"] == "awaiting_manual_judgement"]
        self.assertTrue(pending_rows and all(g["passed"] is None for g in pending_rows))
        verdicts = os.path.join(tempfile.mkdtemp(), "v.json")
        json.dump([{"key": p["key"], "verdict": "YES", "reason": "test"} for p in pending], open(verdicts, "w"))
        self.assertEqual(service.judge_apply(ws, verdicts)["applied"], len(pending))
        service.do_grade(ws, "manual")
        self.assertFalse(any(g["skip_reason"] == "awaiting_manual_judgement" for g in read_jsonl(ws.grades)))
        self.assertEqual(len(read_jsonl(ws.judge_log)), len(pending))                              # audit trail kept

    def test_bad_verdicts_are_rejected(self):
        ws = Workspace(tempfile.mkdtemp()); service.do_ingest(ws, str(DATASET))
        run(ws, MockChat(), conv_ids=["LIS-S11"], conditions=["EN"]); service.do_grade(ws, "manual")
        key = json.load(open(ws.judge_pending))[0]["key"]
        path = os.path.join(tempfile.mkdtemp(), "v.json")
        json.dump([{"key": key, "verdict": "MAYBE"}], open(path, "w"))
        with self.assertRaises(ValueError):
            service.judge_apply(ws, path)


class ResilienceTests(unittest.TestCase):
    def test_timeout_is_retried_then_succeeds(self):
        import threading, time
        from http.server import BaseHTTPRequestHandler, HTTPServer
        from mcd.backends import OpenAIChat
        hits = []
        class H(BaseHTTPRequestHandler):
            def do_POST(self):
                hits.append(1); self.rfile.read(int(self.headers["Content-Length"]))
                if len(hits) == 1:
                    time.sleep(1.5)                       # longer than the client's timeout
                out = json.dumps({"choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}]}).encode()
                try:
                    self.send_response(200); self.send_header("Content-Length", str(len(out))); self.end_headers(); self.wfile.write(out)
                except OSError:
                    pass
            def log_message(self, *a): pass
        srv = HTTPServer(("127.0.0.1", 0), H); threading.Thread(target=srv.serve_forever, daemon=True).start()
        reply = OpenAIChat("m", f"http://127.0.0.1:{srv.server_address[1]}/v1", timeout=0.5).chat([{"role": "user", "content": "x"}])
        srv.shutdown()
        self.assertEqual(reply.text, "ok")
        self.assertGreaterEqual(len(hits), 2)

    def test_failed_conversation_resumes_from_last_finished_turn(self):
        ws = Workspace(tempfile.mkdtemp()); service.do_ingest(ws, str(DATASET))
        calls = []
        class Flaky:
            fail_on_call = 3
            def chat(self, messages):
                calls.append(len(messages))
                if len(calls) == self.fail_on_call:
                    raise TimeoutError("read operation timed out")
                return Reply("reply")
            def settings(self):
                return {"model": "flaky", "extra_body": {}}
        backend = Flaky()
        p1 = run(ws, backend, conv_ids=["LIS-X01"], conditions=["EN"], workers=1)
        self.assertEqual((p1.failed, p1.snapshot()["failed"]), (1, 1))
        self.assertIn("FAILED", p1.message)
        self.assertEqual(read_jsonl(ws.responses), [])                    # nothing half-saved as a result
        before = len(calls)
        backend.fail_on_call = 10 ** 9                                    # now it works
        p2 = run(ws, backend, conv_ids=["LIS-X01"], conditions=["EN"], workers=1)
        self.assertEqual(p2.failed, 0)
        self.assertEqual(len(calls) - before, 3)                          # 2 turns were kept; only turns 3-5 were redone
        self.assertEqual(len(read_jsonl(ws.responses)), 1)
        self.assertFalse(json.load(open(ws.partial)))                     # checkpoint cleared after success


class ServerDownTests(unittest.TestCase):
    def test_run_stops_early_with_a_clear_message_when_the_server_is_not_running(self):
        from mcd.backends import OpenAIChat
        ws = Workspace(tempfile.mkdtemp()); service.do_ingest(ws, str(DATASET))
        backend = OpenAIChat("m", "http://127.0.0.1:1/v1", retries=1, timeout=2)      # nothing listens on port 1
        p = run(ws, backend, conditions=["EN"], workers=2)                              # 40 conversations queued
        self.assertIn("cannot reach the model server", p.message)
        self.assertLessEqual(p.failed, 4)                                               # stopped, did not fail all 40
        self.assertEqual(read_jsonl(ws.responses), [])


class ErrorDetailTests(unittest.TestCase):
    def test_a_rejected_request_shows_the_servers_reason(self):
        import threading
        from http.server import BaseHTTPRequestHandler, HTTPServer
        from mcd.backends import OpenAIChat
        class H(BaseHTTPRequestHandler):
            def do_POST(self):
                self.rfile.read(int(self.headers["Content-Length"]))
                out = json.dumps({"error": {"message": "maximum context length is 16384 tokens, you requested 17000"}}).encode()
                self.send_response(400); self.send_header("Content-Length", str(len(out))); self.end_headers(); self.wfile.write(out)
            def log_message(self, *a): pass
        srv = HTTPServer(("127.0.0.1", 0), H); threading.Thread(target=srv.serve_forever, daemon=True).start()
        with self.assertRaises(RuntimeError) as ctx:
            OpenAIChat("m", f"http://127.0.0.1:{srv.server_address[1]}/v1").chat([{"role": "user", "content": "x"}])
        srv.shutdown()
        self.assertIn("maximum context length", str(ctx.exception))
        self.assertIn("400", str(ctx.exception))


class ParallelTests(unittest.TestCase):
    def test_parallel_run_and_grade_match_sequential(self):
        outputs = []
        for workers in (1, 8):
            ws = Workspace(tempfile.mkdtemp())
            service.do_ingest(ws, str(DATASET))
            run(ws, MockChat(), conditions=["EN", "MIX"], workers=workers)
            grade(ws, MockChat(), workers=workers)
            key = lambda r: (r["conv_id"], r["condition"])
            outputs.append(([(r["conv_id"], r["condition"], r["replies"]) for r in sorted(read_jsonl(ws.responses), key=key)],
                            sorted(json.dumps(g, sort_keys=True) for g in read_jsonl(ws.grades))))
        self.assertEqual(outputs[0], outputs[1])

    def test_parallel_run_is_really_concurrent(self):
        import time
        class Slow:
            def chat(self, messages):
                time.sleep(0.05); return Reply("ok")
            def settings(self):
                return {"model": "slow"}
        ws = Workspace(tempfile.mkdtemp()); service.do_ingest(ws, str(DATASET))
        t0 = time.time(); run(ws, Slow(), conv_ids=[f"LIS-X{i:02d}" for i in range(1, 9)], conditions=["EN"], workers=8)
        concurrent = time.time() - t0                       # 8 conversations x 5 turns x 0.05s = 2.0s if sequential
        self.assertLess(concurrent, 1.0)

    def test_interval_is_flagged_untrustworthy_with_few_conversations(self):
        def row(cond, conv, passed):
            return {"model": "m", "condition": cond, "conv_id": conv, "rule_id": "r", "turn": 5,
                    "passed": passed, "polarity": "must", "truncated": False, "rule_hash": "x"}
        few = [row(c, f"c{i}", True) for i in range(3) for c in ("EN", "MIX")]
        many = [row(c, f"c{i}", True) for i in range(12) for c in ("EN", "MIX")]
        self.assertTrue(analysis.switch_cost(analysis.first_and_last(few), "m")["too_few"])
        self.assertFalse(analysis.switch_cost(analysis.first_and_last(many), "m")["too_few"])

    def test_headroom_uses_only_english_and_flags_ceiling(self):
        def row(cond, conv, passed):
            return {"model": "m", "condition": cond, "conv_id": conv, "rule_id": conv + "r", "turn": 5,
                    "passed": passed, "polarity": "must", "truncated": False, "rule_hash": "x"}
        grades = [row("EN", f"LIS-X0{i}", True) for i in range(1, 6)] + [row("MIX", "LIS-X01", False)]
        h = analysis.headroom(grades)
        self.assertEqual(h["m"]["X"]["status"], "ceiling")          # MIX failure is ignored
        self.assertEqual(h["m"]["X"]["pass"], 1.0)


class LanguageScopeTests(unittest.TestCase):
    """english_only rules must be dropped as a PAIR, never as a single side."""
    def setUp(self):
        self.ws = Workspace(tempfile.mkdtemp())
        service.do_ingest(self.ws, str(DATASET))

    def test_hindi_reply_is_not_graded_on_english_only_rule_and_pair_is_dropped(self):
        class HindiInMix:                       # replies in Hindi whenever Hindi appeared before
            def chat(self, messages):
                hindi = any("\u0900" <= ch <= "\u097f" for m in messages if m["role"] == "user" for ch in m["content"])
                return Reply("यह एक उत्तर है।" if hindi else "this is a reply without any c-word.")
            def settings(self):
                return {"model": "hi-mix"}
        run(self.ws, HindiInMix(), conv_ids=["LIS-S20"], conditions=["EN", "MIX"])
        grade(self.ws, MockChat())
        rows = [g for g in read_jsonl(self.ws.grades) if g["rule_id"] == "LIS-S20-R2"]   # no letter c (english_only)
        mix_rows = [g for g in rows if g["condition"] == "MIX"]
        self.assertTrue(all(g["passed"] is None and g["skip_reason"] for g in mix_rows))
        self.assertTrue(all(g["passed"] is not None for g in rows if g["condition"] == "EN"))
        final = analysis.first_and_last(read_jsonl(self.ws.grades))
        s = analysis.switch_cost(final, "hi-mix", "EN", "MIX")
        # the S20 'no c' pair is dropped; other S20 rules (any-scope) still pair up
        self.assertEqual(s["dropped_pairs"], 1)
        self.assertEqual(s["pairs"], 2)

    def test_sensitivity_bounds_bracket_the_dropped_pairs(self):
        final = {}
        def row(cond, conv, passed, skip=None):
            return {"model": "m", "condition": cond, "conv_id": conv, "rule_id": "r", "turn": 5,
                    "passed": passed, "skip_reason": skip, "polarity": "must", "truncated": False, "rule_hash": "x"}
        grades = [row("EN", "c1", True), row("MIX", "c1", True),
                  row("EN", "c2", True), row("MIX", "c2", None, "english_only_rule_hindi_reply")]
        final = analysis.first_and_last(grades)
        main = analysis.switch_cost_with_sensitivity(final, "m", "MIX")
        self.assertEqual(main["dropped_pairs"], 1)
        self.assertEqual(main["diff"], 0.0)
        self.assertEqual(main["if_dropped_pass"], 0.0)
        self.assertEqual(main["if_dropped_fail"], 0.5)      # dropped pair counted as a MIX failure


class ApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.ws = Workspace(tempfile.mkdtemp())
        service.do_ingest(cls.ws, str(DATASET))
        cls.app = App(cls.ws)
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(cls.app))
        cls.base = f"http://127.0.0.1:{cls.server.server_address[1]}"
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()

    def call(self, path, method="GET", body=None):
        req = urllib.request.Request(self.base + path, method=method,
                                     data=json.dumps(body).encode() if body is not None else None)
        try:
            with urllib.request.urlopen(req) as r:
                return r.status, json.load(r) if "json" in r.headers["Content-Type"] else r.read()
        except urllib.error.HTTPError as e:
            return e.code, json.load(e)

    def wait_for_job(self):
        import time
        for _ in range(100):
            job = self.call("/api/job")[1]
            if job["state"] != "running":
                return job
            time.sleep(0.1)

    def test_ui_is_served_and_blocks_path_traversal(self):
        self.assertEqual(self.call("/")[0], 200)
        self.assertEqual(self.call("/app.js")[0], 200)
        self.assertEqual(self.call("/../mcd/server.py")[0], 404)

    def test_rule_crud_validates_input(self):
        rule = {"id": "T-1", "conv_id": "LIS-S01", "name": "t", "polarity": "must",
                "from_turn": 1, "check": {"type": "contains", "terms": ["x"]}}
        self.assertEqual(self.call("/api/rulebook/rule", "PUT", rule)[0], 200)
        self.assertEqual(self.call("/api/rulebook/rule", "PUT", {**rule, "polarity": "bad"})[0], 400)
        self.assertEqual(self.call("/api/rulebook/rule", "PUT", {**rule, "check": {"type": "nope"}})[0], 400)
        self.assertEqual(self.call("/api/rulebook/rule?id=T-1", "DELETE")[0], 200)
        ids = [r["id"] for r in self.call("/api/rulebook")[1]["rules"]]
        self.assertNotIn("T-1", ids)

    def test_run_grade_results_transcript_flow(self):
        self.assertEqual(self.call("/api/run", "POST", {"model": "mock", "conditions": ["EN", "MIX"]})[0], 200)
        self.assertEqual(self.wait_for_job()["state"], "finished")
        self.assertEqual(self.call("/api/grade", "POST", {"judge_model": "mock"})[0], 200)
        self.assertEqual(self.wait_for_job()["state"], "finished")
        summary = self.call("/api/results")[1]["summary"]
        self.assertEqual(set(summary["mock"]["conditions"]), {"EN", "MIX"})
        t = self.call("/api/transcript?conv=LIS-S01&condition=MIX&model=mock")[1]
        self.assertEqual(len(t["turns"]), 13)
        self.assertTrue(any(turn["grades"] for turn in t["turns"]))
        last = self.call("/api/rulebook")[1]["last_run"]["LIS-S01-R1"]
        self.assertIn("mock|EN", last)


if __name__ == "__main__":
    unittest.main()
