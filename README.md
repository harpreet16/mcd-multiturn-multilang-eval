# MCD: Multilingual Context Drift

Does a model keep a rule from early in a conversation after some turns switch from English to Hindi?
Data (40 conversations):
- **LIS-S01..S20** from the spreadsheet *Lost in the Switch — Simple Conversations*: 4–13 turns, each authored in an
  "All English" and an "English + Hindi" version, with distractor questions mixed in.
- **LIS-X01..X20** curated for this study (`data/extra_conversations.json`): 10 matched pairs, 5 turns each. Within a pair
  turns 2–5 are identical and only the rule in turn 1 flips between *must* and *must not*. The rule is given in English at
  turn 1, turns 2–4 are Hindi, turn 5 is English (EN→HI→EN). Each also has a full Hindi version for the controls.
  **The Hindi text is not yet verified by a native speaker.**

Conditions: `EN`, `MIX` (authored), `MIX_CLEAN` (rule-introducing turns kept English; the cleanest memory test),
`HI_EARLY`, `HI_LATE`, and for curated conversations `HI_ALL` (Hindi-only control) and `HI_EN_HI` (reverse direction).

Rules have a `language_scope`. `english_only` rules (e.g. "no letter c") are not graded on Hindi replies; the matching
EN/MIX pair is dropped, and results report sensitivity bounds if the dropped pairs had passed or failed.

## Architecture

    xlsx ─► ingest ─► remixer ─► run ─► grade ─► analysis ─► UI
            ingest.py remixer.py runner.py grader.py analysis.py  server.py + ui/
                                   │         │
                              backends.py   rulebook.py + checks.py   (editable rules)

| Module | Job |
|---|---|
| `xlsx.py` | Standard-library .xlsx reader (no dependencies). |
| `ingest.py` | Spreadsheet → `Conversation` (both versions, aligned by turn). Lints the alignment. |
| `remixer.py` | Conversation → runnable `Variant` per condition. Conditions are small functions (`EN`, `MIX`, `HI_EARLY`, `HI_LATE`). Hindi only where a human wrote it. |
| `backends.py` | `chat(messages) → Reply`. OpenAI-compatible client (Voyager, vLLM) and a mock. |
| `runner.py` | Plays each variant turn by turn with full history. Resumable; one failed call doesn't stop the run. |
| `rulebook.py`, `checks.py` | The editable rulebook and the deterministic check types. `data/seed_rulebook.json` ships 67 starter rules. |
| `grader.py` | Checks each reply against each rule on every task turn (never distractors). Judge answers are cached. |
| `analysis.py` | Final-turn pass rate, forgetting, must vs must-not, switch cost (EN − MIX) with a cluster bootstrap. |
| `service.py` | The one place stages are wired together. CLI and server both call it. |
| `server.py`, `ui/` | Local JSON API + plain HTML/JS console (no build step). |

Each stage reads and writes files in one workspace folder (`runs/main` by default), so any stage can be re-run alone.

## The rulebook

A rule = *from turn X to turn Y, the reply must satisfy a check*. Checks are code (`contains`, `regex`,
`word_count`, …) or a `judge` yes/no question. Passing a check always means the rule was **followed**.
Edit rules in the UI (Rulebook tab). Rules edited after the last grading are flagged **stale**; re-grade
to refresh. Re-grading does not call the model under test again, and only changed judge questions are re-asked.

The seed rules are one reader's interpretation of each conversation. Review them before drawing conclusions.

## Use

    python -m mcd demo                       # offline, mock model (results are not meaningful)
    python -m mcd serve                      # UI at http://127.0.0.1:8765

    export MCD_BASE_URL=https://<voyager-endpoint>/v1
    export MCD_API_KEY=...
    python -m mcd ingest
    python -m mcd run   --model <model> --conditions EN MIX
    python -m mcd grade --judge-model <model>
    python -m mcd report

Colab: `colab_run.ipynb`. Tests: `python -m unittest discover -s tests`.

## Not verified
- Voyager's endpoint, auth and model names (assumed OpenAI-compatible).
- Judge rules depend on the judge model; spot-check them in the Transcripts tab.
- Judging with the same model that is being tested can bias results; prefer a different judge.
