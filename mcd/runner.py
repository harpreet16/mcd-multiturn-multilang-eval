"""Stage 3: run every (conversation, condition) through a model.

For each variant the user turns are sent one at a time, with the full history
(including the model's earlier replies). One JSONL line is written per finished
variant, so an interrupted run resumes where it stopped.
"""
import json
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

from .backends import ServerUnreachable
from .jobs import Progress
from .workspace import append_jsonl, latest_responses


def play(variant, backend, replies=None, truncated=None, on_turn=None):
    """Return (replies, truncated_flags) for one variant, sending the turns one at a time.

    replies/truncated hold turns already finished in an earlier attempt: they are replayed
    into the history without calling the model again. on_turn is called after each new reply.
    """
    replies, truncated = list(replies or []), list(truncated or [])
    messages, done = [], len(replies)
    for i, turn in enumerate(variant.turns):
        messages.append({"role": "user", "content": turn.text})
        if i < done:
            messages.append({"role": "assistant", "content": replies[i]})
            continue
        reply = backend.chat(messages)
        messages.append({"role": "assistant", "content": reply.text})
        replies.append(reply.text)
        truncated.append(reply.truncated)
        if on_turn:
            on_turn(replies, truncated)
    return replies, truncated


def run(workspace, backend, conv_ids=None, conditions=None, progress=None, workers=4):
    progress = progress or Progress("run")
    model = backend.settings()["model"]
    # A run counts as done only if nothing was cut off and the request fields match.
    # So raising max_tokens (or switching thinking off) and re-running redoes just those.
    extra = backend.settings().get("extra_body", {})
    done = {(r["conv_id"], r["condition"], r["model"]) for r in latest_responses(workspace.responses)
            if not any(r["truncated"]) and r["settings"].get("extra_body", {}) == extra}
    todo = [v for v in workspace.load_variants()
            if (not conv_ids or v.conv_id in conv_ids)
            and (not conditions or v.condition in conditions)
            and (v.conv_id, v.condition, model) not in done]
    progress.total, progress.done, progress.state = len(todo), 0, "running"
    dataset_hash = workspace.dataset_hash()

    # Turns finished in conversations that failed part-way are kept, so a retry resumes there.
    lock = threading.Lock()
    unreachable = []
    partial = json.load(open(workspace.partial)) if os.path.exists(workspace.partial) else {}

    def save_partial():
        with open(workspace.partial, "w", encoding="utf-8") as f:
            json.dump(partial, f, ensure_ascii=False)

    def work(variant):
        """Runs in a worker thread. Turns inside one conversation stay sequential."""
        if progress.should_stop:
            return variant, None, None
        key = f"{variant.conv_id}|{variant.condition}|{model}|{json.dumps(extra, sort_keys=True)}"
        saved = partial.get(key, {"replies": [], "truncated": []})
        keep = next((i for i, cut in enumerate(saved["truncated"]) if cut), len(saved["replies"]))
        start = (saved["replies"][:keep], saved["truncated"][:keep])     # a cut-off turn is redone

        def on_turn(replies, truncated):
            with lock:
                partial[key] = {"replies": replies, "truncated": truncated}
                save_partial()
        try:
            result = play(variant, backend, *start, on_turn=on_turn)
            with lock:
                partial.pop(key, None)
                save_partial()
            return variant, result, None
        except ServerUnreachable as e:    # no point trying the other conversations
            unreachable.append(str(e))
            progress.stop()
            return variant, None, e
        except Exception as e:            # one bad call should not kill a long run
            return variant, None, e

    # Different conversations are independent, so they run in parallel. All file
    # writes happen here in the main thread, so no locking is needed.
    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        for future in as_completed([pool.submit(work, v) for v in todo]):
            variant, result, error = future.result()
            if result is None and error is None:       # skipped because of Stop
                continue
            progress.message = f"{variant.conv_id} {variant.condition}"
            if error:
                progress.errors.append(f"{variant.conv_id} {variant.condition}: {error}")
                progress.failed += 1
            else:
                replies, truncated = result
                append_jsonl(workspace.responses, {
                    "conv_id": variant.conv_id, "condition": variant.condition,
                    "model": model, "replies": replies, "truncated": truncated,
                    "settings": backend.settings(), "dataset_hash": dataset_hash,
                    "finished_at": time.strftime("%Y-%m-%d %H:%M:%S")})
            progress.done += 1
    if unreachable:
        progress.message = (f"STOPPED: cannot reach the model server at {unreachable[0]}. Start the model server first "
                            f"(Colab: run cell 3 and wait for 'server ready'), then run the same command again.")
    elif progress.failed:
        progress.message = (f"{progress.failed} conversation(s) FAILED and are NOT saved. Re-run the same command: "
                            f"turns already finished are kept and only the rest is redone.")
    cut = sum(1 for r in latest_responses(workspace.responses)
              if r["model"] == model and any(r["truncated"]))
    if cut and not progress.failed and not unreachable:
        progress.message = (f"{cut} saved run(s) have replies cut off by max_tokens: "
                            f"raise --max-tokens (or switch thinking off) and run again to redo them")
    progress.state = "stopped" if progress.should_stop else "finished"
    return progress
