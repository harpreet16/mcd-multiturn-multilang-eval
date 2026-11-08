"""Model backends. A backend is anything with chat(messages) -> Reply.

- OpenAIChat: any OpenAI-compatible chat API (the Voyager API, a vLLM server,
              or vLLM inside Colab). Set MCD_BASE_URL and MCD_API_KEY, or pass
              them explicitly. Keys are never written to disk.
- MockChat:   offline stand-in for tests and demos. Its output is NOT evidence
              about any real model.
"""
import json
import os
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass


class ServerUnreachable(Exception):
    """Nothing is listening at the model URL (e.g. the vLLM server was never started)."""


@dataclass
class Reply:
    text: str
    truncated: bool = False    # True if the model hit the max_tokens limit


class OpenAIChat:
    def __init__(self, model, base_url=None, api_key=None,
                 max_tokens=4096, temperature=0.0, retries=5, extra_body=None, max_tokens_cap=16384, timeout=900):
        self.model = model
        self.base_url = (base_url or os.environ.get("MCD_BASE_URL")
                         or "http://localhost:8000/v1").rstrip("/")
        self.api_key = api_key or os.environ.get("MCD_API_KEY", "")
        self.max_tokens = max_tokens
        self.max_tokens_cap = max(max_tokens, max_tokens_cap)
        self.timeout = timeout           # seconds to wait for one reply; long budgets need minutes
        self.temperature = temperature
        self.retries = retries
        self.extra_body = extra_body or {}      # extra JSON fields merged into every request

    def chat(self, messages):
        """One reply. If it is cut off or empty (a reasoning model used its budget thinking),
        retry with double the budget up to max_tokens_cap. Decoding is greedy, so a bigger
        budget only lets the same answer finish."""
        budget = self.max_tokens
        while True:
            reply = self._call(messages, budget)
            if (not reply.truncated and reply.text.strip()) or budget >= self.max_tokens_cap:
                return reply
            new_budget = min(budget * 2, self.max_tokens_cap)
            print(f"  [note] reply {'cut off' if reply.truncated else 'empty'} at {budget} tokens; "
                  f"retrying with {new_budget} (each retry can take minutes)", file=sys.stderr, flush=True)
            budget = new_budget

    def _call(self, messages, max_tokens):
        body = json.dumps({"model": self.model, "messages": messages,
                           "max_tokens": max_tokens,
                           "temperature": self.temperature, **self.extra_body}).encode()
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        for attempt in range(self.retries):
            try:
                req = urllib.request.Request(self.base_url + "/chat/completions", body, headers)
                with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                    choice = json.load(resp)["choices"][0]
                return Reply(choice["message"]["content"] or "",
                             choice.get("finish_reason") == "length")
            except urllib.error.HTTPError as e:
                # Retry rate limits / server errors; fail fast on bad requests or keys.
                if e.code not in (429, 500, 502, 503) or attempt == self.retries - 1:
                    raise
                time.sleep(2 ** attempt)
            except (TimeoutError, urllib.error.URLError, ConnectionError, OSError) as e:
                # A slow or dropped connection is retried a few times, then reported.
                if attempt >= min(self.retries, 3) - 1:
                    if isinstance(getattr(e, "reason", e), ConnectionRefusedError):
                        raise ServerUnreachable(self.base_url) from e
                    raise
                print(f"  [note] {type(e).__name__}: {e}; retrying ({attempt + 1})", file=sys.stderr, flush=True)
                time.sleep(2 ** attempt)

    def settings(self):
        """Safe-to-store description of this backend (no API key)."""
        return {"model": self.model, "base_url": self.base_url,
                "max_tokens": self.max_tokens, "max_tokens_cap": self.max_tokens_cap, "timeout": self.timeout,
                "temperature": self.temperature, "extra_body": self.extra_body}


class MockChat:
    """Canned answers. Judge questions get YES so the pipeline can be exercised."""
    model = "mock"

    def chat(self, messages):
        last = messages[-1]["content"]
        if "Answer YES or NO" in last:
            return Reply("YES")
        return Reply("This is a short mock reply without any special formatting.")

    def settings(self):
        return {"model": "mock"}


def make_backend(model, base_url=None, api_key=None, max_tokens=1024, extra_body=None):
    if model == "mock":
        return MockChat()
    return OpenAIChat(model, base_url, api_key, max_tokens, extra_body=extra_body)
