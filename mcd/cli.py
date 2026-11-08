"""Command line:  python -m mcd [--workspace DIR] <command>

  ingest [xlsx]   spreadsheet -> conversations + variants + seeded rulebook
  remix           rebuild variants (after changing conditions)
  run             send variants to a model (resumable)
  grade           grade replies against the rulebook
  report          print the summary table
  serve           open the web UI
  demo            ingest + run + grade with a mock model (no network)
"""
import argparse
import json
import sys

from . import service
from .workspace import Workspace


def _with_live_progress(fn, kind="job"):
    """Run fn(progress) while printing one status line every 3 seconds."""
    import sys, threading, time
    from .jobs import Progress
    progress, finished, t0 = Progress(kind), threading.Event(), time.time()

    def show():
        while not finished.wait(3):
            s = progress.snapshot()
            elapsed = time.time() - t0
            left = int(elapsed / s["done"] * (s["total"] - s["done"])) if s["done"] else None
            eta = "" if left is None else f", ~{left // 60}m{left % 60:02d}s left"
            print(f"  [{s['kind'] or 'job'}] {s['done']}/{s['total']} done, {s['error_count']} errors, "
                  f"{int(elapsed // 60)}m{int(elapsed % 60):02d}s elapsed{eta} | {s['message'][:60]}", file=sys.stderr, flush=True)
    threading.Thread(target=show, daemon=True).start()
    try:
        return fn(progress)
    finally:
        finished.set()


def _add_connection_args(parser):
    parser.add_argument("--base-url", help="API base URL, e.g. https://host/v1 (else MCD_BASE_URL)")
    parser.add_argument("--api-key-file", help="file containing only the API key (safer than --api-key)")
    parser.add_argument("--workers", type=int, default=4, help="parallel conversations (default 4)")
    parser.add_argument("--api-key", help="API key; visible in shell history and `ps`, prefer --api-key-file")


def _api_key(args):
    if args.api_key_file:
        with open(args.api_key_file, encoding="utf-8") as f:
            return f.read().strip()
    return args.api_key


def main(argv=None):
    ap = argparse.ArgumentParser(prog="mcd", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--workspace", default="runs/main")
    sub = ap.add_subparsers(dest="command", required=True)

    s = sub.add_parser("ingest"); s.add_argument("xlsx", nargs="?", default=service.DEFAULT_DATASET)
    sub.add_parser("remix")
    s = sub.add_parser("run")
    s.add_argument("--model", required=True, help="model name, or 'mock'")
    _add_connection_args(s); s.add_argument("--max-tokens", type=int, default=4096,
        help="reasoning models spend tokens thinking, so keep this large (default 4096)")
    s.add_argument("--extra-body", help='extra JSON request fields, e.g. \'{"chat_template_kwargs": {"enable_thinking": false}}\'')
    s.add_argument("--conditions", nargs="+"); s.add_argument("--conv-ids", nargs="+")
    s = sub.add_parser("grade")
    s.add_argument("--judge-model", required=True, help="a model name, or 'manual' to export the questions for a person/Claude to answer")
    _add_connection_args(s)
    s.add_argument("--extra-body", help="extra JSON request fields for the judge, e.g. to switch thinking off")
    sub.add_parser("report")
    sub.add_parser("status", help="progress per model and condition (run it from a second terminal)")
    s = sub.add_parser("judge-apply", help="merge manual verdicts (JSON list of {key, verdict, reason}) into the judge cache")
    s.add_argument("verdicts")
    s = sub.add_parser("preflight", help="1-minute check of connection, token budget and judge before a long run")
    s.add_argument("--model", required=True); s.add_argument("--judge-model"); _add_connection_args(s)
    s.add_argument("--max-tokens", type=int, default=4096)
    s.add_argument("--extra-body", help="extra JSON request fields, e.g. to switch thinking off")
    s = sub.add_parser("serve"); s.add_argument("--port", type=int, default=8765)
    sub.add_parser("demo")
    args = ap.parse_args(argv)
    ws = Workspace(args.workspace)

    if args.command == "ingest":
        print(service.do_ingest(ws, args.xlsx))
    elif args.command == "remix":
        print(service.do_remix(ws))
    elif args.command == "run":
        p = _with_live_progress(lambda prog: service.do_run(
            ws, args.model, args.base_url, _api_key(args), args.max_tokens, args.conv_ids, args.conditions,
            progress=prog, workers=args.workers, extra_body=json.loads(args.extra_body) if args.extra_body else None), "run")
        print(p.snapshot())
        if p.failed:
            print(f"\n!! {p.failed} conversation(s) FAILED and were NOT saved. Re-run the same command: "
                  f"finished turns are kept, only the rest is redone.", file=sys.stderr)
    elif args.command == "grade":
        print(_with_live_progress(lambda prog: service.do_grade(
            ws, args.judge_model, args.base_url, _api_key(args), progress=prog, workers=args.workers,
            extra_body=json.loads(args.extra_body) if args.extra_body else None), "grade").snapshot())
    elif args.command == "report":
        print(json.dumps(service.results(ws)["summary"], indent=1))
    elif args.command == "status":
        rows = service.status(ws)
        print(f"{'model':16} {'condition':10} {'saved/expected':>15} {'cut off':>8}  last saved")
        for r in rows or []:
            print(f"{r['model']:16} {r['condition']:10} {str(r['saved']) + '/' + str(r['expected']):>15} {r['cut_off']:>8}  {r['last']}")
        if not rows:
            print("nothing saved yet")
    elif args.command == "judge-apply":
        print(service.judge_apply(ws, args.verdicts))
    elif args.command == "preflight":
        steps = service.preflight(ws, args.model, args.base_url, _api_key(args), args.max_tokens,
                                  json.loads(args.extra_body) if args.extra_body else None, args.judge_model)
        for name, ok, detail, secs in steps:
            print(f"[{'PASS' if ok else 'FAIL'}] {name}  ({secs}s)\n       {detail}")
        ready = len(steps) == 3 and all(s[1] for s in steps)
        print("\nReady for a full run." if ready else
              "\nNOT ready: fix the failed step first (a failed step means avoidable data loss).")
    elif args.command == "serve":
        from .server import serve
        serve(args.workspace, args.port)
    elif args.command == "demo":
        print(service.do_ingest(ws))
        print(service.do_run(ws, "mock", conditions=["EN", "MIX"]).snapshot())
        print(service.do_grade(ws, "mock").snapshot())
        print(json.dumps(service.results(ws)["summary"], indent=1))


if __name__ == "__main__":
    main()
