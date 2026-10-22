"""CLI entry point: python -m orchestrator {run,resume,status,dashboard,agents}."""
import argparse
import sys
from pathlib import Path

from . import dashboard
from .config import DEFAULT_AGENTS_FILE, load_agents, load_dotenv
from .runner import Orchestrator, Run


def main(argv=None):
    p = argparse.ArgumentParser(prog="python -m orchestrator",
                                description="Coordinate free-tier AI agents to build a project locally.")
    p.add_argument("--agents", default=str(DEFAULT_AGENTS_FILE), help="agents/limits config (JSON)")
    p.add_argument("--workdir", default=".orchestrator", help="local state, logs and run history")
    sub = p.add_subparsers(dest="cmd", required=True)

    r = sub.add_parser("run", help="start a new task (prompts for it if omitted)")
    r.add_argument("task", nargs="?")
    r.add_argument("--output", default="output", help="where generated projects are written")
    r.add_argument("--dry-run", action="store_true", help="use offline mock agents; no API calls")
    r.add_argument("--max-wait", type=int, default=90, help="max seconds to wait out per-minute limits")

    rs = sub.add_parser("resume", help="continue a paused run")
    rs.add_argument("run_id")
    rs.add_argument("--max-wait", type=int, default=90)

    st = sub.add_parser("status", help="print usage and progress")
    st.add_argument("run_id", nargs="?")

    d = sub.add_parser("dashboard", help="serve a live status page on 127.0.0.1")
    d.add_argument("--port", type=int, default=8765)

    sub.add_parser("agents", help="list agents and whether they're usable")
    args = p.parse_args(argv)

    load_dotenv(Path.cwd() / ".env")
    agents, fallbacks = load_agents(args.agents)
    workdir = Path(args.workdir)

    if args.cmd == "agents":
        for a in sorted(agents, key=lambda a: a.priority):
            ready = "ready" if a.is_configured() else ("disabled" if not a.enabled else f"set {a.api_key_env}")
            print(f"{a.name:<16} {a.model:<40} {ready:<22} {', '.join(a.capabilities)}")
        return 0
    if args.cmd == "status":
        print(dashboard.render_terminal(dashboard.collect(workdir, agents, args.run_id)))
        return 0
    if args.cmd == "dashboard":
        dashboard.serve(workdir, agents, args.port)
        return 0

    if args.cmd == "run" and args.dry_run:
        for a in agents:
            a.type, a.enabled = "mock", True
    orch = Orchestrator(agents, fallbacks, workdir, max_wait=args.max_wait)

    if args.cmd == "resume":
        if Run.load(workdir / "runs", args.run_id).data["dry_run"]:
            for a in agents:
                a.type, a.enabled = "mock", True
        run = orch.resume(args.run_id)
    else:
        task = args.task or input("What do you want built? ").strip()
        if not task:
            p.error("a task is required")
        if not args.dry_run and not any(a.is_configured() for a in agents):
            print("No agents are usable: add API keys to .env (see .env.example) or enable ollama-local "
                  "in agents.json. Try --dry-run to test the pipeline offline.", file=sys.stderr)
            return 1
        run = orch.start(task, args.output, dry_run=args.dry_run)

    print()
    print(dashboard.render_terminal(dashboard.collect(workdir, agents, run.data["run_id"])))
    return 0 if all(s["state"] == "done" for s in run.data["status"].values()) else 2


if __name__ == "__main__":
    sys.exit(main())
