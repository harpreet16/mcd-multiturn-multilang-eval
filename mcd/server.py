"""Local web server: a small JSON API plus the static UI in ui/.

Binds to 127.0.0.1 only. API keys typed in the UI are used for the request and
kept in memory; they are never written to disk.
"""
import json
import threading
from dataclasses import asdict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from . import service
from .jobs import Progress
from .rulebook import Rulebook
from .workspace import Workspace

UI_DIR = Path(__file__).resolve().parent.parent / "ui"
CONTENT_TYPES = {".html": "text/html", ".js": "text/javascript", ".css": "text/css"}


class App:
    """Shared state: the workspace and the single background job."""

    def __init__(self, workspace):
        self.ws = workspace
        self.job = Progress()
        self.lock = threading.Lock()

    def start_job(self, progress, fn):
        with self.lock:
            if self.job.state == "running":
                raise RuntimeError("A job is already running")
            progress.state = "running"
            self.job = progress

        def target():
            try:
                fn(progress)
            except Exception as e:
                progress.state = "failed"
                progress.errors.append(str(e))
        threading.Thread(target=target, daemon=True).start()


def make_handler(app):
    ws = app.ws

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        # --- helpers -------------------------------------------------
        def send_json(self, data, status=200):
            body = json.dumps(data, ensure_ascii=False).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def read_body(self):
            length = int(self.headers.get("Content-Length") or 0)
            return json.loads(self.rfile.read(length) or b"{}")

        def handle_api(self, method, path, query, body):
            if method == "GET":
                if path == "/api/state":
                    return service.state(ws)
                if path == "/api/job":
                    return app.job.snapshot()
                if path == "/api/conversations":
                    return conversations_view()
                if path == "/api/rulebook":
                    return service.rulebook_view(ws)
                if path == "/api/results":
                    return service.results(ws, query.get("group", ["all"])[0])
                if path == "/api/transcript":
                    q = {k: v[0] for k, v in query.items()}
                    return service.transcript(ws, q["conv"], q["condition"], q["model"])
            if method == "POST":
                if path == "/api/ingest":
                    return service.do_ingest(ws, body.get("path") or service.DEFAULT_DATASET)
                if path == "/api/remix":
                    return service.do_remix(ws, body.get("conditions"))
                if path == "/api/run":
                    app.start_job(Progress("run"), lambda p: service.do_run(
                        ws, body["model"], body.get("base_url"), body.get("api_key"),
                        int(body.get("max_tokens") or 4096), body.get("conv_ids"),
                        body.get("conditions"), p, int(body.get("workers") or 4),
                        json.loads(body["extra_body"]) if body.get("extra_body") else None))
                    return {"started": True}
                if path == "/api/grade":
                    app.start_job(Progress("grade"), lambda p: service.do_grade(
                        ws, body["judge_model"], body.get("base_url"), body.get("api_key"), p,
                        int(body.get("workers") or 4),
                        json.loads(body["extra_body"]) if body.get("extra_body") else None))
                    return {"started": True}
                if path == "/api/job/stop":
                    app.job.stop()
                    return {"stopping": True}
                if path == "/api/rulebook/reset":
                    Rulebook(ws.rulebook).reset_to_seed()
                    return service.rulebook_view(ws)
            if method == "PUT" and path == "/api/rulebook/rule":
                Rulebook(ws.rulebook).upsert(body)
                return service.rulebook_view(ws)
            if method == "DELETE" and path == "/api/rulebook/rule":
                Rulebook(ws.rulebook).delete(query["id"][0])
                return service.rulebook_view(ws)
            return None

        def dispatch(self, method):
            url = urlparse(self.path)
            if url.path.startswith("/api/"):
                try:
                    body = self.read_body() if method in ("POST", "PUT") else {}
                    result = self.handle_api(method, url.path, parse_qs(url.query), body)
                except (ValueError, KeyError, RuntimeError, FileNotFoundError) as e:
                    return self.send_json({"error": str(e)}, 400)
                if result is None:
                    return self.send_json({"error": "not found"}, 404)
                return self.send_json(result)
            self.serve_static(url.path)

        def serve_static(self, path):
            name = "index.html" if path in ("", "/") else path.lstrip("/")
            file = (UI_DIR / name).resolve()
            if UI_DIR not in file.parents or not file.is_file():
                return self.send_json({"error": "not found"}, 404)
            body = file.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", CONTENT_TYPES.get(file.suffix, "text/plain") + "; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        do_GET = lambda self: self.dispatch("GET")
        do_POST = lambda self: self.dispatch("POST")
        do_PUT = lambda self: self.dispatch("PUT")
        do_DELETE = lambda self: self.dispatch("DELETE")

    def conversations_view():
        by_conv = {}
        for v in ws.load_variants():
            by_conv.setdefault(v.conv_id, {})[v.condition] = [asdict(t) for t in v.turns]
        return [{"id": cid, "variants": variants} for cid, variants in by_conv.items()]

    return Handler


def serve(workspace_dir, port=8765):
    app = App(Workspace(workspace_dir))
    try:
        server = ThreadingHTTPServer(("127.0.0.1", port), make_handler(app))
    except OSError:
        raise SystemExit(f"Port {port} is already in use (an earlier server may be suspended or running).\n"
                         f"Stop it with:  lsof -ti :{port} | xargs kill\n"
                         f"or start on another port:  python3 -m mcd serve --port {port + 1}")
    print(f"MCD UI on http://127.0.0.1:{port}  (workspace: {workspace_dir})")
    server.serve_forever()
